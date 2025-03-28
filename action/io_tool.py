

# -*- coding: UTF-8 -*-
from storage_evaluation_system_zzj.action.host import Actions


class IOTool(Actions):

    def run(self, *args, **kwargs):
        """
        执行工具，启动进程，抽象函数
        :param args:
        :param kwargs:
        :return:
        """
        pass

    def stop(self):
        """
        结束运行进程，抽象函数
        :return:
        """
        pass

    def is_running(self):
        """
        检查进程是否运行中
        :return:
        """
        pass

    def wait_until_stop(self, *args, **kwargs) -> bool:
        """
        等待进程结束
        :param args:
        :param kwargs:
        :return:
        """
        pass

    def prepare(self, *args, **kwargs) -> str:
        """
        准备运行脚本
        :param args:
        :param kwargs:
        :return:
        """
        pass
