"""
Modified SpMiddleFHD (see github.com/traveller59/second.pytorch).
"""

import itertools
import torch
import numpy as np
from torch import nn
from torch.nn.modules.batchnorm import _BatchNorm

from torchsearchsorted import searchsorted
import spconv


def make_subm_layer(C_in, C_out, *args, **kwargs):
    layer = spconv.SparseSequential(
        spconv.SubMConv3d(C_in, C_out, 3, *args, **kwargs),
        nn.BatchNorm1d(C_out, eps=1e-3, momentum=0.01),
        nn.ReLU(),
    )
    return layer


def make_sparse_conv_layer(C_in, C_out, *args, **kwargs):
    layer = spconv.SparseSequential(
        spconv.SparseConv3d(C_in, C_out, *args, **kwargs),
        nn.BatchNorm1d(C_out, eps=1e-3, momentum=0.01),
        nn.ReLU(),
    )
    return layer


def random_choice(x, n, dim=0):
    """Emulate numpy.random.choice."""
    assert dim == 0, 'Currently support only dim 0.'
    inds = torch.randint(0, x.size(dim), (n,), device=x.device)
    return x[inds]


def compute_grid_shape(cfg):
    voxel_size = np.r_[cfg.VOXEL_SIZE]
    lower, upper = np.reshape(cfg.GRID_BOUNDS, (2, 3))
    grid_shape = (upper - lower) / voxel_size + [0, 0, 1]
    grid_shape = np.int32(grid_shape)[::-1].tolist()
    return grid_shape


class VoxelFeatureExtractor(nn.Module):
    """Computes mean of non-zero points within voxel."""

    def __init__(self):
        super(VoxelFeatureExtractor, self).__init__()

    def forward(self, feature, occupancy):
        """
        :feature FloatTensor of shape (N, K, C)
        :return FloatTensor of shape (N, C)
        """
        denominator = occupancy.type_as(feature).view(-1, 1)
        feature = (feature.sum(1) / denominator).contiguous()
        return feature


class SparseCNN(nn.Module):
    """
    Returns feature volumes strided 1x, 2x, 4x, 8x, 8x.
    block_1: [ 4, 8y, 8x, 41] -> [32, 4y, 4x, 21]
    block_2: [32, 4y, 4x, 21] -> [64, 2y, 2x, 11]
    block_3: [64, 2y, 2x, 11] -> [64, 1y, 1x,  5]
    block_4: [64, 1y, 1x,  5] -> [64, 1y, 1x,  2]
    """

    def __init__(self, cfg):
        """:grid_shape voxel grid dimensions in ZYX order."""
        super(SparseCNN, self).__init__()
        self.cfg = cfg
        self.grid_shape = compute_grid_shape(cfg)
        self.base_voxel_size = torch.cuda.FloatTensor(cfg.VOXEL_SIZE)
        self.voxel_offset = torch.cuda.FloatTensor(cfg.GRID_BOUNDS[:3])
        self.block1 = spconv.SparseSequential(
            make_subm_layer(cfg.C_IN, 16, 3, indice_key="subm0", bias=False),
            make_subm_layer(16, 16, 3, indice_key="subm0", bias=False),
            make_sparse_conv_layer(16, 32, 3, 2, padding=1, bias=False),
        )
        self.block2 = spconv.SparseSequential(
            make_subm_layer(32, 32, 3, indice_key="subm1", bias=False),
            make_subm_layer(32, 32, 3, indice_key="subm1", bias=False),
            make_sparse_conv_layer(32, 64, 3, 2, padding=1, bias=False),
        )
        self.block3 = spconv.SparseSequential(
            make_subm_layer(64, 64, 3, indice_key="subm2", bias=False),
            make_subm_layer(64, 64, 3, indice_key="subm2", bias=False),
            make_subm_layer(64, 64, 3, indice_key="subm2", bias=False),
            make_sparse_conv_layer(64, 64, 3, 2, padding=[0, 1, 1], bias=False),
        )
        self.block4 = spconv.SparseSequential(
            make_subm_layer(64, 64, 3, indice_key="subm3", bias=False),
            make_subm_layer(64, 64, 3, indice_key="subm3", bias=False),
            make_subm_layer(64, 64, 3, indice_key="subm3", bias=False),
            make_sparse_conv_layer(64, 64, (3, 1, 1), (2, 1, 1), bias=False),
        )

    def maybe_bias_init(self, module, val):
        if hasattr(module, "bias") and module.bias is not None:
            nn.init.constant_(module.bias, val)

    def kaiming_init(self, module):
        nn.init.kaiming_normal_(
            module.weight, a=0, mode='fan_out', nonlinearity='relu')
        self.maybe_bias_init(module, 0)

    def batchnorm_init(self, module):
        nn.init.constant_(module.weight, 1)
        self.maybe_bias_init(module, 0)

    def init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                self.kaiming_init(m)
            elif isinstance(m, _BatchNorm):
                self.batchnorm_init(m)

    def to_global(self, stride, volume):
        """
        Convert integer voxel indices to metric coordinates.
        Indices are reversed ijk -> kji to maintain correspondence with xyz.
        Sparse voxels are padded with subsamples to allow batch PointNet processing.
        :voxel_size length-3 tensor describing size of atomic voxel, accounting for stride.
        :voxel_offset length-3 tensor describing coordinate offset of voxel grid.
        """
        index = torch.flip(volume.indices, (1,))
        voxel_size = self.base_voxel_size * stride
        xyz = index[..., 0:3].float() * voxel_size
        xyz = (xyz + self.voxel_offset)
        xyz = self.pad_batch(xyz, index[..., -1], volume.batch_size)
        feature = self.pad_batch(volume.features, index[..., -1], volume.batch_size)
        return xyz, feature

    def compute_pad_amounts(self, batch_index, batch_size):
        """Compute padding needed to form dense minibatch."""
        helper_index = torch.arange(batch_size + 1, device=batch_index.device)
        helper_index = helper_index.unsqueeze(0).contiguous().int()
        batch_index = batch_index.unsqueeze(0).contiguous().int()
        start_index = searchsorted(batch_index, helper_index).squeeze(0)
        batch_count = start_index[1:] - start_index[:-1]
        pad = list((batch_count.max() - batch_count).cpu().numpy())
        batch_count = list(batch_count.cpu().numpy())
        return batch_count, pad

    def pad_batch(self, x, batch_index, batch_size):
        """Pad sparse tensor with subsamples to form dense minibatch."""
        if batch_size == 1:
            return x.unsqueeze(0)
        batch_count, pad = self.compute_pad_amounts(batch_index, batch_size)
        chunks = x.split(batch_count)
        pad_values = [random_choice(c, n) for (c, n) in zip(chunks, pad)]
        chunks = [torch.cat((c, p)) for (c, p) in zip(chunks, pad_values)]
        return torch.stack(chunks)

    def to_bev(self, volume):
        """Collapse z-dimension to form BEV feature map."""
        volume = volume.dense()
        N, C, D, H, W = volume.shape
        bev = volume.view(N, C * D, H, W)
        return bev

    def forward(self, features, coordinates, batch_size):
        x0 = spconv.SparseConvTensor(
            features, coordinates.int(), self.grid_shape, batch_size
        )
        x1 = self.block1(x0)
        x2 = self.block2(x1)
        x3 = self.block3(x2)
        x4 = self.block4(x3)
        x4 = self.to_bev(x4)
        args = zip(self.cfg.STRIDES, (x0, x1, x2, x3))
        x = list(itertools.starmap(self.to_global, args))
        return x, x4
