from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
from typing import Optional, Dict, Tuple, Any

import torch
from safetensors.torch import load_file
from torch.utils.hooks import RemovableHandle

from diffusers.models import UNet2DConditionModel
from transformers import CLIPTextModel

class LoRALayerBase:
    #rank: Optional[int]
    #alpha: Optional[float]
    #bias: Optional[torch.Tensor]
    #layer_key: str

    #@property
    #def scale(self):
    #    return self.alpha / self.rank if (self.alpha and self.rank) else 1.0

    def __init__(
        self,
        layer_key: str,
        values: dict,
    ):
        if "alpha" in values:
            self.alpha = values["alpha"].item()
        else:
            self.alpha = None

        if (
            "bias_indices" in values
            and "bias_values" in values
            and "bias_size" in values
        ):
            self.bias = torch.sparse_coo_tensor(
                values["bias_indices"],
                values["bias_values"],
                tuple(values["bias_size"]),
            )

        else:
            self.bias = None

        self.rank = None # set in layer implementation
        self.layer_key = layer_key

    def forward(
        self,
        module: torch.nn.Module,
        input_h: Any, # for real looks like Tuple[torch.nn.Tensor] but not sure
        multiplier: float,
    ):
        if type(module) == torch.nn.Conv2d:
            op = torch.nn.functional.conv2d
            extra_args = dict(
                stride=module.stride,
                padding=module.padding,
                dilation=module.dilation,
                groups=module.groups,
            )

        else:
            op = torch.nn.functional.linear
            extra_args = {}

        weight = self.get_weight(module)

        bias = self.bias if self.bias is not None else 0
        scale = self.alpha / self.rank if (self.alpha and self.rank) else 1.0
        return op(
            *input_h,
            (weight + bias).view(module.weight.shape),
            None,
            **extra_args,
        ) * multiplier * scale

    def get_weight(self, module: torch.nn.Module):
        raise NotImplementedError()

    def calc_size(self) -> int:
        model_size = 0
        for val in [self.bias]:
            if val is not None:
                model_size += val.nelement() * val.element_size()
        return model_size

    def to(
        self,
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
    ):
        if self.bias is not None:
            self.bias = self.bias.to(device=device, dtype=dtype)


# TODO: find and debug lora/locon with bias
class LoRALayer(LoRALayerBase):
    #up: torch.Tensor
    #mid: Optional[torch.Tensor]
    #down: torch.Tensor

    def __init__(
        self,
        layer_key: str,
        values: dict,
    ):
        super().__init__(layer_key, values)

        self.up = values["lora_up.weight"]
        self.down = values["lora_down.weight"]
        if "lora_mid.weight" in values:
            self.mid = values["lora_mid.weight"]
        else:
            self.mid = None

        self.rank = self.down.shape[0]

    def get_weight(self, module: torch.nn.Module):
        if self.mid is not None:
            up = self.up.reshape(up.shape[0], up.shape[1])
            down = self.down.reshape(up.shape[0], up.shape[1])
            weight = torch.einsum("m n w h, i m, n j -> i j w h", self.mid, up, down)
        else:
            weight = self.up.reshape(self.up.shape[0], -1) @ self.down.reshape(self.down.shape[0], -1)

        return weight

    def calc_size(self) -> int:
        model_size = super().calc_size()
        for val in [self.up, self.mid, self.down]:
            if val is not None:
                model_size += val.nelement() * val.element_size()
        return model_size

    def to(
        self,
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
    ):
        super().to(device=device, dtype=dtype)

        self.up = self.up.to(device=device, dtype=dtype)
        self.down = self.down.to(device=device, dtype=dtype)

        if self.mid is not None:
            self.mid = self.mid.to(device=device, dtype=dtype)


class LoHALayer(LoRALayerBase):
    #w1_a: torch.Tensor
    #w1_b: torch.Tensor
    #w2_a: torch.Tensor
    #w2_b: torch.Tensor
    #t1: Optional[torch.Tensor] = None
    #t2: Optional[torch.Tensor] = None

    def __init__(
        self,
        layer_key: str,
        values: dict,
    ):
        super().__init__(module_key, rank, alpha, bias)

        self.w1_a = values["hada_w1_a"]
        self.w1_b = values["hada_w1_b"]
        self.w2_a = values["hada_w2_a"]
        self.w2_b = values["hada_w2_b"]

        if "hada_t1" in values:
            self.t1 = values["hada_t1"]
        else:
            self.t1 = None

        if "hada_t2" in values:
            self.t2 = values["hada_t2"]
        else:
            self.t2 = None

        self.rank = self.w1_b.shape[0]

    def get_weight(self, module: torch.nn.Module):
        if self.t1 is None:
            weight = (self.w1_a @ self.w1_b) * (self.w2_a @ self.w2_b)

        else:
            rebuild1 = torch.einsum(
                "i j k l, j r, i p -> p r k l", self.t1, self.w1_b, self.w1_a
            )
            rebuild2 = torch.einsum(
                "i j k l, j r, i p -> p r k l", self.t2, self.w2_b, self.w2_a
            )
            weight = rebuild1 * rebuild2

        return weight

    def calc_size(self) -> int:
        model_size = super().calc_size()
        for val in [self.w1_a, self.w1_b, self.w2_a, self.w2_b, self.t1, self.t2]:
            if val is not None:
                model_size += val.nelement() * val.element_size()
        return model_size

    def to(
        self,
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
    ):
        super().to(device=device, dtype=dtype)

        self.w1_a = self.w1_a.to(device=device, dtype=dtype)
        self.w1_b = self.w1_b.to(device=device, dtype=dtype)
        if self.t1 is not None:
            self.t1 = self.t1.to(device=device, dtype=dtype)

        self.w2_a = self.w2_a.to(device=device, dtype=dtype)
        self.w2_b = self.w2_b.to(device=device, dtype=dtype)
        if self.t2 is not None:
            self.t2 = self.t2.to(device=device, dtype=dtype)


class LoKRLayer(LoRALayerBase):
    #w1: Optional[torch.Tensor] = None
    #w1_a: Optional[torch.Tensor] = None
    #w1_b: Optional[torch.Tensor] = None
    #w2: Optional[torch.Tensor] = None
    #w2_a: Optional[torch.Tensor] = None
    #w2_b: Optional[torch.Tensor] = None
    #t2: Optional[torch.Tensor] = None

    def __init__(
        self,
        layer_key: str,
        values: dict,
    ):
        super().__init__(module_key, rank, alpha, bias)        

        if "lokr_w1" in values:
            self.w1 = values["lokr_w1"]
            self.w1_a = None
            self.w1_b = None
        else:
            self.w1 = None
            self.w1_a = values["lokr_w1_a"]
            self.w1_b = values["lokr_w1_b"]

        if "lokr_w2" in values:
            self.w2 = values["lokr_w2"]
            self.w2_a = None
            self.w2_b = None
        else:
            self.w2 = None
            self.w2_a = values["lokr_w2_a"]
            self.w2_b = values["lokr_w2_b"]

        if "lokr_t2" in values:
            self.t2 = values["lokr_t2"]
        else:
            self.t2 = None

        if "lokr_w1_b" in values:
            self.rank = values["lokr_w1_b"].shape[0]
        elif "lokr_w2_b" in values:
            self.rank = values["lokr_w2_b"].shape[0]
        else:
            self.rank = None # unscaled

    def get_weight(self, module: torch.nn.Module):
        w1 = self.w1
        if w1 is None:
            w1 = self.w1_a @ self.w1_b

        w2 = self.w2
        if w2 is None:
            if self.t2 is None:
                w2 = self.w2_a @ self.w2_b
            else:
                w2 = torch.einsum('i j k l, i p, j r -> p r k l', self.t2, self.w2_a, self.w2_b)

        if len(w2.shape) == 4:
            w1 = w1.unsqueeze(2).unsqueeze(2)
        w2 = w2.contiguous()
        weight = torch.kron(w1, w2).reshape(module.weight.shape) # TODO: can we remove reshape?

        return weight

    def calc_size(self) -> int:
        model_size = super().calc_size()
        for val in [self.w1, self.w1_a, self.w1_b, self.w2, self.w2_a, self.w2_b, self.t2]:
            if val is not None:
                model_size += val.nelement() * val.element_size()
        return model_size

    def to(
        self,
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
    ):
        super().to(device=device, dtype=dtype)

        if self.w1 is not None:
            self.w1 = self.w1.to(device=device, dtype=dtype)
        else:
            self.w1_a = self.w1_a.to(device=device, dtype=dtype)
            self.w1_b = self.w1_b.to(device=device, dtype=dtype)

        if self.w2 is not None:
            self.w2 = self.w2.to(device=device, dtype=dtype)
        else:
            self.w2_a = self.w2_a.to(device=device, dtype=dtype)
            self.w2_b = self.w2_b.to(device=device, dtype=dtype)

        if self.t2 is not None:
            self.t2 = self.t2.to(device=device, dtype=dtype)


class LoRAModel: #(torch.nn.Module):
    _name: str
    layers: Dict[str, LoRALayer]
    _device: torch.device
    _dtype: torch.dtype

    def __init__(
        self,
        name: str,
        layers: Dict[str, LoRALayer],
        device: torch.device,
        dtype: torch.dtype,
    ):
        self._name = name
        self._device = device or torch.cpu
        self._dtype = dtype or torch.float32
        self.layers = layers

    @property
    def name(self):
        return self._name

    @property
    def device(self):
        return self._device

    @property
    def dtype(self):
        return self._dtype    

    def to(
        self,
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
    ) -> LoRAModel:
        # TODO: try revert if exception?
        for key, layer in self.layers.items():
            layer.to(device=device, dtype=dtype)
        self._device = device
        self._dtype = dtype

    def calc_size(self) -> int:
        model_size = 0
        for _, layer in self.layers.items():
            model_size += layer.calc_size()
        return model_size

    @classmethod
    def from_checkpoint(
        cls,
        file_path: Union[str, Path],
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
    ):
        device = device or torch.device("cpu")
        dtype = dtype or torch.float32

        if isinstance(file_path, str):
            file_path = Path(file_path)

        model = cls(
            device=device,
            dtype=dtype,
            name=file_path.stem, # TODO:
            layers=dict(),
        )

        if file_path.suffix == ".safetensors":
            state_dict = load_file(file_path.absolute().as_posix(), device="cpu")
        else:
            state_dict = torch.load(file_path, map_location="cpu")

        state_dict = cls._group_state(state_dict)

        for layer_key, values in state_dict.items():

            # lora and locon
            if "lora_down.weight" in values:
                layer = LoRALayer(layer_key, values)

            # loha
            elif "hada_w1_b" in values:
                layer = LoHALayer(layer_key, values)

            # lokr
            elif "lokr_w1_b" in values or "lokr_w1" in values:
                layer = LoKRLayer(layer_key, values)

            else:
                # TODO: diff/ia3/... format
                print(
                    f">> Encountered unknown lora layer module in {self.name}: {layer_key}"
                )
                return

            # lower memory consumption by removing already parsed layer values
            state_dict[layer_key].clear()

            layer.to(device=device, dtype=dtype)
            model.layers[layer_key] = layer

        return model

    @staticmethod
    def _group_state(state_dict: dict):
        state_dict_groupped = dict()

        for key, value in state_dict.items():
            stem, leaf = key.split(".", 1)
            if stem not in state_dict_groupped:
                state_dict_groupped[stem] = dict()
            state_dict_groupped[stem][leaf] = value

        return state_dict_groupped


"""
loras = [
    (lora_model1, 0.7),
    (lora_model2, 0.4),
]
with LoRAHelper.apply_lora_unet(unet, loras):
    # unet with applied loras
# unmodified unet

"""
# TODO: rename smth like ModelPatcher and add TI method?
class LoRAHelper:

    @staticmethod
    def _resolve_lora_key(model: torch.nn.Module, lora_key: str, prefix: str) -> Tuple[str, torch.nn.Module]:
        assert "." not in lora_key

        if not lora_key.startswith(prefix):
            raise Exception(f"lora_key with invalid prefix: {lora_key}, {prefix}")

        module = model
        module_key = ""
        key_parts = lora_key[len(prefix):].split('_')

        submodule_name = key_parts.pop(0)
        
        while len(key_parts) > 0:
            try:
                module = module.get_submodule(submodule_name)
                module_key += "." + submodule_name
                submodule_name = key_parts.pop(0)
            except:
                submodule_name += "_" + key_parts.pop(0)

        module = module.get_submodule(submodule_name)
        module_key = module_key.rstrip(".")

        return (module_key, module)

    @staticmethod
    def _lora_forward_hook(
        applied_loras: List[Tuple[LoraModel, float]],
        layer_name: str,
    ):

        def lora_forward(module, input_h, output):
            if len(applied_loras) == 0:
                return output

            for lora, weight in applied_loras:
                layer = lora.layers.get(layer_name, None)
                if layer is None:
                    continue
                output += layer.forward(module, input_h, weight)
            return output

        return lora_forward


    @classmethod
    @contextmanager
    def apply_lora_unet(
        cls,
        unet: UNet2DConditionModel,
        loras: List[Tuple[LoRAModel, float]],
    ):
        with cls.apply_lora(unet, loras, "lora_unet_"):
            yield


    @classmethod
    @contextmanager
    def apply_lora_text_encoder(
        cls,
        text_encoder: CLIPTextModel,
        loras: List[Tuple[LoRAModel, float]],
    ):
        with cls.apply_lora(text_encoder, loras, "lora_te_"):
            yield


    @classmethod
    @contextmanager
    def apply_lora(
        cls,
        model: torch.nn.Module,
        loras: List[Tuple[LoraModel, float]],
        prefix: str,
    ):
        hooks = dict()
        try:
            for lora, lora_weight in loras:
                for layer_key, layer in lora.layers.items():
                    if not layer_key.startswith(prefix):
                        continue

                    module_key, module = cls._resolve_lora_key(model, layer_key, prefix)
                    if module_key not in hooks:
                        hooks[module_key] = module.register_forward_hook(cls._lora_forward_hook(loras, layer_key))

            yield # wait for context manager exit

        finally:
            for module_key, hook in hooks.items():
                hook.remove()
            hooks.clear()
