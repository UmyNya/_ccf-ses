# -*- coding: UTF-8 -*-

import datetime
import math
import time
from typing import List, Union
from colorama import Fore, Back, Style
from inputimeout import inputimeout

from storage_evaluation_system_zzj import constants, util
from storage_evaluation_system_zzj.constants import ClientTarget, TimeFormat, CacheDataKey
from storage_evaluation_system_zzj.parameter import DefaultParameter
from storage_evaluation_system_zzj.util import wait_for, WaitResult, manual


class StorageAction:

    def __init__(self, case, **kwargs):

        """ 存储接口 """
        self.case = case
        self.logger = case.logger
        self.output_dir = case.step_output_dir
        self.case_parameter = case.parameters

    def action_impl(self):
        return self

    def login(self):
        pass

    @staticmethod
    def validate_timestamp(timestamp_str):
        try:
            datetime.datetime.strptime(timestamp_str, TimeFormat.DEFAULT)
            return True
        except ValueError:
            return False

    @staticmethod
    def validate_number(number_str):
        try:
            float(number_str)
            return True
        except ValueError:
            return False

    @manual("the current number of nodes or controllers")
    def query_nodes_or_controllers_num(self) -> int:
        """查询存储节点或控制器个数"""
        raise NotImplementedError

    @manual("the current capacity size of storage-system(TB)")
    def get_storage_capacity_size(self) -> float:
        """存储系统容量（TB）"""
        raise NotImplementedError

    def handle_manual_action(self, *args, **kwargs):
        """手动执行。用户完成/放弃手动操作后，输入字符串表明动作结束。"""
        kwargs.update(logger_obj=self.logger)
        return util.request_user_input(*args, **kwargs)

    def network_interface_off_action(self) -> List[float]:
        timestamps: List[float] = []
        self.logger.warning(f"Please manually turn off network interface.\n"
                            f"* Enter 'y': Confirm that the operation is completed.\n"
                            f"* Enter 'n': Quit and fail this case.")
        msg = f"Turn off network interface"
        last_input = self.handle_manual_action(msg, validator="yn")
        if last_input == "y":
            timestamps.append(time.time())

        _msg = [util.sec2timestr(t) for t in timestamps]
        self.logger.debug(f"storage network interface off at: {_msg}")
        return timestamps

    def recover_network_action(self):
        """恢复网络端口"""
        try:
            self.recover_network()
        except NotImplementedError:
            self.recover_fault_manually("storage network")

    def recover_network(self):
        """恢复网络端口（可选。未实现将触发手动确认）"""
        raise NotImplementedError

    def start_expansion(self):
        """
        是否开始扩容
        Args:
            timeout: 等待操作时间
        """
        self.logger.warning("Please manually expand the controllers or nodes.\n"
                            f"* Enter 'y': Confirm that the expansion is started.\n"
                            f"* Enter 'n': Quit and fail this case.")
        last_input = self.handle_manual_action(validator="yn")
        if last_input == "y":
            return time.time()

        return None

    def remove_disk_action(self):
        """移除硬盘"""
        try:
            result = self.remove_disk()
            return result
        except NotImplementedError:
            self.logger.warning(f"Please manually remove or shutdown a disk.\n"
                                f"* Enter 'y': Confirm that the operation is completed.\n"
                                f"* Enter 'n': Quit and fail this case.")
            return self.handle_manual_action(validator="yn") == "y"

    def remove_disk(self):
        """移除硬盘"""
        raise NotImplementedError

    def recover_disk(self):
        """恢复硬盘"""
        raise NotImplementedError

    def recover_disk_action(self):
        """恢复硬盘"""
        try:
            self.recover_disk()
        except NotImplementedError:
            self.recover_fault_manually("disks")

    def recover_fault_manually(self, fault_name):
        """手动恢复故障、手动确认系统已恢复"""
        msg = f"Please recover {fault_name}. Enter 'y' to confirm"
        self.handle_manual_action(msg, validator="yn", ignore_timeout=False)

    def confirm_recovery(self):
        """手动确认系统已恢复正常"""
        timeout = constants.FAULT_RECOVER_TIMEOUT
        timeout_str = round(timeout / 3600, 1)
        msg = f"Please confirm storage-system has fully recovered. Confirmation timeout: {timeout_str} hour. Enter 'y' to confirm"
        return self.handle_manual_action(msg, timeout=timeout, validator="yn", ignore_timeout=False) == "y"
