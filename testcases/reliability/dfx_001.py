# -*- coding: UTF-8 -*-
import time
from storage_evaluation_system_zzj.testcases.reliability.dfx_base import DFXBase


class DFX001(DFXBase):
    """系统数据盘故障"""

    @property
    def fault_name(self):
        return "移除存储系统硬盘"

    def inject_fault_implement(self, *args):
        # 执行故障
        if self.storage.remove_disk_action():
            self.op_time.append(time.time())
        else:
            self.fail(f"未执行{self.fault_name}")

    def recover_fault_implement(self, *args):
        self.storage.recover_disk_action()
