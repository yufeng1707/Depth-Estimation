# Copyright (C) 2019 Jin Han Lee
#
# This file is a part of BTS.
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>

import time
import argparse
import datetime
import sys
import os

import torch
import torch.nn as nn
import torch.nn.utils as utils
import numpy as np

import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.multiprocessing as mp
from torchvision import transforms

from tensorboardX import SummaryWriter

import matplotlib
import matplotlib.cm
import threading
from tqdm import tqdm

from model import RDNet
from eval import compute_errors, compute_loss, silog_loss
from dataloader import Loader
from args import Arg_train


DEVICE = torch.device('cuda')

args = Arg_train()

inv_normalize = transforms.Normalize(
    mean=[-0.485/0.229, -0.456/0.224, -0.406/0.225],
    std=[1/0.229, 1/0.224, 1/0.225]
)
silog_criterion = silog_loss(variance_focus=args.variance_focus, num_scale=args.num_scale)

num_metrics = 10
low_num = 7
eval_metrics = ['loss', 'silog', 'abs_rel', 'log10',
                'rms', 'sq_rel', 'log_rms', 'd1', 'd2', 'd3']


def block_print():
    sys.stdout = open(os.devnull, 'w')


def enable_print():
    sys.stdout = sys.__stdout__


def get_num_lines(file_path):
    f = open(file_path, 'r')
    lines = f.readlines()
    f.close()
    return len(lines)


def colorize(value, vmin=None, vmax=None, cmap='Greys'):
    value = value.cpu().numpy()[:, :, :]
    value = np.log10(value)

    vmin = value.min() if vmin is None else vmin
    vmax = value.max() if vmax is None else vmax

    if vmin != vmax:
        value = (value - vmin) / (vmax - vmin)
    else:
        value = value*0.

    cmapper = matplotlib.cm.get_cmap(cmap)
    value = cmapper(value, bytes=True)

    img = value[:, :, :3]

    return img.transpose((2, 0, 1))


def normalize_result(value, vmin=None, vmax=None):
    value = value.cpu().numpy()[0, :, :]

    vmin = value.min() if vmin is None else vmin
    vmax = value.max() if vmax is None else vmax

    if vmin != vmax:
        value = (value - vmin) / (vmax - vmin)
    else:
        value = value * 0.

    return np.expand_dims(value, 0)

def standardize(depth_map):
    depth_map[depth_map < args.min_depth_eval] = args.min_depth_eval
    depth_map[depth_map > args.max_depth_eval] = args.max_depth_eval
    depth_map[np.isinf(depth_map)] = args.max_depth_eval
    depth_map[np.isnan(depth_map)] = args.min_depth_eval
    return depth_map

def online_eval(model, dataloader_eval, gpu, ngpus):
    eval_measures = np.zeros(num_metrics + 1)
    for _, eval_sample_batched in enumerate(tqdm(dataloader_eval.data)):
        with torch.no_grad():
            image = eval_sample_batched['image'].to(DEVICE)
            gt_depth = eval_sample_batched['depth'].to(DEVICE)
            embedding = eval_sample_batched['embedding'].to(DEVICE)
            location = eval_sample_batched['bbox'].to(DEVICE)
            mask = eval_sample_batched['mask'].to(DEVICE)

            disp_est = model(image, embedding, location).detach()
            disp_gt = 1. / gt_depth
            # loss = compute_loss(pred_depth, gt_depth, mask, eps=args.eps,
            #                     trimmed=args.trimmed, num_scale=args.num_scale, alpha=args.alpha)
            loss = silog_criterion(disp_est, disp_gt, mask).cpu().numpy()
            gt_depth = gt_depth.cpu().numpy()
            depth_est = 1. / disp_est.cpu().numpy()
            pred_depth = standardize(depth_est)

        valid_mask = np.logical_and(
            gt_depth > args.min_depth_eval, gt_depth < args.max_depth_eval)
        
        if args.eigen_crop or args.garg_crop:
            b, _, gt_height, gt_width = gt_depth.shape
            eval_mask = np.zeros(valid_mask.shape)
            '''
            if args.garg_crop:
                eval_mask[int(0.40810811 * gt_height):int(0.99189189 * gt_height),
                          int(0.03594771 * gt_width):int(0.96405229 * gt_width)] = 1
            elif args.eigen_crop:
            '''
            eval_mask[:, :, 45:471, 41:601] = 1

            valid_mask = np.logical_and(valid_mask, eval_mask)

        measures = compute_errors(gt_depth, pred_depth, valid_mask)
        try:
            eval_measures[:-1] += np.insert(measures, 0, loss, axis=0)
        except:
            print(measures.shape)
            assert False
        eval_measures[-1] += 1

    cnt = int(eval_measures[-1])
    eval_measures /= cnt
    print('Computing errors for {} eval samples'.format(cnt))
    print("{:>7}, {:>7}, {:>7}, {:>7}, {:>7}, {:>7}, {:>7}, {:>7}, {:>7}, {:>7}".format(
        'loss', 'silog', 'abs_rel', 'log10', 'rms', 'sq_rel', 'log_rms', 'd1', 'd2', 'd3'))

    for i in range(num_metrics - 1):
        print('{:7.3f}, '.format(eval_measures[i]), end='')
    print('{:7.3f}'.format(eval_measures[-2]))

    return eval_measures


def main_worker(gpu, ngpus_per_node, args):
    args.gpu = gpu

    if args.gpu is not None:
        print("Use GPU: {} for training".format(args.gpu))

    # Create model
    model = RDNet(image_size=args.image_size,
                  patch_size=args.patch_size,
                  knowledge_dims=args.knowledge_dims,
                  dense_dims=args.dense_dims,
                  latent_dim=args.latent_dims,
                  data_path=args.data_path,
                  emb_size=args.emb_size,
                  use_readout=args.use_readout,
                  hooks=args.hooks,
                  activation=args.activation,
                  landmarks=args.landmarks,
                  scale=args.scale,
                  shift=args.shift,
                  invert=args.invert,
                  transformer=args.transformer)
    model.train()
    num_params = sum([np.prod(p.size()) for p in model.parameters()])
    print("Total number of parameters: {}".format(num_params))

    num_params_update = sum([np.prod(p.shape)
                             for p in model.parameters() if p.requires_grad])
    print("Total number of learning parameters: {}".format(num_params_update))

    model = torch.nn.DataParallel(model)
    model.to(DEVICE)

    print("Model Initialized")

    global_step = 0
    best_eval_measures_lower_better = torch.zeros(low_num).cpu() + 1/args.eps
    best_eval_measures_higher_better = torch.zeros(num_metrics - low_num).cpu()
    best_eval_steps = np.zeros(num_metrics, dtype=np.int32)

    model_just_loaded = False
    if args.checkpoint_path != '':
        if os.path.isfile(args.checkpoint_path):
            print("Loading checkpoint '{}'".format(args.checkpoint_path))
            if args.gpu is None:
                checkpoint = torch.load(args.checkpoint_path)
            else:
                loc = 'cuda:{}'.format(args.gpu)
                checkpoint = torch.load(args.checkpoint_path, map_location=loc)
            global_step = checkpoint['global_step']
            model.load_state_dict(checkpoint['model'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            try:
                best_eval_measures_higher_better = checkpoint['best_eval_measures_higher_better'].cpu(
                )
                best_eval_measures_lower_better = checkpoint['best_eval_measures_lower_better'].cpu(
                )
                best_eval_steps = checkpoint['best_eval_steps']
            except KeyError:
                print("Could not load values for online evaluation")

            print("Loaded checkpoint '{}' (global_step {})".format(
                args.checkpoint_path, checkpoint['global_step']))
        else:
            print("No checkpoint found at '{}'".format(args.checkpoint_path))
        model_just_loaded = True

    if args.retrain:
        global_step = 0

    cudnn.benchmark = True

    dataloader = Loader(args, 'train')
    dataloader_eval = Loader(args, 'online_eval')

    # Logging
    writer = SummaryWriter(args.log_directory + '/' +
                           args.model_name + '/summaries', flush_secs=30)
    if args.do_online_eval:
        if args.eval_summary_directory != '':
            eval_summary_path = os.path.join(
                args.eval_summary_directory, args.model_name)
        else:
            eval_summary_path = os.path.join(args.log_directory, 'eval')
        eval_summary_writer = SummaryWriter(eval_summary_path, flush_secs=30)

    start_time = time.time()
    duration = 0

    num_log_images = args.batch_size
    end_learning_rate = args.end_learning_rate if args.end_learning_rate != - \
        1 else 0.1 * args.learning_rate

    var_sum = [var.sum() for var in model.parameters() if var.requires_grad]
    var_cnt = len(var_sum)
    var_sum = np.sum(var_sum)

    print("Initial variables' sum: {:.3f}, avg: {:.3f}".format(
        var_sum, var_sum/var_cnt))

    steps_per_epoch = len(dataloader.data)
    num_total_steps = args.num_epochs * steps_per_epoch
    epoch = global_step // steps_per_epoch

    # Training parameters
    if args.optim == 'adam':
        optimizer = torch.optim.AdamW(params=model.parameters(), lr=args.learning_rate,
                                      eps=args.adam_eps, weight_decay=args.weight_decay)
    elif args.optim == 'sgd':
        optimizer = torch.optim.SGD(params=model.parameters(), lr=args.learning_rate, momentum=.9)
    
    if args.schedule == 'cycle':
        scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, max_lr=args.learning_rate, total_steps=num_total_steps)
    elif args.schedule == 'plateau':
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=args.patience, # threshold_mode='abs',
                                                               threshold=args.thresh, verbose=True)

    while epoch < args.num_epochs:
        print(epoch, '/', args.num_epochs)
        for step, sample_batched in enumerate(dataloader.data):
            optimizer.zero_grad()
            before_op_time = time.time()

            image = sample_batched['image'].to(DEVICE)
            depth_gt = sample_batched['depth'].to(DEVICE)
            embedding = sample_batched['embedding'].to(DEVICE)
            location = sample_batched['bbox'].to(DEVICE)
            mask = sample_batched['mask'].to(DEVICE)

            disp_est = model(image, embedding, location)
            disp_gt = 1. / depth_gt

            # computeloss
            # loss = compute_loss(depth_est, depth_gt, mask, eps=args.eps,
            #                     trimmed=args.trimmed, num_scale=args.num_scale, alpha=args.alpha)
            # assert depth_est.min() > 0
            loss = silog_criterion(disp_est, disp_gt, mask)

            assert 0 not in loss
            loss.backward()

            # for param_group in optimizer.param_groups:
            #     current_lr = (args.learning_rate - end_learning_rate) * \
            #         (1 - global_step / num_total_steps) ** 0.9 + end_learning_rate
            #     param_group['lr'] = current_lr

            optimizer.step()
            if args.schedule == 'cycle':
                scheduler.step()

            print('[epoch][s/s_per_e/gs]: [{}][{}/{}/{}], loss: {:.12f}'.format(
                epoch, step, steps_per_epoch, global_step, loss))
            # print('Current lr: {:.12f}, {:.12f}'.format(current_lr, args.learning_rate))
            if np.isnan(loss.cpu().item()):
                print('NaN in loss occurred. Aborting training.')
                return -1

            duration += time.time() - before_op_time
            if global_step and global_step % args.log_freq == 0 and not model_just_loaded:
                var_sum = [var.sum()
                           for var in model.parameters() if var.requires_grad]
                var_cnt = len(var_sum)
                var_sum = np.sum(var_sum)
                examples_per_sec = args.batch_size / duration * args.log_freq
                duration = 0
                time_sofar = (time.time() - start_time) / 3600
                training_time_left = (
                    num_total_steps / global_step - 1.0) * time_sofar

                print_string = 'GPU: {} | examples/s: {:4.2f} | loss: {:.5f} | var sum: {:.3f} avg: {:.3f} | time elapsed: {:.2f}h | time left: {:.2f}h'
                print(print_string.format(args.gpu, examples_per_sec, loss, var_sum.item(
                ), var_sum.item()/var_cnt, time_sofar, training_time_left))

                writer.add_scalar('loss', loss, global_step)
                writer.add_scalar(
                    'var average', var_sum.item()/var_cnt, global_step)
                depth_gt = torch.where(
                    depth_gt < 1e-3, depth_gt * 0 + 1e3, depth_gt)
                # for i in range(num_log_images):
                #     writer.add_image(
                #         'depth_gt/image/{}'.format(i), normalize_result(1/depth_gt[i, :, :, :].data), global_step)
                #     writer.add_image(
                #         'depth_est/image/{}'.format(i), normalize_result(1/depth_est[i, :, :, :].data), global_step)
                #     writer.add_image(
                #         'image/image/{}'.format(i), inv_normalize(image[i, :, :, :]).data, global_step)
                writer.flush()

            if not args.do_online_eval and global_step and global_step % args.save_freq == 0:
                checkpoint = {'global_step': global_step,
                              'model': model.state_dict(),
                              'optimizer': optimizer.state_dict()}
                torch.save(checkpoint, args.log_directory + '/' +
                           args.model_name + '/model-{}'.format(global_step))

            if args.do_online_eval and global_step and global_step % args.eval_freq == 0 and not model_just_loaded:
                model.eval()
                eval_measures = online_eval(
                    model, dataloader_eval, gpu, ngpus_per_node)

                if eval_measures is not None:
                    if args.schedule == 'plateau':
                        scheduler.step(eval_measures[0])
                    
                    for i in range(len(eval_metrics)):
                        eval_summary_writer.add_scalar(
                            eval_metrics[i], int(global_step))
                        measure = eval_measures[i]
                        is_best = False

                        if i < low_num and measure < best_eval_measures_lower_better[i]:
                            old_best = best_eval_measures_lower_better[i].item(
                            )
                            best_eval_measures_lower_better[i] = measure.item()
                            is_best = True
                        elif i >= low_num and measure > best_eval_measures_higher_better[i - low_num]:
                            old_best = best_eval_measures_higher_better[i - low_num].item(
                            )
                            best_eval_measures_higher_better[i - low_num] = measure.item()
                            is_best = True
                        if is_best:
                            old_best_step = best_eval_steps[i]
                            old_best_name = '/model-{}-best_{}_{:.5f}'.format(
                                old_best_step, eval_metrics[i], old_best)
                            model_path = args.log_directory + '/' + args.model_name + old_best_name
                            if os.path.exists(model_path):
                                command = 'rm {}'.format(model_path)
                                os.system(command)
                            best_eval_steps[i] = global_step
                            model_save_name = '/model-{}-best_{}_{:.5f}'.format(
                                global_step, eval_metrics[i], measure)
                            print('New best for {}. Saving model: {}'.format(
                                eval_metrics[i], model_save_name))
                            checkpoint = {'global_step': global_step,
                                          'model': model.state_dict(),
                                          'optimizer': optimizer.state_dict(),
                                          'best_eval_measures_higher_better': best_eval_measures_higher_better,
                                          'best_eval_measures_lower_better': best_eval_measures_lower_better,
                                          'best_eval_steps': best_eval_steps
                                          }
                            torch.save(checkpoint, args.log_directory +
                                       '/' + args.model_name + model_save_name)
                    eval_summary_writer.flush()
                model.train()
                block_print()
                enable_print()

            model_just_loaded = False
            global_step += 1

        epoch += 1


def main():
    if args.mode != 'train':
        print('bts_main.py is only for training. Use bts_test.py instead.')
        return -1

    model_filename = args.model_name + '.py'
    command = 'mkdir ' + args.log_directory + '/' + args.model_name
    if not os.path.exists(args.log_directory + '/' + args.model_name):
        os.system(command)

    args_out_path = args.log_directory + '/' + \
        args.model_name + '/' + sys.argv[0]
    command = 'cp ' + sys.argv[0] + ' ' + args_out_path
    os.system(command)

    torch.cuda.empty_cache()

    ngpus_per_node = torch.cuda.device_count()

    if args.do_online_eval:
        print("You have specified --do_online_eval.")
        print("This will evaluate the model every eval_freq {} steps and save best models for individual eval metrics."
              .format(args.eval_freq))

    main_worker(args.gpu, ngpus_per_node, args)


if __name__ == '__main__':
    main()
