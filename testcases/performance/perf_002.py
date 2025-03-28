# -*- coding: UTF-8 -*-
from storage_evaluation_system_zzj.testcases.performance.perf_base import PERFBase
from storage_evaluation_system_zzj.indicator import Bandwidth, SingleBandwidth


class PERF002(PERFBase):
    """AI训练存储性能（带宽性能）"""

    def make_report(self):
        chart = super().make_report()
        self.reporter.handle_benchmark()
        return chart

    def register_indicator(self):
        total_node_num = self.get_parameter("total_node_num", int)
        return SingleBandwidth(self.bw, total_node_num), Bandwidth(self.bw)
