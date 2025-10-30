# from .auto import AutoDistributedTargetModel, AutoDraftModelConfig, AutoEagle3DraftModel
from .draft.llama3_eagle import LlamaForCausalLMEagle3
from .target.eagle3_target_model import SGLangEagle3TargetModel, HFEagle3TargetModel, CustomEagle3TargetModel, get_eagle3_target_model
from .auto import AutoDraftModelConfig, AutoEagle3DraftModel

__all__ = [
    "LlamaForCausalLMEagle3",
    "SGLangEagle3TargetModel",
    "HFEagle3TargetModel",
    "CustomEagle3TargetModel",
    "get_eagle3_target_model",
    "AutoDraftModelConfig",
    "AutoEagle3DraftModel",
]
