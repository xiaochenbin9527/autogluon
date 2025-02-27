import numpy as np
import pandas as pd
import pytest
from gluonts.evaluation import Evaluator as GluonTSEvaluator
from gluonts.evaluation import make_evaluation_predictions
from gluonts.model.forecast import SampleForecast

from autogluon.timeseries import TimeSeriesPredictor
from autogluon.timeseries.evaluator import TimeSeriesEvaluator
from autogluon.timeseries.models.gluonts.abstract_gluonts import AbstractGluonTSModel

from .common import DUMMY_TS_DATAFRAME

GLUONTS_PARITY_METRICS = ["mean_wQuantileLoss", "MAPE", "sMAPE", "MSE", "RMSE"]


pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(scope="module")
def deepar_trained() -> AbstractGluonTSModel:
    pred = TimeSeriesPredictor(prediction_length=2, verbosity=4)
    pred.fit(
        DUMMY_TS_DATAFRAME,
        hyperparameters={
            "DeepAR": dict(epochs=2),
        },
    )
    return pred._trainer.load_model("DeepAR")


@pytest.fixture(scope="module")
def deepar_trained_zero_data() -> AbstractGluonTSModel:
    pred = TimeSeriesPredictor(prediction_length=2, verbosity=4)

    data = DUMMY_TS_DATAFRAME.copy() * 0

    pred.fit(
        data,
        hyperparameters={
            "DeepAR": dict(epochs=2),
        },
    )
    return pred._trainer.load_model("DeepAR")


@pytest.mark.parametrize("metric_name", GLUONTS_PARITY_METRICS)
def test_when_given_learned_model_when_evaluator_called_then_output_equal_to_gluonts(metric_name, deepar_trained):
    model = deepar_trained

    forecast_iter, ts_iter = make_evaluation_predictions(
        dataset=model._to_gluonts_dataset(DUMMY_TS_DATAFRAME),
        predictor=model.gts_predictor,
        num_samples=100,
    )
    fcast_list, ts_list = list(forecast_iter), list(ts_iter)
    prediction_length = 2
    forecast_index = DUMMY_TS_DATAFRAME.slice_by_timestep(-prediction_length, None).index
    forecast_df = model._gluonts_forecasts_to_data_frame(
        fcast_list,
        quantile_levels=model.quantile_levels,
        forecast_index=forecast_index,
    )

    ag_evaluator = TimeSeriesEvaluator(eval_metric=metric_name, prediction_length=prediction_length)
    ag_value = ag_evaluator(DUMMY_TS_DATAFRAME, forecast_df)

    gts_evaluator = GluonTSEvaluator()
    gts_results, _ = gts_evaluator(
        ts_iterator=ts_list,
        fcst_iterator=fcast_list,
    )

    assert np.isclose(gts_results[metric_name], ag_value, atol=1e-5)


@pytest.mark.parametrize("metric_name", GLUONTS_PARITY_METRICS)
def test_when_given_all_zero_data_when_evaluator_called_then_output_equal_to_gluonts(
    metric_name, deepar_trained_zero_data
):
    model = deepar_trained_zero_data
    data = DUMMY_TS_DATAFRAME.copy() * 0

    forecast_iter, ts_iter = make_evaluation_predictions(
        dataset=model._to_gluonts_dataset(data),
        predictor=model.gts_predictor,
        num_samples=100,
    )
    fcast_list, ts_list = list(forecast_iter), list(ts_iter)
    prediction_length = 2
    forecast_index = DUMMY_TS_DATAFRAME.slice_by_timestep(-prediction_length, None).index
    forecast_df = model._gluonts_forecasts_to_data_frame(
        fcast_list,
        quantile_levels=model.quantile_levels,
        forecast_index=forecast_index,
    )

    ag_evaluator = TimeSeriesEvaluator(eval_metric=metric_name, prediction_length=prediction_length)
    ag_value = ag_evaluator(data, forecast_df)

    gts_evaluator = GluonTSEvaluator()
    gts_results, _ = gts_evaluator(ts_iterator=ts_list, fcst_iterator=fcast_list)

    assert np.isclose(gts_results[metric_name], ag_value, atol=1e-5, equal_nan=True)


@pytest.mark.parametrize("metric_name", GLUONTS_PARITY_METRICS)
def test_when_given_zero_forecasts_when_evaluator_called_then_output_equal_to_gluonts(metric_name, deepar_trained):
    model = deepar_trained
    forecast_iter, ts_iter = make_evaluation_predictions(
        dataset=model._to_gluonts_dataset(DUMMY_TS_DATAFRAME),
        predictor=model.gts_predictor,
        num_samples=100,
    )
    fcast_list, ts_list = list(forecast_iter), list(ts_iter)

    zero_forecast_list = []
    for s in fcast_list:
        zero_forecast_list.append(
            SampleForecast(
                samples=np.zeros_like(s.samples),  # noqa
                start_date=pd.Period(s.start_date, freq=s.freq),
                item_id=s.item_id,
            )
        )
    prediction_length = 2
    forecast_index = DUMMY_TS_DATAFRAME.slice_by_timestep(-prediction_length, None).index
    forecast_df = model._gluonts_forecasts_to_data_frame(
        zero_forecast_list,
        quantile_levels=model.quantile_levels,
        forecast_index=forecast_index,
    )

    ag_evaluator = TimeSeriesEvaluator(eval_metric=metric_name, prediction_length=prediction_length)
    ag_value = ag_evaluator(DUMMY_TS_DATAFRAME, forecast_df)

    gts_evaluator = GluonTSEvaluator()
    gts_results, _ = gts_evaluator(ts_iterator=ts_list, fcst_iterator=zero_forecast_list)

    assert np.isclose(gts_results[metric_name], ag_value, atol=1e-5)


def test_available_metrics_have_coefficients():
    for m in TimeSeriesEvaluator.AVAILABLE_METRICS:
        assert TimeSeriesEvaluator.METRIC_COEFFICIENTS[m]


@pytest.mark.parametrize(
    "check_input, expected_output",
    [
        (None, TimeSeriesEvaluator.DEFAULT_METRIC),
    ]
    + [(k, k) for k in TimeSeriesEvaluator.AVAILABLE_METRICS],
)
@pytest.mark.parametrize("raise_errors", [True, False])
def test_given_correct_input_check_get_eval_metric_output_correct(check_input, expected_output, raise_errors):
    assert expected_output == TimeSeriesEvaluator.check_get_evaluation_metric(
        check_input, raise_if_not_available=raise_errors
    )


@pytest.mark.parametrize("raise_errors", [True, False])
def test_given_no_input_check_get_eval_metric_output_default(raise_errors):
    assert TimeSeriesEvaluator.DEFAULT_METRIC == TimeSeriesEvaluator.check_get_evaluation_metric(
        raise_if_not_available=raise_errors
    )


def test_given_unavailable_input_and_raise_check_get_eval_metric_raises():
    with pytest.raises(ValueError):
        TimeSeriesEvaluator.check_get_evaluation_metric("some_nonsense_eval_metric", raise_if_not_available=True)


def test_given_unavailable_input_and_no_raise_check_get_eval_metric_output_default():
    assert TimeSeriesEvaluator.DEFAULT_METRIC == TimeSeriesEvaluator.check_get_evaluation_metric(
        "some_nonsense_eval_metric", raise_if_not_available=False
    )


@pytest.mark.parametrize("eval_metric", ["MASE", "sMAPE"])
def test_given_historic_data_not_cached_when_scoring_then_exception_is_raised(eval_metric):
    prediction_length = 3
    evaluator = TimeSeriesEvaluator(eval_metric=eval_metric, prediction_length=prediction_length)
    data_future = DUMMY_TS_DATAFRAME.slice_by_timestep(-prediction_length, None)
    predictions = data_future.rename({"target": "mean"}, axis=1)
    with pytest.raises(AssertionError, match="Call save_past_metrics before"):
        evaluator.score_with_saved_past_metrics(data_future=data_future, predictions=predictions)
