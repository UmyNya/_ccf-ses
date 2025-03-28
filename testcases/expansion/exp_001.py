# -*- coding: UTF-8 -*-
import time

import pandas as pd

from storage_evaluation_system_zzj import constants, util
from storage_evaluation_system_zzj.exception import CaseFailedError
from storage_evaluation_system_zzj.report import ReportUtil, TitledContent
from storage_evaluation_system_zzj.testcases.reliability.dfx_base import DFXBase
from storage_evaluation_system_zzj.basecase import add_record
from storage_evaluation_system_zzj.action.vdbench import VdbenchIO
from storage_evaluation_system_zzj.constants import CacheDataKey, CaseResult
from storage_evaluation_system_zzj.action.storage import StorageAction
from storage_evaluation_system_zzj.indicator import Functionality


class EXP001(DFXBase):
    """
    扩展性测试
    """

    @property
    def fault_name(self):
        return "存储系统扩容"

    def pre_condition(self):
        super().pre_condition()
        self.functional = False
        self.done_recovery = False
        self.before_num = -1
        self.before_cap_tb = -1
        self.after_num = -1
        self.after_cap_tb = -1

        try:
            ops = self.get_cache_data(CacheDataKey.AVG_OPS_BENCHMARK)
        except:
            self.fail("缺少性能用例数据作为基线值",
                      exception=CaseFailedError("OPS benchmark not found"),
                      raise_exception=True)
        else:
            self.fwdrate = int(ops * 0.1)

    def procedure(self):
        # 获取控制器数量、存储容量
        self.storage: StorageAction = StorageAction(self)
        self.before_num = self.storage.query_nodes_or_controllers_num()

        if not isinstance(self.before_num, int) or self.before_num < 1:
            self.fail(f"控制器/节点数量异常：{self.before_num}")
            return

        self.before_cap_tb = self.storage.get_storage_capacity_size()
        if not isinstance(self.before_cap_tb, (int, float)):
            self.fail(f"存储系统容量异常：{self.before_cap_tb}")
            return

        self.before_cap_tb = round(self.before_cap_tb, 3)
        self.logger.info(f"Before expansion: "
                         f"controller/node num: {self.before_num}, storage-system capacity: {self.before_cap_tb}TB")

        self.start_io()
        # 等待性能稳定
        self.wait_with_record()
        # 提示扩容
        self.expand_capacity()
        # 用户确认存储系统扩容已完成
        self.confirm_expansion_done()

    @add_record("下发并等待IO业务启动")
    def start_io(self):
        elapsed = self.get_vdbench_elapsed()
        self.vdbench = VdbenchIO(self).action_impl
        self.vdbench.run(
            vdbench_dir=self.get_parameter("vdbench_dir"),
            config_template_file=self.get_parameter("config_template_file"),
            anchor_paths=self.get_parameter("anchor_paths", dict),
            threads_config=self.get_parameter("threads_config", int),
            fsd_group_number=self.get_parameter("fsd_group_number", int),
            fsd_width=self.get_parameter("fsd_width", int),
            elapsed=elapsed,
            multiple=self.get_parameter("multiple", int),
            fwdrate=self.fwdrate,
            wait=False)

        # 等待vdbench开始读写
        if not self.vdbench.wait_for_stage_start():
            self.fail(message="IO业务启动超时", raise_exception=True)

    @add_record("存储系统开始数据扩容")
    def expand_capacity(self):
        """等待任务稳定，并对存储扩容"""
        timestamp = self.storage.start_expansion()
        if timestamp is None:
            self.fail(message="存储扩容失败", raise_exception=True)
            return
        self.op_time.append(timestamp)

    @add_record("用户确认存储系统已完成扩容")
    def confirm_expansion_done(self):
        user_input = self._confirm_expansion_done()
        if user_input == "y":
            self.op_time.append(time.time())
        else:
            self.fail("未确认存储系统已完成扩容", raise_exception=True)

        # 检查节点数 容量
        self.logger.debug("Query data after capacity expansion and check whether nodes are added.")
        self.after_num = self.storage.query_nodes_or_controllers_num()
        self.after_cap_tb = self.storage.get_storage_capacity_size()
        self.after_cap_tb = round(self.after_cap_tb, 3)
        self.logger.info(f"After expansion: "
                         f"controller/node num: {self.after_num}, storage-system capacity: {self.after_cap_tb}TB")

        if self.after_num <= self.before_num:
            self.fail(
                message=f"存储扩容失败，扩容后节点或控制器数量({self.after_num})未大于扩容前数量({self.before_num})")
        if self.after_cap_tb <= self.before_cap_tb:
            self.fail(
                message=f"存储扩容失败，扩容后的存储系统容量({self.after_cap_tb}TB)未大于扩容前容量({self.before_cap_tb}TB)"
            )

        if self.status != CaseResult.FAILED:
            self.functional = True

    def _confirm_expansion_done(self):
        wait = 7200  # 2小时
        wait_str = wait // 3600
        msg = (f"Please confirm storage-system has finished expansion."
               f" Confirmation timeout: {wait_str} hours. Enter 'y' to confirm")
        self.logger.warning(msg)
        return util.request_user_input(timeout=wait, validator="yn", ignore_timeout=False)

    def make_report(self):
        if self.op_time:
            data = {"": ["扩容前", "扩容后"],
                    "控制器/节点数": [self._get_display_num(self.before_num),
                                      self._get_display_num(self.after_num)],
                    "存储系统容量(TB)": [self._get_display_num(self.before_cap_tb),
                                         self._get_display_num(self.after_cap_tb)]
                    }
            table = ReportUtil.create_table(data, header_pos="v")

            return TitledContent("扩容前后数据对比", table).to_soup()

        else:
            self.fail("未执行扩容操作")

    @staticmethod
    def _get_display_num(value):
        return value if value != -1 else "-"

    def register_indicator(self):
        return Functionality(self.functional)
