# -*- coding: UTF-8 -*-
from storage_evaluation_system_zzj.testcases.performance.perf_base import PERFBase
from storage_evaluation_system_zzj.indicator import Ops, SingleOps


class PERF001(PERFBase):
    """AI训练存储性能（OPS性能）"""

    def register_indicator(self):
        total_node_num = self.get_parameter("total_node_num", int)
        return SingleOps(self.ops, total_node_num), Ops(self.ops)

