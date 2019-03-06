# -*- coding: utf-8 -*-
"""MLP module for dqn algorithms

- Author: Kh Kim
- Contact: kh.kim@medipixel.io
"""

from collections import defaultdict
from typing import Any, Callable, DefaultDict, Dict

import torch
import torch.nn as nn
import torch.nn.functional as F

from algorithms.common.networks.mlp import MLP, concat

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


class DuelingMLP(MLP):
    """Multilayer perceptron with dueling construction."""

    def __init__(
        self,
        input_size: int,
        output_size: int,
        hidden_sizes: list,
        hidden_activation: Callable = F.relu,
        init_w: float = 3e-3,
    ):
        """Initialization."""
        super(DuelingMLP, self).__init__(
            input_size=input_size,
            output_size=output_size,
            hidden_sizes=hidden_sizes,
            hidden_activation=hidden_activation,
            use_output_layer=False,
        )
        in_size = hidden_sizes[-1]

        # set advantage layer
        self.advantage_hidden_layer = nn.Linear(in_size, in_size)
        self.advantage_layer = nn.Linear(in_size, output_size)
        self.advantage_layer.weight.data.uniform_(-init_w, init_w)
        self.advantage_layer.bias.data.uniform_(-init_w, init_w)

        # set value layer
        self.value_hidden_layer = nn.Linear(in_size, in_size)
        self.value_layer = nn.Linear(in_size, 1)
        self.value_layer.weight.data.uniform_(-init_w, init_w)
        self.value_layer.bias.data.uniform_(-init_w, init_w)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward method implementation."""
        x = super(DuelingMLP, self).forward(x)

        adv_x = self.advantage_hidden_layer(x)
        val_x = self.value_hidden_layer(x)

        advantage = self.advantage_layer(adv_x)
        value = self.value_layer(val_x)
        advantage_mean = advantage.mean(dim=-1, keepdim=True)

        q = value + advantage - advantage_mean

        return q


class LateFusionDuelingMLP(DuelingMLP):
    """DuelingMLP with late input fusion.

    Attributes:
        _late_fusion_info (DefaultDict): information of late fusion inputs
    """

    def __init__(
        self,
        input_size: int,
        output_size: int,
        hidden_sizes: list,
        late_fusion_info: Dict,  # newly added
        hidden_activation: Callable = F.relu,
        init_w: float = 3e-3,
    ):
        """Initialization."""
        self._late_fusion_info: DefaultDict = defaultdict(lambda: 0)
        for i in late_fusion_info:
            # 1st index has 0th hidden layer info
            self._late_fusion_info[i] = late_fusion_info[i]

        super(LateFusionDuelingMLP, self).__init__(
            input_size=input_size,
            output_size=output_size,
            hidden_sizes=hidden_sizes,
            hidden_activation=hidden_activation,
            init_w=init_w,
        )

    def forward(self, *args: Any) -> torch.Tensor:
        """Forward method implementation."""
        x: torch.Tensor = args[0]
        late_in: list = args[1]

        idx_late_in = 0
        for i, hidden_layer in enumerate(self.hidden_layers):
            if self._late_fusion_info[i] > 0:
                x = concat(x, late_in[idx_late_in])
                idx_late_in += 1
            x = self.hidden_activation(hidden_layer(x))

        adv_x = self.advantage_hidden_layer(x)
        val_x = self.value_hidden_layer(x)

        advantage = self.advantage_layer(adv_x)
        value = self.value_layer(val_x)
        advantage_mean = advantage.mean(dim=-1, keepdim=True)

        q = value + advantage - advantage_mean

        return q

    def _late_fusion_dim(self, idx: int) -> int:
        """Return the dimension for late fusion."""
        return self._late_fusion_info[idx]