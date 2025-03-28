# -*- coding: UTF-8 -*-

import abc
import importlib
import inspect
import os
import sys
import time

from typing import List, Callable, Union
from colorama import Fore, Back, Style
from inputimeout import inputimeout, TimeoutOccurred

from storage_evaluation_system_zzj import util
from storage_evaluation_system_zzj.parameter import DefaultParameter
from storage_evaluation_system_zzj.client.client import Client
from storage_evaluation_system_zzj.constants import ClientTarget
from storage_evaluation_system_zzj.exception import CustomAPIError, SESError, CaseFailedError
from storage_evaluation_system_zzj.logger import logger


class Actions(abc.ABC):
    """接口集合

    Actions的子类（如SubAction）需遵循以下规则：

    * 需被外部子类实现的方法，必须在 ``SubAction`` 中定义，并抛出 ``NotImplementedError``
    * 方法加载顺序：
        > foo() -> 执行当前类中的foo
            > raise NotImplementedError -> 执行当前类同模块下子类的foo
                > raise NotImplementedError -> 执行外部子类的foo
                > 无NotImplementedError，视为执行完成
            > 无NotImplementedError，视为执行完成
    """
    client_type = Client

    def __init__(self, case, client: Client = None, target_client: ClientTarget = None, is_impl=False, **kwargs):
        """

         Args:
            client: 此Actions实例的执行环境
        """

        self.case = case
        if not client and target_client:
            client = case.get_executor_clients(target_client)[0]
        self.client = client
        if case:
            self.output_dir = case.step_output_dir
            self.case_parameter = case.parameters

        self.logger = logger
        self._action_class_impl: type = None
        self._action_instance: Actions = None

        custom_actions_path = kwargs.get("custom_actions_path")
        if not custom_actions_path and case:
            custom_actions_path = case.custom_actions_path

        if not is_impl:
            self._action_class_impl = self.__get_action_class_impl(
                self.client.__class__,
                custom_actions_path)
            if not self._action_class_impl:
                self._action_instance = self
            else:
                self._action_instance = self._action_class_impl(case,
                                                                client=self.client,
                                                                custom_actions_path=custom_actions_path,
                                                                is_impl=True)

    def set_output_dir(self):
        self.output_dir = self.case.step_output_dir

    def stop(self):
        pass

    @property
    def action_impl(self):
        return self._action_instance

    def get_env_parameter(self, name: str):
        """获取环境配置中自定义的变量

        Args:
            name: 变量名称
        """

        return self.client.get_parameter(name)

    def get_runtime_result(self, name: str):
        """获取执行中保存的变量结果

        Args:
            name: 变量名称
        """
        return self.case_parameter.get_runtime_result(name)

    def create_report_section(self):
        """创建对应的报告内容。仅用于xml类型用例"""
        return None

    def register_indicator(self):
        """创建用例指标项"""
        return None

    @classmethod
    def _get_ancestors(cls, sub_class):
        """获取基类"""
        ancestors = []
        for base in sub_class.__bases__:
            ancestors.append(base)
            ancestors.extend(cls._get_ancestors(base))
        return ancestors

    @classmethod
    def __get_action_class_impl(cls, client_class: type, custom_actions_path) -> type:
        """加载并返回外部实现类"""

        # 寻找子类，要求子类已被加载（与当前class定义在同一文件）
        for clz in cls.__subclasses__():
            if clz.client_type == client_class:
                return clz

        if custom_actions_path is None:
            return None

        for file in os.listdir(custom_actions_path):
            full_path = os.path.join(custom_actions_path, file)
            module_name = file.split(".")[0]
            spec = importlib.util.spec_from_file_location(module_name, full_path)
            if spec is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            # 加载类
            for name, clazz in inspect.getmembers(module, inspect.isclass):
                if clazz == cls or not hasattr(clazz, "__bases__"):
                    continue

                for base in cls._get_ancestors(clazz):
                    if base == cls:
                        return clazz
        return None
