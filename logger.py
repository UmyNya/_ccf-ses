# -*- coding: UTF-8 -*-

import logging
import logging.config
import os
import threading
import traceback


def exception_wrapper(func):
    """线程函数装饰器

    被装饰函数应优先自行处理所有预期可能有的异常
    """

    def wrapper(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except Exception as e:
            logger.error(e)
            logger.debug(traceback.format_exc())

    return wrapper


def set_logging(output_dir):
    threading.current_thread().name = "Main"
    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "dev": {
                "format": "[%(asctime)s][%(levelname)s][%(threadName)s]"
                          "[%(filename)s:%(funcName)s:%(lineno)d]: %(message)s",
            },
            "simple": {
                "format": "[%(asctime)s][%(levelname)s][%(threadName)s] %(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "simple",
                "level": "INFO",
                'stream': 'ext://sys.stdout'
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "simple",
                "level": "INFO",
                "encoding": "utf-8",
                "filename": os.path.join(output_dir, "user.log"),
            },
            "file_developer": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "dev",
                "level": "DEBUG",
                "encoding": "utf-8",
                "filename": os.path.join(output_dir, "ses.log"),
            }
        },
        "loggers": {
            "ses": {
                "handlers": ["console", "file", "file_developer"],
                "level": "DEBUG",
                "propagate": False,
            },
        },
        "root": {
            "handlers": ["console", "file", "file_developer"],
            "level": "DEBUG",
        },
    }

    logging.config.dictConfig(logging_config)


logger = logging.getLogger()
