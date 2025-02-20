import pickle

import numpy as np
import pystrum.pynd.ndutils as nd
import torch
import torch.nn.functional as F
from scipy.ndimage import gaussian_filter
from torch import nn

import kornia


def pkload(fname):
    with open(fname, "rb") as f:
        return pickle.load(f)


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0
        self.vals = []
        self.std = 0
        self.stderr = 0
        self.median = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count
        self.vals.append(val)
        self.std = np.std(self.vals)
        self.stderr = self.std / np.sqrt(self.count)
        self.median = np.median(self.vals)


class SpatialTransformer(nn.Module):
    """
    N-D Spatial Transformer
    """

    def __init__(self, size, mode="bilinear", gpu=True):
        super().__init__()

        self.mode = mode

        # create sampling grid
        vectors = [torch.arange(0, s) for s in size]
        grids = torch.meshgrid(vectors)
        grid = torch.stack(grids)
        grid = torch.unsqueeze(grid, 0)
        grid = grid.type(torch.FloatTensor)
        if gpu:
            grid = grid.cuda()

        # registering the grid as a buffer cleanly moves it to the GPU, but it also
        # adds it to the state dict. this is annoying since everything in the state dict
        # is included when saving weights to disk, so the model files are way bigger
        # than they need to be. so far, there does not appear to be an elegant solution.
        # see: https://discuss.pytorch.org/t/how-to-register-buffer-without-polluting-state-dict
        self.register_buffer("grid", grid)

    def forward(self, src, flow):
        # new locations
        new_locs = self.grid + flow
        shape = flow.shape[2:]

        # need to normalize grid values to [-1, 1] for resampler
        for i in range(len(shape)):
            new_locs[:, i, ...] = 2 * (new_locs[:, i, ...] / (shape[i] - 1) - 0.5)

        # move channels dim to last position
        # also not sure why, but the channels need to be reversed
        if len(shape) == 2:
            new_locs = new_locs.permute(0, 2, 3, 1)
            new_locs = new_locs[..., [1, 0]]
        elif len(shape) == 3:
            new_locs = new_locs.permute(0, 2, 3, 4, 1)
            new_locs = new_locs[..., [2, 1, 0]]

        return F.grid_sample(src, new_locs, align_corners=True, mode=self.mode)


class register_model(nn.Module):
    def __init__(self, img_size=(64, 256, 256), mode="bilinear", gpu=True):
        super(register_model, self).__init__()
        self.spatial_trans = SpatialTransformer(img_size, mode, gpu)

    def forward(self, x):
        img = x[0]
        flow = x[1]
        out = self.spatial_trans(img, flow)
        return out

def rotate_images(images, angles):
    """
    Rotate a batch of 3D images along the depth axis (D) by specified angles.

    Args:
        images (torch.Tensor): Batch of 3D images with shape (N, C, H, W, D).
        angles (torch.Tensor): Rotation angles in degrees for each image (torch.Tensor).

    Returns:
        torch.Tensor: Rotated 3D images with the same shape as input.
    """
    N, C, H, W, D = images.shape
    rotated_images = []

    for img, angle in zip(images, angles):
        rotated_slices = []
        for d in range(D):
            # 提取每个深度切片 (C, H, W)
            slice_2d = img[:, :, :, d]  # 形状 (C, H, W)

            # 转换角度为张量，并确保在 GPU 上
            angle_tensor = torch.tensor([angle], dtype=torch.float32, device=images.device)

            # 使用 Kornia 旋转
            rotated_slice = kornia.geometry.transform.rotate(
                slice_2d.unsqueeze(0),  # 增加批量维度 -> (1, C, H, W)
                angle_tensor
            )
            rotated_slices.append(rotated_slice.squeeze(0))  # 移除批量维度

        # 将所有切片重新堆叠为 3D 图像
        rotated_3d = torch.stack(rotated_slices, dim=-1)  # 堆叠成 (C, H, W, D)
        rotated_images.append(rotated_3d)

    return torch.stack(rotated_images)  # 返回批量 3D 图像