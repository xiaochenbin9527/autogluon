import logging
import os
import warnings
from collections import defaultdict, namedtuple
from typing import Dict, List, Optional, Union

import pandas as pd
import torch

from ..constants import HF_TEXT, MMDET_IMAGE, TEXT, TIMM_IMAGE
from ..models.fusion import AbstractMultimodalFusionModel
from ..models.huggingface_text import HFAutoModelForTextPrediction
from ..models.mmdet_image import MMDetAutoModelForObjectDetection
from ..models.timm_image import TimmAutoModelForImagePrediction
from .environment import compute_num_gpus, get_precision_context, infer_precision, move_to_device
from .inference import process_batch
from .onnx import onnx_get_dynamic_axes

logger = logging.getLogger(__name__)


class ExportMixin:
    def dump_model(self, save_path: Optional[str] = None):
        """
        Save model weights and config to local directory.
        Model weights are saved in file `pytorch_model.bin` (timm, hf) or '<ckpt_name>.pth' (mmdet);
        Configs are saved in file `config.json` (timm, hf) or  '<ckpt_name>.py' (mmdet).

        Parameters
        ----------
        path : str
            Path to directory where models and configs should be saved.
        """

        if not save_path:
            save_path = self._save_path if self._save_path else "./"

        supported_models = {
            TIMM_IMAGE: TimmAutoModelForImagePrediction,
            HF_TEXT: HFAutoModelForTextPrediction,
            MMDET_IMAGE: MMDetAutoModelForObjectDetection,
        }

        models = defaultdict(list)
        # TODO: simplify the code
        if isinstance(self._model, AbstractMultimodalFusionModel) and isinstance(
            self._model.model, torch.nn.modules.container.ModuleList
        ):
            for per_model in self._model.model:
                for model_key, model_type in supported_models.items():
                    if isinstance(per_model, model_type):
                        models[model_key].append(per_model)
        else:
            for model_key, model_type in supported_models.items():
                if isinstance(self._model, model_type):
                    models[model_key].append(self._model)

        if not models:
            raise NotImplementedError(
                f"No models available for dump. Current supported models are: {supported_models.keys()}"
            )

        # get tokenizers for hf_text
        text_processors = self._data_processors.get(TEXT, {})
        tokenizers = {}
        for per_processor in text_processors:
            tokenizers[per_processor.prefix] = per_processor.tokenizer

        for model_key in models:
            for per_model in models[model_key]:
                subdir = os.path.join(save_path, per_model.prefix)
                os.makedirs(subdir, exist_ok=True)
                per_model.save(save_path=subdir, tokenizers=tokenizers)

        return save_path

    def export_onnx(
        self,
        data: pd.DataFrame,
        path: Optional[str] = None,
        batch_size: Optional[int] = None,
        verbose: Optional[bool] = False,
        opset_version: Optional[int] = 16,
        truncate_long_and_double: Optional[bool] = False,
    ):
        """
        Export this predictor's model to ONNX file.

        Parameters
        ----------
        data
            Raw data used to trace and export the model.
            If this is None, will check if a processed batch is provided.
        path
            The export path of onnx model.
        batch_size
            The batch_size of export model's input.
            Normally the batch_size is a dynamic axis, so we could use a small value for faster export.
        verbose
            verbose flag in torch.onnx.export.
        opset_version
            opset_version flag in torch.onnx.export.
        truncate_long_and_double: bool, default False
            Truncate weights provided in int64 or double (float64) to int32 and float32

        Returns
        -------
        trt_module : OnnxModule
            The onnx-based module that can be used to replace predictor._model for model inference.
        """

        import torch.jit

        from ..models.fusion.fusion_mlp import MultimodalFusionMLP
        from ..models.huggingface_text import HFAutoModelForTextPrediction
        from ..models.timm_image import TimmAutoModelForImagePrediction

        supported_models = (TimmAutoModelForImagePrediction, HFAutoModelForTextPrediction, MultimodalFusionMLP)
        if not isinstance(self._model, supported_models):
            raise NotImplementedError(f"export_onnx doesn't support model type {type(self._model)}")
        warnings.warn("Currently, the functionality of exporting to ONNX is experimental.")

        # Data preprocessing, loading, and filtering
        batch = self.get_processed_batch_for_deployment(
            data=data,
            onnx_tracing=True,
            batch_size=batch_size,
            truncate_long_and_double=truncate_long_and_double,
        )
        input_keys = self._model.input_keys
        input_vec = [batch[k] for k in input_keys]

        # Infer default onnx path, and create parent directory if needed
        if not path:
            path = self.path
        onnx_path = os.path.join(path, "model.onnx")
        dirname = os.path.dirname(os.path.abspath(onnx_path))
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        # Infer dynamic dimensions
        dynamic_axes = onnx_get_dynamic_axes(input_keys)

        torch.onnx.export(
            self._model.eval(),
            args=tuple(input_vec),
            f=onnx_path,
            opset_version=opset_version,
            verbose=verbose,
            input_names=input_keys,
            dynamic_axes=dynamic_axes,
        )

        return onnx_path

    def export_tensorrt(
        self,
        data: Optional[pd.DataFrame] = None,
        path: Optional[str] = None,
        batch_size: Optional[int] = None,
    ):
        """
        Export this predictor's model to ONNX file.

        Parameters
        ----------
        data
            Raw data used to trace and export the model.
            If this is None, will check if a processed batch is provided.
        path
            The export path of onnx model.
        batch_size
            The batch_size of export model's input.
            Normally the batch_size is a dynamic axis, so we could use a small value for faster export.

        Returns
        -------
        trt_module : OnnxModule
            The onnx-based module that can be used to replace predictor._model for model inference.
        """
        import onnx
        import torch

        from .onnx import OnnxModule

        truncate_long_and_double = False
        onnx_path = self.export_onnx(
            data=data, path=path, batch_size=batch_size, truncate_long_and_double=truncate_long_and_double
        )

        logger.info("Loading ONNX file from path {}...".format(onnx_path))
        onnx_model = onnx.load(onnx_path)

        trt_module = OnnxModule(onnx_model)
        trt_module.input_keys = self._model.input_keys
        trt_module.prefix = self._model.prefix
        trt_module.get_output_dict = self._model.get_output_dict
        return trt_module

    def get_processed_batch_for_deployment(
        self,
        data: Union[pd.DataFrame, dict],
        onnx_tracing: bool = False,
        batch_size: int = None,
        to_numpy: bool = True,
        requires_label: bool = False,
        truncate_long_and_double: bool = False,
    ):
        """
        Get the processed batch of raw data given.

        Parameters
        ----------
        data
            The raw data to process
        onnx_tracing
            If the output is used for onnx tracing.
        batch_size
            The batch_size of output batch.
            If onnx_tracing, it will only output one mini-batch, and all int tensor values will be converted to long.
        to_numpy
            Output numpy array if True. Only valid if not onnx_tracing.
        require_label
            Whether do we put label data into the output batch

        Returns
        -------
        Tensor or numpy array.
        The output processed batch could be used for export/evaluate deployed model.
        """
        data, df_preprocessor, data_processors = self._on_predict_start(
            data=data,
            requires_label=requires_label,
        )

        batch = process_batch(
            data=data,
            df_preprocessor=df_preprocessor,
            data_processors=data_processors,
        )

        input_keys = self._model.input_keys

        # Perform tracing on cpu
        device_type = "cpu"
        num_gpus = 0
        strategy = "dp"  # default used in inference.
        device = torch.device(device_type)
        dtype = infer_precision(
            num_gpus=num_gpus, precision=self._config.env.precision, cpu_only_warning=False, as_torch=True
        )

        # Move model data to the specified device
        for key in input_keys:
            inp = batch[key]
            # support mixed precision on floating point inputs, and leave integer inputs (for language models) untouched.
            if inp.dtype.is_floating_point:
                batch[key] = inp.to(device, dtype=dtype)
            else:
                batch[key] = inp.to(device)
        self._model.to(device)

        # Truncate input data types for TensorRT (only support: bool, int32, half, float)
        if truncate_long_and_double:
            for k in batch:
                if batch[k].dtype == torch.int64:
                    batch[k] = batch[k].to(torch.int32)

        # Data filtering
        ret = {}
        for k in batch:
            if input_keys and k not in input_keys:
                continue
            if onnx_tracing:
                ret[k] = batch[k].long() if isinstance(batch[k], torch.IntTensor) else batch[k]
            elif to_numpy:
                ret[k] = batch[k].cpu().detach().numpy().astype(int)
            else:
                ret[k] = batch[k]
        if not onnx_tracing:
            if batch_size:
                raise NotImplementedError("We should split the batch here.")  # TODO
        return ret
