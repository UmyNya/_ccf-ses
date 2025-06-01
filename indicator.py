# -*- coding: UTF-8 -*-

import abc
from typing import Any


class __Indicator(abc.ABC):
    name: str
    _value: Any

    @property
    def value(self):
        return self._value


class BaseIndicator(__Indicator):

    def __init__(self, value: Any):
        self._value = value


class Bandwidth(__Indicator):
    name = "总带宽(GB/s）"

    def __init__(self, bandwidth_mb: float):
        self._value = round(bandwidth_mb / 1024, 2)

class SingleBandwidth(__Indicator):
    name = "单节点带宽(GB/s）"

    def __init__(self, bandwidth_mb: float, total_node_num: int):
        self._value = round(bandwidth_mb / total_node_num / 1024, 2)


class Ops(__Indicator):
    name = "总OPS(OPS)"

    def __init__(self, ops: float):
        self._value = round(ops)


class SingleOps(__Indicator):
    name = "单节点OPS(OPS)"

    def __init__(self, ops: float, total_node_num: int):
        self._value = round(ops / total_node_num)
        
class Resp(__Indicator):
    name = "平均响应时间(ms)"

    def __init__(self, resp: float):
        self._value = round(resp)


class Continuity(__Indicator):
    name = "业务连续性"

    def __init__(self, value: bool):
        self._value = "Pass" if value else "Failed"


class Functionality(__Indicator):
    name = "功能性"

    def __init__(self, value: bool):
        self._value = "Pass" if value else "Failed"


class DataHitBottomDuration(__Indicator):
    name = "跌零时长(s)"

    def __init__(self, value: int):
        if value is None:
            value = "-"
        elif not isinstance(value, int):
            raise TypeError("DataHitBottomDuration requires value of int type: %r" % value)
        self._value = value
