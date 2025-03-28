# -*- coding: UTF-8 -*-
import abc
import json
import logging
import ntpath
import posixpath
import re
import socket
import time
from typing import Union
import warnings

from storage_evaluation_system_zzj import constants
from storage_evaluation_system_zzj.exception import ConfigError, NumberTypeParamValueError

warnings.filterwarnings(action='ignore', module='.*paramiko.*')

import xml.etree.ElementTree as ET

import traceback
import paramiko
from paramiko.channel import Channel
from paramiko.ssh_exception import AuthenticationException, SSHException

from storage_evaluation_system_zzj.client.client import Client, ClientBuilder

logging.getLogger("paramiko.transport").setLevel(logging.ERROR)


class SSHClient(Client):
    """负载客户端"""
    port = 22
    default_expect = ".+([#>$])[ ]*$"
    default_timeout = 120
    banner_timeout = 30
    prompt = None
    sep: str = None

    def __init__(self, env_config: ET.Element):
        self.login_expect = self.default_expect
        self._interact_chan = None
        self.is_aarch64 = False
        self.hd_number = 1
        super(SSHClient, self).__init__(env_config)
        self.check_parameters()

    @staticmethod
    def join_path(*args):
        raise NotImplementedError

    @abc.abstractmethod
    def kill_process(self, *args, **kwargs):
        """停止进程"""
        raise NotImplementedError()

    @abc.abstractmethod
    def check_process(self, *args, **kwargs):
        """检查进程是否存在"""
        raise NotImplementedError()

    @abc.abstractmethod
    def run_cmd_background(self, *args, **kwargs):
        """后台执行命令"""
        raise NotImplementedError()

    @abc.abstractmethod
    def get_child_pid_list(self, *args, **kwargs):
        """根据进程号获取子进程id列表"""
        raise NotImplementedError()

    @abc.abstractmethod
    def get_pids(self, keywords: Union[str, list]) -> list:
        """根据关键字获取pid"""
        raise NotImplementedError

    @abc.abstractmethod
    def is_pid_exists(self, pid) -> bool:
        """pid是否在运行"""
        raise NotImplementedError

    def check_parameters(self):
        try:  # 必选参数
            self.get_parameter("umount_command")
            self.get_parameter("mount_command")
        except KeyError as e:
            raise ConfigError(f"Requires {e} for client role='{self.role}'")

        valid_hd_params = [cid.lower() + "_hd" for cid in constants.CONFIGURABLE_HD_CASE_LIST]
        for param, value in self.parameters.items():
            if param.endswith("_hd"):
                if param not in valid_hd_params:
                    raise ConfigError(f"Invalid hd-number parameter name:'{param}', valid names: {valid_hd_params}")

                if not value.isdigit() or int(value) < 1:
                    raise NumberTypeParamValueError(param, value, prefix=str(self))

    def connect(self):
        """实现父类抽象方法"""
        return self._connect(self.ip, self.username, self.password, port=self.port)

    def _connect(self, ip=None, username=None, password=None, port=None):
        """ 创建SSH连接 """
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        sock = None

        kwargs = dict(
            hostname=ip,
            port=int(port),
            username=username,
            password=password,
            allow_agent=False,
            sock=sock,
            banner_timeout=self.banner_timeout
        )
        if hasattr(self, 'tcp_timeout'):
            kwargs['timeout'] = float(getattr(self, 'tcp_timeout', 30))
        if hasattr(self, 'auth_timeout'):
            kwargs['auth_timeout'] = float(getattr(self, 'auth_timeout', 30))

        try:
            ssh.connect(**kwargs)
            ssh.get_transport().set_keepalive(30)
        except AuthenticationException:
            self.logger.error(f"Login authentication failed: {self}")
            raise
        except Exception:
            self.logger.error(f"{self} connection failed: {traceback.format_exc()}")
            raise
        return ssh

    def run_cmd(self, *args, **kwargs):
        return self.interact_command(*args, **kwargs)

    def exec_command(self, command, timeout: int = None, retry=True, verbose: bool = True):
        """
        执行shell命令

        此方法只适用于无交互式一次性命令下发
        如果属于交互式命令或者脚本中带有expect等交互操作，需要使用interact_command，否则会造成命令下发后挂死或等待超时退出
        """
        if timeout is None:
            timeout = self.default_timeout
        elif timeout == -1:
            self.logger.debug(f"timeout has been disabled for command: {command}")
            timeout = None

        if verbose:
            self.logger.debug(f"{self.ip} >> Send cmd:{command}")
        try:
            out, err, ret_code = self._exec_command(command, timeout)
            response = SSHResponse(ret_code, bytes2str(out), stderr=bytes2str(err))
        except SSHException as e:
            if retry:
                self.logger.debug("Connection not active, re-connecting")
                if self.connector:
                    self.connector.close()
                self._connector = self.connect()
                return self.exec_command(command, timeout=timeout, retry=False)
            else:
                raise e
        if verbose:
            self.logger.debug(f"{self.ip} << {response}")
        return response

    def _exec_command(self, command, timeout: int = None, get_pty=False, environment=None):
        chan = self.connector.get_transport().open_session(timeout=self.banner_timeout)
        if get_pty:
            chan.get_pty()
        chan.settimeout(timeout)
        if environment:
            chan.update_environment(environment)
        chan.exec_command(command)
        try:
            ret_code = self._recv_exit_status(chan, timeout=timeout)
        except Exception:
            self.logger.debug(f"[ERROR] {self.ip} << Executing command timeout: {command}")
            raise socket.timeout(f"{self.ip}: Receiving ssh response timeout")

        chan.makefile_stdin("wb", -1)
        stdout = chan.makefile("r", -1)
        stderr = chan.makefile_stderr("r", -1)
        out = stdout.read().strip()
        err = stderr.read().strip()
        return out, err, ret_code

    @classmethod
    def _recv_exit_status(cls, channel: Channel, timeout):
        """等待命令执行完成，返回退出码

        原生的 Channel.settimeout(SSHClient.exec_command的timeout参数) 表示设置读写操作的超时时间
        * 执行stdout.read()、stderr.read()时触发
        * 若有多次输出，且两次输出的间隔不超过timeout则不会报错（即使整个时长超过了timeout）
        在某些场景下不符合“命令执行完成的超时时间”意图，因此重新实现 `Channel.recv_exit_status`以控制超时时间

        见：`paramiko.channel.Channel.settimeout`
        """
        channel.status_event.wait(timeout=timeout)
        assert channel.status_event.is_set()
        return channel.exit_status

    def interact_command(self, command, expect=None, timeout: int = None,
                         check_code: bool = True, retry=True, log_level=None, verbose: bool = True):
        """
        执行交互式命令

        调用此方法会保持命令上下文，如果属于交互式命令或者脚本中带有expect等交互操作，可调用此方法执行
        """
        if not expect:
            expect = self.default_expect
        if timeout is None:
            timeout = self.default_timeout
        elif timeout == -1:
            self.logger.debug(f"timeout has been disabled for command: {command}")
            timeout = None

        if command.startswith("cd "):
            self.logger.warning("Commands that change the current working directory might change the prompt and "
                                "cause timeout error due to RegEx matching failure. Use absolute path instead.")

        if isinstance(expect, list):
            expect = '(%s)' % '|'.join(expect)
        log_fn = self.logger.debug if not log_level else self.logger.info

        if verbose is True:
            log_fn(f"{self.ip} >> Send cmd:{command}, expect:{expect}")
        try:
            r_str, match_str = self._interact_command(command, expect=expect, timeout=timeout, verbose=verbose)

            ret_code = 0
            # 获取最后一条命令的退出码
            if check_code and bool(match_str) and match_str.strip()[-1] in "#>$":
                try:
                    _out, _match_str = self._interact_command("echo $?", expect=expect, timeout=10, verbose=False)
                    _outs = _out.splitlines()
                    if len(_outs) >= 2:
                        if _outs[-2].isdigit():
                            ret_code = int(_outs[-2])
                        # Windows命令退出码判断
                        elif 'False' in _outs[-2]:
                            ret_code = -1
                except Exception:
                    self.logger.warning("Expected timeout of obtaining the invoke shell exit code, expect: %s"
                                        % match_str)
                    pass
            stdout = r_str
            # 处理掉命令发送字符
            if r_str.strip().startswith(command):
                stdout = r_str.replace(command, "", 1).strip()
            response = SSHResponse(ret_code, stdout, expect=expect, prompt=self.default_expect, match_str=match_str)
        except socket.error as e:
            # 如果retry=True，且不为recv超时(socket.timeout)时，进行重试
            if retry and not isinstance(e, socket.timeout):
                self.logger.debug("Connection not active, re-connecting")
                if self.connector:
                    self.connector.close()
                self._interact_chan = None
                self._connector = self.connect()
                return self.interact_command(command, expect=expect, timeout=timeout,
                                             check_code=check_code, retry=False)
            else:
                raise e
        if verbose:
            log_fn(f"{self.ip} << {response}")
        return response

    def _interact_command(self, command, expect=None, timeout: int = None, verbose=True):
        # 初次建立channel
        if not self._interact_chan:
            self._interact_chan = self.connector.invoke_shell(width=200, height=200)
            # 丢弃登陆头信息，超时时间5s
            _, prompt = self._handle_terminal(self._interact_chan,
                                              "(%s)|(%s)" % (expect, self.login_expect),
                                              self.banner_timeout)
            if not self.prompt:
                self.prompt = prompt
                self.logger.debug(f"{self} prompt={prompt}")

        self._interact_chan.send('%s\r' % command)
        _is_result_check = False
        if command == 'echo $?':
            _is_result_check = True

        r_str, match_str = self._handle_terminal(self._interact_chan,
                                                 expect,
                                                 timeout,
                                                 is_result_check=_is_result_check,
                                                 verbose=verbose)

        return r_str, match_str

    def _handle_terminal(self, channel: Channel, expect: str, timeout: int, is_result_check=False, verbose=True):
        """ 处理终端响应信息 """
        pattern = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\x0f')
        # start_time = time.time()
        self._interact_chan.settimeout(timeout)
        if verbose and timeout != self.default_timeout:
            self.logger.debug("interactive channel set timeout %d" % timeout)
        r_str = ""
        r_str_sub = ""
        try:
            # 捕获recv超时异常
            while True:
                _r_bytes = channel.recv(32768)
                match = None
                if _r_bytes:
                    _r_str = bytes2str(_r_bytes)
                    r_str += _r_str
                    r_str_sub = pattern.sub('', r_str)
                    # 未指定预期响应结束符，按self.prompt处理
                    if self.prompt and expect == self.default_expect:
                        match = r_str_sub.endswith(self.prompt)
                    else:
                        match = re.search(expect, r_str_sub)

                if match:
                    if isinstance(match, re.Match):
                        match_str = match.group()
                    else:
                        match_str = self.prompt
                    # 进行echo $?进行命令结果判定时，如果一次终端接收echo $，不作为结束依据
                    if is_result_check and match_str and match_str.endswith('echo $'):
                        pass
                    else:
                        break
                time.sleep(0.5)
        finally:
            # 换行去重
            r_str_sub = re.compile(r'\n{2,}').sub(r'\n', r_str_sub)
            if verbose:
                self.logger.debug("[RECV] {}".format(r_str_sub.encode('utf-8')))
        return r_str_sub, match_str

    def pkill(self, name):
        """终止指定进程"""
        raise NotImplementedError

    def put(self, local_path, remote_path, callback=None, confirm=True):
        """ 上传文件

        此方法仅适用于单个文件上传，本地和远端路径只能是文件路径，不可适用文件夹路径
        """
        self.logger.debug(f"Uploading file: local path={local_path}, remote path={remote_path}")
        sftp_client = self.connector.open_sftp()
        sftp_client.put(local_path, remote_path, callback=callback, confirm=confirm)
        if sftp_client:
            sftp_client.close()

    def get(self, remote_path, local_path, callback=None):
        """ 下载文件

        此方法仅适用于单个文件下载，本地和远端路径只能是文件路径，不可使用文件夹路径
        """
        self.logger.debug(f"Downloading file: remote path={remote_path}, local path={local_path}")
        sftp_client = self.connector.open_sftp()
        sftp_client.get(remote_path, local_path, callback=callback)
        if sftp_client:
            sftp_client.close()

    def command_exists(self, command: str) -> bool:
        """指令是否存在"""
        raise NotImplementedError

    def exists(self, *args, **kwargs) -> bool:
        raise NotImplementedError

    def remove(self, *args, **kwargs) -> bool:
        raise NotImplementedError

    def get_env(self, name) -> str:
        raise NotImplementedError

    def close(self):
        if self.connector:
            self.connector.close()


class SSHClientBuilder(ClientBuilder):

    def create_client(self) -> SSHClient:
        client = SSHClient(self.env_config)
        # 判断客户端os
        resp = client.exec_command('uname')
        if resp.status_code == 0:
            return LinuxClient(self.env_config)
        else:
            return WindowsClient(self.env_config)


class SSHResponse:
    """
    客户端请问响应对象，一般用于命令行执行返回
    """

    def __init__(self, status_code: int, stdout: str, stderr: str = None, expect: str = None, prompt: str = None,
                 **kwargs):
        self.status_code = status_code
        self.stdout = stdout
        self.stderr = stderr
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

        res_format = f'Response({self.status_code}) stdout: {_out}'

        if self.stderr:
            res_format = f"{res_format}\nstderr: {_err}"

        for k, v in self.__dict__.items():
            if k == "match_str" and self._expect_prompt:
                continue
            if k not in ['status_code', 'stdout', 'stderr', '_expect_prompt'] and v:
                res_format = f"{res_format}\n{k}: {str(v)}"
        return res_format


def bytes2str(bytes_obj: bytes, encoding=None, catch_exception=True):
    """ 字节流转字符串 """
    _str = ""
    try:
        if bytes_obj is not None:
            if encoding:
                _str = bytes_obj.decode(encoding=encoding)
            else:
                _str = bytes_obj.decode()
    except UnicodeDecodeError as e:
        if catch_exception:
            _str = bytes2str(bytes_obj, encoding='gbk', catch_exception=False)
        else:
            raise e
    return _str


class LinuxClient(SSHClient):
    sep = "/"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_aarch64 = self._is_aarch64()

    @staticmethod
    def join_path(*args):
        return posixpath.join(*args)

    def run_cmd(self, cmd, verbose=True, rm_prompt=True, **kwargs):
        """阻塞式执行命令，返回响应对象"""
        return self.exec_command(cmd, verbose=verbose, **kwargs)

    def run_cmd_background(self, cmd, output=None, filter_item=None, interact=True, **kwargs) -> int:
        """后台执行命令，返回进程码"""
        _cmd = cmd
        if output:
            cmd = f"{cmd} > {output}"
        else:
            cmd = f"{cmd} >/dev/null"
        cmd = f"nohup {cmd} 2>&1 &"
        if interact:
            res = self.interact_command(cmd, **kwargs)
            # 根据命令查询Pid
            out = res.stdout.strip()
            try:
                pattern = r"\[\d+\] \d+"
                pid = re.search(pattern, out).group().split()[-1]
                return int(pid)
            except TypeError:
                raise RuntimeError(f"Command failed: '{cmd}'")  # 命令未成功执行
        else:
            self.run_cmd(cmd, **kwargs)

    def get_child_pid_list(self, ppid) -> list:
        """根据进程号获取子进程id列表"""
        resp = self.exec_command(f"ps -o pid=  --ppid {ppid}")
        return resp.stdout.split()

    def put(self, local_path, remote_path, callback=None, confirm=True):
        super().put(local_path, remote_path, callback, confirm)
        self.exec_command("dos2unix %s" % remote_path)

    def mkdir(self, path):
        """创建目录"""
        if path[-1] != "/":
            path += "/"
        return self.exec_command(f"test -d {path} && echo 'Dir exists' || mkdir -p {path}")

    def remove(self, path):
        """创建目录"""
        return self.exec_command(f"rm -rf {path}")

    def exists(self, path, target=None, is_dir=False) -> bool:
        """ 判断文件/文件夹是否存在

        Args:
            path: 目标文件(夹)绝对路径。若target不为None，则判断path底下是否存在target（与自行拼接完整路径效果无异）
            target: 若不为None，则判断path底下是否存在target
            is_dir: 目标是否为目录

        Returns: 文件是否存在
        """
        option = "-d" if is_dir else "-e"  # -d：目录， -e：文件
        if target:
            if path[-1] != "/":
                path += "/"
            path += target
        resp = self.exec_command(f"test {option} {path}")
        return resp.status_code == 0

    def get_env(self, name) -> str:
        """获取环境变量"""
        stdout = self.exec_command(f"source /etc/profile; printenv {name}").stdout
        return stdout if stdout.strip() else None

    def kill_process(self, pid=None, keywords=None):
        """停止进程"""
        if pid:
            self.exec_command(f"if ps -p {pid} > /dev/null;then kill -9 {pid};fi")
        elif keywords:
            for pid in self.get_pids(keywords):
                self.kill_process(pid=pid)
        else:
            raise ValueError("kill_process requires pid or keyword of the process")

    def is_pid_exists(self, pid) -> bool:
        """检查进程是否存在"""
        return self.exec_command(f"ps -p {pid} -o pid,cmd").status_code == 0

    def get_pids(self, keywords: Union[str, list]) -> list:
        """根据关键字获取pid"""
        if isinstance(keywords, str):
            grep_str = f"grep '{keywords}'"
        else:
            grep_str = [f"grep '{k}'" for k in keywords]
            grep_str = " | ".join(grep_str)

        stdout = self.exec_command(f"ps aux | {grep_str} | grep -v grep | awk '{{print $2}}'").stdout
        result = []
        for pid in stdout.split("\n"):
            if pid.isnumeric():
                result.append(int(pid))
        return result

    def command_exists(self, command: str) -> bool:
        return self.exec_command(f"command -V {command}").status_code == 0

    def drop_caches(self):
        """清除缓存"""
        resp = self.exec_command("echo 3 >/proc/sys/vm/drop_caches")
        return resp.status_code == 0

    def pkill(self, name):
        """终止指定进程"""
        self.exec_command(f"pkill {name}")

    def _is_aarch64(self):
        return "aarch64" in self.exec_command("uname -m").stdout


class WindowsClient(SSHClient):
    default_expect = 'PS .*>[ ]*$'
    sep = "\\"

    @staticmethod
    def join_path(*args):
        return ntpath.join(*args)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.interact_command(r"Remove-Module PSReadline", verbose=False, check_code=False)

    def run_cmd(self, command, expect=None, timeout=120, rm_prompt=True, **kwargs):
        """
        执行PowerShell命令
        """
        resp = self.interact_command(command, expect=expect, timeout=timeout, **kwargs)
        # 根据rm_prompt开关状态,是否删除输出内容最后的字符串:"PS C:\Users\Administrator>"
        if rm_prompt:
            stdout_tail = resp.match_str
            if stdout_tail:
                stdout_tail = stdout_tail.strip()
                stdout_match_str = ''.join(resp.stdout.rsplit(stdout_tail, 1)).strip()
                resp.stdout = stdout_match_str
        return resp

    def run_cmd_background(self, cmd, **kwargs) -> int:
        """ 后台执行命令
        Args:
            cmd: 命令
        Returns: 进程id
        """
        self.run_cmd(f"$jobBlock = [ScriptBlock]::Create(\"{cmd}\")")
        self.run_cmd("$job = Start-job -ScriptBlock $jobBlock")

        res = self.get_win_version()
        if "Windows Server" in res:
            cmd = f'Get-WmiObject Win32_Process -Filter \\"commandline like \'%{kwargs.get("filter_item")}%\'\\"'
        else:
            cmd = f'Get-WmiObject Win32_Process -Filter "commandline like \'%{kwargs.get("filter_item")}%\'"'
        cmd += f' | Select ProcessId, CommandLine, CreationDate | ConvertTo-Json -Compress'
        resp = self.exec_command(cmd)
        try:
            out_dict = json.loads(resp.stdout.replace("\r\n", ""))
        except:
            raise RuntimeError(f"Failed to execute command: {cmd}")

        if isinstance(out_dict, dict):
            return int(out_dict["ProcessId"])
        else:
            # 按启动时间从远到近排序
            for proc in out_dict:
                proc["CreationTime"] = float(proc["CreationDate"].split("+")[0])

            proc = sorted(out_dict, key=lambda x: x["CreationTime"])[0]
            return int(proc["ProcessId"])

    def get_win_version(self):
        """
        获得windows系统版本
        """
        res = self.exec_command(f"Get-CimInstance -ClassName Win32_OperatingSystem | select Caption, Version")
        return res.stdout.split("\r\n")[-1]

    def exists(self, path, target=None, is_dir=False, is_print=False) -> bool:
        """ 判断文件/文件夹是否存在

        :param path: 目标文件(夹)绝对路径。若target不为None，则判断path底下是否存在target（与自行拼接完整路径效果无异）
        :param target: 若不为None，则判断path底下是否存在target
        :param is_dir: 目标是否为目录
        :param is_print: 是否打印命令与输出
        """
        path_type = "Container" if is_dir else "leaf"  # 目录/文件
        if target:
            if path[-1] != self.sep:
                path += self.sep
            path += target
        return "True" == self.exec_command(f"Test-Path '{path}' -PathType {path_type}").stdout.strip()

    def get_env(self, name) -> str:
        """获取环境变量"""
        stdout = self.exec_command(f"$Env:{name}").stdout
        return stdout if stdout.strip() else None

    def kill_process(self, pid=None, keywords=None):
        self.exec_command(f"taskkill /F /PID {pid}", check_code=False)

    def is_pid_exists(self, pid) -> bool:
        """检查进程是否结束"""
        res = self.exec_command(f"Get-Process -Id {pid}")
        return res.status_code == -1

    def remove(self, path):
        """删除文件/目录"""
        self.exec_command(f"Remove-Item '{path}' -Force")
