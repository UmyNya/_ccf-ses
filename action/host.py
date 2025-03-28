# -*- coding: UTF-8 -*-
import math
import os.path
import re
import traceback
from typing import Union, Optional

from storage_evaluation_system_zzj.action.action import Actions
from storage_evaluation_system_zzj.client.ssh_client import SSHClient, LinuxClient, WindowsClient
from storage_evaluation_system_zzj.exception import SESError
from storage_evaluation_system_zzj.logger import logger
from storage_evaluation_system_zzj.util import IostatData


class HostAction(Actions):
    client: SSHClient
    client_type = SSHClient

    def copy(self, *args, **kwargs):
        raise NotImplementedError

    def mkdir(self, *args, **kwargs):
        raise NotImplementedError

    def remove(self, path):
        raise NotImplementedError

    def join_path(self, *args):
        return self.client.join_path(*args)

    def exists(self, *args, **kwargs) -> bool:
        raise NotImplementedError

    def is_dir(self, path):
        raise NotImplementedError

    def create_file(self, filepath, content=None):
        raise NotImplementedError

    def create_file_with_size(self, filepath, size):
        raise NotImplementedError

    def is_pid_exists(self, pid):
        return self.client.is_pid_exists(pid)

    def kill_process(self, pid=None, keywords=None):
        self.client.kill_process(pid=pid, keywords=keywords)

    def get_pids(self, keywords: Union[str, list]) -> list:
        """根据关键字获取pid"""
        return self.client.get_pids(keywords)

    # def umount(self, *args, **kwargs):
    #     """解挂载"""
    #     raise NotImplementedError
    #
    # def mount(self, *args, **kwargs):
    #     """存储挂载"""
    #     raise NotImplementedError

    def find_file(self, *args, **kwargs):
        """寻找文件路径"""
        raise NotImplementedError

    def find_dir(self, *args, **kwargs):
        """寻找文件所在目录"""
        raise NotImplementedError

    def get_max_threads(self):
        raise NotImplementedError

    def replace_content(self, replace_file, original_content, replace_content):
        """替换文件内容"""
        raise NotImplementedError

    def file_contains(self, path, target) -> bool:
        """ 文件内容是否包含特定字符串

        Args:
            path: 文件绝对路径
            target: 目标字符串
        """

        raise NotImplementedError

    def get_run_result(self, *args, **kwargs):
        """获取运行结果判断"""
        raise NotImplementedError

    def get_last_line(self, path, trim=True, **kwargs) -> str:
        """获取文件的最后一行"""
        raise NotImplementedError

    def get_file_lc(self, path) -> int:
        """获取文件行数"""
        raise NotImplementedError

    def get_file_md5sum(self, *args, **kwargs):
        """获取文件md5值"""
        raise NotImplementedError

    def replace_content_by_list(self, replace_file, original_contents, replace_contents):
        for original_content, replace_content in zip(original_contents, replace_contents):
            self.replace_content(replace_file, original_content, replace_content)

    def get_sys_temp_dir(self):
        """获取当前系统默认的临时目录"""
        raise NotImplementedError

    def insert_text_to_file(self, *args, **kwargs):
        raise NotImplementedError

    def search_content(self, filepath, *keywords):
        raise NotImplementedError

    def start_iostat(self, input_file: str, interval: int, elapsed=None) -> int:
        """后台启动iostat（单位MB)

        Args:
            input_file: 写入文件
            interval: 记录间隔

        Returns: 进程pid
        """
        raise NotImplementedError

    def collect_iostat_data(self, pid, log_path, local_path) -> Optional[IostatData]:
        """收集iostat平均数据

        Args:
            pid: iostat进程id
            log_path: 数据保存的日志
            local_path: 下载到本地的位置
        """
        self.client.kill_process(pid)
        # 下载到本地（master节点）
        try:
            self.client.get(log_path, local_path)
            if not os.path.exists(local_path):
                self.logger.debug(f"iostat log not exists: {local_path}")
                return None
            self.remove(log_path)
            return self.parse_iostat_file(local_path)
        except:
            self.logger.debug(traceback.format_exc())
            return None

    @staticmethod
    def parse_iostat_file(file_path) -> IostatData:
        """解析iostat日志，获取tps、mb_read/s、mb_wrtn/s"""
        tps_values = []
        mb_read_values = []
        mb_wrtn_values = []
        count = 0  # 执行次数（device出现次数）
        ndigit = 3
        with open(file_path, 'r', encoding="utf-8") as file:
            for line in file:
                columns = line.split()
                # 有6列，且第一列之后的元素都是数字
                if columns and columns[0].lower().startswith("device"):
                    count += 1
                    continue
                if len(columns) == 6 and all(re.match(r'^-?\d+(\.\d+)?$', col) for col in columns[1:]):
                    tps = float(columns[1])
                    mb_read = float(columns[2])
                    mb_writn = float(columns[3])
                    # 收集数据
                    tps_values.append(tps)
                    mb_read_values.append(mb_read)
                    mb_wrtn_values.append(mb_writn)

        # 计算平均值
        tps_avg = sum(tps_values) / count if tps_values else 0
        mb_read_avg = sum(mb_read_values) / count if mb_read_values else 0
        mb_wrtn_avg = sum(mb_wrtn_values) / count if mb_wrtn_values else 0

        return IostatData(tps=round(tps_avg, ndigit),
                          mb_read_ps=round(mb_read_avg, ndigit),
                          mb_wrtn_ps=round(mb_wrtn_avg, ndigit))


class LinuxAction(HostAction):
    client: LinuxClient
    client_type = LinuxClient
    SEP = "/"

    def exists(self, path, target=None, is_dir=False) -> bool:
        """ 判断文件/文件夹是否存在

        Args:
            path: 目标文件(夹)绝对路径。若target不为None，则判断path底下是否存在target（与自行拼接完整路径效果无异）
            target: 若不为None，则判断path底下是否存在target
            is_dir: 目标是否为目录

        Returns: 文件是否存在
        """

        return self.client.exists(path, target=target, is_dir=is_dir)

    def dir_exists(self, path, dirname=None):
        """ 判断文件夹是否存在，参数参见 ``exists`` """
        return self.exists(path, target=dirname, is_dir=True)

    def mkdir(self, path):
        """创建目录"""
        return self.client.mkdir(path)

    def create_file(self, filepath, content=None):
        """创建文件（覆盖已存在文件）

        Args:
            filepath: 文件路径
            content: 内容（为None时touch）
        """
        if content is None:
            cmd = f"touch {filepath}"
        else:
            cmd = f"echo '{content}' > {filepath}"
        return self.client.exec_command(cmd)

    def create_file_with_size(self, filepath, size):
        """创建目标大小文件

        Args:
            filepath: 文件路径
            size: 文件大小
        """
        cmd = f"fallocate -l {size} {filepath}"
        return self.client.exec_command(cmd)

    def is_dir(self, path):
        return self.client.exec_command(f"test -d {path}").status_code == 0

    def copy(self, source: str, dest: str) -> str:
        """复制文件（强制覆盖源文件）

        Args:
            source: 源文件
            dest: 目标位置。为目录时（以 "/" 结尾），或检测为目录时，复制到 $dest/source名称

        Returns: 复制后的目标文件
        """

        fname = source.split("/")[-1]
        if dest.endswith("/"):
            dest += fname
        elif self.is_dir(dest):
            dest = f"{dest}/{fname}"

        self.client.exec_command(f"rm -rf {dest}")
        self.client.exec_command(f"cp {source} {dest}")
        return dest

    def remove(self, path):
        """删除文件/目录"""
        self.client.remove(path)

    def get_file_md5sum(self, path):
        """获取文件md5值"""
        return self.client.exec_command(f"md5sum {path}")

    def umount(self, target_path):
        self.client.exec_command(f"umount {target_path}")

    def mount(self, shared_path, target_path):
        self.client.exec_command(f"mount {shared_path} {target_path}")

    def download_dir_as_zip(self, remote_path, local_zip, timeout=300, remove=True):
        """将远端文件夹中的内容打包并下载到本地

        Args:
            remote_path: 客户端上的文件夹绝对路径
            local_zip: 本地压缩包绝对路径
            timeout: 命令行超时时长
            remove: 下载完成后删除远端压缩包
        """
        if not local_zip.endswith(".zip"):
            raise ValueError(f"Require <local_zip> to be a zip file: {local_zip}")

        remote_zip = f"{remote_path}.zip"
        # 数据需要保留目录层级，此处不加junk参数，后续自处理目录层级
        resp = self.client.interact_command(f"zip -q -r {remote_zip} {remote_path}",
                                            timeout=timeout)
        if resp.status_code != 0:
            raise RuntimeError(f"[{self.client.role}] Failed to zip directory: {remote_path}")

        self.client.get(remote_zip, local_zip)
        self.logger.info(f"Downloaded: {local_zip}")

        if remove:
            self.remove(remote_zip)

    def find_dir(self, remote_path, file_name):
        """查找某路径下是否有该文件，并返回第一个路径(不包含该文件)

        Args:
            remote_path: 客户端上的文件夹绝对路径
            file_name: 文件名称
        """
        res = self.client.exec_command("find %s -name %s -type f" % (remote_path, file_name))
        res = res.stdout.split("\n")[0]
        return "/".join(res.split('/')[:-1])

    def find_file(self, remote_path, file_name):
        """查找某路径下是否有该文件，并返回第一个路径(包含该文件)

        Args:
            remote_path: 客户端上的文件夹绝对路径
            file_name: 文件名称
        """
        res = self.client.exec_command("find %s -name %s -type f" % (remote_path, file_name))
        return res.stdout.split("\n")[0]

    def get_memory_size(self) -> int:
        return int(self.client.exec_command("free -b -t | grep -i total: | awk '{print $2}'").stdout)

    def replace_content(self, replace_file, original_content, replace_content):
        return self.client.exec_command(f"sed -i 's/{original_content}/{replace_content}/g' {replace_file}")

    def file_contains(self, path, target) -> bool:
        """ 文件内容是否包含特定字符串

        Args:
            path: 文件绝对路径
            target: 目标字符串
        """

        resp = self.client.exec_command(f"grep '{target}' {path}")
        return 0 == resp.status_code

    def replace_line_break(self, replace_file):
        return self.client.run_cmd(f"sed -i -e 's/\\r//' {replace_file}")

    def get_max_threads(self):
        cmd = "cat /proc/cpuinfo| grep 'processor'| wc -l"
        stdout = self.client.exec_command(cmd).stdout
        return int(stdout)

    def get_last_line(self, path, trim=True, **kwargs):
        """获取文件最后一行

        Args:
            path: 文件路径
            trim: 是否移除空行
        """
        if trim:
            cmd = f"awk 'NF' '{path}' | tail -n 1"
        else:
            cmd = f"tail -n 1 '{path}'"
        return self.client.exec_command(cmd).stdout

    def get_file_lc(self, path) -> int:
        """获取文件行数

        Args:
            path: 文件路径
        """
        return int(self.client.exec_command(f"grep -cv ^$ {path}").stdout.strip())

    def get_sys_temp_dir(self):
        """获取当前系统默认的临时目录"""
        return self.client.exec_command("echo ${TMPDIR:-${TEMP:-${TMP:-/tmp}}}").stdout

    def insert_text_to_file(self, filepath, content, lineno=1):
        """向文件第N行插入文本

        Args:
            filepath: 文件路径
            content: 插入内容
            lineno: 第N行
        """
        content = content.replace("/", "\\/")
        return self.client.exec_command(f"sed -i '{lineno}s/^/{content}\\n/' {filepath}")

    def get_run_result(self, log_path, flag):
        stdout = self.get_last_line(log_path)
        res = False
        if isinstance(flag, list):
            for f in flag:
                res = res | (f in stdout)
        else:
            res = flag in stdout
        return res

    def search_content(self, filepath, *keywords):
        greps = " | ".join([f"grep '{kw}'" for kw in keywords])
        return self.client.exec_command(f"cat {filepath} | grep -v grep | {greps}")

    def start_iostat(self, input_file: str, interval: int, elapsed=None) -> int:
        """后台启动iostat

        Args:
            input_file: 写入文件
            interval: 记录间隔

        Returns: 进程pid
        """
        if not elapsed:
            count = ""
        elif not isinstance(elapsed, int):
            raise SESError(f"Invalid elapsed={elapsed}")
        else:
            count = math.ceil(elapsed / interval)
        return self.client.run_cmd_background(f"iostat -m -d {interval} {count}", input_file)


class WindowsAction(HostAction):
    client: WindowsClient
    client_type = WindowsClient
    SEP = "\\"

    def exists(self, path, target=None, is_dir=False) -> bool:
        """ 判断文件/文件夹是否存在

        Args:
            path: 目标文件(夹)绝对路径。若target不为None，则判断path底下是否存在target（与自行拼接完整路径效果无异）
            target: 若不为None，则判断path底下是否存在target
            is_dir: 目标是否为目录

        Returns: 文件是否存在
        """

        return self.client.exists(path, target=target, is_dir=is_dir)

    def copy(self, original_file, copy_dir, sep="\\"):
        config_file = f"{copy_dir}{sep}{original_file.split(sep)[-1]}"
        remove_cmd = f"Remove-Item -Path {config_file}"
        delete_cmd = f"""
                if (Test-Path -Path {config_file}) {{{remove_cmd}}}
            """
        self.client.run_cmd(delete_cmd)
        self.client.run_cmd(f"Copy-Item {original_file} {copy_dir}")
        return config_file

    def replace_content(self, replace_file, original_content, replace_content):
        if original_content.startswith("$"):
            original_content = "\\" + original_content
        return self.client.run_cmd(f"(Get-Content {replace_file}) -replace '{original_content}', '{replace_content}' "
                                   f"| Set-Content {replace_file}")

    def get_last_line(self, path, trim=True, **kwargs):
        """获取文件最后一行

        Args:
            path: 文件路径
            trim: 是否移除空行
        """
        trim_cmd = "Where-Object { $_ -match '\S' } |" if trim else ""
        cmd = f'Get-Content -Path "{path}" |{trim_cmd} Select-Object -Last 1'
        return self.client.run_cmd(cmd).stdout

    def get_file_lc(self, path) -> int:
        """获取文件行数

        Args:
            path: 文件路径
        """
        return int(self.client.run_cmd(f'(Get-Content -Path "{path}" | Measure-Object -Line).Lines').stdout)

    def get_sys_temp_dir(self):
        """获取当前系统默认的临时目录"""
        return self.client.run_cmd("$env: temp").stdout

    def mkdir(self, path):
        self.client.run_cmd(f"mkdir {path} -Force")

    def remove(self, path):
        """删除文件/目录"""
        self.client.remove(path)

    def find(self, remote_path, file_name):
        """查找某路径下是否有该文件，并返回第一个路径(不包含该文件)

        Args:
            remote_path: 客户端上的文件夹绝对路径
            file_name: 文件名称
        """
        res = self.client.exec_command(f"Get-ChildItem -Path %s -Recurse -Name %s" % (remote_path, file_name))
        if res.stdout:
            path = res.stdout.split("\r\n")[0]
            return remote_path + "/".join(path.split('\\')[:-1])

    # TODO: 处理不存在账户与密码
    def mount(self, shared_path, mount_path, username=None, password=None):
        """
        windows下挂载存储
        """
        mount_path = mount_path.split("\\")[0]
        self.client.exec_command(f"net use {mount_path} {shared_path} /Persistent:Yes /u:{username} {password}")
        logger.debug("挂载成功")
        self.client.exec_command("net use")

    def umount(self, mount_path):
        """
        windows下解挂载
        """
        mount_path = mount_path.split("\\")[0]
        self.client.exec_command(f"net use {mount_path} /delete /y")
