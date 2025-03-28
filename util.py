# -*- coding: UTF-8 -*-
from dataclasses import dataclass
import json
import os
import re
import logging
from datetime import datetime

from colorama import Fore, Back, Style
from inputimeout import inputimeout, TimeoutOccurred
import math
import time
import traceback
from lxml.etree import Element
from pandas import DataFrame

from storage_evaluation_system_zzj import constants
from storage_evaluation_system_zzj.constants import CaseCategory, TimeFormat
from storage_evaluation_system_zzj.exception import CaseFailedError

from enum import Enum
from functools import wraps
from typing import Union, List, Dict, Callable, get_type_hints

from storage_evaluation_system_zzj.parameter import DefaultParameter

logger = logging.getLogger(__name__)

_OPERATORS = {
    "eq": (lambda a, b: str(a) == str(b), "等于"),
    "gt": (lambda a, b: number(a) > number(b), "高于"),
    "lt": (lambda a, b: number(a) < number(b), "低于"),
    "ge": (lambda a, b: number(a) >= number(b), "不低于"),
    "le": (lambda a, b: number(a) <= number(b), "不高于"),
}

CLIENT_TARGET_MAP = {
    "all_host": lambda c_group: [i for i in c_group if i.tag == "host"],
    "all_storage": lambda c_group: [i for i in c_group if i.tag == "storage"],
    "master_host": lambda c_group: [i for i in c_group if i.tag == "host" and i.role == "master_host"],
    "master_storage_ssh": lambda c_group: [i for i in c_group if i.tag == "storage" and i.role == "master_storage_ssh"],
    "master_storage_http": lambda c_group: [i for i in c_group if
                                            i.tag == "storage" and i.role == "master_storage_http"],
    "all_http_storage": lambda c_group: [i for i in c_group if i.tag == "storage" and i.role.endswith("http")]
}

# 字符串转其他类型
TYPE_PARSER = {
    "str": lambda v: str(v),
    "int": lambda v: int(v),
    "float": lambda v: float(v),
    "bool": lambda v: str2bool(v),
    "list": lambda v: [s.strip() for s in v.split(',')],
    "dict": lambda v: json.loads(v)
}


@dataclass
class IostatData:
    """Iostat命令数据对象"""
    tps: float = 0
    mb_read_ps: float = 0
    mb_wrtn_ps: float = 0


def str2bool(value: str):
    if value.lower() == "true":
        return True
    elif value.lower() == "false":
        return False
    else:
        raise ValueError(f"Invalid bool value: {value}")


def replace_win_sep(config_file):
    if config_file.__contains__("/"):
        return "\\".join(config_file.split("/"))
    return config_file


def is_number(value) -> bool:
    """值是否为数字（正整数/正小数）"""
    reg = r'^\d+(\.\d+)?$'
    return re.match(reg, value) is not None


def number(value):
    """转为数字"""
    if isinstance(value, str):
        if "." in value:
            return float(value)
        else:
            return int(value)
    elif isinstance(value, (int, float)):
        return value
    else:
        raise TypeError(f"Unspported type to convert to number: {value}")


def project_root():
    return os.path.dirname(__file__)


def get_resource_dir():
    """静态资源目录"""
    return os.path.join(os.path.dirname(__file__), "resource")


def get_resource_path(subpath: str):
    """静态资源目录"""
    subpaths = re.split(r"\\\\|/", subpath)
    return os.path.join(os.path.dirname(__file__), "resource", *subpaths)


def get_all_file_in_dir(dir_path):
    """获取文件夹中所有的文件"""
    files = []
    file_or_dir = os.listdir(dir_path)
    for item in file_or_dir:
        path = os.path.join(dir_path, item)
        if os.path.isdir(path):
            files.extend(get_all_file_in_dir(path))
        else:
            files.append(path)
    return files


def find_resource():
    pass


class WaitResult(int, Enum):
    OK = 0  # 达到预期值
    FAIL_FAST = 1  # 快速失败
    TIMEOUT = 2  # 等待超时


def wait_for(expect, fail_fast=(), timeout=86400, interval=3, ignore_err=False, msg: str = None):
    """ 轮询等待装饰器

    注意：首次调用接口如果符合预期，即使超时也将返回OK

    Args:
        expect: 预期值
        fail_fast: 快速失败值
        interval: 轮询间隔（秒）
        timeout: 超时时间。默认为24小时。若要持续等待，指定timeout=-1
            type=int时表示秒，type=str时请参考函数`timestr_to_sec`
        ignore_err: 是否忽略异常。为True，遇到异常时不抛出并直接进入下一轮等待
        msg: 等待日志信息

    Returns:
        参见 ``common.util.api.WaitResult``

    例：
    >>  l = (x for x in range(10))
    >>  @wait_for(5, interval=1)
        def example_ok():
            i = next(l)
            print(i, end=" ")
            return i
    >>  res = example_ok()
    >>  print("\nResult:", res)
    >>  0 1 2 3 4 5
        Result: 0

    >>  @wait_for(5, fail_fast=3, interval=1, msg="例2")
        def example_fail():
            i = next(l)
            print(i, end=" ")
            return i
    >>  res = example_fail()
    >>  print("\nResult:", res)
    >>  0 1 2 3
        [例2]快速失败！结果值：3
        Result: 1
    """

    if not isinstance(expect, (list, set, tuple)):
        expect = [expect]
    if not isinstance(fail_fast, (list, set, tuple)):
        fail_fast = [fail_fast]
    if isinstance(timeout, int):
        if timeout == -1:
            timeout = math.inf
        elif timeout <= 0:
            raise ValueError(f"非法的timeout:{timeout}。timeout需为正整数/-1/有效字符串")
    else:
        timeout = timestr2sec(timeout)

    msg = msg if msg else ""

    def decorator(func):

        @wraps(func)
        def wrapper(*args, **kwargs):
            now = time.time()
            result = None
            if msg:
                logger.debug(msg)
            while time.time() - now < timeout:
                try:
                    result = func(*args, **kwargs)
                except Exception:
                    if not ignore_err:
                        raise
                    else:
                        logger.debug(f'[{msg}] Error：{traceback.format_exc()}')

                if result in expect:
                    logger.debug(f'[{msg}] Success: {result}')
                    return WaitResult.OK
                elif result in fail_fast:
                    logger.debug(f'[{msg}] Fail fast! result: {result}')
                    return WaitResult.FAIL_FAST
                time.sleep(interval)

            logger.warning(f'[{msg}] Timeout! Last result: {result}')
            return WaitResult.TIMEOUT

        return wrapper

    return decorator


def sec2timestr(value: Union[int, float], fmt=None) -> str:
    if not fmt:
        fmt = constants.TimeFormat.DEFAULT

    return time.strftime(fmt, time.localtime(value))


def timestr2sec(duration: Union[int, str]) -> int:
    """ 时间字符串转为秒

    Args:
        duration: 时长
            - type=int： 秒
            - type=str： h-时，m-分，s-秒。无单位则转换为秒

            例：duration=7200 <=> 7200s <=> 120m <=> 2h
    """
    if isinstance(duration, str):
        if duration[-1].isdigit():
            return int(duration)

        try:
            val, unit = int(duration[:-1]), duration[-1].lower()
        except ValueError as e:
            raise ValueError(f"不支持的duration格式：{duration}") from e
        if unit == "h":
            duration = val * 60 * 60
        elif unit == "m":
            duration = val * 60
        elif unit == "s":
            duration = val
        else:
            raise ValueError(f"不支持的duration单位：{duration}。有效值：h/m/s")
    elif not isinstance(duration, int):
        raise ValueError(f"<duration>需为str/int类型")

    return duration


class JsonObject:
    """json转对象"""

    def __init__(self, data):
        for name, value in data.items():
            setattr(self, name, self._wrap(value))

    def _wrap(self, value):
        if isinstance(value, (tuple, list, set, frozenset)):
            return type(value)([self._wrap(v) for v in value])
        else:
            return JsonObject(value) if isinstance(value, dict) else value

    def __getattr__(self, item):
        raise AttributeError(f"JsonObject has no attribute '{item}'. Original data: {self}")

    def __repr__(self):
        return '{%s}' % str(', '.join("'%s': %s" % (k, repr(v)) for (k, v) in self.__dict__.items()))

    def get(self, item):
        """保留get方法"""
        return self.__getattribute__(item)


def convert_capacity(capacity: Union[int, float, str],
                     unit="KB",
                     target_unit="KB",
                     n_digits: int = 0,
                     readable=False) -> Union[int, float, str]:
    """ 存储数据量单位换算

    Args:
        capacity: 数据量
        unit: capacity的单位。支持：B/KB/MB/GB/TB/PB（或K/M/G/T/P）
        target_unit: 换算单位，默认换算为KB。其他支持单位同unit
        n_digits: 保留小数位数（默认为int)。readable=True时不参与（默认为2）
        readable: 自动转换为易读值，返回值str，见示例

    Returns:
        readable=False: 以 <target_unit> 为单位的容量大小（int, float）
        readable=True: 易读值（str）
    Examples:
        >>> convert_capacity(20480, unit="G", target_unit="TB", n_digits=1)
        20.0
        >>> convert_capacity("20480G", target_unit="TB", n_digits=1)
        20.0
        >>> convert_capacity(400, unit="G")
        419430400
        >>> convert_capacity(10.5, unit="TB", target_unit="GB")
        10752
        >>> convert_capacity(419430400, readable=True)
        '400.0GB'
        >>> convert_capacity(1024, unit="G", readable=True)
        '1.0TB'
    """
    if not isinstance(capacity, (int, float, str)):
        raise ValueError("capacity must be a int/float/str")

    if isinstance(capacity, str):
        match = re.search("([1-9]\d*\.?\d*|0\.\d*[1-9])(.*)", capacity)
        if not match:
            raise ValueError("capacity must be start with number")
        capacity = float(match.group(1))
        if match.group(2):
            unit = match.group(2)

    unit = unit.upper()
    if not unit.endswith("B"):
        unit += "B"
    target_unit = target_unit.upper()
    if not target_unit.endswith("B"):
        target_unit += "B"

    units = ("B", "KB", "MB", "GB", "TB", "PB")
    if unit not in units:
        raise ValueError(f"Invalid unit: {unit}")
    if target_unit not in units:
        raise ValueError(f"Invalid target_unit: {target_unit}")
    if target_unit == unit and not readable:
        return capacity

    if n_digits == 0:
        n_digits = None
    if readable:
        index = units.index(unit)
        while capacity >= 1024:
            capacity /= 1024
            index += 1
        return str(round(capacity, ndigits=2)) + units[index]
    else:
        # 换算倍数：1024的dif次方。目标单位大于当前单位时使用除法，即乘以换算倍数的倒数
        dif = units.index(unit) - units.index(target_unit)
        result_val = 1024 ** dif * capacity

        return round(result_val, ndigits=n_digits)


def get_category(name: str):
    """通过名称获取测试类型枚举对象"""
    return CaseCategory.__members__[name.upper()]


def nsstrip(el: Element):
    """处理带namespace的xml"""
    if el.tag.startswith("{"):
        el.tag = el.tag.split('}', 1)[1]  # strip namespace
    for k in el.attrib.keys():
        if k.startswith("{"):
            k2 = k.split('}', 1)[1]
            el.attrib[k2] = el.attrib[k]
            del el.attrib[k]
    for child in el:
        nsstrip(child)


def find_op_time_index(timeline: List[datetime], op_timeline: Union[List[float], float]) -> List[int]:
    """在指定时间点列表中找到部分时间点对应的列表索引

    Args:
        timeline: 完整时间点列表，如x轴列表
        op_timeline: 部分时间点（time.time()），可为一个float值或float列表

    Examples:
        timeline = ["2024-01-01 10:00:01","2024-01-01 10:00:02","2024-01-01 10:00:03"...]
        op_timeline = 1704074405  # 2024-01-01 10:00:05
        => [4]

        op_timeline = [1704074402, 1704074405]  # 2024-01-01 10:00:02, 2024-01-01 10:00:05
        => [1, 4]

    """
    if not isinstance(op_timeline, list):
        op_timeline = [op_timeline]

    result = []
    for tm in op_timeline:
        op_tm = datetime.fromtimestamp(int(tm))
        closest_element = min(timeline, key=lambda t: abs((t - op_tm)).total_seconds())
        index = timeline.index(closest_element)
        result.append(index)

    return result


def timestr(time_value: Union[int, float] = None, fmt=TimeFormat.DEFAULT):
    """时间转指定格式"""
    if not time_value:
        time_value = time.time()
    return time.strftime(fmt, time.localtime(time_value))


def calc_stable_value(df: DataFrame, range_indexes: List, names: List, labels: List) -> List[Dict]:
    """计算操作前后稳定值, 并计算前后变化的值, 用于生成报告
    Args:
        df: vdbench的评价指标值
        range_indexes: 操作前后开始和结束index
        names: 性能评价指标，例如：Rate,Resp等
        labels: 性能评价指标单位，例如：ops,ms等
    """
    avg_data = []

    # 获取操作前后均值
    op_name = ["Before op", "After op"]
    for (range_index, op) in zip(range_indexes, op_name):
        start_index, end_index = range_index[0], range_index[1]
        avg_map = {"Evaluation index": op}
        for col_name, col_name_label in zip(names, labels):
            value = round(df[col_name].loc[start_index:end_index].mean(), ndigits=2)
            avg_map[col_name] = f"{value} {col_name_label}"
        avg_data.append(avg_map)

    # 计算操作前后下降比例
    avg_map = {"Evaluation index": "Ratio"}
    for col_name, col_name_label in zip(names, labels):
        pre_op_val, post_op_val = float(avg_data[0][col_name].split(col_name_label)[0]), \
            float(avg_data[1][col_name].split(col_name_label)[0])
        value = (pre_op_val - post_op_val) / pre_op_val
        avg_map[col_name] = "{:.2f}%".format(value * 100)
    avg_data.append(avg_map)
    return avg_data


def request_user_input(msg=None, timeout=None, validator: Union[str, Callable] = None, ignore_timeout=True,
                       logger_obj=None):
    """手动执行。用户完成/放弃手动操作后，输入字符串表明动作结束。

    每次执行允许2次输入无效

    Args:
        msg: 提示用户执行手动操作的信息
        timeout: 等待用户操作的超时时长
        validator: 校验函数
            若为None，则不校验输入，任意输入后即退出
            若指定为一个函数，该函数需接收一个参数（用户输入字符串），并输出bool值表示是否立即退出
            若指定为int/float，则判断输入数据是否为int/number。不支持负数
            若指定为"yn"，则使用y/n判断输入：
                y - 执行完成、立即退出
                n - 不执行、立即退出
                其他 - 进入下一轮询问

    Returns: 用户最后一次输入字符串
    """

    def run(message, left_time):
        user_input = inputimeout(prompt=message, timeout=left_time)
        return user_input.lower().strip()

    if not msg:
        msg = "Confirm"

    convert = None  # 转换为目标类型
    if validator == "yn":
        validator = lambda inp: inp and inp in "yn"
        msg += " (y/n)"
    elif validator == int:
        validator = lambda inp: inp and re.match(r'^\d+', inp) is not None
        convert = int
    elif validator == float:
        validator = lambda inp: inp and is_number(inp)
        convert = float

    msg = f"{Fore.WHITE}{Back.BLUE}{msg}:{Style.RESET_ALL}"
    if timeout is not None:
        timeout = timestr2sec(timeout)
    else:
        timeout = DefaultParameter.MANUAL_ACTION_TIMEOUT.value

    # 允许2次输入无效
    start_time = time.time()
    last_result = None
    if not logger_obj:
        logger_obj = logger
    for _ in range(3):
        cur_time = time.time()
        _left_time = timeout - (cur_time - start_time)

        try:
            last_result = run(msg, _left_time)
        except TimeoutOccurred:  # 超时未输入
            timestr = f"{timeout // 60} minute(s)" if timeout >= 60 else f"{timeout} seconds"
            _msg = f"Timeout! Expect manual operation to be done in {timestr}"
            logger_obj.error(_msg)
            if not ignore_timeout:
                raise CaseFailedError(_msg)
            break
        else:
            if validator is None or validator(last_result):  # 完成操作
                if convert is None:
                    logger_obj.info("Manual operation confirmed")
                    break

                try:
                    last_result = convert(last_result)
                except:
                    logger_obj.warning(f"Invalid input value: {last_result}")
                else:
                    logger_obj.info("Manual operation confirmed")
                    break

            else:
                logger_obj.warning(f"Invalid input value: {last_result}")

    return last_result


def manual(return_description: str, validator=None):
    """手动操作装饰器

    被装饰函数若直接抛出NotImplementedError，将回落到手动执行

    被装饰函数如果指定return type为int或float，将自动校验
    """

    def deco(func):
        def wrapper(self, *args, **kwargs):
            try:
                ret = func(self, *args, **kwargs)
            except NotImplementedError:
                va = validator or get_type_hints(func).get('return', None)
                return request_user_input(f"Please enter {return_description}", validator=va)
            else:
                return ret

        return wrapper

    return deco
