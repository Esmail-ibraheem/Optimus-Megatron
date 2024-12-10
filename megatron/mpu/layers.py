# coding=utf-8
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


# Parts of the code here are adapted from PyTorch
# repo: https://github.com/pytorch/pytorch


import math

import torch
import torch.nn.functional as F
import torch.nn.init as init
from torch.nn.parameter import Parameter
from functools import partial

from .initialize import get_tensor_model_parallel_rank
from .initialize import get_tensor_model_parallel_world_size
from .mappings import copy_to_tensor_model_parallel_region
from .mappings import gather_from_tensor_model_parallel_region
from .mappings import reduce_from_tensor_model_parallel_region
from .mappings import scatter_to_tensor_model_parallel_region
from .random import get_cuda_rng_tracker
from .utils import divide
from .utils import split_tensor_along_last_dim
from .utils import VocabUtility
from ..model.fused_layer_norm import MixedFusedLayerNorm as LayerNorm
from megatron import get_args, mpu
import deepspeed.runtime.activation_checkpointing.checkpointing as ds_checkpointing


_MODEL_PARALLEL_ATTRIBUTE_DEFAULTS = {'tensor_model_parallel': False,
                                      'partition_dim': -1,
                                      'partition_stride': 1}


def param_is_not_tensor_parallel_duplicate(param):
    return (hasattr(param, 'tensor_model_parallel') and
            param.tensor_model_parallel) or (
                get_tensor_model_parallel_rank() == 0)


def set_tensor_model_parallel_attributes(tensor, is_parallel, dim, stride):
    # Make sure the attributes are not set.
    for attribute in _MODEL_PARALLEL_ATTRIBUTE_DEFAULTS:
        assert not hasattr(tensor, attribute)
    # Set the attributes.
    setattr(tensor, 'tensor_model_parallel', is_parallel)
    setattr(tensor, 'partition_dim', dim)
    setattr(tensor, 'partition_stride', stride)


def set_defaults_if_not_set_tensor_model_parallel_attributes(tensor):
    def maybe_set(attribute, value):
        if not hasattr(tensor, attribute):
            setattr(tensor, attribute, value)
    for attribute in _MODEL_PARALLEL_ATTRIBUTE_DEFAULTS:
        maybe_set(attribute, _MODEL_PARALLEL_ATTRIBUTE_DEFAULTS[attribute])


def copy_tensor_model_parallel_attributes(destination_tensor, source_tensor):
    def maybe_copy(attribute):
        if hasattr(source_tensor, attribute):
            setattr(destination_tensor, attribute,
                    getattr(source_tensor, attribute))
    for attribute in _MODEL_PARALLEL_ATTRIBUTE_DEFAULTS:
        maybe_copy(attribute)


def _initialize_affine_weight_gpu(weight, init_method,
                                  partition_dim, stride=1):
    """Initialize affine weight for model parallel on GPU."""

    set_tensor_model_parallel_attributes(tensor=weight,
                                         is_parallel=True,
                                         dim=partition_dim,
                                         stride=stride)

    if ds_checkpointing.is_configured():
        global get_cuda_rng_tracker
        get_cuda_rng_tracker = ds_checkpointing.get_cuda_rng_tracker

    with get_cuda_rng_tracker().fork():
        init_method(weight)


def _initialize_affine_weight_cpu(weight, output_size, input_size,
                                  per_partition_size, partition_dim,
                                  init_method, stride=1,
                                  return_master_weight=False):
    """Initialize affine weight for model parallel.

    Build the master weight on all processes and scatter
    the relevant chunk."""

    set_tensor_model_parallel_attributes(tensor=weight,
                                         is_parallel=True,
                                         dim=partition_dim,
                                         stride=stride)

    # Initialize master weight
    master_weight = torch.empty(output_size, input_size,
                                dtype=torch.float,
                                requires_grad=False)
    init_method(master_weight)
    args = get_args()
    master_weight = master_weight.to(dtype=args.params_dtype)

    # Split and copy
    per_partition_per_stride_size = divide(per_partition_size, stride)
    weight_list = torch.split(master_weight, per_partition_per_stride_size,
                              dim=partition_dim)
    rank = get_tensor_model_parallel_rank()
    world_size = get_tensor_model_parallel_world_size()
    my_weight_list = weight_list[rank::world_size]

    with torch.no_grad():
        torch.cat(my_weight_list, dim=partition_dim, out=weight)
    if return_master_weight:
        return master_weight
    return None


def xavier_uniform_tensor_parallel_(tensor, gain=1., tp_degree=1):
    r"""
    This is a modified torch.nn.init.xavier_uniform_ with changes to support
    partitioned on the vocab size dim embedding with tensor parallel.

    Additional args:
    - tp_degree: degree of tensor parallel

    Note: the code assumes all partitions are equal in size
    """
    # receptive_field_size=1 as dim==2, so we don't need init._calculate_fan_in_and_fan_out
    fan_out, fan_in = tensor.shape
    fan_out *= tp_degree # tp splits on num_embeddings dim

    std = gain * math.sqrt(2.0 / float(fan_in + fan_out))
    a = math.sqrt(3.0) * std  # Calculate uniform bounds from standard deviation

    return torch.nn.init._no_grad_uniform_(tensor, -a, a)


class VocabParallelEmbedding(torch.nn.Module):
    """Embedding parallelized in the vocabulary dimension.

    This is mainly adapted from torch.nn.Embedding and all the default
    values are kept.
    Arguments:
        num_embeddings: vocabulary size.
        embedding_dim: size of hidden state.
        init_method: method to initialize weights.
    """

    def __init__(self, num_embeddings, embedding_dim,
                 init_method=init.xavier_normal_):
        super(VocabParallelEmbedding, self).__init__()
        # Keep the input dimensions.
        self.num_embeddings = num_embeddings
        self.embedding_dim = embedding_dim
        # Set the defaults for compatibility.
        self.padding_idx = None
        self.max_norm = None
        self.norm_type = 2.
        self.scale_grad_by_freq = False
        self.sparse = False
        self._weight = None
        self.tensor_model_parallel_size = get_tensor_model_parallel_world_size()
        
        # Initialize parallel state
        self.parallel_state = {
            'tp_size': self.tensor_model_parallel_size,
            'tp_rank': get_tensor_model_parallel_rank()
        }
        
        # Divide the weight matrix along the vocabulary dimension.
        self._update_vocab_range()
        
        # Allocate weights and initialize.
        self._initialize_weight(init_method)
        
        args = get_args()
        # Only the first stage embedding runs this class' forward
        if mpu.is_pipeline_first_stage() and (args.use_bnb_optimizer or args.embed_layernorm):
            self.norm = LayerNorm(embedding_dim)

    def _update_vocab_range(self):
        """Update vocabulary range based on current parallel state."""
        self.vocab_start_index, self.vocab_end_index = \
            VocabUtility.vocab_range_from_global_vocab_size(
                self.num_embeddings, 
                self.parallel_state['tp_rank'],
                self.parallel_state['tp_size'])
        self.num_embeddings_per_partition = self.vocab_end_index - \
            self.vocab_start_index

    def _initialize_weight(self, init_method):
        """Initialize the weights."""
        args = get_args()
        
        if args.use_bnb_optimizer:
            # For BNB we use modified xavier_uniform
            init_method = partial(xavier_uniform_tensor_parallel_, 
                                tp_degree=self.parallel_state['tp_size'])
        
        if args.use_cpu_initialization:
            self.weight = Parameter(torch.empty(
                self.num_embeddings_per_partition, self.embedding_dim,
                dtype=args.params_dtype))
            _initialize_affine_weight_cpu(
                self.weight, self.num_embeddings, self.embedding_dim,
                self.num_embeddings_per_partition, 0, init_method)
        else:
            self.weight = Parameter(torch.empty(
                self.num_embeddings_per_partition, self.embedding_dim,
                device=torch.cuda.current_device(), dtype=args.params_dtype))
            _initialize_affine_weight_gpu(self.weight, init_method,
                                      partition_dim=0, stride=1)
        
        if args.use_bnb_optimizer:
            from bitsandbytes.optim import GlobalOptimManager
            GlobalOptimManager.get_instance().override_config(self.weight, 'optim_bits', 32)
            GlobalOptimManager.get_instance().register_parameters(self.weight)

    def update_parallel_state(self, tp_size=None):
        """Update parallel state and reinitialize if necessary."""
        if tp_size is not None and tp_size != self.parallel_state['tp_size']:
            # Update parallel state
            self.parallel_state['tp_size'] = tp_size
            self.parallel_state['tp_rank'] = get_tensor_model_parallel_rank()
            
            # Update vocab range
            self._update_vocab_range()
            
            # Reinitialize weight with new partition size
            args = get_args()
            if args.use_bnb_optimizer:
                init_method = partial(xavier_uniform_tensor_parallel_, 
                                    tp_degree=self.parallel_state['tp_size'])
            else:
                init_method = init.xavier_normal_
            
            self._initialize_weight(init_method)

    def forward(self, input_):
        if torch.any(input_ >= self.num_embeddings):
            raise ValueError(f"There is an input id in the input that is greater than the highest possible input id.\nInput: {input_}\nnum_embeddings: {self.num_embeddings}")

        if self.tensor_model_parallel_size > 1:
            # Build the mask.
            input_mask = (input_ < self.vocab_start_index) | \
                        (input_ >= self.vocab_end_index)
            # Mask the input.
            masked_input = input_.clone() - self.vocab_start_index
            masked_input[input_mask] = 0
        else:
            # input_ is garanted to be in the range [0:self.vocab_end_index - self.vocab_start_index] thanks to the first check
            masked_input = input_

        # Get the embeddings.
        output_parallel = F.embedding(masked_input, self.weight,
                                       self.padding_idx, self.max_norm,
                                       self.norm_type, self.scale_grad_by_freq,
                                       self.sparse)
        # Mask the output embedding.
        if self.tensor_model_parallel_size > 1:
            output_parallel[input_mask, :] = 0.0
        # Reduce across all the model parallel GPUs.
        output = reduce_from_tensor_model_parallel_region(output_parallel)

        if hasattr(self, 'norm'):
            output = self.norm(output)

        return output


class ColumnParallelLinear(torch.nn.Module):
    """Linear layer with column parallelism.

    The linear layer is defined as Y = XA + b. A is parallelized along
    its second dimension as A = [A_1, ..., A_p].

    Arguments:
        input_size: first dimension of matrix A.
        output_size: second dimension of matrix A.
        bias: If true, add bias
        gather_output: If true, call all-gather on output and make Y available
                      to all GPUs, otherwise, every GPU will have its output
                      which is Y_i = XA_i
        init_method: method to initialize weights. Note that bias is always set
                    to zero.
        stride: For the strided linear layers.
        keep_master_weight_for_test: This was added for testing and should be
                                    set to False. It returns the master weights
                                    used for initialization.
        skip_bias_add: This was added to enable performance optimizations where bias
                      can be fused with other elementwise operations. We skip
                      adding bias but instead return it.
    """

    def __init__(self, input_size, output_size, bias=True, gather_output=True,
                 init_method=init.xavier_normal_, stride=1,
                 keep_master_weight_for_test=False,
                 skip_bias_add=False):
        super(ColumnParallelLinear, self).__init__()

        # Keep input parameters
        self.input_size = input_size
        self.output_size = output_size
        self.gather_output = gather_output
        self.init_method = init_method
        self.stride = stride
        self.keep_master_weight_for_test = keep_master_weight_for_test
        self.skip_bias_add = skip_bias_add
        
        # Initialize parallel state
        self.parallel_state = {
            'tp_size': get_tensor_model_parallel_world_size(),
            'tp_rank': get_tensor_model_parallel_rank()
        }
        
        # Update sizes based on parallel state
        self._update_sizes()
        
        # Initialize parameters
        self._initialize_weights()

    def _update_sizes(self):
        """Update sizes based on current parallel state."""
        self.output_size_per_partition = divide(
            self.output_size, self.parallel_state['tp_size'])

    def _initialize_weights(self):
        """Initialize weights and bias."""
        args = get_args()
        
        # Initialize weight
        if args.use_cpu_initialization:
            self.weight = Parameter(torch.empty(
                self.output_size_per_partition, self.input_size,
                dtype=args.params_dtype))
            self.master_weight = _initialize_affine_weight_cpu(
                self.weight, self.output_size, self.input_size,
                self.output_size_per_partition, 0, self.init_method,
                stride=self.stride,
                return_master_weight=self.keep_master_weight_for_test)
        else:
            self.weight = Parameter(torch.empty(
                self.output_size_per_partition, self.input_size,
                device=torch.cuda.current_device(), dtype=args.params_dtype))
            _initialize_affine_weight_gpu(self.weight, self.init_method,
                                      partition_dim=0, stride=self.stride)
        
        if self.bias:
            if args.use_cpu_initialization:
                self.bias = Parameter(torch.empty(
                    self.output_size_per_partition, dtype=args.params_dtype))
            else:
                self.bias = Parameter(torch.empty(
                    self.output_size_per_partition,
                    device=torch.cuda.current_device(),
                    dtype=args.params_dtype))
            # Always initialize bias to zero
            with torch.no_grad():
                self.bias.zero_()
            set_tensor_model_parallel_attributes(self.bias, True, 0, self.stride)
        else:
            self.register_parameter('bias', None)

    def update_parallel_state(self, tp_size=None):
        """Update parallel state and reinitialize if necessary."""
        if tp_size is not None and tp_size != self.parallel_state['tp_size']:
            # Update parallel state
            self.parallel_state['tp_size'] = tp_size
            self.parallel_state['tp_rank'] = get_tensor_model_parallel_rank()
            
            # Update sizes
            old_output_size = self.output_size_per_partition
            self._update_sizes()
            
            # Reinitialize weights if size changed
            if old_output_size != self.output_size_per_partition:
                self._initialize_weights()

    def forward(self, input_):
        # Set up backprop all-reduce.
        input_parallel = copy_to_tensor_model_parallel_region(input_)
        # Matrix multiply.
        output_parallel = F.linear(input_parallel, self.weight)
        if self.bias is not None and not self.skip_bias_add:
            output_parallel = output_parallel + self.bias
        if self.gather_output:
            # All-gather across the partitions.
            output = gather_from_tensor_model_parallel_region(output_parallel)
        else:
            output = output_parallel
        if self.bias is not None and self.skip_bias_add:
            return output, self.bias
        return output

class RowParallelLinear(torch.nn.Module):
    """Linear layer with row parallelism.

    The linear layer is defined as Y = XA + b. A is parallelized along
    its first dimension and X along its second dimension as:
               -   -
              | A_1 |
              | .   |
          A = | .   |        X = [X_1, ..., X_p]
              | .   |
              | A_p |
               -   -
    Arguments:
        input_size: first dimension of matrix A.
        output_size: second dimension of matrix A.
        bias: If true, add bias. Note that bias is not parallelized.
        input_is_parallel: If true, we assume that the input is already
                          split across the GPUs and we do not split
                          again.
        init_method: method to initialize weights. Note that bias is always set
                    to zero.
        stride: For the strided linear layers.
        keep_master_weight_for_test: This was added for testing and should be
                                    set to False. It returns the master weights
                                    used for initialization.
        skip_bias_add: This was added to enable performance optimizations where bias
                      can be fused with other elementwise operations. We skip
                      adding bias but instead return it.
    """

    def __init__(self, input_size, output_size, bias=True,
                 input_is_parallel=False,
                 init_method=init.xavier_normal_, stride=1,
                 keep_master_weight_for_test=False,
                 skip_bias_add=False):
        super(RowParallelLinear, self).__init__()

        # Keep input parameters
        self.input_size = input_size
        self.output_size = output_size
        self.input_is_parallel = input_is_parallel
        self.init_method = init_method
        self.stride = stride
        self.keep_master_weight_for_test = keep_master_weight_for_test
        self.skip_bias_add = skip_bias_add
        
        # Initialize parallel state
        self.parallel_state = {
            'tp_size': get_tensor_model_parallel_world_size(),
            'tp_rank': get_tensor_model_parallel_rank()
        }
        
        # Update sizes based on parallel state
        self._update_sizes()
        
        # Initialize parameters
        self._initialize_weights()
        
        args = get_args()
        self.bias_tp_auto_sync = args.sync_tp_duplicated_parameters

    def _update_sizes(self):
        """Update sizes based on current parallel state."""
        self.input_size_per_partition = divide(
            self.input_size, self.parallel_state['tp_size'])

    def _initialize_weights(self):
        """Initialize weights and bias."""
        args = get_args()
        
        # Initialize weight
        if args.use_cpu_initialization:
            self.weight = Parameter(torch.empty(
                self.output_size, self.input_size_per_partition,
                dtype=args.params_dtype))
            self.master_weight = _initialize_affine_weight_cpu(
                self.weight, self.output_size, self.input_size,
                self.input_size_per_partition, 1, self.init_method,
                stride=self.stride,
                return_master_weight=self.keep_master_weight_for_test)
        else:
            self.weight = Parameter(torch.empty(
                self.output_size, self.input_size_per_partition,
                device=torch.cuda.current_device(), dtype=args.params_dtype))
            _initialize_affine_weight_gpu(self.weight, self.init_method,
                                      partition_dim=1, stride=self.stride)
        
        if self.bias:
            if args.use_cpu_initialization:
                self.bias = Parameter(torch.empty(
                    self.output_size, dtype=args.params_dtype))
            else:
                self.bias = Parameter(torch.empty(
                    self.output_size, device=torch.cuda.current_device(),
                    dtype=args.params_dtype))
            # Always initialize bias to zero
            with torch.no_grad():
                self.bias.zero_()
        else:
            self.register_parameter('bias', None)

    def update_parallel_state(self, tp_size=None):
        """Update parallel state and reinitialize if necessary."""
        if tp_size is not None and tp_size != self.parallel_state['tp_size']:
            # Update parallel state
            self.parallel_state['tp_size'] = tp_size
            self.parallel_state['tp_rank'] = get_tensor_model_parallel_rank()
            
            # Update sizes
            old_input_size = self.input_size_per_partition
            self._update_sizes()
            
            # Reinitialize weights if size changed
            if old_input_size != self.input_size_per_partition:
                self._initialize_weights()

    def forward(self, input_):
        # Set up input tensor.
        if self.input_is_parallel:
            input_parallel = input_
        else:
            input_parallel = scatter_to_tensor_model_parallel_region(input_)
        # Matrix multiply.
        output_parallel = F.linear(input_parallel, self.weight)
        # All-reduce across all the partitions.
        output_ = reduce_from_tensor_model_parallel_region(output_parallel)
        if self.bias is not None and not self.skip_bias_add:
            output = output_ + self.bias
        else:
            output = output_
        if self.bias is not None and self.skip_bias_add:
            return output, self.bias
        return output
