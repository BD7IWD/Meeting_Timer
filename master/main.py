# -*- coding: utf-8 -*-
"""主控端主入口。"""
import os
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402

from common.const import (APP_NAME, APP_TITLE, AUTHOR, AUTHOR_URL,  # noqa: E402
                          VERSION)
from common.logutil import log_path, setup_logger  # noqa: E402
from master.ui_master import MasterWindow  # noqa: E402

def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    console = "--console" in argv

    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    # 不要设 applicationDisplayName：Qt 会把它自动拼到窗口标题后面，
    # 变成「会议倒计时 主控端 v3.0.0 - 会议倒计时 主控端」这种重复。
    # 主控窗口的标题已经在 MasterWindow 里写全了。
    # 任务栏/弹窗统一带主控图标
    from PyQt5.QtGui import QIcon
    from common.assets import asset_path
    app.setWindowIcon(QIcon(asset_path("master.ico")))

    # 注意：主控端**故意不做单实例限制**。
    # 多主控版本允许同一台电脑开多个主控窗口（各管一部分会议室、或自己测试），
    # master/server.py 会自动给每个实例分配一组空闲端口（UDP 47820+i*10 / TCP 47821+i*10），
    # 所以它们互不冲突。客户端的单实例锁则必须保留（一台机器只该有一个客户端）。

    log = setup_logger("master", "master.log", console=console)
    log.info("=" * 60)
    log.info("%s 主控端 v%s 启动", APP_TITLE, VERSION)
    log.info("作者：%s；%s", AUTHOR, AUTHOR_URL)
    log.info("日志文件：%s", log_path("master.log"))

    window = MasterWindow(console=console)
    window.show()

    exit_code = app.exec_()
    log.info("主控端退出，返回码 %s", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
