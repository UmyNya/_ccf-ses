# -*- coding: UTF-8 -*-
from datetime import datetime
from typing import List

import pandas as pd
import time
from storage_evaluation_system_zzj import util, constants
from storage_evaluation_system_zzj.action.storage import StorageAction
from storage_evaluation_system_zzj.action.vdbench import VdbenchIO
from storage_evaluation_system_zzj.basecase import BaseCase, add_record
from storage_evaluation_system_zzj.parameter import DefaultParameter
from storage_evaluation_system_zzj.indicator import Continuity, DataHitBottomDuration


class DFXBase(BaseCase):
    """故障
    主流程：
    1. 执行IO 5min
    2.【执行故障】
    3. 继续执行IO 5min
    4.【恢复故障】
    5. 提示确认系统已恢复正常

    """

    record_b4_dfx = "record_b4_dfx"
    record_dfx = "record_dfx"

    @property
    def fault_name(self):
        raise NotImplementedError

    def get_vdbench_elapsed(self) -> int:
        """获取默认的vdbench执行时长"""
        return (
                constants.STABLE_TIME
                + constants.FAULT_ELAPSED
                + DefaultParameter.MANUAL_ACTION_TIMEOUT.value
                + constants.FAULT_RECOVER_TIMEOUT
                + 60
        )

    def pre_condition(self):
        self.vdbench: VdbenchIO = None
        self.timeline: List[float] = []
        self.vdbench_terminated_time: datetime = None
        self.zero_info = None
        self.op_time = []
        self.record_time = []
        self._record_map = {}
        self.storage = StorageAction(self)
        self.continuous = False
        self.falling_duration = None
        self.update_hd_number()

    def procedure(self):
        self.start_io()
        # 等待性能稳定
        self.wait_with_record(record_name=self.record_b4_dfx)
        # 注入故障
        self.inject_fault()
        # 持续IO 5分钟
        self.run_io_under_fault()
        # 恢复故障并二次确认
        self.recover_fault()
        self.confirm_system_recovery()
        # 故障后平均性能记录截止点
        self.check_io_status()

    @add_record("下发并等待IO业务启动")
    def start_io(self):
        elapsed = self.get_vdbench_elapsed()
        self.vdbench = VdbenchIO(self).action_impl
        self.vdbench.run(
            vdbench_dir=self.get_parameter("vdbench_dir"),
            config_template_file=self.get_parameter("config_template_file"),
            anchor_paths=self.get_parameter("anchor_paths", dict),
            threads_config=self.get_parameter("threads_config", int),
            elapsed=elapsed,
            multiple=self.get_parameter("multiple", int),
            fsd_group_number=self.get_parameter("fsd_group_number", int),
            fsd_width=self.get_parameter("fsd_width", int),
            wait=False)

        # 等待vdbench开始读写
        if not self.vdbench.wait_for_stage_start():
            self.fail(message="IO业务启动超时", raise_exception=True)

    def inject_fault(self):
        # 执行故障
        add_record(f"用户执行故障：{self.fault_name}")(self.inject_fault_implement)(self)

    def inject_fault_implement(self, *args):
        """注入故障，子类需实现此函数，且保留*args"""
        raise NotImplementedError

    @add_record("检查IO业务未中断")
    def check_io_status(self):
        # 检查vdbench是否已经中断
        self.reporter = self.vdbench.reporter
        if not self.vdbench.is_running():
            self.vdbench_terminated_time = self.vdbench.get_last_log_time()
            timestr = str(self.vdbench_terminated_time).rsplit(".", maxsplit=1)[0]
            self.fail(message=f"I/O业务异常中断于：{timestr}", raise_exception=True)
        else:
            self.continuous = True

    def recover_fault(self):
        start = time.time()
        add_record("用户恢复故障")(self.recover_fault_implement)(self)
        self.op_time.append([start, time.time()])

    def recover_fault_implement(self, *args):
        """恢复故障，子类需实现此函数，且保留*args"""
        raise NotImplementedError

    @add_record("在故障状态下持续下发IO业务")
    def run_io_under_fault(self):
        wait = constants.FAULT_ELAPSED
        wait_str = wait // 60
        self.logger.info(f"Keep generating I/O ({wait_str}min)")
        self.wait_with_record(elapsed=wait, record_name=self.record_dfx)

    @add_record("用户确认存储系统已恢复正常")
    def confirm_system_recovery(self):
        recovered = self.storage.confirm_recovery()
        if recovered:
            self.op_time.append(time.time())
        else:
            self.fail("未确认存储系统已恢复正常", raise_exception=True)

    def wait_with_record(self, elapsed=constants.STABLE_TIME, record_name=None):
        """等待一段时间，并添加等待前后的时间点

        Args:
            elapsed: 等待时长
            record_name: 记录名称
        """
        self.logger.debug(f"Add record before wait")
        start_time = time.time()
        self.record_time.append(start_time)

        self.logger.debug(f"Wait for {elapsed} seconds")
        time.sleep(elapsed)

        self.logger.debug(f"Add record after wait")
        end_time = time.time()
        self.record_time.append(end_time)

        if record_name:
            self._record_map[record_name] = (start_time, end_time)

    def post_condition(self):
        # 清理vdbench并确认故障已恢复
        if hasattr(self, "vdbench") and self.vdbench is not None:
            self.vdbench.stop(wait=True)

    def make_report(self):
        if hasattr(self, "vdbench") and self.vdbench is not None:
            self.zero_info, self.falling_duration, _ = self.vdbench.reporter.create_bottom_data_part()
            return self.zero_info

    def register_indicator(self):
        return Continuity(self.continuous), DataHitBottomDuration(self.falling_duration)
