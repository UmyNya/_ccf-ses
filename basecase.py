# -*- coding: UTF-8 -*-
import os
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from storage_evaluation_system_zzj import util, constants
from storage_evaluation_system_zzj.client.client import Client
from storage_evaluation_system_zzj.constants import CaseStatus, CaseResult, RecordResult, ClientTarget, CaseCategory
from storage_evaluation_system_zzj.exception import SESError, CaseFailedError
from storage_evaluation_system_zzj.logger import logger


class BaseCase:

    def __init__(self,
                 suite,
                 config_element: ET.Element,
                 static_parameters: dict,
                 custom_case_parameters: dict,
                 required: bool,
                 sub_id: str = None,
                 sub_name: str = None
                 ):
        """
        Args:
            suite: 所属测试套
            config_element: 定义用例的xml节点
            static_parameters: 静态解析的用例级别参数
            custom_case_parameters：用户用例配置参数
            sub_id: 子id。仅多测试数据类用例有
            sub_name: 子名称。仅多测试数据类用例有
        """
        super().__init__()
        self.suite = suite
        self.cid = self.major_id = config_element.get("id")
        self.cname = self.major_name = config_element.get("name")
        self.required = required
        self.category: str = config_element.get("category")
        if not self.category:
            raise SESError(f"Case [{self.cid}] has no category")
        if sub_id:
            self.cid += f"_{sub_id}"
        if sub_name:
            self.cname = f"{self.cname}-{sub_name}" if self.cname else sub_name
        self.output_dir = os.path.join(suite.output_dir, self.cid)
        self.step_output_dir = self.output_dir
        self.custom_actions_path = suite.custom_actions_path
        self.element = config_element
        self.parameters = CaseParameter(static_parameters, custom_case_parameters)
        self._status = CaseStatus.WAITING

        self.logger = logger
        self.result = CaseResult.UNKNOWN
        self.result_messages = []
        self.records = []
        self._action_instances = {}

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return f"[{self.cid}] {self.cname}"

    @property
    def status(self):
        return self._status

    @property
    def client_group(self):
        return self.suite.client_group

    @property
    def master_host(self) -> Client:
        return self.suite.master_host

    def pre_condition(self):
        pass

    def procedure(self):
        pass

    def post_condition(self):
        pass

    def make_report(self):
        return None

    def register_indicator(self):
        return None

    def update_hd_number(self):
        """更新当前用例使用hd-number
        hd 是 host daemon 的缩写，也就是用例配置的用于测试的主机守护进程。
        若为性能用例，使用自定义数量；否则使用perf_002的数量
        """
        self.anchor_paths = self.get_anchor_paths()
        if self.category.upper() == CaseCategory.PERFORMANCE.name:
            hd_param_name = self.cid + "_hd"
        else:
            hd_param_name = constants.BENCHMARK_CASE_ID + "_hd"
        hd_param_name = hd_param_name.lower()
        self.logger.debug(f"Updating hd-number by '{hd_param_name}'")

        for client in self.anchor_paths.keys():
            try:
                hd_number = client.get_parameter(hd_param_name)
            except KeyError:
                hd_number = 1
            client.hd_number = int(hd_number)

    def cache_runtime_data(self, key, value):
        """保存执行数据"""
        self.suite.cache_runtime_data(self.cid, key, value)

    def get_cache_data(self, key, case_id=None):
        """获取缓存数据"""
        return self.suite.get_cache_data(key, case_id=case_id)

    def get_executor_clients(self, target: ClientTarget) -> list:
        """获取执行环境列表"""
        return self.suite.get_executor_clients(target)

    def get_client_by_role(self, role):
        """获取对应role的环境"""
        return self.suite.get_client_by_role(role)

    def get_anchor_paths(self) -> dict:
        return self.get_parameter("anchor_paths")

    def get_parameter(self, name, ptype: type = None, default=None):
        """获取参数

        Args:
            name: 参数名
            ptype: 参数类型。若执行则记性类型转换
            default: 获取的参数值为None时的后补值
        """
        if name in self.parameters._custom_case_parameters:
            v = self.parameters.get_custom_case_parameter(name)
        elif name in self.parameters._static_parameters:
            v = self.parameters.get_static_parameter(name)
        else:
            v = self.suite.get_default_parameter(name)

        if v is None:
            return default

        if ptype and ptype.__name__ in util.TYPE_PARSER and not isinstance(v, ptype):
            return util.TYPE_PARSER[ptype.__name__](v)
        else:
            return v

    def fail(self, message=None, exception: BaseException = None, raise_exception=False):
        """标记用例失败

        Args:
            message: 失败信息
            exception: 异常对象
            raise_exception: 是否抛出异常。
                抛出指定的exception，或抛出默认CaseFailedError
        """
        if exception:
            self.logger.error(f"-> {exception}")
            exc = traceback.format_exc()
            if not exc.startswith("NoneType: None"):
                self.logger.debug(exc)

        self.result = CaseResult.FAILED
        self.add_report_message(message)

        if raise_exception:
            if not exception or not isinstance(exception, BaseException):
                exception = CaseFailedError
            raise exception

    def save_step_result(self, message, result: RecordResult, fail_case=True, fail_message=None):
        """添加用例执行步骤结果

        Args:
            message: 步骤描述
            result: 执行结果
            fail_case: 是否直接将用例标记为失败
            fail_message: 失败信息
        """
        if result == RecordResult.UNKNOWN:
            result = RecordResult.UNKNOWN.value
            tm = RecordResult.UNKNOWN.value
        else:
            tm = util.timestr()
            if result == RecordResult.PASS:
                color = 'green'
            else:
                color = 'red'
                if fail_case:
                    self.fail(message=fail_message)
            result = f'<span style="color: {color}">{result}</span>'
        self.records.append({"时间": tm, "步骤": message, "结果": result})

    def add_report_message(self, message: str):
        """添加报告说明信息"""
        if message:
            self.result_messages.append(message)
            self.logger.debug(f"Append message: {message}")

    def get_ref_parameter_value(self, ref_name):
        """获取引用的参数值"""
        value = None
        arg_name = ref_name.lstrip("$")
        if ref_name.startswith("$$$"):  # 静态参数
            value = self.parameters.get_static_parameter(arg_name)
        elif ref_name.startswith("$$"):  # 用户自定义
            if arg_name in self.parameters._custom_case_parameters:
                value = self.parameters.get_custom_case_parameter(arg_name)
            else:
                value = self.suite.get_default_parameter(arg_name)

        return value


class CaseParameter:

    def __init__(self, static_parameters: dict, custom_case_parameters: dict):
        """用例参数

        Args:
            static_parameters: 静态参数
            custom_case_parameters: 自定义用例参数
        """
        self._static_parameters = static_parameters
        self._custom_case_parameters = custom_case_parameters

    @property
    def static_parameters(self):
        return self._static_parameters

    @property
    def custom_case_parameters(self):
        return self._custom_case_parameters

    def get_static_parameter(self, name):
        """获取静态参数 """
        return self._static_parameters.get(name)

    def get_custom_case_parameter(self, name):
        """获取case级别用户配置"""
        return self._custom_case_parameters.get(name)


def add_record(message: str):
    """向报告中添加执行步骤记录

    Args:
        message: 步骤描述
        func: 目标函数。若抛出异常，后续添加了@add_record的步骤不再执行
    """

    def step_record(func):
        def run_step(self: BaseCase, *args, **kwargs):
            self.logger.info(f"Step: {message}")
            ret = None
            if self.result == CaseResult.FAILED:
                step_result = RecordResult.UNKNOWN
            else:
                try:
                    ret = func(self, *args, **kwargs)
                    step_result = RecordResult.PASS
                except Exception as e:
                    # 从fail中抛出的异常不再raise
                    tb = traceback.extract_tb(e.__traceback__)
                    last_trace = tb[-1]
                    exception = e
                    if last_trace.name == "fail":
                        exception = None
                    self.fail(exception=exception)
                    step_result = RecordResult.FAILED

            self.save_step_result(message, step_result)
            return ret

        return run_step

    return step_record
