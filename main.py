import torch
import torch.nn as nn
import torch.optim as optim

import os
import random
import time
import datetime
import logging
from pprint import pprint
import configparser
import json
import pickle
from pathlib import Path
from math import log, ceil
from typing import List, Tuple, Set, Dict  # noqa

import click
import numpy as np
from sklearn import metrics
import seaborn as sns
import matplotlib.pyplot as plt

from src.data import prepare_data, prepare_heatmap_data, SOURCE_ASSIST0910_SELF, SOURCE_ASSIST0910_ORIG
from src.utils import sAsMinutes, timeSince
from src.config import get_option_fallback, Config
from model.eddkt import EncDecDKT, get_loss_batch_encdec
from model.basedkt import BaseDKT, get_loss_batch_basedkt
from model.seq2seq import get_Seq2Seq, get_loss_batch_seq2seq
from knowledge_tracing.trainer import Trainer


def save_model(config, model, auc, epoch):
    checkpointsdir = config.resultsdir / 'checkpoints'
    checkpointsdir.mkdir(exist_ok=True)
    torch.save(model.state_dict(), checkpointsdir /
               f'{config.model_name}_auc{auc:.4f}_e{epoch}.model')


def save_log(config, data, auc, epoch):
    lc_datadir = config.resultsdir / 'lc_data'
    lc_datadir.mkdir(exist_ok=True)
    with open(lc_datadir / f'{config.model_name}_auc{auc:.4f}_e{epoch}.pickle', 'wb') as f:
        pickle.dump(data, f)


def save_hm_fig(config, sns_fig):
    hmdir = config.resultsdir / 'heatmap'
    hmdir.mkdir(exist_ok=True)
    sns_fig.savefig(hmdir / f'{config.model_name}.png')


def save_learning_curve(x, train_loss_list, train_auc_list, eval_loss_list, eval_auc_list, config):
    fig = plt.figure()
    ax = fig.add_subplot(111)
    if train_loss_list:
        ax.plot(x, train_loss_list, label='train loss')
    if train_auc_list:
        ax.plot(x, train_auc_list, label='train auc')
    if eval_loss_list:
        ax.plot(x, eval_loss_list, label='eval loss')
    ax.plot(x, eval_auc_list, label='eval auc')
    ax.legend()
    print(len(train_loss_list), len(eval_loss_list), len(eval_auc_list))
    lcdir = config.resultsdir / 'learning_curve'
    plt.savefig(lcdir / f'{config.model_name}.png')


@click.command()
@click.option('--config', '-c', default='')
def main(config):
    if not config:
        print('Other options are depricated. Please use --config.')
        return
    cp = configparser.ConfigParser()
    cp.read(config)
    section_list = cp.sections()
    pprint(section_list)
    common_opt = dict(cp['common']) if 'common' in section_list else dict()
    report_list = list()
    for section in section_list:
        if section == 'common':
            continue
        section_opt = dict(cp[section])
        default_dict = {
            'common_name': '',
            'section_name': common_opt.get('common_name', '') + section,

            'debug': False,
            'model_name': str,
            'load_model': '',
            'plot_heatmap': False,
            'plot_lc': False,
            'source_data': SOURCE_ASSIST0910_ORIG,
            'ks_loss': False,
            'extend_backward': 0,
            'extend_forward': 0,
            'epoch_size': 200,
            'sequence_size': 20,
            'lr': 0.05,
            'n_skills': 124,
            'cuda': True,

            'batch_size': 100,
        }
        config_dict = get_option_fallback(
            {**common_opt, **section_opt}, fallback=default_dict)
        projectdir = Path(os.path.dirname(os.path.realpath(__file__)))
        config = Config(config_dict, projectdir)
        pprint(config.as_dict())

        report = run(config)
        report_list.append(report)
    print(report)
    with open(projectdir / 'output' / 'reports' / '{}result.json'.format(config._get_stem_name()), 'w') as f:
        json.dump(report_list, f)


def run(config):
    assert config.model_name in {'encdec', 'basernn', 'baselstm', 'seq2seq'}
    report = dict()
    # =========================
    # Outfile name
    # =========================
    model_fname = config.model_name
    model_fname += f'eb{config.extend_backward}' if config.extend_backward else ''
    model_fname += f'ef{config.extend_forward}' if config.extend_forward else ''
    model_fname += f'ks' if config.ks_loss else ''
    report['model_fname'] = model_fname

    # =========================
    # Seed
    # =========================
    SEED = 0
    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    # torch.backends.cudnn.deterministic = True
    # torch.backends.cudnn.benchmark = False

    # =========================
    # Version, Device
    # =========================
    print('PyTorch:', torch.__version__)
    dev = torch.device(
        'cuda' if config.cuda and torch.cuda.is_available() else 'cpu')
    print('Using Device:', dev)

    # =========================
    # Logging
    # =========================
    logging.basicConfig()
    logger = logging.getLogger(config.model_name)
    logger.setLevel(logging.INFO)

    # =========================
    # Parameters
    # =========================
    batch_size = config.batch_size
    n_hidden, n_skills, n_layers = 200, config.n_skills, 2
    n_output = n_skills
    PRESERVED_TOKENS = 2  # PAD, SOS
    onehot_size = 2 * n_skills + PRESERVED_TOKENS
    n_input = ceil(log(2 * n_skills))

    INPUT_DIM, ENC_EMB_DIM, ENC_DROPOUT = onehot_size, n_input, 0.6
    OUTPUT_DIM, DEC_EMB_DIM, DEC_DROPOUT = onehot_size, n_input, 0.6
    HID_DIM, N_LAYERS = n_hidden, n_layers
    N_SKILLS = n_skills
    # OUTPUT_DIM = n_output = 124  # TODO: ほんとはこれやりたい

    # =========================
    # Prepare models, LossBatch, and Data
    # =========================
    if config.model_name == 'encdec':
        model = EncDecDKT(
            INPUT_DIM, ENC_EMB_DIM, HID_DIM, N_LAYERS, ENC_DROPOUT,
            OUTPUT_DIM, DEC_EMB_DIM, HID_DIM, N_LAYERS, DEC_DROPOUT,
            N_SKILLS,
            dev).to(dev)
        loss_batch = get_loss_batch_encdec(
            config.extend_forward, ks_loss=config.ks_loss)
        train_dl, eval_dl = prepare_data(
            config.source_data, 'encdec', n_skills, PRESERVED_TOKENS,
            min_n=3, max_n=config.sequence_size, batch_size=batch_size, device=dev, sliding_window=0,
            params={'extend_backward': config.extend_backward, 'extend_forward': config.extend_forward})
    elif config.model_name == 'seq2seq':
        model = get_Seq2Seq(
            onehot_size, ENC_EMB_DIM, HID_DIM, N_LAYERS, ENC_DROPOUT,
            OUTPUT_DIM, DEC_EMB_DIM, DEC_DROPOUT, dev)
        loss_batch = get_loss_batch_seq2seq(
            config.extend_forward, ks_loss=config.ks_loss)
        train_dl, eval_dl = prepare_data(
            config.source_data, 'encdec', n_skills, PRESERVED_TOKENS,
            min_n=3, max_n=config.sequence_size, batch_size=batch_size, device=dev, sliding_window=0,
            params={'extend_backward': config.extend_backward, 'extend_forward': config.extend_forward})

    elif config.model_name == 'basernn':
        model = BaseDKT(
            dev, config.model_name, n_input, n_hidden, n_output, n_layers, batch_size
        ).to(dev)
        loss_batch = get_loss_batch_basedkt(
            onehot_size, n_input, batch_size, config.sequence_size, dev)
        train_dl, eval_dl = prepare_data(
            config.source_data, 'base', n_skills, preserved_tokens='?',
            min_n=3, max_n=config.sequence_size, batch_size=batch_size, device=dev, sliding_window=0)
    elif config.model_name == 'baselstm':
        model = BaseDKT(
            dev, config.model_name, n_input, n_hidden, n_output, n_layers, batch_size
        ).to(dev)
        loss_batch = get_loss_batch_basedkt(
            onehot_size, n_input, batch_size, config.sequence_size, dev)
        train_dl, eval_dl = prepare_data(
            config.source_data, 'base', n_skills, preserved_tokens='?',
            min_n=3, max_n=config.sequence_size, batch_size=batch_size, device=dev, sliding_window=0)
    else:
        raise ValueError(f'model_name {config.model_name} is wrong')
    logger.info('train_dl.dataset size: {}'.format(len(train_dl.dataset)))
    logger.info('eval_dl.dataset size: {}'.format(len(eval_dl.dataset)))

    print(model)

    # ========================
    # Load trained model
    # ========================
    if config.load_model:
        model.load_state_dict(torch.load(config.load_model))
        model = model.to(dev)
    else:
        # -------------------------
        def count_parameters(model):
            return sum(p.numel() for p in model.parameters() if p.requires_grad)
        logger.info(
            f'The model has {count_parameters(model):,} trainable parameters')

        loss = nn.BCELoss()
        opt = optim.SGD(model.parameters(), lr=config.lr)

        trainer = Trainer(config, model, loss_batch, logger, loss, opt, train_dl)
        trainer.train_model()

    #         # =====
    #         # EVAL
    #         # =====
    #         if print_eval:
    #             with torch.no_grad():
    #                 model.eval()

    #                 # ------------------ eval -----------------
    #                 val_pred = []
    #                 val_actual = []
    #                 current_eval_loss = []
    #                 for args in eval_dl:
    #                     loss_item, batch_n, pred, actu_q, actu, pred_ks, _, _ = loss_batch(
    #                         model, loss_func, *args, opt=None)
    #                     current_eval_loss.append(loss_item)
    #                     val_pred.append(pred)
    #                     val_actual.append(actu)

    #                     # stop at first batch if debug
    #                     if config.debug:
    #                         break

    #                 loss = np.array(current_eval_loss)
    #                 if epoch % 100 == 0:
    #                     logger.log(logging.INFO,
    #                                'EVAL  Epoch: {} Loss: {}'.format(epoch,  loss.mean()))
    #                 eval_loss_list.append(loss.mean())

    #                 # AUC, Recall, F1
    #                 if print_auc:
    #                     # TODO: viewしない？　最後の1個で？
    #                     y = torch.cat(val_actual).view(-1).cpu()
    #                     pred = torch.cat(val_pred).view(-1).cpu()
    #                     # AUC
    #                     fpr, tpr, thresholds = metrics.roc_curve(
    #                         y, pred, pos_label=1)
    #                     if epoch % 100 == 0:
    #                         logger.log(logging.INFO,
    #                                    'EVAL  Epoch: {} AUC: {}'.format(epoch, metrics.auc(fpr, tpr)))
    #                     auc = metrics.auc(fpr, tpr)
    #                     eval_auc_list.append(auc)
    #                     if epoch % 100 == 0:
    #                         save_model(config, model, auc,epoch)
    #                         save_log(
    #                             config, 
    #                             (x, train_loss_list, train_auc_list,
    #                              eval_loss_list, eval_auc_list),
    #                             auc, epoch
    #                         )
    #                         if auc > bset_eval_auc:
    #                             bset_eval_auc = auc
    #                             report['best_eval_auc'] = bset_eval_auc
    #                             report['best_eval_auc_epoch'] = epoch


    #         if epoch % 10 == 0:
    #             x.append(epoch)
    #             if epoch % 100 == 0:
    #                 logger.log(logging.INFO,
    #                            f'{timeSince(start_time, epoch / config.epoch_size)} ({epoch} {epoch / config.epoch_size * 100})')

    #     if config.plot_lc:
    #         fname = model_fname
    #         save_learning_curve(x, train_loss_list, train_auc_list,
    #                             eval_loss_list, eval_auc_list, config)

    # # model is trained or loaded now.

    # if config.plot_heatmap:
    #     batch_size = 1
    #     # TODO: don't repeat yourself
    #     if config.model_name == 'encdec':
    #         model = EncDecDKT(
    #             INPUT_DIM, ENC_EMB_DIM, HID_DIM, N_LAYERS, ENC_DROPOUT,
    #             OUTPUT_DIM, DEC_EMB_DIM, HID_DIM, N_LAYERS, DEC_DROPOUT,
    #             N_SKILLS,
    #             dev).to(dev)
    #         loss_batch = get_loss_batch_encdec(
    #             config.extend_forward, ks_loss=config.ks_loss)
    #     elif config.model_name == 'seq2seq':
    #         model = get_Seq2Seq(
    #             onehot_size, ENC_EMB_DIM, HID_DIM, N_LAYERS, ENC_DROPOUT,
    #             OUTPUT_DIM, DEC_EMB_DIM, DEC_DROPOUT, dev)
    #         loss_batch = get_loss_batch_seq2seq(
    #             config.extend_forward, ks_loss=config.ks_loss)
    #     elif config.model_name == 'basernn':
    #         model = BaseDKT(
    #             dev, config.model_name, n_input, n_hidden, n_output, n_layers, batch_size
    #         ).to(dev)
    #         loss_batch = get_loss_batch_basedkt(
    #             onehot_size, n_input, batch_size, config.sequence_size, dev)
    #     elif config.model_name == 'baselstm':
    #         model = BaseDKT(
    #             dev, config.model_name, n_input, n_hidden, n_output, n_layers, batch_size
    #         ).to(dev)
    #         loss_batch = get_loss_batch_basedkt(
    #             onehot_size, n_input, batch_size, config.sequence_size, dev)
    #     else:
    #         raise ValueError(f'model_name {config.model_name} is wrong')
    #     if config.load_model:
    #         model.load_state_dict(torch.load(config.load_model))
    #         model = model.to(dev)
    #     heat_dl = prepare_heatmap_data(
    #         config.source_data, config.model_name, n_skills, PRESERVED_TOKENS,
    #         min_n=3, max_n=config.sequence_size, batch_size=batch_size, device=dev, sliding_window=0,
    #         params={'extend_backward': config.extend_backward, 'extend_forward': config.extend_forward})
    #     loss_func = nn.BCELoss()
    #     opt = optim.SGD(model.parameters(), lr=config.lr)

    #     debug = False
    #     logging.basicConfig()
    #     logger = logging.getLogger('dkt log')
    #     logger.setLevel(logging.INFO)
    #     train_loss_list = []
    #     train_auc_list = []
    #     eval_loss_list = []
    #     eval_auc_list = []
    #     eval_recall_list = []
    #     eval_f1_list = []
    #     x = []

    #     with torch.no_grad():
    #         model.eval()
    #         # =====
    #         # HEATMAP
    #         # =====
    #         all_out_prob = []
    #         # ------------------ heatmap (eval) -----------------
    #         val_pred = []
    #         val_actual = []
    #         current_eval_loss = []
    #         yticklabels = set()
    #         xticklabels = []
    #         for args in heat_dl:
    #             loss_item, batch_n, pred, actu_q, actu, pred_ks, _, _ = loss_batch(
    #                 model, loss_func, *args, opt=None)
    #             # current_eval_loss.append(loss_item[-1])
    #             # print(pred.shape, actu.shape)
    #             # val_pred.append(pred[-1])
    #             # val_actual.append(actu[-1])
    #             yq = torch.max(actu_q.squeeze(), 0)[1].item()
    #             ya = int(actu.item())
    #             yticklabels.add(yq)
    #             xticklabels.append((yq, ya))

    #             # print(pred_ks.shape)
    #             assert len(pred_ks.shape) == 1, 'pred_ks dimention {}, expected 1'.format(
    #                 pred_ks.shape)
    #             assert pred_ks.shape[0] == n_skills
    #             all_out_prob.append(pred_ks.unsqueeze(0))


    #     _d = torch.cat(all_out_prob).transpose(0, 1)
    #     _d = _d.cpu().numpy()
    #     print(_d.shape)
    #     print(len(yticklabels), len(xticklabels))
    #     yticklabels = sorted(list(yticklabels))
    #     related_d = np.matrix([_d[x, :] for x in yticklabels])

    #     # Regular Heatmap
    #     # fig, ax = plt.subplots(figsize=(20, 10))
    #     # sns.heatmap(_d, ax=ax)

    #     fig, ax = plt.subplots(figsize=(20, 7))
    #     sns.heatmap(
    #         related_d, vmin=0, vmax=1, ax=ax,
    #         # cmap="Reds_r",
    #         xticklabels=['{}'.format(y) for y in xticklabels],
    #         yticklabels=['s{}'.format(x) for x in yticklabels],
    #     )
    #     xtick_dic = {s: i for i, s in enumerate(yticklabels)}
    #     # 正解
    #     sca_x = [t + 0.5 for t, qa in enumerate(xticklabels) if qa[1] == 1]
    #     sca_y = [xtick_dic[qa[0]] + 0.5 for t,
    #              qa in enumerate(xticklabels) if qa[1] == 1]
    #     ax.scatter(sca_x, sca_y, marker='o', s=100, color='white')
    #     # 不正解
    #     sca_x = [t + 0.5 for t, qa in enumerate(xticklabels) if qa[1] == 0]
    #     sca_y = [xtick_dic[qa[0]] + 0.5 for t,
    #              qa in enumerate(xticklabels) if qa[1] == 0]
    #     ax.scatter(sca_x, sca_y, marker='X', s=100, color='black')

    #     save_hm_fig(config, fig)

    # return report


if __name__ == '__main__':
    main()
