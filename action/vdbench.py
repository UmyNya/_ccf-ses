# -*- coding: UTF-8 -*-
import copy
from dataclasses import fields
from datetime import datetime
import datetime as dt
from enum import Enum
from itertools import groupby
from operator import itemgetter
import os.path
import re
import socket
from statistics import mean
import time
import traceback
import math
from typing import List, Optional, Union, Dict, Callable

import pandas as pd

from storage_evaluation_system_zzj import constants
from storage_evaluation_system_zzj.action.host import LinuxAction, WindowsAction, HostAction
from storage_evaluation_system_zzj.action.io_tool import IOTool
from storage_evaluation_system_zzj.client.ssh_client import SSHClient, LinuxClient, WindowsClient
from storage_evaluation_system_zzj.constants import ClientTarget, CaseCategory, CacheDataKey
from storage_evaluation_system_zzj.exception import SESError, CaseFailedError
from storage_evaluation_system_zzj.indicator import Bandwidth, Ops
from storage_evaluation_system_zzj.report import ReportUtil, TitledContent
from storage_evaluation_system_zzj.util import wait_for, WaitResult, get_resource_path, \
    replace_win_sep, convert_capacity, IostatData


class VdbenchState(int, Enum):
    INIT = 0
    FORMAT = 1
    DONE_FORMAT = 2
    RD = 3
    COMPLETE = 4


class VdbenchIO(IOTool):
    """
    vdbench工具基类

    子类为各个OS下vdbench操作实现，并实现和OS依赖的所有函数，如`is_running`。见所有raise NotImplementedError的函数

    如果要收集iostat数据，确保在用例中调用 `stop`
    """
    client: SSHClient
    client_type = SSHClient

    def __init__(self, case, target_client=ClientTarget.MASTER_HOST, **kwargs):
        super().__init__(case, target_client=target_client, **kwargs)
        self.host: HostAction = None
        self.executable = None  # 可执行文件，windows为vdbench.bat，linux为vdbench
        self._executable_path = None  # 可执行文件完成路径
        self.anchor_paths = None
        self.format_status = None  # 预埋标志
        self.pid = None  # 正式执行pid，不包含预埋
        self.elapsed = constants.VDBENCH_ELAPSED
        self.resource_dir = get_resource_path(constants.IO_MODEL_PARAM_PATH)
        self._reporter = None
        self.clean = True
        self.is_perf_case = self.case.category.upper() == CaseCategory.PERFORMANCE.name
        self.capacity = convert_capacity(self.case.get_cache_data(CacheDataKey.STORAGE_CAPACITY), target_unit="TB",
                                         n_digits=2)
        self.status = VdbenchState.INIT
        self.iostat_data: Dict[HostAction, dict] = {}
        self.to_print_data_structure = True
        self.root_output_dir = self.output_dir

    @property
    def executable_path(self):
        return self._executable_path

    @executable_path.setter
    def executable_path(self, value):
        self._executable_path = value

    @property
    def reporter(self):
        """必须在 prepare 执行之后调用"""
        if not self._reporter:
            self._reporter = VdbenchReporter(self)
        return self._reporter

    @property
    def sep(self):
        raise NotImplementedError

    def get_summary_html(self, output_dir: str = None):
        return self.get_output_file_path("summary.html", output_dir)

    def get_logfile_html(self, output_dir: str = None):
        return self.get_output_file_path("logfile.html", output_dir)

    def get_errorlog_html(self, output_dir: str = None):
        return self.get_output_file_path("errorlog.html", output_dir)

    def get_output_file_path(self, filename, output_dir: str = None):
        if not output_dir:
            output_dir = self.output_dir
        return self.client_type.join_path(output_dir, filename)

    @property
    def summary_html(self):
        raise NotImplementedError

    @property
    def logfile_html(self):
        raise NotImplementedError

    @property
    def errorlog_html(self):
        raise NotImplementedError

    @property
    def prepare_output_dir(self):
        raise NotImplementedError

    @staticmethod
    def get_stage_keyword(stage):
        """对应阶段在summary.html中的查询关键字"""
        if stage == VdbenchState.RD:
            return "elapsed="
        elif stage == VdbenchState.FORMAT:
            return "RD=format"
        else:
            raise ValueError(f"Invalid stage: {stage}")

    def get_status_message(self, elapsed=None, status=None):
        """获取状态对应日志"""
        elapsed = elapsed or self.elapsed
        status = status or self.status

        return {
            VdbenchState.INIT: "File I/O: Initiating",
            VdbenchState.FORMAT: "File I/O: Preparing files",
            VdbenchState.DONE_FORMAT: "File I/O: File-preparation completed",
            VdbenchState.RD: f"File I/O: Generating workload: {elapsed}s",
            VdbenchState.COMPLETE: "File I/O: Completed"
        }.get(status)

    def get_mon_file(self):
        """vdbench监控文件"""
        return self.client_type.join_path(self.output_dir, constants.VDBENCH_MON_FILE)

    def run(self, vdbench_dir: str, config_template_file: str, anchor_paths: dict, fsd_group_number: int,
            fsd_width: int, multiple: int, threads_config: int = None, elapsed: int = None, timeout=None,
            wait=True, format_status="restart", capacity=None, fwdrate="max", dir_depth: int = 1):
        """
        后台运行vdbench

        :param vdbench_dir: vdbench安装目录
        :param config_template_file: 配置模板文件
        :param anchor_paths: vdbench的挂载点 [master$mount_point,..,]
        :param fsd_width: fsd目录宽度
        :param fsd_group_number: fsd组的数量
        :param threads_config: 最佳线程数
        :param multiple: 线程的倍数
        :param elapsed: vdbench运行时长（s）
        :param timeout: 工具最长等待时长（s）
        :param wait: 是否等待vdbench执行完成并返回结果
        :param format_status: 指定format方式,默认为restart
        :param capacity: 存储容量（TB）
        :param fwdrate: 每秒钟的操作数
        :param dir_depth: 构造目录深度，默认为1
        :return:
        """
        self.set_output_dir()
        self.output_dir = self.host.join_path(self.output_dir, "test")
        self.anchor_paths = anchor_paths
        self.executable_path = f"{vdbench_dir}{self.sep}{self.executable}"
        config_file = self.prepare(vdbench_dir,
                                   config_template_file,
                                   anchor_paths,
                                   fsd_width,
                                   fsd_group_number,
                                   threads_config,
                                   multiple,
                                   elapsed,
                                   format_status,
                                   fwdrate,
                                   dir_depth)

        cmd = f"{self.executable_path} -f {config_file} -o {self.output_dir} -w {constants.VDBENCH_WARMUP}"
        if self.is_perf_case:
            self.client_drop_caches()
            self.client_mount()
            self.start_iostat()

        self.pid = self._run_vdbench_cmd(config_file, self.output_dir, f"-w {constants.VDBENCH_WARMUP}")
        if self.pid is None:
            return False

        if wait:
            return self.wait_for_complete(timeout=timeout)
        else:
            self.logger.debug(f"Vdbench will be running in background (pid={self.pid})")

    def start_iostat(self):
        """所有主机启动iostat"""
        self.logger.debug(f"Starting iostat")
        elapsed = self.elapsed + 60
        for client in self.anchor_paths.keys():
            host = self.host.__class__(self.case, client=client)
            host.mkdir(self.output_dir)
            rstr = time.time_ns()
            log_path = self.client.join_path(self.output_dir, f"{self.case.cid}_iostat_{rstr}.log")
            pid = host.start_iostat(log_path, interval=constants.IOSTAT_INTERVAL, elapsed=elapsed)
            self.iostat_data[host] = dict(log_path=log_path, pid=pid, data=None)
            self.logger.debug(f"[{client.role}] iostat started, pid={pid}, log={log_path}")
        self.logger.debug(f"iostat started")

    def stop(self, wait=True, force_kill=True):
        """安全停止，向vdbench-monitor文件写入结束标志，并收集iostat数据

        Args:
            wait: 是否等待vdbench进程结束（60秒）
            force_kill: 若安全停止后，检查主机上仍有在运行的vdbench进程，则使用pid再次kill
        """

        self.host.create_file(self.get_mon_file(), content="end_vdbench")

        if self.pid:
            if wait:
                start = time.time()
                while time.time() - start < 60:
                    if not self.host.is_pid_exists(self.pid):
                        break
                    time.sleep(2)

            if force_kill:
                # 主机清理
                self.host.kill_process(self.pid)
                time.sleep(5)
                self.clean_slaves()  # 清理slave
            self.status = VdbenchState.COMPLETE

        self.logger.debug(f"vdbench stopped: {self.pid}")

        if self.iostat_data:
            try:
                self.handle_iostat_data()
            except:
                self.logger.debug(f"Collecting iostat data error: {traceback.format_exc()}")

    def clean_slaves(self):
        """清理slave中的所有vdbench进程"""
        self.logger.debug("Clean slave process")
        for client in self.anchor_paths.keys():
            client.kill_process(keywords="vdbench.jar")

    def handle_iostat_data(self):
        """处理iostat数据"""
        self.logger.debug("Collecting iostat data")
        local_dir = os.path.join(self.root_output_dir, "iostat")
        os.makedirs(local_dir, exist_ok=True)
        # 取回所有主机的日志，解析数据
        host_data_dict = {f.name: [] for f in fields(IostatData)}
        for host, info in self.iostat_data.items():
            client_role = host.client.role
            local_path = os.path.join(local_dir, client_role + "_iostat.log")
            data = host.collect_iostat_data(info["pid"], info["log_path"], local_path)
            if not data:
                self.logger.debug(f"Collecting iostat data failed on client: {client_role}({host.client.ip})")
                continue
            else:
                self.logger.debug(f"{client_role}({host.client.ip}): {data}")
            for k, v in vars(data).items():
                host_data_dict[k].append(v)

        f_results = []
        for field, host_values in host_data_dict.items():
            f_results.append(f"{field}: {round(mean(host_values), 3)}")
        f_str = ", ".join(f_results)
        self.logger.info(f"Avg disk I/O: {f_str}")

    def is_running(self):
        """ 检查vdbench进程是否在运行中 """
        raise NotImplementedError

    def _is_complete_successfully(self, pid=None, output_dir=None) -> Optional[bool]:
        """判断当前执行是否正常结束

        Returns:
            True: vdbench执行正常完成
            False: vdbench执行异常结束
            None: vdbench仍在执行中
        """
        raise NotImplementedError

    def wait_for_complete(self, pid=None, timeout=None, interval=10, update_status=True,
                          custom_func: Callable = None, custom_func_parameter=None, output_dir=None) -> bool:
        """
        等待vdbench执行完成，并返回执行是否正常结束

        Args:
            pid: vdbench进程号
            output_dir: 进程输出目录
            timeout: 持续检查等待超时时间
            interval: 检查vdbench进程的间隔时间
            update_status: 是否更新状态。仅正式文件操作（rd)才使用True
            custom_func: 自定义可调用函数
            custom_func_parameter: 自定义可调用函数参数
        """
        if not timeout:
            timeout = constants.VDBENCH_EXEC_TIMEOUT

        if not pid:
            pid = self.pid

        @wait_for(True,
                  fail_fast=False,
                  timeout=timeout,
                  interval=interval,
                  msg="Wait for file I/O to complete")
        def check():
            if update_status:
                self.update_status()
            if custom_func:
                custom_func(**custom_func_parameter)
            return self._is_complete_successfully(pid, output_dir)

        result = check()
        proc_end = False
        if result == WaitResult.OK:
            # 等待进程结束
            self.logger.debug("Vdbench execution completed successfully, waiting for process to end")
            start = time.time()
            while time.time() - start < constants.VDBENCH_PROC_END_TIMEOUT:
                if not self.host.is_pid_exists(pid):  # 其他
                    proc_end = True
                    break

                time.sleep(5)

            if not proc_end:
                self.logger.debug("Vdbench execution completed successfully with process hanging. Force stop")
                self.stop(wait=False)

        return result

    def update_status(self):
        """更新当前运行状态，并打印一次状态变更信息"""
        if self.status == VdbenchState.COMPLETE:
            return

        to_print = False
        if self.status == VdbenchState.RD:
            if not self.is_running():
                to_print = True
                self.status = VdbenchState.COMPLETE
        elif self.status == VdbenchState.FORMAT:
            if self.is_stage_start(VdbenchState.RD):
                to_print = True
                self.create_tag_file()
                self.status = VdbenchState.RD
        elif self.is_stage_start(VdbenchState.FORMAT):
            # format在前期已经完成，不再打印format状态
            self.status = VdbenchState.FORMAT

        if to_print:
            self.logger.info(self.get_status_message())

    def print_data_structure(self, log_file=None, output_dir=None):
        """打印数据结构与数据量"""
        try:
            if self.is_stage_start(VdbenchState.FORMAT, output_dir) and self.to_print_data_structure:
                if not log_file:
                    log_file = self.get_logfile_html(output_dir)
                resp = self.host.search_content(log_file, "Estimate")
                if not resp.stdout:
                    return

                # 多个anchor时vdbench会汇总打印totals；否则说明只有一个
                totals = re.findall(".*Estimated totals.*", resp.stdout)
                message = totals[0] if totals else resp.stdout
                message = message.split(maxsplit=1)[1]  # 去掉时间戳
                self.logger.info(f"File I/O structure: {message}")
                self.to_print_data_structure = False
        except:
            self.logger.debug(traceback.format_exc())

    def wait_until_stop(self, timeout=None, interval=10) -> bool:
        """
        等待vdbench进程结束，要求子类实现 ''is_running''方法

        Args:
            timeout: 持续检查等待超时时间
            interval: 检查vdbench进程的间隔时间

        """
        if not timeout:
            timeout = constants.VDBENCH_EXEC_TIMEOUT

        @wait_for(False,
                  timeout=timeout,
                  interval=interval,
                  msg="Waiting for vdbench to stop")
        def check():
            return self.is_running()

        return check() == WaitResult.OK

    def get_last_log_time(self) -> datetime:
        """获取vdbench最后的在线时间"""
        last_line = self.host.get_last_line(self.get_output_file_path("flatfile.html"))
        ts = last_line.split()[0]
        return self._reporter.ts2datetime(ts)

    def wait_for_stage_start(self, stage=VdbenchState.RD, timeout=None, interval=10, delay=None) -> bool:
        """ 等待vdbench启动、并达到预期阶段

        Args:
            stage: 预期阶段，有效值：rd/format
                rd：开始执行rd即返回
                format：开始预埋即返回
            timeout: 等待时长（秒）。默认：constants.VDBENCH_EXEC_TIMEOUT
            interval: 轮询间隔（秒）
            delay: 检测达到预期到，延迟指定时间后退出

        Raises:
            RuntimeError: vdbench已停止运行，或exit_on_error=True时检测到vdbench异常日志

        Returns:
            vdbench是否达到预期阶段
        """
        if stage == VdbenchState.RD:
            msg = "Waiting for file I/O operations to start"
        else:
            msg = "Waiting for file I/O data-preparation to start"

        if timeout is None:
            timeout = constants.VDBENCH_EXEC_TIMEOUT

        _ise = False
        _iee = False

        @wait_for(True, timeout=timeout, interval=interval, msg=msg)
        def check():
            nonlocal _ise, _iee
            if not self.is_running():
                raise RuntimeError(f"File I/O has stopped with error, check log for details: {self.logfile_html}")

            if not _ise:
                if not self.host.exists(self.summary_html):
                    return False
                _ise = True

            # 检查日志文件是否已开始输出读写数据
            self.update_status()

            is_success = self._is_complete_successfully()
            if is_success is False:
                raise RuntimeError("File I/O has stopped with error")

            return self.status >= stage

        result = check() == WaitResult.OK
        if result and delay:
            time.sleep(delay)

        return result

    def generate_param_file(self, **kwargs):
        """
        在指定目录下生成配置文件，并校验
        :return:
        """
        try:
            return self.parsing_configuration_file(**kwargs)
        except Exception:
            self.logger.error("Generating file I/O workload model config error")
            raise

    def prepare(self,
                vdbench_dir: str,
                config_template_file: str,
                anchor_paths: dict,
                fsd_width: int,
                fsd_group_number: int,
                threads_config: int,
                multiple: int,
                elapsed,
                format_status,
                fwdrate,
                dir_depth):
        """
        准备vdbench运行命令

        :param vdbench_dir: vdbench安装目录
        :param config_template_file: 配置模板文件
        :param anchor_paths: vdbench挂载点
        :param fsd_width: fsd目录宽度
        :param fsd_group_number: fsd组的数量
        :param threads_config: 最佳线程数
        :param multiple: 线程倍数
        :param elapsed: vdbench运行时长（s）
        :param format_status: 预埋规则
        :param fwdrate: 每秒钟的操作数
        :param dir_depth: 构造目录深度，默认为1
        :return:
        """
        config_file = self.generate_param_file(vdbench_dir=vdbench_dir,
                                               config_template_file=config_template_file,
                                               anchor_paths=anchor_paths,
                                               fsd_width=fsd_width,
                                               fsd_group_number=fsd_group_number,
                                               threads_config=threads_config,
                                               multiple=multiple,
                                               elapsed=elapsed,
                                               format_status=format_status,
                                               fwdrate=fwdrate,
                                               dir_depth=dir_depth)
        if not self.clean:
            return config_file

        clean_dir = self.host.join_path(self.case.output_dir, "clean")
        self.host.mkdir(clean_dir)
        clean_file = self.host.join_path(clean_dir, "clean.txt")
        self.host.copy(config_file, clean_file)
        self.host.replace_content(clean_file, "format=\(.*\)", "format=clean")
        # 执行清除程序
        self.logger.debug("Do clean")
        clean_pid = self._run_vdbench_cmd(clean_file, clean_dir)
        self.wait_for_complete(pid=clean_pid, update_status=False, output_dir=clean_dir)

        # 执行预埋程序
        self.status = VdbenchState.FORMAT
        self.logger.info(self.get_status_message())
        prepare_dir = self.prepare_output_dir
        self.host.mkdir(prepare_dir)
        prepare_file = self.host.join_path(prepare_dir, "prepare.txt")
        self.host.copy(config_file, prepare_file)
        self.host.replace_content(prepare_file,
                                  "elapsed=\(.*\)",
                                  f"elapsed={constants.VDBENCH_ELAPSED_PRE}")
        prepare_pid = self._run_vdbench_cmd(prepare_file, prepare_dir)
        timeout = constants.VDBENCH_EXEC_TIMEOUT

        custom_func = self.print_data_structure
        result = self.wait_for_complete(pid=prepare_pid,
                                        timeout=timeout,
                                        update_status=False,
                                        custom_func=custom_func,
                                        custom_func_parameter=dict(output_dir=prepare_dir),
                                        output_dir=prepare_dir)
        if result == WaitResult.OK:
            self.create_tag_file()
            self.status = VdbenchState.DONE_FORMAT
            self.logger.info(self.get_status_message())
        elif result == WaitResult.TIMEOUT and not self.is_stage_start(VdbenchState.RD, prepare_dir):
            _elapsed = dt.timedelta(seconds=timeout)
            raise CaseFailedError(f"File-preparation timeout: {_elapsed}")
        else:
            prepare_log = self.host.join_path(prepare_dir, "logfile.html")
            raise CaseFailedError(f"File-preparation failed, check log for details: {prepare_log}")

        return config_file

    def parsing_configuration_file(self, vdbench_dir, config_template_file, anchor_paths, fsd_width: int,
                                   fsd_group_number: int, threads_config: int, multiple: int, format_status, fwdrate,
                                   dir_depth: int = 1, elapsed=None):
        # 1. copy模板文件
        if self.client_type == WindowsClient:
            config_template_file = replace_win_sep(config_template_file)
        self.host.mkdir(self.output_dir)
        config_file = self.host.copy(f"{self.resource_dir}{config_template_file}", self.output_dir)

        # 2. 配置vdbench安装路径、线程数、文件数与执行时间
        # 2.0 插入配置：容忍错误次数、自动创建anchor、不打印系统消息、安全停止
        self.host.insert_text_to_file(config_file,
                                      f"data_errors=1,create_anchors=yes,messagescan=no,monitor={self.get_mon_file()}")

        # 2.1 配置脚本线程数：大文件io与小文件io不同最优线程
        self.host.replace_content(config_file, "$vdbench_dir", vdbench_dir.replace("/", "\/"))
        self.host.replace_content(config_file, "$format", format_status)
        self.host.replace_content(config_file, "$fwdrate", fwdrate)

        original_contents = ["$elapsed"]
        self.elapsed = elapsed or self.elapsed
        replace_contents = [self.elapsed]
        self.host.replace_content_by_list(config_file, original_contents, replace_contents)

        multiple_thread = fsd_group_number * multiple
        client_thread = math.ceil(int(threads_config) / multiple_thread) * multiple_thread

        # 3. 生成配置文件的挂载路径
        self.create_vdbench_config(anchor_paths, config_file, client_thread, fsd_width, fsd_group_number, dir_depth)
        return config_file

    def create_vdbench_config(self, mount_paths, config_file, client_thread, fsd_width, fsd_group_number, dir_depth):
        hd_line, fsd_line, fwd_line = "", "", ""

        with open(config_file, 'r') as file:
            config_content = file.read()

        number = 0
        clients = []
        for i, (host_client, paths) in enumerate(mount_paths.items()):
            for j in range(host_client.hd_number):
                clients.append([host_client, paths[0]])
                hd_line += f"hd=hd{number},system={host_client.ip}\n"
                number += 1

        # 整理fsd
        fsd_tmp = re.findall(rf"tmp=fsd(\S+)", config_content)
        if not fsd_tmp:
            fsd_tmp.append("")
        for index, fsd in enumerate(fsd_tmp):
            fsd = fsd.replace('$width', f'{fsd_width}')
            fsd = fsd.replace('$depth', f'{constants.VDBENCH_DEPTH}')
            for i in range(fsd_group_number):
                fsd_number = index * fsd_group_number + i
                anchor_path = self.host.get_env_parameter("anchor_path")
                for j in range(dir_depth):
                    anchor_path = self.host.join_path(anchor_path, f"dir{fsd_number}")
                fsd_line += f"fsd=fsd{fsd_number},anchor={anchor_path}{fsd}\n"

        # 整理fwd
        fwd_tmp = re.findall(rf"tmp=fwd(\S+)", config_content)
        if not fwd_tmp:
            fwd_tmp.append("")
        for i, fwd in enumerate(fwd_tmp):
            # 仅支持fsd相同的情况
            fsd = re.search(r"fsd=([^,]+)", fwd).group(1)
            fwd = fwd.replace(f",fsd={fsd}", '')
            if fsd.isdigit():
                if fsd_group_number == 1:
                    fsd_info = f"fsd=fsd{fsd}"
                else:
                    fsd_info = f"fsd=(fsd{int(fsd) * fsd_group_number}-fsd{(int(fsd) + 1) * fsd_group_number - 1})"
                fwd_line += f"fwd=fwd{i},{fsd_info}{fwd}\n"
            else:
                fsd_end = len(fsd_tmp) * fsd_group_number - 1
                fwd_line += f"fwd=fwd{i},fsd=(fsd0-fsd{fsd_end}){fwd}\n"

        with open(config_file, 'r') as file:
            lines = file.readlines()

        tmpline = []
        for i, line in enumerate(lines):
            if "hd=default" in line:
                lines.insert(i + 1, hd_line)
            elif "fsd=default" in line:
                lines.insert(i + 1, fsd_line)
            elif "fwd=default" in line:
                lines.insert(i + 1, fwd_line)
            elif "tmp=" in line:
                tmpline.append(line)

        # 删除模板
        for line in tmpline:
            lines.remove(line)
        with open(config_file, 'w') as file:
            file.writelines(lines)

        thread = client_thread * len(clients) // len(fwd_tmp)
        self.host.replace_content(config_file, "$thread", thread)

        # 判断是否需要清理环境
        self.format_status = f"{fsd_group_number}&{fsd_width}"
        self.is_clean(self.format_status)

        return config_file

    def create_tag_file(self):
        anchor_path = self.client.parameters.get("anchor_path")
        tag_path = self.client.join_path(anchor_path, "tag")
        self.client.exec_command(f"echo '{self.format_status}' > {tag_path}")

    def is_clean(self, new_tag):
        anchor_path = self.client.parameters.get("anchor_path")
        tag_path = self.client.join_path(anchor_path, "tag")
        try:
            tag = self.client.exec_command(f"cat {tag_path}", timeout=30).stdout
        except socket.timeout:
            raise CaseFailedError(f"mount-point {anchor_path} may be disconnected, please check")

        if new_tag == tag:
            self.clean = False

    def client_drop_caches(self):
        self.logger.debug("drop cache")
        for client in self.anchor_paths.keys():
            res = client.drop_caches()
            if not res:
                self.logger.error(f"{client.ip} drop caches fail")
        self.logger.debug("Done drop cache")

    def client_mount(self):
        self.logger.debug("umount and mount")
        for client in self.anchor_paths.keys():
            umount_cmd = client.get_parameter("umount_command")
            try:
                res_umount = client.exec_command(umount_cmd)
                if res_umount.status_code != 0:
                    raise RuntimeError
            except Exception:
                raise CaseFailedError(f"{client.ip} umount failed. Command: {umount_cmd}")

            mount_cmd = client.get_parameter("mount_command")
            try:
                res_mount = client.exec_command(mount_cmd)
                if res_mount.status_code != 0:
                    raise RuntimeError
            except Exception:
                raise CaseFailedError(f"{client.ip} mount failed. Command: {mount_cmd}")

        self.logger.debug("Done unmount and mount")

    def find_bottom_data(self, *args, **kwargs) -> list:
        raise NotImplementedError

    def parse_zero(self, matched_lines: List[str], continuous=None) -> list:
        """ 解析有归零情况的日志行，返回归零时间或详细归零数据

        Args:
            matched_lines: 有归零情况的数据行
            continuous: 为None时，不检查持续归零情况。为整数N时，检查(至少)连续N秒都处于归零状态的情况

        Returns:
             无归零情况：返回[]
            - 有归零情况，且continuous=None
                返回归零的所有时间点（List[str]）。例：['16:36:49.007', '16:36:52.006']
            - 有归零情况，且continuous=<int>N
                返回至少连续N秒都归零的数据（List[dict]）。排序规则：1.持续时长倒序 > 2.开始序号正序
                例：  [
                        {
                            "归零开始时间": '16:36:49.007',
                            "归零结束时间": '16:36:52.006',
                            "持续时长": 3
                        }
                ]
                注意：此功能要求vdbench执行配置中的日志输出间隔为1秒（interval=1)
        """
        matched_lines = [_ for _ in matched_lines if "0" in _]
        if not matched_lines:
            return []

        if continuous is None:
            result = [line.split(maxsplit=1)[0] for line in matched_lines]
        else:
            line_dict = {}
            result = []
            for line in matched_lines:
                try:
                    time_str, lineno, _ = line.split(maxsplit=2)
                except ValueError:
                    continue
                if lineno.isdigit():
                    line_dict[int(lineno)] = time_str

            # 按前后序号差值分组 => 连号的为一组
            for _, g in groupby(enumerate(line_dict), lambda x: x[0] - x[1]):  # g：连续数据组（要求interval=1）
                group = [(k, line_dict.get(k)) for k in map(itemgetter(1), g)]  # k：序号
                if len(group) == 1:
                    continue
                start_lineno, start_time = group[0]
                _, end_time = group[-1]
                start = datetime.strptime(start_time, "%H:%M:%S.%f")
                end = datetime.strptime(end_time, "%H:%M:%S.%f")
                duration = (end - start).seconds
                if continuous <= duration:
                    result.append(
                        {
                            "跌零开始时间": start_time,
                            "跌零结束时间": end_time,
                            "持续时长": duration
                        }
                    )
            if result:
                df = pd.DataFrame(result, index=None)
                result = df.to_dict("records")
        self.logger.debug(f"Vdbench zero-io-data：{result}")
        return result

    def create_report_section(self):
        return self.reporter.make_common_report_section()

    def register_indicator(self) -> tuple:
        return self.reporter.make_indicator()

    def _verify_io_data(self, stage=VdbenchState.RD, **kwargs):
        """ 解析io数据前的校验

        Args:
            stage：阶段（rd/format），根据阶段对应关键字向后获取io数据

        Raises:
            FileNotFoundError: 未找到日志文件
            RuntimeError:
                - wait_for_start=True：等待vdbench数据预制异常
                - wait_for_start=False：日志文件中未找到vdbench正式操作数据
        """
        setup_success = self.wait_for_stage_start(stage=stage, **kwargs)
        if not setup_success:
            raise RuntimeError(f"Waiting for vdbench to reach stage {stage} error")

    def is_stage_start(self, stage: VdbenchState, output_dir=None):
        """ 日志是否可以查询到io数据。此函数应由各主机子类实现 """
        raise NotImplementedError

    def rate_cols(self, *args, **kwargs) -> List[int]:
        """ 获取需校验的指标索引列表。此函数应由各主机封装实现

        ************************************ summary.html示例  ***************************************

        Jan 01, 2022 .Interval.  .ReqstdOps..  ...cpu%...    read    ....read.....   ....write.......
                                rate   resp    total  sys    pct     rate    resp    rate    resp ...
        08:00:01.003    1       7876.0 0.088   51.1  36.7    44.9    3536.0  0.097   4340.0  0.081...
        08:00:02.003    2       10744  0.087   53.1  36.5    45.0    4838.0  0.095   5906.0  0.079...

        **********************************************************************************************
            [0]        [1]      【2】    [3]    [4]   [5]     [6]     【7】    [8]    【9】    [10]...

        需校验所有含 ``rate`` 字段的指标：ReqstdOps，read，write，mkdir，rmdir，create，open，close，delete...

        Returns:
            需校验的指标索引列表。例：[2,7,9,15,17,19,21,23,25]
        """
        pass

    def get_avg_resp(self) -> float:
        """获取IO平均时延（毫秒）"""
        raise NotImplementedError

    def get_avg_bw(self) -> float:
        """获取IO平均带宽（MBPS）"""
        raise NotImplementedError

    def get_avg_ops(self) -> float:
        """获取IO平均OPS（OPS）"""
        raise NotImplementedError

    def handle_benchmark(self):
        """性能基线用例执行完成后，保存基线、处理功耗"""
        self.reporter.handle_benchmark()
        # 处理功耗
        if self.case.suite.run_energy_csmpt:
            self.case.suite.request_energy_consumption()

    def _run_vdbench_cmd(self, config_file , output_dir, param_str=None):
        """执行vdbench，并返回可用于kill的进程id"""

        cmd = f"{self.executable_path} -f {config_file} -o {output_dir} "
        if param_str:
            cmd += param_str
        self.host.client.run_cmd_background(cmd, interact=False) # 不使用交互式，避免响应被异常截断
        time.sleep(3)
        keywords = ["vdbench.jar", config_file]
        pid_list = self.host.client.get_pids(keywords)
        try:
            return pid_list[0]
        except:
            self.logger.warning("File I/O not started")
        return None

class LinuxVdbenchIO(VdbenchIO):
    client_type = LinuxClient

    def __init__(self, *args, **kwargs):
        super(LinuxVdbenchIO, self).__init__(*args, **kwargs)
        self.host = LinuxAction(*args, **kwargs)
        self.executable = "vdbench"

    @property
    def sep(self):
        return "/"

    @property
    def summary_html(self):
        return self.get_summary_html()

    @property
    def logfile_html(self):
        return self.get_logfile_html()

    @property
    def prepare_output_dir(self):
        return self.host.join_path(self.case.output_dir, "prepare")

    @property
    def errorlog_html(self):
        return self.get_errorlog_html()

    def is_running(self):
        """ 判断当前进程是否在运行 """
        return self.host.is_pid_exists(self.pid)

    def _is_complete_successfully(self, pid=None, output_dir=None) -> Optional[bool]:
        """判断当前执行是否正常结束

        Returns:
            True: vdbench执行正常完成
            False: vdbench执行异常结束
            None: vdbench仍在执行中
        """
        if not pid:
            pid = self.pid
        logfile = self.get_logfile_html(output_dir)

        if self.host.file_contains(logfile, "Vdbench execution completed successfully"):
            return True

        if self.host.is_pid_exists(pid):
            return None  # 执行中
        else:
            self.logger.debug(f"Vdbench process(pid={pid}) terminated abnormally")
            return False  # 异常中断

    def check_validation_error(self):
        """校验文件数据一致性"""
        if self.host.file_contains(self.errorlog_html, "Data Validation error"):
            self.case.fail(f"检测到文件数据一致性校验失败")
            self.logger.warning(f"Detected file data validation error, check log for details: {self.errorlog_html}")

    def find_bottom_data(self, continuous=None, **kwargs) -> list:
        """检查vdbench日志性能数据归零情况

        Args:
            continuous: 为None时，不检查持续归零情况。为整数N时，检查连续N秒都处于归零状态的情况
            kwargs: 见 ``_verify_io_data``

        Returns:
            参见 ``VdbenchIO.parse_zero``
        """

        # 截取正式读写数据并保存
        trim_log = f"{self.output_dir}/trim_log.txt"
        keyword = self.get_stage_keyword(VdbenchState.RD)
        self.client.exec_command(
            f"sed -n '/{keyword}/,$p' {self.logfile_html} | sed -n '/[Ii]nterval/,$p' > {trim_log}")

        # 获取需要校验的指标索引列表
        cols_to_check = self.rate_cols(trim_log)
        matched = self.get_matched_lines(trim_log, cols_to_check)
        return self.parse_zero(matched, continuous=continuous)

    def get_matched_lines(self, trim_log, cols_to_check: List[int]):
        """获取有归零情况的日志行

        Args:
            trim_log: 截取后的summary.html路径。内容从vdbench执行的第一行正式数据开始，即："Jan 01, 2022 ..Interval.."
            cols_to_check: 日志文件中需校验的指标索引，如：[2, 7, 9...]
        """

        # 由于后续awk命令中$0为整行，指标索引需往后推一位
        sum_col = "+".join([f"${c + 1}" for c in cols_to_check])
        max_col = cols_to_check[-1] + 2

        # awk筛选条件：1、该行列数=最大列数 2、是有效数据行 3、校验列值总和为0
        cmd = f"awk '{{sum={sum_col}; if (NF == {max_col} && $0 !~ /rate/ && sum == 0) {{print $0}}}}' {trim_log}"
        matched = self.client.exec_command(cmd).stdout.strip()
        if matched:
            return matched.splitlines()
        else:
            self.logger.debug("vdbench: io-bottom not detected")
            return []

    def rate_cols(self, trim_log) -> List[int]:
        """  获取summary.html中需校验的指标索引列表。详细说明参见 ``VdbenchIO._cols_to_check()``

        Args:
            trim_log: 截取后的logfile.html路径。内容从vdbench执行的第一行正式数据开始，即："Jan 01, 2022 ..Interval.."
        """
        # 查询rate行
        cmd = f'rate_line=(`grep rate {trim_log} | head -1`);'
        # 将rate行以空格分隔为列表，获取并以逗号分隔打印所有"rate"字符串的索引
        cmd += 'for i in "${!rate_line[@]}"; do [[  "${rate_line[$i]}" = "rate"  ]] && printf $i, ; done;echo'
        stdout = self.client.exec_command(cmd).stdout.strip()
        index_match = None
        if stdout:
            index_match = re.findall(r".*\d+,+", stdout)

        if not index_match:
            raise RuntimeError("Parsing vdbench logfile rate-cols-index failed")

        # +2说明：[0]=时间，[1]=序号，[2]=实际数据列开始
        rate_index_list = sorted([int(c) + 2 for c in index_match[0].strip(",").split(",") if c.isnumeric()])
        self.logger.debug(f"rate cols to check：{rate_index_list}")
        return rate_index_list

    def is_stage_start(self, stage: VdbenchState, output_dir=None) -> bool:
        """ 检查vdbench是否已开始输出io数据

        Args:
            stage：阶段（rd/format），根据阶段对应关键字向后获取io数据
        """
        # 开始读写/数据预制的日志行数
        path = self.get_logfile_html(output_dir)
        keyword = self.get_stage_keyword(stage)
        lineno = self.client.exec_command(f"grep -n '{keyword}' '{path}' | cut -d: -f1 | head -n1").stdout.strip()
        if not lineno:
            return False

        # 从第lineno起以时间戳匹配IO数据行
        lineno = int(lineno) + 1
        cmd = f"grep -q '[0-9][0-9]:[0-9][0-9]:[0-9][0-9]' <(tail -n +{lineno} '{path}') && echo True"
        output = self.client.exec_command(cmd).stdout.strip()
        return output == "True"

    def get_avg_resp(self) -> float:
        """获取IO平均时延（毫秒）"""
        return self._get_avg_value(4)

    def get_avg_ops(self) -> float:
        return self._get_avg_value(3)

    def get_avg_bw(self) -> float:
        """获取平均带宽（mb/s）"""
        return self._get_avg_value(14)

    def _get_avg_value(self, index):
        resp = self.client.exec_command(f"tac {self.logfile_html} | grep -m 1 avg | awk '{{print ${index}}}'")
        result = resp.stdout.strip()
        if result:
            return float(result)
        else:
            raise RuntimeError("Parse vdbench avg data failed")


class WindowsVdbenchIO(VdbenchIO):
    client_type = WindowsClient

    def __init__(self, *args, **kwargs):
        super(WindowsVdbenchIO, self).__init__(*args, **kwargs)
        self.host = WindowsAction(*args, **kwargs)
        self.pid = None
        self.executable = "vdbench.bat"

    @property
    def sep(self):
        return "\\"

    @property
    def summary_html(self):
        return self.get_summary_html()

    @property
    def logfile_html(self):
        return self.get_logfile_html()

    @property
    def errorlog_html(self):
        return self.get_errorlog_html()

    def is_running(self):
        """
        判断当前进程是否在运行
        :return:
        """
        cmd = "get-process | where-object {$_.name -eq 'java'}"
        resp = self.client.exec_command(cmd)
        return resp.stdout.strip() != ""

    def get_matched_lines(self, trim_log, cols_to_check: list, log=True) -> list:
        """  获取有归零情况的日志行

        Args:
            trim_log: 截取后的summary.html路径。内容从vdbench执行的第一行正式数据开始，即："Jan 01, 2022 ..Interval.."
            cols_to_check: 日志文件中需校验的指标索引
            log: 是否保存归零数据日志（若有）
        """
        cmd = """
        $trim_log='%(trim_log)s';
        $check_cols=%(check_cols)s;
        echo "zero lines are below";
        ForEach($line in Get-Content $trim_log){   
            if (!($line -like '*rate*') -And !($line -like '*Interval*') -And $line ){        
                $curline_arr = $line -split '\s+';
                if ($curline_arr.Length -gt $check_cols[-1]){
                    $zr_line = $true;
                    :outer ForEach( $ind in $check_cols){
                        if ( ![decimal]$curline_arr[$ind] -eq 0.0 ){
                            $zr_line = $false;
                            break :outer
                        }             
                    }
                    if ($zr_line){ echo $line }
                }
            }
        } """
        check_cols = ",".join([str(r) for r in cols_to_check])
        cmd = re.sub(r"\s{4,}", " ", cmd)
        cmd = cmd % dict(trim_log=trim_log, check_cols=check_cols)
        stdout = self.client.run_cmd(cmd).stdout.strip()
        result = []
        if stdout:
            real_output = stdout.split("zero lines are below")[-1]
            if real_output.strip():
                head = self.client.run_cmd(f"Get-Content $trim_log -encoding UTF8 | Select-Object -First 2",
                                           is_print=False)
                result = real_output.splitlines()

        return result

    def find_bottom_data(self, continuous=None, wait_for_start=False,
                         wait_args: dict = None, log=True, **kwargs) -> list:
        """ 检查vdbench日志logfile_html中数据归零情况

        若有归零情况，所有归零行将保存到 用例目录/logs/vdbench_return_zero_{当前时间}_{用例名称}.txt中

        Args:
            continuous: 为None时，不检查持续归零情况。为整数N时，检查连续N秒都处于归零状态的情况
            wait_for_start: 等待vdbench正式开始执行(数据预制完成、且已开始输出日志)。
                               默认不等待，若此时日志尚未就绪将报错
            wait_args: ``VdbenchIO.wait_for_setup()`` 的参数
            log: 是否保存归零数据日志（若有）

        Returns:
            参见 ``VdbenchIO.parse_zero()``
        """

        # 截取正式读写数据并保存
        keyword = "elapsed="
        if wait_args and wait_args.get("contain_format"):
            keyword = "RD=format"
        trim_log = f"{self.output_dir}\\trim_log.txt"
        cmd = f'$lineno = ((Select-String -Pattern "{keyword}" -Path {self.logfile_html}).LineNumber)[0]; ' \
              f'Get-Content {self.logfile_html} | Select-Object -Skip $lineno ' \
              f'| Where-Object {{$_}} | Set-Content {trim_log} -encoding utf8'
        self.client.exec_command(cmd)

        # 获取需要校验的指标索引列表
        cols_to_check = self.rate_cols(trim_log)
        # 获取归零数据并保存日志
        matched = self.get_matched_lines(trim_log, cols_to_check, log=log)
        return self.parse_zero(matched, continuous=continuous)

    def rate_cols(self, trim_log) -> List[int]:
        """获取summary.html中需校验的指标索引列表

        详细说明参见 ``VdbenchIO._cols_to_check()``

        Args:
            trim_log: 截取后的summary.html路径。
                内容从vdbench执行的第一行正式数据开始，即："Jan 01, 2022 ..Interval.."
        """
        cmd = """
        $trim_log='%(trim_log)s';
        $header=(Select-String -Pattern 'rate' -Path $trim_log | Select-Object -First 1 -ExpandProperty Line).Trim();
        $header=$header -split '\s+';
        $check_cols = @();
        ForEach ($i in (0..($header.Count - 1))){
            if ($header[$i] -eq 'rate'){
                $check_cols += $i
            }
        }
        echo result<$check_cols>
        """
        result = self.client.run_cmd(cmd % dict(trim_log=trim_log)).stdout.strip()
        check_cols = re.findall(r".*result<(?!\$)(.*)>.*", result)
        check_cols = [col for col in check_cols if col != '']
        if not check_cols:
            raise RuntimeError(f"vdbench summary file rate cols index parsing failed")

        # +2说明：[0]=时间，[1]=序号，[2]=实际数据列开始
        check_cols = sorted([int(i) + 2 for i in check_cols[0].split()])
        self.logger.debug(f"vdbench rate cols：{check_cols}")
        return check_cols

    def is_stage_start(self, stage, output_dir=None):
        """ 日志是否可以查询到读写数据

        Args:
            stage：阶段（rd/format），根据阶段对应关键字向后获取io数据
        """

        # 开始数据预制/操作的日志行数
        path = self.logfile_html
        keyword = self.get_stage_keyword(stage)
        cmd = f'Select-String -Path "{path}" -Pattern "{keyword}" | Select-Object -ExpandProperty LineNumber -First 1'
        lineno = self.client.exec_command(cmd).stdout.strip()
        if not lineno:
            return False

        # 从第lineno起以时间戳匹配IO数据行
        cmd = f'Get-Content "{path}" | Select-Object -Skip {lineno} | Select-String -Pattern "\d+:\d+:\d+" -Quiet'
        output = self.client.exec_command(cmd).stdout.strip()
        return output == "True"


class VdbenchReporter:
    FLAT_CSV = "flat.csv"
    AVG_CSV = "avg_flat.csv"

    def __init__(self, vdbench: VdbenchIO):
        self.vdbench = vdbench
        self.client = vdbench.client
        self.exec_path = vdbench.executable_path
        self.today = datetime.now()
        if not self.exec_path:
            raise SESError("Please make sure VdbenchIO.run has been called")

        self.output_dir = vdbench.output_dir
        # 基本绘图子图信息
        self.labels = [
            {"name": "Rate", "ylabel": "ops"},
            {"name": "MB/sec", "ylabel": "MB"},
        ]

    @property
    def get_csv_file(self):
        return f"{self.output_dir}/{self.FLAT_CSV}"

    @property
    def get_avg_csv_file(self):
        return f"{self.output_dir}/{self.AVG_CSV}"

    def ts2datetime(self, value, last_value=None) -> datetime:
        """vdbench时间戳转换为时间对象"""
        if last_value and last_value < value:
            self.today -= dt.timedelta(days=1)  # 跨天
        value = self.today.strftime("%Y-%m-%d") + value
        if value.__contains__("."):
            value = datetime.strptime(value, "%Y-%m-%d%H:%M:%S.%f")
        else:
            value = datetime.strptime(value, "%Y-%m-%d%H:%M:%S")
        return value

    def generate_timestamp_xaix(self, df) -> List[datetime]:
        x = []
        last_item = None
        for item in reversed(df["tod"].values.tolist()):
            x.append(self.ts2datetime(item, last_item))
            last_item = item
        return x[::-1]

    @staticmethod
    def del_format_data(df, reset_index=True):
        run_types = df["Run"].values.tolist()
        for i, v in enumerate(run_types):
            if "format" not in v:
                continue
            df = df.drop(index=i)
        if reset_index:
            df.reset_index(drop=True, inplace=True)
        return df

    def get_y_cols(self) -> list:
        """获取y轴名称列表"""
        return [d["name"] for d in self.labels]

    def get_y_cols_labels(self) -> list:
        """获取y轴名称的单位"""
        return [d["ylabel"] for d in self.labels]

    def make_common_report_section(self):
        """创建默认报告"""
        remote_csv, remote_avg_csv = self.create_csv_file()
        return ReportUtil.build_perf_test_html_object(remote_csv, remote_avg_csv,
                                                      self.create_common_chart, self.create_common_avg_table)

    def make_indicator(self) -> tuple:
        bw = self.vdbench.get_avg_bw()
        ops = self.vdbench.get_avg_ops()
        return Bandwidth(bw), Ops(ops)

    def create_csv_file(self):
        """解析原生报告并生成csv"""
        remote_path = self.client.join_path(self.output_dir, "flatfile.html")
        remote_csv = self.get_csv_file
        remote_avg_csv = self.get_avg_csv_file
        cols = ["tod", "Run", "Interval"]
        cols.extend(self.get_y_cols())

        cols_str = " ".join(cols)
        cmd = f"{self.exec_path} parseflat -i {remote_path} -c {cols_str} -o {remote_csv}"
        resp = self.client.exec_command(cmd)
        if resp.status_code != 0:
            self.notice_report_parsing_error()

        avg_cols = ["tod", "Run", "Interval"]
        labels = copy.deepcopy(self.get_y_cols())
        for op in self._get_configured_operations():
            _op = op.capitalize()
            labels.append(f"{_op}_rate")
            if op in ["read", "write"]:
                labels.append(f"MB_{op}")
        avg_cols.extend(labels)

        avg_cols_str = " ".join(avg_cols)
        cmd = f"{self.exec_path} parseflat -i {remote_path} -c {avg_cols_str} -o {remote_avg_csv} -a"
        resp = self.client.exec_command(cmd)
        if resp.status_code != 0:
            self.notice_report_parsing_error()

        # 去除warmup时间
        df = pd.read_csv(remote_csv)
        warm_up = constants.VDBENCH_WARMUP
        df = df[warm_up:]
        df.to_csv(remote_csv, index=False)
        return remote_csv, remote_avg_csv

    def notice_report_parsing_error(self):
        """数据解析失败处理"""
        if self.vdbench.status < VdbenchState.DONE_FORMAT:
            logfile = self.vdbench.host.join_path(self.vdbench.prepare_output_dir, "logfile.html")
        else:
            logfile = self.vdbench.logfile_html
        msg = f"File I/O operation may have failed, check log for details: {logfile}"
        self.vdbench.logger.error(msg)
        raise CaseFailedError(msg)

    def create_common_avg_table(self, avg_csv_file):
        """平均值表"""
        df = pd.read_csv(avg_csv_file)
        df = self.del_format_data(df)

        timestamp = [x.strftime("%Y-%m-%d %H:%M:%S") for x in self.generate_timestamp_xaix(df)]
        df.insert(loc=0, column='Time', value=timestamp)
        df = df.drop(columns=['tod', 'Run', 'Interval'])

        return df

    def create_common_chart(self, csv_file):
        """ 根据csv格式性能数据绘图"""
        df = pd.read_csv(csv_file)
        df = self.del_format_data(df)

        x = self.generate_timestamp_xaix(df)
        y = df[self.get_y_cols()]

        return ReportUtil.create_subplots(x, y, labels=self.labels)

    def create_bottom_data_part(self, **kwargs) -> tuple:
        """创建归零数据信息报告

        Returns:
            有归零情况：报告元素
            无：None
        """
        data = self.vdbench.find_bottom_data(**kwargs)
        if data:
            duration, times = 0, len(data)
            if kwargs.get("continuous") is not None:
                for info in data:
                    duration += info["持续时长"]
                zero_info = ReportUtil.create_table(data, header_pos="v")
            else:
                duration = len(data)
                _data = ', '.join(data)
                zero_info = ReportUtil.str2element(f"<div>时间点：{_data}</div>")
        else:
            duration = times = 0
            zero_info = ReportUtil.str2element(f"<div>无</div>")
        return TitledContent("I/O跌零", zero_info).to_soup(), duration, times

    def handle_benchmark(self):
        """保存性能数据。必须在csv文件生成之后"""
        csv_path = self.client.join_path(self.output_dir, self.AVG_CSV)
        if os.path.exists(csv_path):
            raw_avg_csv = pd.read_csv(csv_path)
            self.vdbench.case.cache_runtime_data(CacheDataKey.AVG_BANDWIDTH_BENCHMARK,
                                                 raw_avg_csv["MB/sec"].iloc[0])
            self.vdbench.case.cache_runtime_data(CacheDataKey.AVG_OPS_BENCHMARK,
                                                 raw_avg_csv["Rate"].iloc[0])
        else:
            self.vdbench.case.logger.debug(f"handle benchmark failed, file not exist: {csv_path}")

    def _get_configured_operations(self) -> List[str]:
        """匹配执行的operation(s)参数"""
        parmscan_file = self.client.join_path(self.output_dir, "parmscan.html")
        with open(parmscan_file, mode="r") as f:
            content = f.read()

        result = []
        operations = re.findall(r"keyw.*operations?=(.*)", content)
        for op in operations:
            op = op.strip()
            if op.startswith("("):
                result.extend(op[1:-1].split(","))
            else:
                result.append(op)
        return list(set(result))
