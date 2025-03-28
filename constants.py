# -*- coding: UTF-8 -*-

from collections import namedtuple
from enum import Enum, IntEnum, unique

# 报告打包目录前缀
REPORT_DIR_PREFIX = "SESReport"
# 校验文件名
VALIDATION_FILENAME = "validation"

CASES_PATTERN_PATH = "patterns/cases.xml"
SCENES_PATTERN_PATH = "patterns/scenes.xml"
IO_MODEL_PARAM_PATH = "io_model_param_file/"
TOOLS_PATH = "tools/"

DEFAULT_MON = "ses.mon"
# BENCHMARK_CASE_ID = "PERF_002"
BENCHMARK_CASE_ID = "NOT_PERF_CASES"

CONFIGURABLE_HD_CASE_LIST = ["PERF_001", "PERF_002", "16K_RW","64K_RW","AI_16K","AI_64K","PHOTO_AVG","NOT_PERF_CASES"]

# 用例间休眠
CASE_RUN_INTERVAL = 60 # 300

# 参与测评存储的最小容量(KB）：200T -> 200G
# MIN_STORAGE_CAPACITY = 200 * 1024 * 1024 * 1024
MIN_STORAGE_CAPACITY = 200 * 1024 * 1024

# 工具配置
ToolInfo = namedtuple('ToolInfo', ['env_name', 'default_dir_name', 'executable'])
TOOL_BASE_DIR_UNIX = '/opt/ses'
TOOL_BASE_DIR_WIN = 'C:\\Program Files\\ses'
TOOL_VALIDATE_DICT = {
    "vdbench": ToolInfo("VDB_DIR", "vdb", "vdbench")
}

# sysstat工具获取本地磁盘读写间隔（秒）
IOSTAT_INTERVAL = 5

# vdbench
# 一组fsd的固定数据量（KB）
VDBENCH_FSD_GROUP_SIZE = 4706 * 1024
VDBENCH_DEPTH = 4
VDBENCH_MON_FILE = "ses_vdb.mon"
VDBENCH_STABLE_TIME = 150 #300
VDBENCH_ELAPSED_PRE = 5
VDBENCH_ELAPSED =  180 #6 * 60 * 60 #600
VDBENCH_LONG_OP_ELAPSED = 4 * 60 * 60 #4 * 60 * 60
VDBENCH_AVG_CONTINUOUS_TIME = 180 #2 * 60 * 60 #600
# vdbench执行完成 到进程结束的等待时长
VDBENCH_PROC_END_TIMEOUT = 60 #600


VDBENCH_WARMUP = 60
# 3天
VDBENCH_EXEC_TIMEOUT = 3 * 24 * 60 * 60
VDBENCH_LARGE_FILE = (3, 4, 1024 * 1024)
VDBENCH_SMALL_FILE = (5, 8, 32)

# 等待性能稳定的时间（故障执行前）
STABLE_TIME = 300
# 故障持续时间（故障执行 ~ 故障恢复）
FAULT_ELAPSED = 300
# 故障恢复后，等待N秒二次确认系统是否已恢复
FAULT_ENSURE_RECOVER_TIME = 300
# 系统恢复等待时长
FAULT_RECOVER_TIMEOUT = 3600

# 设备信息表
DEVICE_INFO_MAP = {
    "storage": ["MODEL", "TOTAL_NODE_NUM", "CPU_MODEL_AND_NUM", "MEMORY", "HARD_DRIVE_TYPE",
                "SINGLE_DISK_CAPACITY", "NUMBER_OF_HARD_DRIVE", "EC_OR_RAID_RATIO", "INTERFACE_TYPE", "INTERFACE_RATE",
                "NUMBER_OF_INTERFACES", "SOFTWARE_VERSION"],
    "storage_ch": ["机型", "控制器/节点数量", "CPU型号*总数", "内存", "硬盘类型",
                   "硬盘单盘容量", "硬盘数量", "EC/RAID比例", "业务接口类型", "业务接口速率",
                   "业务接口数量", "软件版本"]
}

DEVICE_INFO_TYPE = ["CLIENT", "CPU", "SYSTEM", "SYSTEM_TYPE",
                    "STORAGE_PROTOCOL/SOFTWARE", "NETWORKING", "HARDWARE", "COMPUTE_NODES"]
SCENE_TYPES = ["AI", "AI_PERF_DFX", "AI_EXP","ZZJ","QQ_PHOTO_ALBUM"]

# 性能用例时延最大值（毫秒）
MAX_IO_RESP = 10
# 时延不达标是否直接跳过后续所有用例
IS_IO_RESP_CRITICAL = False
# 性能波动容忍值，不达标用例无效
PERF_BENCHMARK_TOLERATE = 0.2


class SuiteStatus(Enum):
    """测试套状态"""
    PREPARING = 0
    PREPARED = 1
    RUNNING = 2
    COMPLETED = 3
    PAUSE = 4
    STOP = 5


class CaseStatus(Enum):
    """用例状态"""
    WAITING = 0
    RUNNING = 1
    COMPLETED = 2


class CaseResult(str, Enum):
    """用例结果"""
    UNKNOWN = "Unknown"
    PASS = "Pass"
    FAILED = "Failed"
    SKIPPED = "Skipped"
    INVALID = "Invalid"


class RecordResult(str, Enum):
    """用例步骤结果"""
    UNKNOWN = "-"
    PASS = "Pass"
    FAILED = "Failed"


class CaseCategory(str, Enum):
    """测试类型（报告以此排序）"""
    PERFORMANCE = "性能测试"
    RELIABILITY = "可靠性测试"
    ECOLOGY = "生态测试"
    SECURITY = "数据安全测试"
    CAPACITY = "容量测试"
    EFFICIENCY = "能效测试"
    EXPANSION = "扩展性测试"


class ClientTarget(str, Enum):
    """环境类型"""
    MASTER_HOST = "master_host"
    ALL_HOST = "all_host"
    MASTER_STORAGE_HTTP = "master_storage_http"
    MASTER_STORAGE_SSH = "master_storage_ssh"
    ALL_STORAGE = "all_storage"
    ALL_HTTP_STORAGE = "all_http_storage"


class TimeFormat(str, Enum):
    DEFAULT = "%Y-%m-%d %H:%M:%S"
    COMPACT = "%Y%m%d%H%M%S"


@unique
class CacheDataKey(str, Enum):
    """执行数据key值"""

    # 存储系统可用硬盘数
    TOTAL_DISK_NUM = "total_disk_num"
    # 单个硬盘容量（KB）
    SINGLE_DISK_CAPACITY = "single_disk_capacity"
    # 存储系统总容量（ total_disk_num * single_disk_capacity）
    STORAGE_CAPACITY = "storage_capacity"
    # 总预埋数据量
    TARGET_DATA_SIZE = "target_data_size"
    # 性能用例平均带宽
    AVG_BANDWIDTH_BENCHMARK = "avg_bandwidth_benchmark"
    # 性能用例平均OPS
    AVG_OPS_BENCHMARK = "avg_ops_benchmark"
    # 控制器/节点个数
    TOTAL_NODE_NUM = "total_node_num"
