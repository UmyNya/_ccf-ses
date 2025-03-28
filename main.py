# -*- coding: UTF-8 -*-

import argparse
import os
import shutil
import signal
import sys
import textwrap
import traceback
import xml.etree.ElementTree as ET
sys.path.append('/home/zwm/ccf_ses')
from storage_evaluation_system_zzj import __version__
from storage_evaluation_system_zzj import constants


def main():
    options = parse_options()

    from storage_evaluation_system_zzj.suite import Suite
    from storage_evaluation_system_zzj.logger import set_logging, logger
    from storage_evaluation_system_zzj.exception import ConfigError

    signal.signal(signal.SIGINT, signal_handler)
    config_file = options.config_file
    scenario = options.scenario
    root = ET.parse(config_file)

    output = root.find("output")
    if output is not None:
        output_dir = output.get("path")
    else:
        output_dir = os.path.join(os.getcwd(), "output")

    shutil.rmtree(output_dir, ignore_errors=True)
    os.makedirs(output_dir, exist_ok=True)

    set_logging(output_dir)
    logger.info(f"storage-evaluation-system: v{__version__}")
    logger.info("Parameters: " + ", ".join([f"{k}={v}" for k, v in options.__dict__.items()]))
    # 初始化一个测试套件对象
    suite = Suite(root,
                  scenario,
                  output_dir,
                  memory=options.memory,
                  ignore_toolcheck_error=options.ignore_toolcheck_error)
    try:
        # 运行测试
        suite.run()
    except ConfigError as e:
        suite.error_stop = True
        logger.error(e)
        logger.error("Exit with code: 1")
        sys.exit(1)
    except Exception as e:
        suite.error_stop = True
        logger.error(e)
        logger.debug(traceback.format_exc())
        logger.error("Exit with code: 1")
        sys.exit(1)


def signal_handler(a, b):
    """处理 Ctrl+C"""
    sys.exit(1)


def parse_options(args=None):
    parser = argparse.ArgumentParser(
        usage=argparse.SUPPRESS,
        allow_abbrev=False,
        description=textwrap.dedent(
            """
            Usage: ses [OPTIONS]
            
            e.g.:
              ses -s scenario -f /path/to/config.xml -m 1TB
            """
        ),
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "-f",
        "--config-file",
        type=str,
        help="A xml config file for defining run parameters. One parameter file is required."
    )
    parser.add_argument(
        "-s",
        "--scenario",
        required=True,
        type=str,
        choices=constants.SCENE_TYPES,
        help="Optional scenario name to use with the configuration file. One scenario name is required."
             "\nChoose from ({})".format(', '.join(constants.SCENE_TYPES))
    )
    parser.add_argument(
        "-m",
        "--memory",
        type=str,
        help="Total memory of the storage-system. e.g. 512GB/1TB"
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        help="Release version",
        version=f"storage-evaluation-system {__version__}"
    )
    parser.add_argument(
        "--ignore-toolcheck-error",
        action="store_true",
        help="Ignore tool validation error if there are clients that don't need dependent tools."
             "\nOr testcases in target suite don't need certain tool"
    )
    options, _ = parser.parse_known_args(args=args)

    if len(_) > 0:
        parser.error("Unrecognized arguments: %s" % ' '.join(_))
        parser.print_help()
        sys.exit(0)

    if not os.path.isabs(options.config_file):
        config_file = os.path.join(os.curdir, options.config_file)
    else:
        config_file = options.config_file

    if not os.path.exists(config_file):
        parser.error(f"Config file not exists: {config_file}")
        sys.exit(1)
    elif not config_file.endswith(".xml"):
        parser.error(f"Config file must be a xml file: {config_file}")
        sys.exit(1)

    return options
