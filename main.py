import torch
import torch.nn as nn
import torch.optim as optim

import os
import sys
import random
import time
import datetime
import logging
from pprint import pprint
import json
import pickle
from pathlib import Path
from typing import List, Tuple, Set, Dict  # noqa

import numpy as np
from sklearn import metrics
import seaborn as sns
import matplotlib.pyplot as plt

from src.utils import sAsMinutes, timeSince
from src.config import get_option_fallback, Config
from src.save import save_model, save_log, save_hm_fig, save_learning_curve
from src.slack import slack_message
from src.logging import get_logger
from knowledge_tracing.trainer import Trainer


logger = get_logger(__name__, 'tmp.log')


def seed_everything(seed: int=42):
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True


def main(configpath: Path):
    with open(configpath, 'r') as f:
        cfg = json.load(f)
    with open(configpath.parent / 'fallback.json', 'r') as f:
        default_cfg = json.load(f)
    default_cfg['config_name'] = configpath.stem
    projectdir = Path(os.path.dirname(os.path.realpath(__file__)))
    experiments = cfg['experiments']
    assert len(experiments) == len(
        set([e['exp_name'] for e in experiments])), 'exp_name has duplicate.'
    cmn_dict = cfg.get('common', dict())
    cmn_dict = get_option_fallback(cmn_dict, fallback=default_cfg)
    for exp_dict in experiments:
        config_dict = get_option_fallback(exp_dict, fallback=cmn_dict)
        config = Config(config_dict, projectdir=projectdir)
        logger.info(
            '\nStarting Experiment: {}\n--- * --- * ---'.format(config.exp_name))

        run(config)
    logger.info('All experiments done!')
    slack_message('All experiments done for {}'.format(configpath.stem))


def run(config):
    seed_everything()

    trainer = Trainer(config)
    if not config.load_model:
        try:
            trainer.pre_train_model()
            trainer.train_model()
        except KeyboardInterrupt as e:
            print(e)
        finally:
            trainer.dump_report()

    trainer.evaluate_model()
    # trainer.evaluate_model_heatmap()


if __name__ == '__main__':
    config = sys.argv[1]
    config = Path(config)
    assert config.exists(), config
    main(config)
