# -*- coding: UTF-8 -*-
from typing import List

from storage_evaluation_system_zzj.testcases.reliability.dfx_base import DFXBase


class DFX002(DFXBase):
    """接口卡故障"""

    @property
    def fault_name(self):
        return "存储网口故障"

    def inject_fault_implement(self, *args):
        # 执行故障
        timeline: List[float] = self.storage.network_interface_off_action()
        if not timeline:
            self.fail(f"未执行{self.fault_name}")
            return
        self.op_time.append(timeline)

    def recover_fault_implement(self, *args):
        self.storage.recover_network_action()
