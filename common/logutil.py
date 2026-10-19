# -*- coding: utf-8 -*-
"""日志：无界面客户端唯一的排障手段。

写到 %LOCALAPPDATA%\\MeetingTimer\\<name>.log，按天轮转，保留 7 天。
写文件失败时（只读盘/权限）自动降级为仅控制台输出，绝不因为日志把程序搞崩。
"""
import logging
import os

from .const import app_data_dir

_FORMAT = "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def log_path(filename):
    return os.path.join(app_data_dir(), filename)


def setup_logger(name, filename, console=False):
    """获取（必要时创建）一个带轮转文件输出的 logger。幂等。"""
    logger = logging.getLogger(name)
    if getattr(logger, "_mt_configured", False):
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    formatter = logging.Formatter(_FORMAT, _DATEFMT)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    try:
        from logging.handlers import TimedRotatingFileHandler

        handler = TimedRotatingFileHandler(
            log_path(filename), when="midnight", backupCount=7, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except (OSError, ValueError):
        if not console:
            # 文件日志不可用，至少保证有输出，便于用 --console 排查
            stream = logging.StreamHandler()
            stream.setFormatter(formatter)
            logger.addHandler(stream)

    logger._mt_configured = True
    return logger
