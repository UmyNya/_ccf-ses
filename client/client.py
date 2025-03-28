import random
import logging
import xml.etree.ElementTree as ET
from typing import Dict, Any

from storage_evaluation_system_zzj.exception import ConfigError
from storage_evaluation_system_zzj.logger import logger

# 注册客户端名与类映射关系, {client_class: client_name}
clients = {}
# 用例已实例化客户端集合，{user_instance：{class+service_name+role, client_obj}}
conn_pool = {}


class Response:
    """
    客户端请问响应对象，一般用于命令行执行返回
    """

    def __init__(self, status_code: int, stdout: str, stderr: str = None, expect: str = None, prompt: str = None,
                 **kwargs):
        self.status_code = status_code
        self.stdout = stdout
        self.stderr = stderr
        # result_code仅为了适配之前ssh响应变量，后续建议使用status_code
        self.result_code = status_code
        # 若命令行预期匹配回显，则打印时不输出match_str
        self._expect_prompt = (prompt == expect)
        if len(kwargs) > 0:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def __str__(self):
        # 输出结果过多情况下，省略日志输出
        _out = self.stdout
        if _out is not None and len(_out) > 2048:
            _out = '\n... ...\n'.join([_out[:1023].strip(), _out[-1024:].strip()])

        _err = self.stderr
        if _err is not None and len(_err) > 2048:
            _err = '\n... ...\n'.join([_err[:1023].strip(), _err[-1024:].strip()])
        res_format = 'Response status_code: {0}\nstdout: {1}'.format(self.status_code, _out)

        if self.stderr:
            res_format = "%s\nstderr: %s" % (res_format, _err)

        for k, v in self.__dict__.items():
            if k == "match_str" and self._expect_prompt:
                continue
            if k not in ['status_code', 'result_code', 'stdout', 'stderr', '_expect_prompt'] and v:
                res_format = "%s\n%s: %s" % (res_format, k, str(v))
        return res_format


class Client:
    """
    客户端基类
    """

    def __init__(self, env_config: ET.Element):
        # xml属性为对象属性
        self.env_config = env_config
        self.ip = env_config.attrib.get("ip")
        self.username = env_config.attrib.get("username")
        self.password = env_config.attrib.get("password")
        self.protocol = env_config.attrib.get("protocol")
        self.role = env_config.attrib.get("role")
        for k, v in env_config.attrib.items():
            setattr(self, k, v)

        self.logger = logger
        self.tag = env_config.tag

        self.parameters: Dict[str, Any] = {}
        self.facts: Dict[str, Any] = {}
        self._extract_elements("parameter", env_config, self.parameters)
        self._extract_elements("fact", env_config, self.facts)
        self._connector = self.connect()

    @staticmethod
    def _extract_elements(element_type: str, parent_element: ET.Element, storage_dict: Dict[str, Any]):
        for elem in parent_element.findall(element_type):
            name = elem.get("name")
            if name is None:
                raise ConfigError(f"Requires a non-null value for attribute 'name' of <{element_type}>")
            storage_dict[name] = elem.text

    def __del__(self):
        self.close()

    def __repr__(self):
        return f"<{self.protocol}-client ip={self.ip} role={self.role}>"

    def __str__(self):
        return self.__repr__()

    @property
    def connector(self):
        # 如果不存在属性_connector，说明client初始化过程失败，connector返回None
        if not hasattr(self, "_connector"):
            return None
        if not self._connector:
            self._connector = self.connect()

        return self._connector

    def connect(self):
        """
        与外部系统建立连接实现，必须返回连接对象
        """
        raise NotImplementedError()

    def close(self):
        raise NotImplementedError()

    def get_parameter(self, name):
        """获取用户自定义环境参数"""
        return self.parameters[name]


class ClientBuilder:
    """客户端工厂"""

    def __init__(self, env_config: ET.Element):
        self.env_config = env_config

    def create_client(self) -> Client:
        raise NotImplementedError
