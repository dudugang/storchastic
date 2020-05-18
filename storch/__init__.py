from .wrappers import (
    deterministic,
    stochastic,
    reduce,
    _exception_wrapper,
    _unpack_wrapper,
    ignore_wrapping,
)
from .tensor import Tensor, CostTensor, StochasticTensor, Plate, is_tensor
import storch.sampling
import storch.method
from .inference import backward, add_cost, reset, denote_independent, gather_samples
from .util import print_graph
from .storch import *
import storch.typing
import storch.nn


import torch as _torch

_debug = False
_torch.is_tensor = storch.is_tensor
_torch.Tensor.to = deterministic(_torch.Tensor.to)
