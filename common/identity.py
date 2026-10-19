# -*- coding: utf-8 -*-
"""身份：谁是谁。

多主控版本里"认人"必须靠**稳定 ID**，不能靠名字：
用名字当身份的话，两台都填「301会议室」就混成一台了；而且用户一改名，
主控那边的配对关系也跟着乱。

所以：
  * 客户端 = 首次运行生成的 GUID（存在 client_config.json 里），名字只是显示用
  * 主控   = **每个进程**一个新 GUID（同机多开时互不混淆）+ 可配置的显示名
"""
import os
import socket
import uuid


def machine_name():
    """电脑名（客户端名称弹窗的默认值）。"""
    name = os.environ.get("COMPUTERNAME") or ""
    if not name:
        try:
            name = socket.gethostname()
        except OSError:
            name = ""
    return name or "本机"


def ensure_client_id(cfg):
    """取得（必要时生成并落盘）客户端稳定 ID。"""
    cid = str(cfg.get("client_id") or "").strip()
    if cid:
        return cid
    cid = uuid.uuid4().hex
    cfg["client_id"] = cid
    try:
        from .config import save_client_config
        save_client_config(cfg, cfg.get("_source") or None)
    except (OSError, ImportError, ValueError):
        pass
    return cid


def default_client_name(cfg):
    """弹窗的预填值：上次用过的名字 > 配置里的 client_name > 电脑名。"""
    for key in ("name", "client_name"):
        value = str(cfg.get(key) or "").strip()
        if value:
            return value
    return machine_name()


def new_master_id():
    """主控会话 ID：每个进程一个，同机多开也不会互相冒充。"""
    return uuid.uuid4().hex


def master_name(mcfg):
    """主控显示名：配置里的 master_name > 「电脑名 主控」。"""
    name = str(mcfg.get("master_name") or "").strip()
    return name or ("%s 主控" % machine_name())
