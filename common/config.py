# -*- coding: utf-8 -*-
"""客户端本地配置：默认走自动发现，这里只提供"手填主控 IP"的兜底。

查找顺序：
    1. exe/工程目录下的 client_config.json   （便携式部署，推荐）
    2. %LOCALAPPDATA%\\MeetingTimer\\client_config.json
两个都没有就在第 2 处生成一份默认配置。
"""
import json
import os

from .const import (
    APP_NAME,
    BANNER_SCREENS_DEFAULT,
    TCP_CONTROL_PORT,
    UDP_DISCOVERY_PORT,
    app_base_dir,
    app_data_dir,
)

_FILENAME = "client_config.json"

DEFAULT_CONFIG = {
    #: 手填的主控端 IP；留空 = 纯靠 UDP 自动发现
    "master_ip": "",
    "tcp_port": TCP_CONTROL_PORT,
    "udp_port": UDP_DISCOVERY_PORT,
    #: 横幅显示屏幕：all / primary / cursor
    "banner_screens": BANNER_SCREENS_DEFAULT,
    #: 客户端显示名；留空则自动用主机名
    "client_name": "",
    #: 客户端稳定身份（首次运行生成 GUID）。主控靠它认机器，
    #: 所以改名不会丢配对关系，两台同名机器也不会混。
    "client_id": "",
    "_comment": "master_ip 留空则自动发现主控；填了则在自动发现失败后使用。",
}


def config_paths():
    return [
        os.path.join(app_base_dir(), _FILENAME),
        os.path.join(app_data_dir(), _FILENAME),
    ]


def load_client_config():
    """读取配置；任何异常都回退到默认值（客户端必须能无脑启动）。"""
    cfg = dict(DEFAULT_CONFIG)
    for path in config_paths():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                for key in DEFAULT_CONFIG:
                    if key in data:
                        cfg[key] = data[key]
                cfg["_source"] = path
            return cfg
        except (OSError, ValueError):
            continue

    # 谁都没有 -> 在数据目录落一份默认，方便用户直接改
    try:
        save_client_config(cfg, os.path.join(app_data_dir(), _FILENAME))
        cfg["_source"] = os.path.join(app_data_dir(), _FILENAME)
    except OSError:
        cfg["_source"] = ""
    return cfg


# ------------------------------------------------------------------ 主控配置
_MASTER_FILENAME = "master_config.json"

DEFAULT_MASTER_CONFIG = {
    #: 主控显示名（客户端界面里会显示"已被 XXX 主控占用"）。留空 = 电脑名 + 主控
    "master_name": "",
    "_comment": "master_name 留空则自动用「电脑名 主控」。",
}


def load_master_config():
    cfg = dict(DEFAULT_MASTER_CONFIG)
    for path in (os.path.join(app_base_dir(), _MASTER_FILENAME),
                 os.path.join(app_data_dir(), _MASTER_FILENAME)):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                for key in DEFAULT_MASTER_CONFIG:
                    if key in data:
                        cfg[key] = data[key]
                cfg["_source"] = path
            return cfg
        except (OSError, ValueError):
            continue
    return cfg


def save_client_config(cfg, path=None):
    path = path or os.path.join(app_data_dir(), _FILENAME)
    payload = {k: v for k, v in cfg.items() if not k.startswith("_")}
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    return path
