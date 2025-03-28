from dataclasses import dataclass
import datetime
import logging
import os
import re
import shutil
import tarfile
import threading
import time
import traceback
from types import SimpleNamespace
from typing import Dict, List, Callable, Union, Optional
import uuid
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import matplotlib
from matplotlib import pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure
import numpy as np
from pandas import DataFrame, Series
import pandas as pd
from scipy.signal import find_peaks

from storage_evaluation_system_zzj import util, __version__, constants
from storage_evaluation_system_zzj.constants import CaseCategory, CaseResult, DEVICE_INFO_MAP, CacheDataKey, TimeFormat
from storage_evaluation_system_zzj.exception import SESError, ConfigError
from storage_evaluation_system_zzj.logger import logger
from storage_evaluation_system_zzj.util import convert_capacity

# html
_REPORT_SUMMARY_TPL = """
<div id="report-summary">
    <h1 id="title">{title}</h1>
    <h2>总览</h2><div class="sep-line"></div>
    <p><b>测试开始时间: </b>{start_time}</p>
    <p><b>测试结束时间: </b><span id="test-end-time">-</span></p>
    <p><b>测试总时长: </b><span id="test-elapsed">-</span></p>
    <p><b>用例总数: </b>{case_count}</p>
    <p><div><b>用例总览: </b>{case_table}</div></p>
    <p><div><b>测试设备信息: </b>{storage_device}</div></p>
    <p><div><b>测试参数: </b>{parameters}</div></p>
</div>
"""
_CATEGORY_TPL = """
<div id="{cat_type}" class="category">
    <h2>{name}</h2>
    <div class="sep-line"></div>
</div>"""
_CASE_TPL = """
<div id="{id}" class="case">
    <h3>{name}<span>{id}</span></h3>
    <div class="case-summary"></div>
    <div class="case-content"></div>
</div>"""

# 未执行的用例
_NE_CASE_SUM_TPL = """
<p><b>执行结果: </b><span class="case-result case-result-{result_css}">{result}</span></p>
"""
# 已执行的用例
_CASE_SUM_TPL = """
<p><b>开始时间: </b>{c_start}</p>
<p><b>结束时间: </b>{c_end}</p>
<p><b>执行时长: </b>{c_elapsed}</p>
<p><b>执行结果: </b><span class="case-result case-result-{result_css}">{result}</span></p>
"""

_CASE_SUM_MSG_TPL = '<div class="case-sum-message"><b>说明: </b>{message}</div>'
_SIDEBAR_CASE_TPL = '<li id="sidebar_{cid}"><a href="#{cid}" title="{cid}">{cname}</a></li>'

# 绘图参数
matplotlib.use('Agg')
matplotlib.rcParams['font.size'] = 10
matplotlib.rcParams['savefig.dpi'] = 120  # 图片像素
matplotlib.rcParams['figure.dpi'] = 150  # 分辨率
matplotlib.rcParams['axes.unicode_minus'] = False

logging.getLogger('matplotlib.font_manager').setLevel(logging.ERROR)

COLORED_SIDEBAR_MARKER = True


@dataclass
class TitledContent:
    """含标题的报告内容"""
    title: str
    content: BeautifulSoup
    comment: str = None

    def to_soup(self, parent="div", title_tag="h4", inline_title=False):
        """生成html对象

        Args:
            parent: 所属标签类型
            title_tag: 标题标签类型
            inline_title: 标题是否不空行。元素排版需自行处理，常用于标题后跟折叠按钮
        """
        ele = ReportUtil.str2element(f"<{parent}></{parent}>")
        title_clazz = ' class="inline-block-element"' if inline_title else ""
        title_str = f"<{title_tag}{title_clazz}>{self.title}</{title_tag}>"
        ele.append(ReportUtil.str2element(title_str))
        ele.append(self.content)
        if self.comment:
            ele.append(ReportUtil.create_comment(self.comment))
        return ele


class Report:

    def __init__(self, suite):
        self.suite = suite
        self.scene_name = suite.name
        self.cases = suite.cases
        self.client_group = suite.client_group
        self.output_dir = suite.output_dir
        self.path = os.path.join(self.output_dir, f"SESReport_{self.scene_name}.html")
        self.custom_config = suite.custom_config
        image_dir = ReportUtil.get_image_dir()
        os.makedirs(image_dir, exist_ok=True)
        self.start_sec: float = None
        self.build()
        self._lock = threading.Lock()

    @property
    def html_object(self):
        return BeautifulSoup(open(self.path, encoding='UTF-8'), features="html.parser")

    def build(self):
        """创建框架"""
        title = f"{self.scene_name}场景存储测评报告"
        template_path = util.get_resource_path('report/Template.html')
        with open(self.path, 'w', encoding='UTF-8') as inf:
            with open(template_path, 'r', encoding='UTF-8') as tf:
                inf.write(tf.read())

        # 标题
        soup = self.html_object
        head_tag = soup.find("head")
        head_tag.append(ReportUtil.str2element(f"<title>{title}</title>"))

        # 总览
        self.start_sec = time.time()
        content_div = self.find_element_by_id("content", html_object=soup)
        summary_str = self.build_summary(title)
        content_div.append(ReportUtil.str2element(summary_str))

        self.build_toc_and_sum_table(soup, content_div)
        self._update_html(soup)

    def build_summary(self, title) -> str:

        def param_tag(name, value):
            return f'<p class="summary-parameters">{name} = {value}</p>'

        start_time = util.timestr()
        device_table = ReportUtil.create_device_table(self.custom_config, self.suite)

        # 用例总览
        case_table = pd.DataFrame(columns=["用例", "类型", "是否必选", "测试结果", "", "", ""])
        to_html_args = dict(border=0, justify="left", escape=False, classes="default-table")
        case_table = case_table.to_html(index=False, table_id="summary-table", **to_html_args)
        if self.suite.valid_skips:
            comment = ReportUtil.create_comment("用例在配置中被指定跳过")
            case_table += str(comment)
        case_table = ReportUtil.create_collapsable_wrapper(case_table, parent=None)

        # 测试参数
        parameters_str = ''
        for param_name in ["storage_available_capacity",
                           "total_host_memory",
                           "total_storage_memory",
                           "thread_n1", "thread_n2",
                           "fsd_group_number", "fsd_width"
                           ]:
            value = self.suite.get_required_parameter(param_name)
            parameters_str += param_tag(param_name, value)

        p_ele = ReportUtil.create_collapsable_wrapper(parameters_str, expand=False, parent=None)
        return _REPORT_SUMMARY_TPL.format(
            storage_device=device_table,
            case_table=case_table,
            title=title,
            start_time=start_time,
            case_count=len(self.cases),
            parameters=str(p_ele)
        )

    def build_toc_and_sum_table(self, soup, content_div):
        """创建左侧栏及用例总览表"""
        cate_cases_content_dict: Dict[CaseCategory: BeautifulSoup] = dict()
        raw_cate_cases = dict()

        # 填充用例类型
        for case in self.cases:
            cate = case.category.upper()
            if cate not in cate_cases_content_dict:
                category_str = _CATEGORY_TPL.format(cat_type=cate, name=util.get_category(cate))
                category_ele = ReportUtil.str2element(category_str)
                cate_cases_content_dict[cate] = category_ele
                raw_cate_cases[cate] = []

            case_str = _CASE_TPL.format(id=case.cid, name=case.cname)
            cate_cases_content_dict[cate].append(ReportUtil.str2element(case_str))
            raw_cate_cases[cate].append(SimpleNamespace(id=case.cid,
                                                        name=case.cname,
                                                        required=case.required,
                                                        major_id=case.major_id
                                                        ))

        summary_table = self.find_element_by_id("summary-table", html_object=soup)

        # 左侧栏、用例总览
        toc = self.find_element_by_id("toc", html_object=soup)
        for cate_type, cate_obj in CaseCategory.__members__.items():
            if cate_type in cate_cases_content_dict:
                category_ele = cate_cases_content_dict[cate_type]
                content_div.append(category_ele)

                case_li_list = []
                for case in raw_cate_cases[cate_type]:
                    case_li_list.append(_SIDEBAR_CASE_TPL.format(cid=case.id, cname=case.name))
                    required = "是" if case.required else "否"
                    prefix = "* " if case.major_id in self.suite.valid_skips else ""
                    summary_table.append(ReportUtil.str2element(f'<tr id=indicator_{case.id}>'
                                                                f'<td><span class="prefix">{prefix}</span>{case.name}</td>'
                                                                f'<td>{cate_obj.value}</td>'
                                                                f'<td>{required}</td>'
                                                                f'</tr>'))

                # 左侧栏
                li_list = "".join(case_li_list)
                ul_ele = ReportUtil.str2element(f"<ul>{li_list}</ul>")
                toc.append(ReportUtil.str2element(f"<b>{cate_obj.value}</b>"))
                toc.append(ul_ele)

        sidebar_footer = self.find_element_by_id("sidebar-footer", html_object=soup)
        sidebar_footer.string.replace_with(f"storage-evaluation-system v{__version__}")

    def find_element_by_id(self, id, html_object: BeautifulSoup = None):
        """ 通过id查找唯一元素

        Args:
            id: id值
            html_object: 为None时，打开新的报告对象

        """
        if not html_object:
            html_object = self.html_object
        return html_object.find(attrs={"id": id})

    def insert_casestep_content(self, case_id, content: BeautifulSoup):
        """插入用例报告内容"""
        soup = self.html_object
        case_ele = self.find_element_by_id(case_id, html_object=soup)
        content_ele = case_ele.find("div", attrs={"class": "case-content"})
        content_ele.append(content)
        self._update_html(soup)

    def handle_case_report(self,
                           case_id: str,
                           result: CaseResult,
                           start_time: int,
                           messages=[],
                           records=[],
                           content: BeautifulSoup = None,
                           ):
        """用例报告

        Args:
            case_id: 用例id
            result: 用例结果
            start_time: 开始时间
            records: 用例步骤信息
            messages: 用例结果补充信息
            content: 用例报告
        """

        def _handle_case_report():
            nonlocal result

            soup = self.html_object
            case_ele = self.find_element_by_id(case_id, html_object=soup)

            summary_ele = case_ele.find("div", attrs={"class": "case-summary"})
            if result == CaseResult.UNKNOWN:
                result = "-"

            end_time = time.time()
            elapsed = datetime.timedelta(seconds=int(end_time - start_time))

            kwargs = dict(result_css=result.lower(), result=result)
            template = _NE_CASE_SUM_TPL
            if case_id not in self.suite.valid_skips:
                template = _CASE_SUM_TPL
                kwargs.update(c_start=util.timestr(start_time),
                              c_end=util.timestr(end_time),
                              c_elapsed=elapsed)
            ele_str = template.format(**kwargs)
            summary_ele.append(ReportUtil.str2element(ele_str))

            if messages:
                if len(messages) == 1:
                    message = messages[0]
                else:
                    ms_list = "".join([f"<li>{m}</li>" for m in messages])
                    message = f'<ul>{ms_list}</ul>'
                summary_ele.append(ReportUtil.str2element(_CASE_SUM_MSG_TPL.format(message=message)))

            content_ele = case_ele.find("div", attrs={"class": "case-content"})
            if records:
                content_ele.append(ReportUtil.create_record_table(records))
            if content:
                content_ele.append(content)

            # 目录渲染
            if COLORED_SIDEBAR_MARKER:
                sidebar_li = self.find_element_by_id(f"sidebar_{case_id}", html_object=soup)
                if sidebar_li:
                    sidebar_li["class"] = f"case-result-{result.lower()}"
            self._update_html(soup)

        with self._lock:  # 避免同时修改报告
            _handle_case_report()

    def insert_summary_table(self, case_id: str, indicators: tuple = None):
        if not indicators:
            return

        soup = self.html_object
        case_summary = self.find_element_by_id(f"indicator_{case_id}", html_object=soup)

        summary = ""
        if isinstance(indicators, list):
            case_common = ""
            for item in case_summary.find_all("td")[1:]:
                case_common += f"{item}"
            for case_indicator in indicators:
                summary = ""
                for indicator in case_indicator[1]:
                    summary += f"<td>{indicator.name}: {indicator.value}</td>"
                case_name = f"<td>{case_indicator[0]}</td>"
                case_summary.insert_before(ReportUtil.str2element(f"<tr>{case_name}{case_common}{summary}</tr>"))
            case_summary.decompose()
        else:
            if not isinstance(indicators, tuple):
                indicators = (indicators,)
            for indicator in indicators:
                summary += f"<td>{indicator.name}: {indicator.value}</td>"
            case_summary.append(ReportUtil.str2element(summary))
        self._update_html(soup)

    def add_energy_consumption(self, value: float):
        """添加能耗值"""
        soup = self.html_object
        summary_table = self.find_element_by_id("summary-table", html_object=soup)
        summary_table.append(ReportUtil.str2element(f'<tr id=indicator_energy_consumption>'
                                                    f'<td>基础能效</td>'
                                                    f'<td>能效</td>'
                                                    f'<td>否</td>'
                                                    f'<td>功率(W): {value}</td>'
                                                    f'</tr>'))
        self._update_html(soup)

    def finish(self):
        """根据套餐名结束测试报告回填结果"""
        logger.info("Preparing report package")
        self._handle_user_config_file()

        soup = self.html_object
        end_time_ele = self.find_element_by_id("test-end-time", html_object=soup)
        end_sec = int(time.time())
        end_time = util.timestr(end_sec)
        end_time_ele.string.replace_with(end_time)

        elapsed_ele = self.find_element_by_id("test-elapsed", html_object=soup)
        elapsed = datetime.timedelta(seconds=int(end_sec - self.start_sec))
        elapsed_ele.string.replace_with(str(elapsed))
        self._update_html(soup)

        # 所有文件复制到打包目录下
        dir_name = "{}_{}_{}".format(constants.REPORT_DIR_PREFIX,
                                     self.scene_name,
                                     util.timestr(fmt=TimeFormat.COMPACT))
        tmp_zip_dir = os.path.join(os.path.dirname(self.output_dir), dir_name)
        shutil.rmtree(tmp_zip_dir, ignore_errors=True)
        shutil.copytree(self.output_dir, tmp_zip_dir)
        zip_dir = os.path.join(self.output_dir, dir_name)
        shutil.move(tmp_zip_dir, zip_dir)  # 将目录移动到output下
        self.__encrypt(zip_dir)
        try:
            shutil.make_archive(zip_dir,
                                'zip',
                                root_dir=self.output_dir,
                                base_dir=dir_name)
        except Exception:
            logger.debug(traceback.format_exc())
            logger.info(f"Report directory: {zip_dir}")

        zip_path = zip_dir + ".zip"
        if os.path.exists(zip_path):
            try:
                shutil.rmtree(zip_dir, ignore_errors=True)
            except Exception:
                logger.warning(f"Unable to delete temporary report directory: {zip_dir}. Try deleting manually")

            logger.info(f"Report package generated: {zip_path}")
        else:
            logger.warning(f"Generating report package error: {zip_dir}")

    @staticmethod
    def compress_folder(folder_path, output_path):
        with tarfile.open(output_path, 'w:gz') as tar:
            tar.add(folder_path, arcname=os.path.basename(folder_path))

    def _handle_user_config_file(self):
        """移除环境密码信息后保存配置文件"""
        output_config = os.path.join(self.output_dir, "user_config.xml")

        def pretty(element: ET.Element, indent='\t', newline='\n', level=0):
            """ XML格式美化 """
            if element is not None:
                if (element.text is None) or element.text.isspace():
                    element.text = newline + indent * (level + 1)
            temp = list(element)
            for sub_element in temp:
                if temp.index(sub_element) < (len(temp) - 1):
                    sub_element.tail = newline + indent * (level + 1)
                else:
                    sub_element.tail = newline + indent * level
                pretty(sub_element, indent, newline, level=level + 1)

        root = self.custom_config.getroot()
        new_config_ele = ET.Element(root.tag)
        for child in root:
            if child.tag == "environment":
                for client in child:
                    if client.get("password") is not None:
                        client.set("password", "***")
            new_config_ele.append(child)

        pretty(new_config_ele)
        ET.ElementTree(new_config_ele).write(output_config,
                                             encoding='utf-8',
                                             xml_declaration=True)

    def _update_html(self, soup):
        with open(self.path, 'w', encoding='UTF-8') as f:
            f.write(str(soup))

    def __encrypt(self, zip_dir):
        """报告加密"""
        # 暂只支持unix
        resp = self.suite.master_host.exec_command(f"uname -m")
        if resp.status_code != 0:
            logger.debug(f"Unsupported OS for generating report validation file")
            return

        filename = "encrypt_arm" if self.suite.master_host.is_aarch64 else "encrypt"
        bash_path = util.get_resource_path(f"report/{filename}")
        validation_fname = constants.VALIDATION_FILENAME
        self.suite.master_host.exec_command(f"chmod 777 {bash_path}")
        self.suite.master_host.exec_command(f"{bash_path} {zip_dir} {validation_fname}")
        filepath = os.path.join(zip_dir, validation_fname)
        if os.path.exists(filepath):
            logger.debug(f"Report validation file generated: {filepath}")
        else:
            logger.debug(f"Generating report validation file failed: {filepath}")


class ReportUtil:
    THEME_COLOR = "#4169E1"
    DIFF_LINE_COLORS = [
        {"name": "Before op", "color": "#FF0000"},
        {"name": "After op", "color": "#EE82EE"}
    ]

    @staticmethod
    def str2element(string) -> BeautifulSoup:
        """文本转html元素"""
        return BeautifulSoup(string, 'html.parser')

    @staticmethod
    def combine_elements(*elements: Union[BeautifulSoup, TitledContent], parent="div"):
        """ 合并多个html元素

        Args:
            *elements: 元素列表，元素可为dict或BeautifulSoup或None。为None时不展示
            parent: 包装tag

        Examples:
            data1 = TitledContent("Title 1", xxx, "yyy")
            data2 = ReportUtil.create_table(zzz)
            combine_elements( data1, data2 )
            >> html内容：
            Title 1
            {xxx}
            yyy

            {zzz}
        """

        wrapper = ReportUtil.str2element(f"<{parent}></{parent}>")
        for ele in elements:
            if ele is None:
                continue
            if isinstance(ele, TitledContent):
                ele = ele.to_soup()
            wrapper.append(ele)
        return wrapper

    @staticmethod
    def create_text(text, tag="div", style=None) -> BeautifulSoup:
        """创建文本元素

        Args:
            text: 文本内容
            tag: 元素标签
            style: 内联css样式
        """
        style = f'style="{style}"' if style else ""
        return ReportUtil.str2element(f"<{tag} {style}>{text}</{tag}>")

    @staticmethod
    def create_record_table(records: List[dict]):
        """创建用例步骤表"""
        table = ReportUtil.create_table(records, header_pos="v", index=True)
        return TitledContent("执行记录", table).to_soup()

    @staticmethod
    def create_table(data, header_pos="h", index=False, clazz: str = None, title: list = None):
        """创建表格

        Args:
            data: 数据
            header_pos: 表头位置。h：第一列，v：第一行
            index: 是否添加索引（行数/列数）
            clazz: 补充css class名称
            title: 用于展示的表头，需与数据一一对应
        Examples:
            data 支持格式：
            1. { "A": 1, "B": 2, "C": 3 }
            2. { "A": [1,7], "B": [2,8], "C": [3,9] }
            3. [{ "A": 1, "B": 2, "C": 3 }, { "A": 7, "B": 8, "C": 9 }]

            >> header_pos="h"
            A    1     7
            B    2     8
            C    3     9

            >> header_pos="v", index=True
                 A      B     C
            1    1      2     3
            2    7      8     9

            >> header_pos="v", index=False
                 A      B     C
                 1      2     3
                 7      8     9

            >> header_pos="v", index=False, title=[a, b, c]
                 a      b     c
                 1      2     3
                 7      8     9
        """
        if header_pos not in ["h", "v"]:
            raise ValueError("header_pos must be 'h'(for horizontal) or 'v'(for vertical)")

        _index = index
        classes = ["default-table"]
        if clazz:
            classes.append(clazz)

        to_html_args = dict(border=0, justify="left", escape=False, classes=classes)
        if isinstance(data, dict):
            # 若dict值非列表，自动封装
            if not isinstance(list(data.values())[0], list):
                for k, v in data.items():
                    data[k] = [v]

            orient = "columns"
            if header_pos == "h":
                orient = 'index'
                _index = True
                to_html_args["header"] = index

            df = pd.DataFrame.from_dict(data, orient=orient)
            if index:
                if header_pos == "h":
                    df.columns += 1
                else:
                    df.index += 1

        elif isinstance(data, list):
            df = pd.DataFrame.from_records(data)
            return ReportUtil.create_table(df.to_dict(orient="list"),
                                           header_pos=header_pos,
                                           index=index,
                                           clazz=clazz)
        else:
            df = data

        if title:
            df.columns = title

        table = df.to_html(index=_index, **to_html_args)
        return ReportUtil.str2element(table)

    @staticmethod
    def get_image_dir():
        """图片保存路径"""
        from storage_evaluation_system_zzj.suite import global_output_dir
        return os.path.join(global_output_dir, "images")

    @staticmethod
    def create_img_element(picture_name, title=None, title_tag="h4", inline_title=True):
        """创建图片（将被包装在可折叠标签中）"""
        img = f'<img style="max-width: 100%" src="./images/{picture_name}"></img>'
        return ReportUtil.create_collapsable_wrapper(img, title=title, title_tag=title_tag, inline_title=inline_title)

    @staticmethod
    def create_collapsable_wrapper(inner: str,
                                   expand=True,
                                   parent: Optional[str] = "div",
                                   title=None,
                                   title_tag="b",
                                   inline_title=False):
        """创建可折叠包装器，点击按钮将折叠/展开后面所有的相邻元素

        Args:
            inner: 内部元素
            expand: 初始是否为展开状态
            parent: 内部元素所属元素
            title: 添加在上方的标题
            title_tag: 标题标签
            inline_title: 标题是否不换行
        """
        string = f'<div class="triangle-wrapper" data-expand="{expand}" onclick="toggleSiblings(this)">' \
                 f'     <div class="triangle down"></div>' \
                 f'</div>' \
                 f'<div class="collapse_button_sep"></div>' \
                 f'{inner}'
        if parent:
            if title:
                return TitledContent(title, ReportUtil.str2element(string)).to_soup(parent=parent,
                                                                                    title_tag=title_tag,
                                                                                    inline_title=inline_title)
            else:
                string = f"<{parent}>{string}</{parent}>"
        elif title:
            string = f"<{title_tag}>{title}</{title_tag}>{string}"
        return ReportUtil.str2element(string)

    @staticmethod
    def create_comment(comment, prefix="*"):
        """创建注释文本"""
        return ReportUtil.str2element(f'<div class="comment">{prefix} {comment}</div>')

    @staticmethod
    def create_subplots(x: list, data: DataFrame, labels: List[dict], ncols=2, ax_handler=None, **kwargs):
        """使用DataFrame数据画图

        Args:
            x: x轴数据
            data: y轴数据（可为多列）
            labels: 子图信息，汇总图图以此顺序排版。dict格式：
               name: 子图名称（必填）
               xlabel: x轴说明（可选，默认为time）
               ylabel: y轴说明（必填）
               例：[
                   {"name": "Rate", "ylabel": "ops"},
                   {"name": "Resp", "ylabel": "ms", "xlabel": "time"}
               ]
            ncols: 子图列数

        Returns: 已生成并保存的图片名称

        """

        # 排版子图
        nrows = (len(labels) + ncols - 1) // ncols
        fig, axes_array = plt.subplots(nrows, ncols, figsize=(20, 4 * nrows), constrained_layout=True, facecolor='none')

        ax_count = 0
        if not ax_handler:
            ax_handler = ReportUtil.handle_basic_ax

        for index, axs in enumerate(axes_array):
            # 单行时axes为一维数组，多行时为二维
            if isinstance(axs, Axes):
                label = labels[index]
                sub_data = data[label["name"]]
                ax_handler(x, sub_data, axs, label, **kwargs)
            else:
                for ax in axs:
                    if ax_count >= len(labels):  # 处理奇数子图情况
                        break
                    label = labels[ax_count]
                    sub_data = data[label["name"]]
                    ax_handler(x, sub_data, ax, label, **kwargs)
                    ax_count += 1

        if len(labels) % 2 == 1:  # 若子图为奇数，移除最后的空白图
            fig.delaxes(axes_array[-1, -1])

        fig_name = ReportUtil.save_fig(fig)
        plt.close()
        return fig_name

    @staticmethod
    def init_ax(x, data: DataFrame, ax_obj: Axes, label: dict):
        """通用ax处理"""
        label_name = label["name"]
        ax_obj.tick_params(axis='x', labelrotation=45, labelsize=9)
        ax_obj.set_ylim(0.8 * min(data), 1.2 * max(data))
        ax_obj.set_ylabel(label["ylabel"], rotation=90)
        ax_obj.set_xlabel(label.get("xlabel", "time"))
        ax_obj.grid(axis='y', linestyle='--', color="#d9d9d9")
        ax_obj.patch.set_alpha(0.3)
        ax_obj.set_title(f"{label_name} Chart", fontsize=8, fontweight='bold')
        # 背景颜色
        ax_obj.patch.set_facecolor('silver')
        ax_obj.fill_between(x, y1=data, color=ReportUtil.THEME_COLOR, alpha=0.5)

    @staticmethod
    def handle_basic_ax(x: list,
                        data: DataFrame,
                        ax_obj: Axes,
                        label: dict,
                        **kwargs):
        """默认方式处理单张子图

        绘制曲线，并填满空间

        Args:
            x: x轴数据
            data: 单个坐标轴数据
            ax_obj: 坐标轴对象
            label: 见 ``create_subplots`` labels

        """
        ReportUtil.init_ax(x, data, ax_obj, label)

        label_name = label["name"]
        ax_obj.plot(x, data, label=label_name, color=ReportUtil.THEME_COLOR)

    @staticmethod
    def handle_ax_add_inflection(x,
                                 data: Union[Series, DataFrame],
                                 ax_obj: Axes,
                                 label: dict,
                                 added_dots=[],
                                 added_label=None,
                                 threshold=0.1,
                                 avg_range: tuple = None,
                                 avg_threshold=0.3,
                                 mark_range: tuple = None,
                                 **kwargs):
        """峰谷值变化比例计算及标记

        Args:
            x: x轴数据
            data: 单个坐标轴数据
            ax_obj: 坐标轴对象
            label: 见 ``create_subplots`` labels
            added_dots: 补充点坐标
            added_label: 补充点图标
            threshold: 峰谷值变化比例的阈值，当abs(比例) >= 该值时，此峰谷变化将被标记
            avg_range: 平均值计算样本区间，格式：(i, j)。区间为左开右闭
                峰/谷值在 平均值 * (1 ± avg_threshold) 范围内时不标记
            avg_threshold: 根据平均值标记波动率的阈值
            mark_range: 仅在此区间内标记波动，格式：(i, )或(i, j)。区间为左开右闭
                仅有一个元素时，表示范围为 (i, len(data))

        Examples:
                                                ┌────────────────┐
                ↑                               │ ▼  added_label │
            30_ │   _________▼                  └────────────────┘
            20_ │  ╱         ┊╲_________▼
            10_ │ ╱          ┊┊         ┊￣`·————————
             0_ │╱___________┊┊_________┊___┊__________
                              └→ -20%      └→ -10%
                0  1  2  3  4  5  6  7  8  9  10  11

            avg_range = (1, 5)               => 平均值：30
            avg_threshold = 0.3              => 波动峰谷值均在[21, 39)内-则不视为波动
            mark_range = (7, ) 或 (7, 11)    => 从x轴索引7开始计算波动（-20%将不被标记）
            mark_range = (4, 8)              => 在x轴索引4~7内计算波动（-10%将不被标记）

        """
        # 平均值
        avg = None
        if avg_range:
            if not isinstance(avg_range, tuple) or len(avg_range) != 2:
                raise ValueError("avg_range must be a range-like tuple")
            avg = data.loc[slice(*avg_range)].mean()

        # 标记区间
        mark_start = 0
        mark_end = len(data)
        if mark_range:
            if not isinstance(mark_range, tuple) or len(avg_range) not in [1, 2]:
                raise ValueError("mark_range must be a range-like tuple")
            elif len(mark_range) == 2:
                mark_end = mark_range[1]
            mark_start = mark_range[0]

        ReportUtil.init_ax(x, data, ax_obj, label)

        # 找峰谷值
        peaks_i, _ = find_peaks(data, distance=5)
        valley_i, _ = find_peaks(-data, distance=5)

        # ratios: [(start_index, end_index, value)]
        ratios = []
        waves = sorted(list(peaks_i) + list(valley_i))
        for i, ind in enumerate(waves):
            if i >= len(waves) - 1:
                break

            if ind < mark_start or ind >= mark_end:
                continue

            next_ind = waves[i + 1]
            next_val = data[next_ind]
            cur_val = 1 if data[ind] == 0 else data[ind]  # 避免比例计算无效

            if avg and \
                    abs(cur_val - avg) / avg <= avg_threshold and \
                    abs(next_val - avg) / avg <= avg_threshold:
                # 排除波动峰、谷值都在 ± 平均值*avg_threshold 内的数据
                continue

            ratio = round((next_val - cur_val) / cur_val, ndigits=2)
            ratios.append((ind, next_ind, ratio))

        df = DataFrame(ratios, columns=["start", "end", "ratio"])
        df = df[abs(df["ratio"]) > threshold]
        label_name = label["name"]
        xarray = np.array(x)
        ax_obj.plot(xarray, data, label=label_name, color=ReportUtil.THEME_COLOR)

        # ReportUtil.paint_fluctuation(data, df, ax_obj, mark_start, mark_end, xarray)
        # 添加补充点
        if added_dots:
            ReportUtil.add_dots(ax_obj, xarray, data, added_dots, added_label)

        plt.close()

    @staticmethod
    def paint_fluctuation(data, df, ax_obj, mark_start, mark_end, xarray):
        """绘制数据涨跌区间"""
        for _, area in df.iterrows():
            start_index = int(area["start"])
            end_index = int(area["end"])
            if start_index < mark_start or start_index >= mark_end:
                continue
            ratio = area["ratio"]
            color = ReportUtil.THEME_COLOR
            filled = ax_obj.fill_between(xarray, data,
                                         where=((xarray >= xarray[start_index]) &
                                                (xarray <= xarray[end_index])),
                                         color=color,
                                         alpha=0.1)
            # 添加比例标注
            (x0, y0), (x1, y1) = filled.get_paths()[0].get_extents().get_points()
            pct = "{:.1%}".format(ratio)
            if ratio > 0:
                pct = "+" + pct
            ax_obj.text((x0 + x1) / 2, (y0 + y1) / 2, pct, ha='center', va='center', color=color, fontsize=9)

    @staticmethod
    def add_dots(ax: Axes, x, y, dots, labels):
        """补充点

        Args:
            ax: 坐标轴对象
            x: x轴
            y: 数据
            dots: 点在横坐标中的索引
            labels: 图例

        Examples:
            单个label：dots: List[int]，labels：str
                例：dots=[1,2,3], labels='labelName'
            多个label：dots: List[List[int]]，labels: List[str]
                例：dots=[ [1,2,3], [10] ], labels=['Label1', 'Label2']

        """
        colors = ["orange", "green", "red", "purple", "#FF6347", "gold"]
        if not labels:
            raise SESError("labels is required")

        if isinstance(dots[0], list):
            if not isinstance(labels, (list, tuple)):
                raise SESError("labels must be list or tuple when dots is a 2D-array")

            for i, d_group in enumerate(dots):
                ax.plot(x[d_group], y[d_group], marker="v", color=colors[i], label=labels[i])
        else:
            ax.plot(x[dots], y[dots], color="orange", marker="v", label=labels)

        ax.legend(loc='lower left', prop={'size': 8})

    @staticmethod
    def handle_dots(x, data, ax_obj, label: dict, added_dots=None, added_label=None):
        """
        增加操作点
        """
        ReportUtil.init_ax(x, data, ax_obj, label)

        label_name = label["name"]
        xarray = np.array(x)
        ax_obj.plot(xarray, data, label=label_name, color=ReportUtil.THEME_COLOR)
        if added_dots:
            if not isinstance(added_dots, list):
                added_dots = [added_dots]
            ReportUtil.add_dots(ax_obj, xarray, data, added_dots, added_label)

        plt.close()

    @staticmethod
    def handle_ax_add_differ(x,
                             data: DataFrame,
                             ax_obj: Axes,
                             label: dict,
                             range_indexes: List,
                             added_dots=[],
                             added_label=None,
                             **kwargs):
        """操作前后稳定值计算及画线

        ↑                        ▼: added_label
        40-->│___▼
             │    \         ________   <--30
             │     \      /
             │______\___/_______________
        操作前后稳定值下降：(40-30)/40 = 10%

        Args:
            x: x轴数据
            data: 单个坐标轴数据
            ax_obj: 坐标轴对象
            label: 见 ``create_subplots`` labels
            range_indexes: [[start_index, end_index],..]操作前后的起始下标，用于计算操作前后的稳定值
            added_dots: 补充点坐标
            added_label: 补充点图标
        """

        ReportUtil.init_ax(x, data, ax_obj, label)

        label_name = label["name"]
        xarray = np.array(x)
        ax_obj.plot(xarray, data, label=label_name, color=ReportUtil.THEME_COLOR)

        # 计算每个阶段的稳定平均值
        for (range_index, line_color) in zip(range_indexes, ReportUtil.DIFF_LINE_COLORS):
            start_index, end_index = range_index[0], range_index[1]
            data_range = data.loc[start_index:end_index]
            value = round(data_range.mean(), ndigits=2)
            ax_obj.axhline(value, color=line_color['color'], label=f"{line_color['name']}: {value}")

        # 添加补充点
        if added_dots:
            ReportUtil.add_dots(ax_obj, xarray, data, added_dots, added_label)
        plt.close()

    @staticmethod
    def save_fig(fig: Figure, filename=None):
        """保存图片

        Args:
            fig: 图像对象
            filename: 文件名。默认为uuid4

        Returns: 生成或传入的图片名

        """
        if not filename:
            filename = str(uuid.uuid4()).replace("-", "") + ".png"
        path = os.path.join(ReportUtil.get_image_dir(), filename)
        fig.savefig(path, bbox_inches='tight', transparent=False)
        
        return filename

    @staticmethod
    def build_perf_test_html_object(csv_file=None, avg_csv_file=None, csv_handler: Callable = None,
                                    avg_csv_handler: Callable = None, chart_title="性能数据图"):
        """绘制html对象

        Args:
            csv_file: 性能数据文件 或pandas解析后的Dataframe。为None时，不处理生成数据报告
            avg_csv_file: 性能数据平均值文件 或pandas解析后的Dataframe。为None时，不处理生成数据报告
            csv_handler: 性能数据处理方法，
                为None时使用默认方法。为自定义方法时，接收csv_file，返回已保存的图片名称
            avg_csv_handler: 性能数据平均值处理方法，
                为None时使用默认方法。为自定义方法时，接收avg_csv_file，返回已保存的图片名称
            chart_title: 图标标题

        Returns: 绘制的html对象
        """
        soup = BeautifulSoup(f"<div></div>", 'html.parser')
        target = soup.find("div")

        # avg表格
        if avg_csv_file is not None and avg_csv_handler is not None:
            try:
                df_avg = avg_csv_handler(avg_csv_file)
                if df_avg is not None:
                    table = ReportUtil.create_table(df_avg.to_dict('records'), header_pos="v")
                    target.append(TitledContent("平均性能数据", table).to_soup())
            except Exception:
                logger.error("Report: parsing avg performance data error")
                logger.debug(f"Build html object failed. File: {avg_csv_file}\n Traceback:{traceback.format_exc()}")

        # io图片
        if csv_file is not None and csv_handler is not None:
            try:
                picture_name = csv_handler(csv_file)
                if picture_name is not None:
                    pic = ReportUtil.create_img_element(picture_name, chart_title)
                    target.append(pic)
            except Exception:
                logger.error("Report: parsing performance data error")
                logger.debug(f"Build html object failed. File: {csv_file}\n Traceback:{traceback.format_exc()}")
        return soup

    @staticmethod
    def create_device_table(custom_config, suite):
        """创建设备信息表格
        Args:
            custom_config: 用户配置
            suite: 测试套信息
        Returns: 设备信息表格，html对象
        """
        device_table = []
        environment = custom_config.find("environment")

        for device_type in ["storage"]:
            device = {key: [] for key in DEVICE_INFO_MAP[device_type]}

            def add_client_info(client, device):
                facts = client.findall("fact")
                if not facts:
                    raise ConfigError(f"Requires fact info for {device_type}")
                for fact in facts:
                    info = "-"
                    key = fact.attrib.get("name")
                    if key in DEVICE_INFO_MAP[device_type]:
                        info = fact.text
                    device[key].append(info)
                # 处理特殊情况
                if device_type == "storage":
                    device["TOTAL_NODE_NUM"] = suite.get_cache_data(CacheDataKey.TOTAL_NODE_NUM)
                    device["SINGLE_DISK_CAPACITY"] = (
                        convert_capacity(suite.get_cache_data(CacheDataKey.SINGLE_DISK_CAPACITY),
                                         target_unit="TB",
                                         n_digits=2,
                                         readable=True))
                    device["NUMBER_OF_HARD_DRIVE"] = suite.get_cache_data(CacheDataKey.TOTAL_DISK_NUM)

            for c in environment:
                if c.tag == device_type:
                    add_client_info(c, device)
            device_table.append(ReportUtil.create_table(data=device,
                                                        header_pos="v",
                                                        clazz="device-table",
                                                        title=DEVICE_INFO_MAP[f"{device_type}_ch"]))

        table = ReportUtil.combine_elements(device_table[0])
        return ReportUtil.create_collapsable_wrapper(str(table), expand=False, parent=None)

    @staticmethod
    def sort_by_client_role(client):
        """按客户端名称排序"""
        parts = re.split(r'(\d+)', client.role)
        # 将数字部分转换为整数以便进行正确的排序
        return [int(part) if part.isdigit() else part for part in parts]

    @staticmethod
    def get_diff_percent(after_data, before_data, after_num=None, before_num=None, ndigit=3, overflow_control=False):
        """获取差值百分比
        
        公式： (subtrahend - minuend) / subtrahend
        
        Args:
            before_data: 操作前数据
            before_num: 操作前控制器数量
            after_data: 操作后数据
            after_num: 操作后控制器数量
            ndigit: 保留小数
            overflow_control: 是否纠偏、数据保持在0-100内
        """
        try:
            if after_num is not None and before_num is not None:
                before_rate = round(before_data / before_num, ndigit)
                after_rate = round(after_data / after_num, ndigit)
                res = round(after_rate / before_rate * 100, ndigit)
            else:
                res = round((before_data - after_data) / before_data * 100, ndigit)

            if overflow_control:
                if res > 100:
                    res = 100
                elif res < 0:
                    res = 0
            return f"{int(res)}%"
        except:
            logger.error(f"Calculating diff percent error: {locals()}")
            logger.debug(traceback.format_exc())
            return "-"
