#  -*- coding: UTF-8 -*-

from enum import Enum


class DefaultParameter(Enum):
    # 手动操作等待时长（秒）
    MANUAL_ACTION_TIMEOUT = 900
    # 等待停止单个控制器的时长
    SHUTDOWN_CONTROLLER_TIMEOUT = 900
    # Http自动重连时长
    HTTP_RECONNECT_TIMEOUT = 900
    # 预处理阶段对应单主机负载并发（线程）数
    THREAD_N1 = 32
    # 多AI训练总并行任务（线程）数
    THREAD_N2 = 32
