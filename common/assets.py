# -*- coding: utf-8 -*-
"""资源文件定位。

为什么要单独一个函数：PyInstaller 打成**单文件** exe 后，资源不会留在原地，
而是被解压到 `sys._MEIPASS` 指向的临时目录。直接用 `__file__` 拼路径会找不到。
"""
import os
import sys

_ASSET_DIR = "assets"


def asset_path(name):
    """返回 assets/<name> 的绝对路径（打包后指向解包目录）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        packed = os.path.join(base, _ASSET_DIR, name)
        if os.path.exists(packed):
            return packed
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, _ASSET_DIR, name)


def icon_exists(name):
    return os.path.exists(asset_path(name))