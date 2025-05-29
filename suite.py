# -*- coding: UTF-8 -*-
import importlib
from importlib import util
import inspect
import os
from os.path import expanduser
import sys
import threading
import time
import traceback
from types import SimpleNamespace
from typing import List, Dict
import xml.etree.ElementTree as ET

from storage_evaluation_system_zzj import util, constants
from storage_evaluation_system_zzj.basecase import BaseCase
from storage_evaluation_system_zzj.client.client import Client
from storage_evaluation_system_zzj.client.ssh_client import SSHClientBuilder, SSHClient
from storage_evaluation_system_zzj.constants import *
from storage_evaluation_system_zzj.exception import SESError, ConfigError, EnvironmentValidationError, \
    SceneParameterNotFound, NumberTypeParamValueError
from storage_evaluation_system_zzj.logger import logger, exception_wrapper
from storage_evaluation_system_zzj.parameter import DefaultParameter
from storage_evaluation_system_zzj.report import Report
from storage_evaluation_system_zzj.util import convert_capacity

global_output_dir = ""


class Suite:
    """测试套（对应一个场景）"""

    def __init__(self, custom_config: ET,
                 scenario: str,
                 output_dir: str,
                 memory: str = None,
                 ignore_toolcheck_error=False):
        self.logger = logger
        self.custom_config: ET = custom_config
        self.name = scenario   
        self.status = SuiteStatus.PREPARING
        self.storage_memory: str = memory
        self.client_group: List[Client] = []
        self.master_host: Client
        self.cases: List[BaseCase] = []
        self.cases_by_major_id: Dict = {}
        self.pre_condition_result = {}
        self.error_stop = False
        self.custom_actions_path = None
        self.ignore_toolcheck_error = ignore_toolcheck_error
        self.tools_dir = {}
        self.run_energy_csmpt = False
        # 执行过程中保存的用例性能数据等
        self.cache = {}

        global global_output_dir
        global_output_dir = self.output_dir = output_dir

        # 自定义参数
        # 这里似乎可以通过自定义参数来自行扩展配置文件的关键词
        self.custom_suite_parameters = {}
        self.custom_case_parameters = {}
        self.validate()
        self.report: Report
        self.valid_skips = []
        self.skip_report_message = None
        self.all_skipped = False
        self._running_case_runner = None

    def validate(self):
        """配置校验"""
        environment = self.custom_config.find("environment")
        if environment is None:
            raise ConfigError("Requires config definition: environment")

        # 监控文件
        monitor = self.custom_config.find("monitor")
        if monitor is not None:
            self._monitor_file = monitor.get("path")
            if not self._monitor_file:
                raise ConfigError("Requires attribute 'path' for parameter 'monitor'")
        else:
            self._monitor_file = os.path.join(expanduser("~"), constants.DEFAULT_MON)

        if os.path.exists(self._monitor_file):
            os.remove(self._monitor_file)

        with open(self._monitor_file, 'w') as _:
            pass

    def update_custom_parameters(self):
        """校验自定义参数 """
        self.logger.info(f"Parsing custom config")
        # 自定义测试套级参数（可引用client）
        custom_suite_parameters = {}
        base_param_ele = self.custom_config.find("parameters")
        scene_param_ele = self.custom_config.find(f"./scene[@name='{self.name}']")

        # 辅助函数 add_custom_parameters：遍历XML节点中的 <parameter> 元素，将其 name 和 text 存入字典。
        def add_custom_parameters(root: ET.Element, custom_parameters_dict):
            if root is not None:
                for p in root.findall("parameter"):
                    custom_parameters_dict[p.get("name")] = p.text

        # 获取自定义测试套级参数
        add_custom_parameters(base_param_ele, custom_suite_parameters)
        # 获取自定义场景级参数（会覆盖同名测试套级参数）
        add_custom_parameters(scene_param_ele, custom_suite_parameters)
        self.custom_suite_parameters = self.parse_parameters(custom_suite_parameters)

        # 更新工具目录
        for tool_name, tool_dir in self.tools_dir.items():
            self.custom_suite_parameters[f"{tool_name}_dir"] = tool_dir

        # 资格校验
        self.validate_storage_specification()
        self.cal_min_data_size()
        self.logger.info(f"Custom config loaded successfully")

    def cal_min_data_size(self):
        """计算预埋数据量
        max(主机客户端总和内存2倍,存储控制器内存5倍,存储池可用容量20%(需要配置))
        """
        # 主机客户端总和
        host_total_mem = 0
        mem = self.get_required_parameter("total_host_memory")
        total_host_memory = util.convert_capacity(mem)
        logger.debug(f"total_host_memory = {total_host_memory}KB")

        # 存储控制器内存
        if not self.storage_memory:
            self.storage_memory = self.get_required_parameter("total_storage_memory")
        storage_mem = convert_capacity(self.storage_memory)
        logger.debug(f"storage_mem = {storage_mem}KB")

        # 存储池可用容量
        storage_a_cap = self.get_required_parameter("storage_available_capacity")
        try:
            storage_cap = util.convert_capacity(storage_a_cap)
        except Exception:
            raise ConfigError(f"Invalid storage_available_capacity value='{storage_a_cap}'")
        logger.debug(f"storage_cap = {storage_cap}KB")
        
        min_data_size_kb = 0
        
        # 获取预埋最小数据量
        if self.name == "QQ_PHOTO_ALBUM":
            min_data_size_kb = max(host_total_mem * 2, storage_mem * 5, int(storage_cap * 0.6))
        else:
            min_data_size_kb = max(host_total_mem * 2, storage_mem * 5, int(storage_cap * 0.1))
        
        
        min_data_size_r = convert_capacity(min_data_size_kb, readable=True)
        # 获取根据用户配置计算预埋数据量
        custom_data_size_kb = self._cal_custom_data_size()
        custom_data_size_r = convert_capacity(custom_data_size_kb, readable=True)
        if custom_data_size_kb < min_data_size_kb:
            raise ConfigError(f"Requires a data size larger than {min_data_size_r}. Current size: {custom_data_size_r}")

        logger.info(f"Estimated I/O data size: {custom_data_size_kb:,}KB ({custom_data_size_r})")

    def _cal_custom_data_size(self) -> int:
        """根据用户配置计算预埋数据量（KB）"""
        fsd_group_number = self.get_required_parameter("fsd_group_number")
        if not fsd_group_number.isdigit() or int(fsd_group_number) < 1:
            raise NumberTypeParamValueError("fsd_group_number", fsd_group_number)

        fsd_width = self.get_required_parameter("fsd_width")
        if not fsd_width.isdigit() or int(fsd_width) < 1:
            raise NumberTypeParamValueError("fsd_width", fsd_width)

        fsd_group_number = int(fsd_group_number)
        fsd_width = int(fsd_width)
        data_size = fsd_width ** VDBENCH_DEPTH * fsd_group_number * VDBENCH_FSD_GROUP_SIZE
        return data_size

    def load_cases(self):
        """加载用例配置"""
        # 根据 XML 配置文件中的 skip_case 的配置，跳过用例
        specified_skips = []
        param_skips = self.custom_suite_parameters.get("skip_cases")
        
        if param_skips:
            if not isinstance(param_skips, list):
                param_skips = [param_skips]
            specified_skips = list(set(param_skips))

        suite_major_cids = []
        major_cids_to_skip = {}

        # 定义核心逻辑函数 `_extend_cases`，后续会调用这个函数
        # 用于根据 XML 元素加载单个用例或用例组，跳过需要跳过的用例。
        # 然后通过  self._gen_cases() 生成一个需要执行的用例列表
        # 对于存在多个子用例的情况，使用 major_id 管理用例集合。
        # 最终的用例列表会存储在 self.cases 列表中。这些对象由 
        # basecase.py 中的 BaseCase 派生而来。
        # 其中， Suite._gen_cases() 函数用于生成 case 对象。
        # 
        # 其中 case_pattern_ele 这个是 load_cases 的变量，后面会定义。
        # 指 cases.xml 中对应场景下包含的 case。
        def _extend_cases(case_element: ET.Element, is_required: bool):
            cid = case_element.get('id')
            suite_major_cids.append(cid)
            try:
                # 从 XML 元素的 id 属性获取用例唯一标识。找出所有 case
                case_ele = case_pattern_ele.find(f"./case[@id='{cid}']")
                if case_ele is None:
                    raise SESError(f"case-id={cid} not found in resource")

                # 若用例在前面的 `specified_skips` 中，则跳过用例
                if cid in specified_skips:  # 跳过的用例只实例化基本信息
                    cases = self._create_dumb_cases(case_ele, is_required)
                    major_cids_to_skip[cid] = [c.cid for c in cases]
                else:
                    # 生成 case 对象
                    cases = self._gen_cases(case_ele, is_required)
                    
                # 用例分组：若一个主要用例（`major_id`）对应多个子用例，将其分组存储在 `cases_by_major_id` 字典中。
                if len(cases) > 1:
                    type_case = cases[0]
                    self.cases_by_major_id[type_case.major_id] = cases
                self.cases.extend(cases)
            except Exception as e:
                self.logger.error(f"Loading case [{cid}] error: {e}")
                self.logger.debug(traceback.format_exc())
                raise

        self.logger.info("Loading testcases")
        # 设置输出目录（output_dir），用于存储测试结果
        output = self.custom_config.find("output")
        if output is not None:
            self.output_dir = output.get("path")

        # 加载预定义的场景模板（SCENES_PATTERN_PATH）和用例模板（CASES_PATTERN_PATH）。
        # 这两个变量在 constants.py 文件中定义。
        scene_pattern = util.get_resource_path(constants.SCENES_PATTERN_PATH)
        case_pattern = util.get_resource_path(constants.CASES_PATTERN_PATH)

        scene_pattern_ele = ET.parse(scene_pattern)
        # 场景配置文件 scenes.xml 中对应（同名）的场景的 xml 元素。
        scene_ele = scene_pattern_ele.find(f"./scene[@name='{self.name}']")

        # 校验当前场景 scene_ele 是否存在，若不存在则抛出 ConfigError。如果存在，执行前面定义的 _extend_cases() 函数
        # case_pattern_ele 是该场景下包含的用例 scene_ele
        if scene_ele is None:
            raise ConfigError(f"'{self.name}' is not a valid scene")
        else:
            # 解析 cases.xml 中的根元素节点
            case_pattern_ele = ET.parse(case_pattern).getroot()
            util.nsstrip(case_pattern_ele) 
            # 找出该场景下包含的所有同名用例，然后执行用例
            for case_set in scene_ele:
                is_required = case_set.attrib.get("required", "").lower() == "true"
                # 根据用例的 tag 来判断是否并行执行用例。
                # 这个tag通过前面的util.nsstrip(case_pattern_ele) 来解析出来的。
                if case_set.tag == "p":
                    # 并行用例
                    # <p> 标签内的用例可能被标记为并行执行。
                    for case in case_set:
                        _extend_cases(case, is_required)
                elif case_set.tag == "case":
                    _extend_cases(case_set, is_required)

        # 检查要跳过的 cases 是否在配置文件中合法存在，并且在logger中记录要跳过的 cases
        self.handle_skips(specified_skips, suite_major_cids, major_cids_to_skip)
         # 把要 run 的 cases 记录到 logger 上
        to_run = [case.cid for case in self.cases if case.major_id not in self.valid_skips]
        msg = ",".join(to_run)
        self.logger.info(f"Loaded {len(to_run)} cases: {msg}")

    def handle_skips(self, specified_skips, suite_major_cids, major_cids_to_skip):
        valid_skips = specified_skips

        if specified_skips:
            valid_skips = [skip for skip in specified_skips if skip in suite_major_cids]
            invalid_skips = set(specified_skips) - set(valid_skips)
            if invalid_skips:
                msg = ",".join(invalid_skips)
                self.logger.warning(f"Value of parameter 'skip-cases' contains case(s) "
                                    f"that does not contain in scene '{self.name}': {msg}")
            if len(valid_skips) > 0:
                cid_list = [cid for _, cid_list in major_cids_to_skip.items() for cid in cid_list]
                skips_str = ",".join(cid_list)
                totals = len(cid_list)
                self.logger.warning(f"Cases to be skipped({totals}): {skips_str}")

        self.valid_skips = valid_skips

    def parse_parameters(self, parameters: Dict[str, str], source: dict = None):
        """解析并替换参数值"""
        result = {}
        for k, text in parameters.items():
            try:
                if text is None or text == "":
                    raise ConfigError(f"Value of parameter '{k}' is empty!")

                text = text.strip()
                vl = []
                for _v in text.split(","):
                    if "$" in text:
                        if text.startswith("$"):  # real_v：单个值
                            _, real_v = self.parse_custom_case_parameter(_v, source=source)
                        else:  # real_v：tuple
                            real_v = self.parse_custom_case_parameter(_v, source=source)
                    else:
                        real_v = _v
                    vl.append(real_v)

                if isinstance(vl[0], tuple):  # 存在引用（比如anchor_paths）需要合并client
                    parsed_v = {}
                    for client_list, v_list in vl:
                        for client, v in zip(client_list, v_list):
                            parsed_v.setdefault(client, []).append(v)
                elif len(vl) == 1:
                    parsed_v = vl[0]
                else:
                    parsed_v = vl

                result[k] = parsed_v
            except Exception:
                self.logger.error(f"Parsing parameter error: {k}={text}")
                raise
        return result

    def parse_custom_case_parameter(self, value: str, source: dict = None):
        """解析自定义用例参数

        Args:
            value: 参数值
            source: 引用源
        Returns:
            - 存在引用（value包含$）：client列表，参数值
            - 其他：None，参数值
        """
        try:
            if value.startswith("$"):
                param = value.strip("$")
                if param in source.keys():
                    return None, source[param]
                else:
                    return None, self.get_default_parameter(param)
            elif "$" in value:
                target, ref_name = value.split("$")
                if target.startswith("all"):
                    if target == "all_host":
                        target = ClientTarget.ALL_HOST
                    elif target == "all_storage":
                        target = ClientTarget.ALL_STORAGE
                    clients = self.get_executor_clients(target)
                else:
                    _target = {
                        "msh": ClientTarget.MASTER_STORAGE_HTTP,
                        "mss": ClientTarget.MASTER_STORAGE_SSH,
                        "mh": ClientTarget.MASTER_HOST
                    }.get(target, target)

                    clients = [self.get_client_by_role(_target)]

                clients_list, refs_list = [], []
                if not clients:
                    raise ConfigError(f"Parsing parameter value '{value}' failed, client role not exist: '{target}'")

                for client in clients:
                    clients_list.append(client)
                    refs_list.append(client.get_parameter(ref_name))
                return clients_list, refs_list
            else:
                return None, value
        except KeyError as e:
            raise ConfigError(f"Parsing parameter value '{value}' failed, parameter not exist: {e}")

    def get_default_parameter(self, name: str):
        """获取默认anchor_path

           Args:
               name: 默认参数的名称
        """
        value = None
        name = name.upper()
        if name == "ANCHOR_PATHS":
            value = self._get_default_anchor_path()
        elif name in DefaultParameter.__members__:
            return DefaultParameter.__getitem__(name).value
        return self.parse_parameters({"temp": value}).get("temp") if value else None

    def _get_default_anchor_path(self):
        """获取默认anchor_path
        """
        default_path = []

        hosts = self.custom_config.findall("environment/host")
        for host in hosts:
            for param in host.findall("parameter"):
                param_name = param.get("name")
                role = host.attrib.get("role")
                if param_name.startswith("anchor_path"):
                    default_path.append(f"{role}${param_name}")
                    break
        return ",".join(default_path)

    def load_client(self):
        """加载客户端。清理所有IO工具进程"""
        threads = []
        for env_element in self.custom_config.find("environment"):
            if env_element.tag in ["storage", "switch"]:
                continue
            t = threading.Thread(target=self._init_client, args=(env_element,), name="Main")
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        roles = [client.role for client in self.client_group]
        dups = [x for x in set(roles) if roles.count(x) > 1]
        if dups:
            raise ConfigError("Duplicated roles: {}".format(",".join(dups)))

        defined_hn = len(self.custom_config.findall("environment/host"))
        if defined_hn == 0:
            raise ConfigError("Requires environment info: host")
        try:
            hosts = self.get_executor_clients(ClientTarget.ALL_HOST)
        except Exception:
            raise EnvironmentValidationError("No available hosts")

        if len(hosts) != defined_hn:
            self.status = SuiteStatus.COMPLETED
            raise EnvironmentValidationError(f"Client preparation failed")

        self.status = SuiteStatus.PREPARED
        self.logger.info(f"All clients prepared, total number: {defined_hn}")
        self._prepare_tools()

    def run(self):
        """执行"""
        self.logger.info(f"Running scene: {self.name}")
        self.logger.info(f"Report and logs: {self.output_dir}")

        self.load_client()
        self.update_custom_parameters()  # 先加载Client后校验自定义参数
        self.load_cases()
        self.monitor()
        self.report = Report(self)
        # 根据场景判断是否需要输入基线能耗
        if self.name in ["AI_EXP"]:
            self.request_base_iorate()
        
        for index, case in enumerate(self.cases):
            runner = CaseRunner(self, case)
            self._running_case_runner = runner
            runner.start()
            runner.join()

            # 最后一个用例
            if index >= len(self.cases) - 1:
                continue

            if case.result != CaseResult.PASS:
                continue

            # 后面的用例全部被跳过
            pending_cids = [c.cid for c in self.cases[index + 1:]]
            if set(pending_cids).issubset(set(self.valid_skips)):
                continue

            mins = constants.CASE_RUN_INTERVAL // 60
            self.logger.info(f"Start the next case after {mins} mins")
            time.sleep(constants.CASE_RUN_INTERVAL)

        self.status = SuiteStatus.COMPLETED
        self.logger.info(f"Scene [{self.name}] Test completed")
        self.logger.info(f"Report: {self.report.path}")
        self.report.finish()

    def request_base_iorate(self):
        """ 请求输入基线iorate """
        msg = "Please enter the baseline performance OPS of case PERF_002"
        value = util.request_user_input(msg=msg, validator=util.is_number, ignore_timeout=True)
        if util.is_number(value):
            self.cache_suite_runtime_data(CacheDataKey.AVG_OPS_BENCHMARK, round(float(value), 2))
        else:
            self.logger.warning(f"Invalid base OPS value: {value}. Result will be ignored")

    def get_executor_clients(self, target: ClientTarget) -> List[Client]:
        """获取执行步骤对应的环境"""
        if target in util.CLIENT_TARGET_MAP:
            result = util.CLIENT_TARGET_MAP[target](self.client_group)
            if not result:
                raise SESError(f"Requires environment definition: {target}")
            return result
        else:
            raise SESError(f"Unsupported target: {target}")

    def get_client_by_role(self, role):
        """根据role名获取Client"""
        for c in self.client_group:
            if c.role == role:
                return c
        raise ConfigError(f"No client matched role={role}")

    def monitor(self):
        """信号监听"""

        def foo():
            while self.status != SuiteStatus.COMPLETED:
                if self.error_stop:
                    self.stop_cases()
                    break

                with open(self._monitor_file, "a+", encoding='utf-8') as file:
                    file.seek(0, 0)
                    text = file.read().lower()
                    if "stop" in text:
                        self.logger.warning("Detected stop in monitor file, stopping execution")
                        self.status = SuiteStatus.STOP
                        self.stop_cases()
                        self.status = SuiteStatus.COMPLETED
                    elif "pause" in text:
                        self.status = SuiteStatus.PAUSE
                        self.stop_cases()
                    elif "restart" in text and self.status == SuiteStatus.PAUSE:
                        self.status = SuiteStatus.PREPARED
                    file.truncate(0)

                time.sleep(2)

        t = threading.Thread(target=foo, name="SESMonitor")
        t.daemon = True
        t.start()
        self.logger.info(f"Fill in monitor-file to stop the test at any time ({self._monitor_file})")

    def stop_cases(self):
        self._running_case_runner.stop()

    def get_required_parameter(self, param_name):
        """获取必要的通用参数/scene参数"""
        try:
            return self.custom_suite_parameters[param_name]
        except KeyError as e:
            raise SceneParameterNotFound(str(e))

    @exception_wrapper
    def _init_client(self, env_element: ET.Element):
        self.logger.info(f"Connecting client: {env_element.get('ip')}")
        protocol = _protocol = env_element.get("protocol")
        if protocol == "ssh":
            builder = SSHClientBuilder(env_element)
        else:
            raise SESError(f"Unsupported client protocol: {protocol}")

        client = builder.create_client()
        self.logger.info(f"Client connected successfully: {client}")
        if client.role == ClientTarget.MASTER_HOST:
            self.master_host = client
        self.client_group.append(client)

    def _prepare_tools(self):
        """校验客户端上是否安装了必要工具，并清理工具执行进程

        - 正常情况下主机应存在`env_name`环境变量，值为工具安装目录（安装脚本中处理）
          校验该目录下是否存在`executable`
        - 若不存则使用默认目录
          校验 TOOL_BASE_DIR_<OS>/`default_dir_name`目录下是否存在 `executable`
        """
        failure = []
        for client in self.get_executor_clients(ClientTarget.ALL_HOST):
            client: SSHClient
            client_failure = []
            if not client.command_exists("iostat"):
                client_failure.append("sysstat")
            else:
                client.pkill("iostat")

            for tool_name, info in constants.TOOL_VALIDATE_DICT.items():
                tool_success = False
                tool_path = client.get_env(info.env_name)
                if tool_path and client.exists(client.join_path(tool_path, info.executable)):
                    tool_success = True
                else:
                    if "windows" in client.__class__.__name__.lower():
                        tool_base_dir = constants.TOOL_BASE_DIR_WIN
                    else:
                        tool_base_dir = constants.TOOL_BASE_DIR_UNIX

                    tool_path = client.join_path(tool_base_dir, info.default_dir_name)
                    filepath = client.join_path(tool_path, info.executable)
                    if client.exists(filepath):
                        tool_success = True

                if not tool_success:
                    client_failure.append(f"{tool_name}({tool_path})")

                # 以master客户端的工具目录为准
                if client.role == ClientTarget.MASTER_HOST:
                    self.tools_dir[tool_name] = tool_path

                # 根据进程名清理所有工具
                client.kill_process(keywords=info.executable)

            if client_failure:
                tool_str = ",".join(client_failure)
                failure.append(f"role: {client.role}, requires tool: {tool_str}")

        if failure:
            fstr = ",".join(failure)
            if self.ignore_toolcheck_error:
                self.logger.warning(f"Client tools validation failed (ignored):\n{fstr}")
            else:
                self.logger.error(f"Client tools validation failed:\n{fstr}\n"
                                  "Use '--ignore-toolcheck-error' when start SES if "
                                  "there are clients that don't need dependent tools")
                raise EnvironmentValidationError("Tools validation failed")
        else:
            self.logger.info("Tools validation success")

    def validate_storage_specification(self): 
        """校验存储规格"""
        spec = SimpleNamespace(total_disk_num=None, single_disk_capacity=None, total_node_num=None)
        for param in spec.__dict__:
            value = self.get_required_parameter(param)
            try:
                value = int(value)
                if value < 1:
                    raise NumberTypeParamValueError(param, value)
            except ValueError:
                if param == "single_disk_capacity":
                    try:
                        value = util.convert_capacity(value, target_unit="KB")
                    except:
                        raise ConfigError(f"Invalid single_disk_capacity value={value}")
                else:
                    raise NumberTypeParamValueError(param, value)
            setattr(spec, param, value)

        self.cache_suite_runtime_data(CacheDataKey.TOTAL_DISK_NUM, spec.total_disk_num)
        self.cache_suite_runtime_data(CacheDataKey.SINGLE_DISK_CAPACITY, spec.single_disk_capacity)
        self.cache_suite_runtime_data(CacheDataKey.TOTAL_NODE_NUM, spec.total_node_num)

        capacity_kb = spec.total_disk_num * spec.single_disk_capacity
        readable_single_disk_capacity = convert_capacity(spec.single_disk_capacity, readable=True)
        readable_capacity = convert_capacity(capacity_kb, readable=True)
        self.cache_suite_runtime_data(CacheDataKey.STORAGE_CAPACITY, capacity_kb)

        msg = f"Storage Disk-num={spec.total_disk_num}, single-disk-capacity={spec.single_disk_capacity:,}KB " \
              f"({readable_single_disk_capacity}). Physical capacity={readable_capacity}"
        if self.name != "QQ_PHOTO_ALBUM" and capacity_kb < MIN_STORAGE_CAPACITY:
            required_capacity = convert_capacity(MIN_STORAGE_CAPACITY, readable=True)
            msg += f". Minimum capacity requirements not met: {required_capacity}"
            raise ConfigError(msg)

        self.logger.info(msg)

    def _gen_cases(self, case_ele: ET.Element, required: bool) -> list:
        """生成用例"""
        cases = []
        cid = case_ele.get("id")

        # 合并自定义参数
        custom_case_parameters = self.custom_suite_parameters.copy()
        custom_case_parameters.update(**self.custom_case_parameters.get(cid, {}))
        basic_info = dict(
            config_element=case_ele,
            custom_case_parameters=custom_case_parameters,
            required=required
        )
        # 读取 cases.xml 中的相应配置信息
        case_static_parameters = {}
        for p in case_ele.findall("./parameters/parameter"):
            pname = p.get("name")
            case_static_parameters[pname] = p.text

        case_class = self.find_case_class_by_name(case_ele)
        # cases.xml 中可以通过 dataset 关键词来自定义测试数据。
        if case_ele.find("dataset") is not None:
            for data in case_ele.findall("./dataset/data"):
                _static_parameters = case_static_parameters.copy()
                _static_parameters.update(**{p.get("name"): p.text for p in data.findall("./parameter")})

                # 引用用户自定义参数
                _static_parameters = self.parse_parameters(_static_parameters, source=custom_case_parameters)
                _info = basic_info.copy()
                _info.update(static_parameters=_static_parameters,
                             sub_id=data.get("id"),
                             sub_name=data.get("name"),
                             )
                case = case_class(self, **_info)
                cases.append(case)
        else:
            case_static_parameters = self.parse_parameters(case_static_parameters, source=custom_case_parameters)
            case = case_class(self, static_parameters=case_static_parameters, **basic_info)
            cases.append(case)
        return cases

    def cache_runtime_data(self, case_id, key: CacheDataKey, value):
        """保存用例执行数据"""
        if not isinstance(key, CacheDataKey):
            raise TypeError("cache_case_runtime_data: key must be type of CacheDataKey")
        if value is None:
            self.logger.debug(f"cache value must not be None: {key}")

        if case_id not in self.cache:
            self.cache[case_id] = {}

        self.cache[case_id][key.value] = value
        self.logger.debug(f"Cached runtime data: case-id[{case_id}] {key}={value}")

    def cache_suite_runtime_data(self, key: CacheDataKey, value):
        """保存执行数据"""
        if value is None:
            self.logger.debug(f"cache value must not be None: {key}")

        if "suite" not in self.cache:
            self.cache["suite"] = {}

        self.cache["suite"][key.value] = value
        self.logger.debug(f"Cached suite runtime data: {key}={value}")

    def get_cache_data(self, key: CacheDataKey, case_id=None):
        """获取保存的用例数据

        Args:
            key: 数据key
            case_id: 用例id。为None时按key值匹配，返回第一个数据
        Raises:
            KeyError:
                - cache中不存在对应case_id
                - cache中不存在key对应的数据
        """
        if not isinstance(key, CacheDataKey):
            raise TypeError("get_cache_data: key must be type of CacheDataKey")

        if case_id:
            try:
                ckv = self.cache[case_id]
            except KeyError:
                raise KeyError(f"case_id={case_id} not found in cache data")

            try:
                return ckv[key.value]
            except KeyError:
                raise KeyError(f"key={key} not found in cache data of case {case_id}")

        else:
            for _, kv in self.cache.items():
                if key.value in kv:  # 这里可能是suite/case
                    return kv[key.value]
            raise KeyError(f"key={key} not found in cache data")

    def skip_all(self, log_message, report_message=None):
        """跳过后续所有用例

        Args:
            log_message: 日志打印信息
            report_message: 后续跳过用例在报告中展示的信息
        """
        self.logger.error(f"Skip all pending testcases. Reason: {log_message}")
        self.skip_report_message = report_message
        self.all_skipped = True

    @staticmethod
    def find_case_class_by_name(case_definition) -> type:
        """通过id查询用例类"""
        category = case_definition.get("category")
        cid = case_definition.get("id")
        path = os.path.join(util.project_root(), "testcases", category)
        module_name = None
        for item in os.listdir(path):
            file = os.path.join(path, item)
            if os.path.isfile(file) and item.replace(".py", "") == cid.lower():
                module_name = item
                break

        if not module_name:
            raise SESError(f"Cannot find {category} case: {cid}")

        case_file = os.path.join(path, module_name)
        spec = importlib.util.spec_from_file_location(module_name, case_file)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        for name, obj in inspect.getmembers(module):
            if inspect.isclass(obj) and issubclass(obj, BaseCase) and obj.__module__ == module_name:
                return obj

        raise SESError(f"No BaseCase class in: {case_definition.get('id')}")

    def _create_dumb_cases(self, case_element: ET.Element, is_required: bool):
        """创建不执行用例"""
        cases = []
        if case_element.find("dataset") is not None:
            for data in case_element.findall("./dataset/data"):
                cases.append(BaseCase(self,
                                      case_element,
                                      dict(), dict(),
                                      is_required,
                                      sub_id=data.get("id"),
                                      sub_name=data.get("name"),
                                      ))
        else:
            cases.append(BaseCase(self,
                                  case_element,
                                  dict(), dict(),
                                  is_required))
        return cases


class CaseRunner(threading.Thread):

    def __init__(self, suite, case: BaseCase):
        super().__init__()
        self.suite = suite
        self.case = case
        self.logger = case.logger

    @property
    def name(self):
        """for logger name"""
        return self.case_id

    @property
    def case_id(self):
        return self.case.cid

    @property
    def case_result(self):
        return self.case.result

    @exception_wrapper
    def run(self):
        self._update_status()
        err = None
        start_time = time.time()
        
        if self.case_result != CaseResult.SKIPPED:
            logger.info(f"Running case: [{self.case_id}] {self.case.cname}")
            self._status = CaseStatus.RUNNING
            pre_condition_passed = False
            try:
                self.case.pre_condition()
                pre_condition_passed = True
                if self.case_result != CaseResult.FAILED:
                    self.case.procedure()
            except NotImplementedError as e:
                err = e
                tb = traceback.extract_tb(e.__traceback__)
                last_trace = tb[-1]
                self.logger.error(f"未实现必选接口: {last_trace.name}")
            except Exception as e:
                err = e
                self.logger.debug(traceback.format_exc())

            if pre_condition_passed:
                try:
                    self.case.post_condition()
                except NotImplementedError as e:
                    err = e
                    tb = traceback.extract_tb(e.__traceback__)
                    last_trace = tb[-1]
                    self.logger.error(f"未实现必选接口: {last_trace.name}")
                except Exception as e:
                    err = e
                    self.logger.debug(traceback.format_exc())

        if err:
            self.case.fail(message="执行异常，请查看日志", exception=err)
        elif self.case.result not in [CaseResult.FAILED, CaseResult.SKIPPED]:
            self.case.result = CaseResult.PASS

        self.handle_report(start_time)
        self._complete()

    def handle_report(self, start_time):
        content = None
        indicators = None
        self.logger.debug("Handle report")
        if self.case_result != CaseResult.SKIPPED:
            try:
                content = self.case.make_report()
                indicators = self.case.register_indicator()
            except:
                self.case.fail(message="报告数据生成异常")
                self.logger.debug(f"handle_report error: {traceback.format_exc()}")

        if self.case.result_messages:
            self.case.result_messages = list(set(self.case.result_messages))

        self.suite.report.handle_case_report(self.case_id,
                                             self.case_result,
                                             start_time,
                                             messages=self.case.result_messages,
                                             records=self.case.records,
                                             content=content
                                             )
        self.suite.report.insert_summary_table(self.case_id, indicators=indicators)

    def skip(self, message=None):
        """标记用例为跳过"""
        self.case.result = CaseResult.SKIPPED
        self.case.add_report_message(message)

    def stop(self):
        self.case._status = CaseStatus.WAITING

    def _complete(self):
        self.logger.info(f"Case [{self.case_id}] completed ({self.case_result})")
        self._status = CaseStatus.COMPLETED

        if self.case_result == CaseResult.FAILED:
            self.suite.skip_all(f"case [{self.case_id}] failed",
                                f"用例 [{self.case.cname}] 执行失败，测试终止"
                                )

    def _update_status(self):
        if self.suite.all_skipped:
            self.skip(message=self.suite.skip_report_message)
        else:
            for c in self.suite.valid_skips:
                if c in self.case_id:
                    self.skip(message=f"此用例在配置中被指定跳过")
                    break
