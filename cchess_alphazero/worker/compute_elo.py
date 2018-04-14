import os
import sys
import shutil
from time import sleep
from collections import deque
from concurrent.futures import ProcessPoolExecutor, wait
from datetime import datetime
from logging import getLogger
from multiprocessing import Manager
from threading import Thread
from time import time, sleep
from collections import defaultdict
from multiprocessing import Lock
from random import random
import numpy as np
import subprocess

import cchess_alphazero.environment.static_env as senv
from cchess_alphazero.agent.model import CChessModel
from cchess_alphazero.agent.player import CChessPlayer, VisitState
from cchess_alphazero.agent.api import CChessModelAPI
from cchess_alphazero.config import Config
from cchess_alphazero.environment.env import CChessEnv
from cchess_alphazero.environment.lookup_tables import Winner, flip_move, ActionLabelsRed
from cchess_alphazero.lib.data_helper import get_game_data_filenames, write_game_data_to_file
from cchess_alphazero.lib.model_helper import load_model_weight
from cchess_alphazero.lib.tf_util import set_session_config
from cchess_alphazero.lib.web_helper import http_request, download_file
from cchess_alphazero.lib.elo_helper import compute_elo

logger = getLogger(__name__)

def start(config: Config):
    set_session_config(per_process_gpu_memory_fraction=1, allow_growth=True, device_list=config.opts.device_list)
    m = Manager()
    response = http_request(config.internet.get_evaluate_model_url)
    while response['status'] == 0:
        data = response['data']
        logger.info(f"评测开始，基准模型：{data['base']['digest'][0:8]}, elo = {data['base']['elo']};"
                    f"待评测模型：{data['unchecked']['digest'][0:8]}, elo = {data['unchecked']['elo']}")
        # make path
        base_weight_path = os.path.join(config.resource.next_generation_model_dir, data['base']['digest'] + '.h5')
        ng_weight_path = os.path.join(config.resource.next_generation_model_dir, data['unchecked']['digest'] + '.h5')
        # load model
        model_base = load_model(config, base_weight_path, data['base']['digest'])
        model_ng = load_model(config, ng_weight_path, data['unchecked']['digest'])
        # make pipes
        model_base_pipes = m.list([model_base.get_pipes(need_reload=False) for _ in range(config.play.max_processes)])
        model_ng_pipes = m.list([model_ng.get_pipes(need_reload=False) for _ in range(config.play.max_processes)])

        # eval_worker = EvaluateWorker(config, model_base_pipes, model_ng_pipes)
        # eval_worker.start()
        with ProcessPoolExecutor(max_workers=config.play.max_processes) as executor:
            futures = []
            for i in range(config.play.max_processes):
                eval_worker = EvaluateWorker(config, model_base_pipes, model_ng_pipes, pid=i, data=data)
                futures.append(executor.submit(eval_worker.start))
                sleep(1)
        
        wait(futures)
        model_base_pipes.close_pipes()
        model_ng_pipes.close_pipes()

        response = http_request(config.internet.get_evaluate_model_url)
    logger.info(f"没有待评测权重，请稍等或继续跑谱")

class EvaluateWorker:
    def __init__(self, config: Config, pipes1=None, pipes2=None, pid=None, data=None):
        self.config = config
        self.player_bt = None
        self.player_ng = None
        self.pid = pid
        self.pipes_bt = pipes1
        self.pipes_ng = pipes2
        self.data = data

    def start(self):
        logger.debug(f"Evaluate#Start Process index = {self.pid}, pid = {os.getpid()}")
        score1 = 0
        score2 = 0
        results = []

        idx = 0 if random() > 0.5 else 1
        start_time = time()
        value, turns = self.start_game(idx)
        end_time = time()
        
        if (value == 1 and idx == 0) or (value == 0 and idx == 1):
            result = '基准模型胜'
        elif (value == 1 and idx == 1) or (value == 0 and idx == 0):
            result = '带评测模型胜'
        else:
            result = '双方连续60回合未吃子，和棋'

        url = self.config.internet.get_elo_url + self.data['unchecked']['digest']
        response = http_request(url)
        if response['status'] == 0:
            self.data['unchecked']['elo'] = response['data']['elo']

        if value == -1: # loss
            score = 0
        elif value != 1: # draw
            score = 0.5
        else:
            score = 1
        if idx == 0:
            _, new_elo = compute_elo(data['base']['elo'], data['unchecked']['elo'], score)
        else:
            new_elo, _ = compute_elo(data['unchecked']['elo'], data['base']['elo'], 1 - score)

        relative_elo = new_elo - data['unchecked']['elo']
        logger.info(f"进程{self.pid}评测完毕 用时{(end_time - start_time):.1f}秒, "
                     f"{turns / 2}回合, {result}, Elo 增加 {relative_elo} 分")

        data = {'digest': data['unchecked']['digest'], 'relative_elo': relative_elo}
        response = http_request(self.config.internet.update_elo_url, post=True, data=data)
        if response and response['status'] == 0:
            logger.info('评测结果上传成功！')
            return True
        else:
            return False

    def start_game(self, idx):
        pipe1 = self.pipes_bt.pop()
        pipe2 = self.pipes_ng.pop()
        search_tree1 = defaultdict(VisitState)
        search_tree2 = defaultdict(VisitState)

        self.player1 = CChessPlayer(self.config, search_tree=search_tree1, pipes=pipe1, 
                        debugging=False, enable_resign=True)
        self.player2 = CChessPlayer(self.config, search_tree=search_tree2, pipes=pipe2, 
                        debugging=False, enable_resign=True)

        # even: bst = red, ng = black; odd: bst = black, ng = red
        if idx % 2 == 0:
            red = self.player1
            black = self.player2
            logger.info(f"进程id = {self.pid} 基准模型执红，待评测模型执黑")
        else:
            red = self.player2
            black = self.player1
            logger.info(f"进程id = {self.pid} 待评测模型执红，基准模型执黑")

        state = senv.INIT_STATE
        history = [state]
        value = 0       # best model's value
        turns = 0       # even == red; odd == black
        game_over = False
        no_eat_count = 0

        while not game_over:
            start_time = time()
            no_act = None
            if state in history[:-1]:
                no_act = []
                for i in range(len(history) - 1):
                    if history[i] == state:
                        no_act.append(history[i + 1])
            if turns % 2 == 0:
                action, _ = red.action(state, turns, no_act=no_act)
            else:
                action, _ = black.action(state, turns, no_act=no_act)
            end_time = time()
            logger.debug(f"进程id = {self.pid}, action = {action}, turns = {turns}, time = {(end_time-start_time):.1f}")
            if action is None:
                logger.debug(f"{turns % 2} (0 = red; 1 = black) has resigned!")
                value = -1
                break
            history.append(action)
            state, no_eat = senv.new_step(state, action)
            turns += 1
            if no_eat:
                no_eat_count += 1
            history.append(state)

            if no_eat_count >= 120:
                game_over = True
                value = 0
            else:
                game_over, value, final_move = senv.done(state)

        self.player1.close()
        self.player2.close()

        if turns % 2 == 1:  # black turn
            value = -value

        if idx % 2 == 1:   # return player1' value
            value = -value

        self.pipes_bt.append(pipe1)
        self.pipes_ng.append(pipe2)
        return value, turns


def load_model(config, weight_path, digest):
    model = CChessModel(config)
    config_path = config.resource.model_best_config_path
    if (not load_model_weight(model, config_path, weight_path)) or model.digest != digest:
        logger.info(f"开始下载权重 {digest[0:8]}")
        url = self.download_base_url + digest + '.h5'
        download_file(url, weight_path)
        if not load_model_weight(model, config_path, weight_path):
            logger.info(f"待评测权重还未上传，请稍后再试")
            sys.exit()
    logger.info(f"加载权重 {digest[0:8]} 成功")
    return model

