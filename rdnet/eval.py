import torch
from torch import nn
from torch.nn import functional as F

import numpy as np

from einops import rearrange, repeat
from kornia import filters


def compute_errors(depths, preds, masks):
    errors = 0
    cnt = 0

    for depth, est, mask in zip(depths, preds, masks):
        gt = depth[mask]
        pred = est[mask]

        gt *= np.median(pred) / np.median(gt)

        # err = np.log(pred) - np.log(gt)
        # thresh = np.abs(err)
        # d1 = (thresh < np.log(1.25) * 1).mean()
        # d2 = (thresh < np.log(1.25) * 2).mean()
        # d3 = (thresh < np.log(1.25) * 3).mean()

        # sqe = (gt - pred) ** 2
        # rmse = np.sqrt(sqe.mean())
        # abs_rel = np.mean(np.abs(gt - pred) / gt)
        # sq_rel = np.mean(sqe / gt)

        # mse_log = np.mean(err ** 2)
        # rmse_log = np.sqrt(mse_log)
        # silog = np.sqrt(mse_log - np.mean(err) ** 2) * 100
        # log10 = np.mean(thresh / np.log(10))

        thresh = np.maximum((gt / pred), (pred / gt))
        d1 = (thresh < 1.25).mean()
        d2 = (thresh < 1.25 ** 2).mean()
        d3 = (thresh < 1.25 ** 3).mean()

        rms = (gt - pred) ** 2
        rms = np.sqrt(rms.mean())

        log_rms = (np.log(gt) - np.log(pred)) ** 2
        log_rms = np.sqrt(log_rms.mean())

        abs_rel = np.mean(np.abs(gt - pred) / gt)
        sq_rel = np.mean(((gt - pred) ** 2) / gt)

        err = np.log(pred) - np.log(gt)
        silog = np.sqrt(np.mean(err ** 2) - np.mean(err) ** 2) * 100

        err = np.abs(np.log10(pred) - np.log10(gt))
        log10 = np.mean(err)

        errors += np.asarray([silog, abs_rel, log10, rms, sq_rel, log_rms, d1, d2, d3])
        cnt += 1

    return errors / cnt


def compute_ssi(preds, targets, masks, trimmed=1., eps=1e-4):
    masks = rearrange(masks, 'b c h w -> b c (h w)')
    errors = rearrange(torch.abs(preds - targets), 'b c h w -> b c (h w)')
    b, _, n = masks.shape
    valids = masks.sum(2, True)
    invalids = (~masks).sum(2, True)

    errors -= (errors + eps) * (~masks)
    sorted_errors, _ = torch.sort(errors, dim=2)
    assert torch.isnan(sorted_errors).sum() == 0
    idxs = repeat(torch.arange(end=n, device=valids.device),
                  'n -> b c n', b=b, c=1)
    cutoff = (trimmed * valids) + invalids
    trimmed_errors = torch.where((invalids <= idxs) & (
        idxs < cutoff), sorted_errors, sorted_errors - sorted_errors)

    assert torch.isnan(trimmed_errors).sum() == 0
    return (trimmed_errors / valids).sum(dim=2)


def compute_reg(preds, targets, masks, num_scale=4):
    def compute_grad(preds, targets, masks):
        # grads = filters.spatial_gradient(preds - targets)
        # abs_grads = torch.abs(grads[:, :, 0]) + torch.abs(grads[:, :, 1])
        # sum_grads = torch.sum(abs_grads * masks, (2, 3))
        # return sum_grads / masks.sum((2, 3))

        pred = preds[masks]
        target = targets[masks]
        mse = (pred - target) ** 2
        return torch.sqrt(mse.mean())

    total = 0
    step = 1

    for scale in range(num_scale):
        total += compute_grad(preds[:, :, ::step, ::step],
                              targets[:, :, ::step, ::step], masks[:, :, ::step, ::step])
        step *= 2

    return total


def compute_loss(preds, targets, masks, trimmed=1., num_scale=4, alpha=.5, eps=1e-4, **kwargs):
    def align(imgs, masks):
        patches = rearrange(imgs, 'b c h w -> b c (h w)')
        patched_masks = rearrange(masks, 'b c h w -> b c (h w)')
        meds = []

        for img, mask in zip(imgs, masks):
            med = torch.masked_select(img, mask).median(0, True)[0]
            meds.append(med.unsqueeze(1))

        t = repeat(torch.cat(meds), 'b c -> b c d', d=1)
        masked_abs = torch.abs(patches - t) * patched_masks
        assert torch.isnan(masked_abs).sum() == 0

        s = masked_abs.sum(2, True) / patched_masks.sum(2, True) + eps
        try:
            assert 0 not in s
        except:
            print("Masked absolute: ", masked_abs[s[:, :, 0] < eps])
            print("Patches: ", patches[s[:, :, 0] < eps])
            assert False
        assert torch.isnan(s).sum() == 0
        temp = (imgs - t.unsqueeze(3)) / s.unsqueeze(3)
        assert torch.isnan(temp).sum() == 0

        return (imgs - t.unsqueeze(3)) / s.unsqueeze(3)

    assert (preds * preds).sum() > eps
    aligned_preds = align(preds, masks)
    aligned_targets = align(targets, masks)
    assert torch.isnan(aligned_preds).sum() == 0
    assert torch.isnan(aligned_targets).sum() == 0

    # loss = compute_ssi(aligned_preds, aligned_targets, masks, trimmed) / 2
    # assert torch.isnan(loss).sum() == 0
    loss = 0
    if alpha > 0.:
        loss += alpha * compute_reg(aligned_preds, aligned_targets,
                                    masks, num_scale)
    assert torch.isnan(loss).sum() == 0
    return loss.mean()

class silog_loss(nn.Module):
    def __init__(self, variance_focus, num_scale):
        super(silog_loss, self).__init__()
        self.variance_focus = variance_focus
        self.num_scale = num_scale

    def silog(self, depth_est, depth_gt, mask):
        d = torch.log(depth_est[mask]) - torch.log(depth_gt[mask])
        return torch.sqrt((d ** 2).mean() - self.variance_focus * (d.mean() ** 2))

    def forward(self, preds, targets, masks):
        total = 0
        step = 1

        try:
            assert preds[masks].min() > 1e-6
        except:
            print(preds[masks].min())
            assert False

        for scale in range(self.num_scale):
            total += self.silog(preds[:, :, ::step, ::step],
                                targets[:, :, ::step, ::step], masks[:, :, ::step, ::step])
            step *= 2

        return total / self.num_scale