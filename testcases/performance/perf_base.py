# -*- coding: UTF-8 -*-
from storage_evaluation_system_zzj import constants
from storage_evaluation_system_zzj.action.vdbench import VdbenchIO
from storage_evaluation_system_zzj.basecase import BaseCase
from storage_evaluation_system_zzj.indicator import Bandwidth, Ops, Resp
from storage_evaluation_system_zzj.report import ReportUtil, TitledContent


class PERFBase(BaseCase):
    """
    性能测试通用流程
    """

    def pre_condition(self):
        self.vdbench = VdbenchIO(self).action_impl
        self.bw = None
        self.ops = None
        self.resp = None
        self.csv_file_name = "flat.csv"
        self.avg_csv_file_name = "avg_flat.csv"
        self.anchor_paths = self.get_anchor_paths()
        self.update_hd_number()

    def procedure(self):
        self.vdbench.run(
            vdbench_dir=self.get_parameter("vdbench_dir"),
            config_template_file=self.get_parameter("config_template_file"),
            anchor_paths=self.anchor_paths,
            threads_config=self.get_parameter("threads_config"),
            fsd_group_number=self.get_parameter("fsd_group_number", int),
            fsd_width=self.get_parameter("fsd_width", int),
            elapsed=constants.VDBENCH_ELAPSED,
            multiple=self.get_parameter("multiple", int),
            dir_depth=self.get_parameter("dir_depth", ptype=int, default=1),
        )
        self.bw = self.vdbench.get_avg_bw()
        self.ops = self.vdbench.get_avg_ops()
        self.resp = self.vdbench.get_avg_resp()

    def post_condition(self):
        # 清理vdbench
        if hasattr(self, "vdbench") and self.vdbench is not None:
            self.vdbench.stop(wait=True)

    def make_report(self):
        self.reporter = self.vdbench.reporter
        csv_file, avg_csv_file = self.reporter.create_csv_file()
        # 生成性能图
        soup = ReportUtil.build_perf_test_html_object(
            csv_file,
            avg_csv_file,
            self.reporter.create_common_chart,
            self.reporter.create_common_avg_table,
        )
        return soup

    def register_indicator(self):
        return Bandwidth(self.bw), Ops(self.ops), Resp(self.resp)
