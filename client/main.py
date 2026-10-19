# -*- coding: utf-8 -*-
"""客户端主入口：静默后台运行，只有托盘图标 + 到点横幅。

启动顺序
--------
1. 单实例锁（QLocalServer 命名管道）——防止鼠标双击两次开出两个进程抢托盘
2. 读配置（自动发现为主，手填 master_ip 兜底）
3. 起网络 worker 线程，开始找主控
4. 装托盘图标，全程无主窗口

线程模型
--------
UI 线程：倒计时控制器（要弹窗）、横幅、托盘
worker 线程：所有网络收发
两者只通过信号槽通信；UI 每 200ms 把 (状态, 剩余秒数) 排队给
worker 塞进心跳——计时只在 UI 线程算一次，不存在两处真相。
"""
import os
import sys

# 打包成 exe 后，PyInstaller 会把 packages 放进 _MEIPASS，
# 源码运行时把工程根目录塞进 sys.path，两种方式都能 import common/master/client
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QMetaObject, QObject, QThread, Qt, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QInputDialog, QLineEdit,  # noqa: E402
                             QMessageBox, QSystemTrayIcon)
from PyQt5.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PyQt5.QtCore import QCoreApplication  # noqa: E402

from client.banner import BannerManager  # noqa: E402
from client.countdown import CountdownController  # noqa: E402
from client.net import ClientNet  # noqa: E402
from client.tray import TrayIcon  # noqa: E402
from common.config import load_client_config, save_client_config  # noqa: E402
from common.identity import default_client_name, machine_name  # noqa: E402
from common.const import (  # noqa: E402
    APP_NAME,
    APP_TITLE,
    AUTHOR,
    AUTHOR_URL,
    CLIENT_NAME_MAX,
    BANNER_DONE_SUB_TEXT,
    BANNER_DONE_TEXT,
    BANNER_SCREENS_DEFAULT,
    VERSION,
)
from common.logutil import log_path, setup_logger  # noqa: E402

_SINGLE_INSTANCE_KEY = APP_NAME + "-Client-Singleton"


class _RuntimeBridge(QObject):
    """把 UI 线程的 (状态, 剩余) 排队送到 worker 线程。"""

    updated = pyqtSignal(str, "qint64")


#: 设为 1 就允许同一台电脑开多个客户端（测试/演示用）。
#: 多开时每个实例会带进程号作为会话 ID，主控能把它们当成不同的机器分别认领。
MULTI_INSTANCE_ENV = "MEETINGTIMER_ALLOW_MULTI_CLIENT"


def _acquire_single_instance():
    """拿单实例锁。成功返回 QLocalServer，失败（已有实例在跑）返回 None。

    ⚠️ 正确写法是"先试着连一下"：连得上就说明已经有实例在跑。
    原来是直接 removeServer() 再 listen() —— removeServer 会把**正在运行**的
    那个实例的锁删掉，于是第二个、第三个实例都能启动。实测开来 3 个客户端，
    它们共用同一份配置（client_id 相同），主控列表里只剩最后一行有内容。
    """
    probe = QLocalSocket()
    probe.connectToServer(_SINGLE_INSTANCE_KEY)
    if probe.waitForConnected(300):
        probe.abort()
        return None                      # 已经有实例了
    probe.abort()
    QLocalServer.removeServer(_SINGLE_INSTANCE_KEY)   # 清理崩溃后残留的名字
    server = QLocalServer()
    if server.listen(_SINGLE_INSTANCE_KEY):
        return server
    return None


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    console = "--console" in argv

    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName("%s 客户端" % APP_TITLE)
    # 取名弹窗、消息框、托盘统一用客户端图标
    from PyQt5.QtGui import QIcon
    from common.assets import asset_path
    app.setWindowIcon(QIcon(asset_path("client.ico")))
    # 关键：没有主窗口，最后一个窗口关掉也不能退出进程
    app.setQuitOnLastWindowClosed(False)

    # ---------------------------------------------------- 1. 单实例
    # 允许同机多开的两种方式（测试/演示用）：
    #   1) 命令行加 --allow-multi  （推荐，双击时可在快捷方式"目标"后面加）
    #   2) 环境变量 MEETINGTIMER_ALLOW_MULTI_CLIENT=1
    allow_multi = (os.environ.get(MULTI_INSTANCE_ENV) == "1"
                   or "--allow-multi" in argv)
    lock = None
    if not allow_multi:
        lock = _acquire_single_instance()
    if (not allow_multi) and lock is None:
        QMessageBox.warning(None, APP_TITLE,
                            "客户端已经在运行了。\n\n请查看屏幕右下角托盘区的图标。")
        return 2

    # ---------------------------------------------------- 2. 配置 & 日志
    cfg = load_client_config()
    log = setup_logger("client", "client.log", console=console)
    log.info("=" * 60)
    log.info("%s 客户端 v%s 启动", APP_TITLE, VERSION)
    log.info("作者：%s；%s", AUTHOR, AUTHOR_URL)
    log.info("配置来源：%s", cfg.get("_source") or "(默认值)")
    log.info("日志文件：%s", log_path("client.log"))

    # ---------------------------------------------------- 2.5 本机名称
    # 多主控版本里，主控端列表要能认出"这是哪个会议室"，所以启动时问一次名字。
    # 预填上次用过的名字（首次是电脑名），回车即可——不该让用户每次开会都重打一遍。
    picked = default_client_name(cfg)
    text, accepted = QInputDialog.getText(
        None, "%s 客户端" % APP_TITLE,
        "给这台电脑起个名字，主控端会显示它：\n"
        "（例如：301会议室 / Q3季度汇报 / 前台大屏）\n\n",
        QLineEdit.Normal, picked)
    if accepted:
        picked = (text or "").strip()[:CLIENT_NAME_MAX]
    picked = picked or machine_name()
    cfg["name"] = picked
    cfg["client_name"] = picked
    try:
        save_client_config(cfg, cfg.get("_source") or None)
    except (OSError, ValueError):
        pass
    log.info("本机名称：%s", picked)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("系统托盘不可用，客户端仍会后台运行，但无法从托盘退出")

    # ---------------------------------------------------- 3. 组件
    screens_mode = cfg.get("banner_screens") or BANNER_SCREENS_DEFAULT
    banner = BannerManager(screens_mode)
    banner.sigLog.connect(log.info)

    countdown = CountdownController()

    net_thread = QThread()
    net_thread.setObjectName("client-net")
    net = ClientNet(cfg)
    net.moveToThread(net_thread)

    bridge = _RuntimeBridge()
    bridge.updated.connect(net.set_runtime)      # 自动变成跨线程排队调用

    tray = TrayIcon()
    tray.show()

    # ---------------------------------------------------- 4. 接线
    # 网络 -> 倒计时
    net.sigStart.connect(countdown.start)
    net.sigAddTime.connect(countdown.add_time)
    net.sigStop.connect(countdown.stop)
    net.sigReset.connect(countdown.reset)

    # 网络 -> 托盘 / 日志
    net.sigStatus.connect(tray.set_status)
    net.sigClaim.connect(tray.set_owner)
    net.sigClaim.connect(lambda mid, name: log.info(
        "占用状态：%s", ("已被「%s」控制" % name) if mid else "空闲"))
    net.sigStatus.connect(lambda s: log.info("状态：%s", s.get("detail")))
    net.sigLog.connect(log.info)

    # 倒计时 -> 横幅
    def on_node_fired(minutes):
        text = countdown.node_text(minutes)
        log.info("节点提醒：%s", text)
        banner.show_node(text)

    def on_finished():
        log.info("倒计时归零")
        banner.show_done(BANNER_DONE_TEXT, BANNER_DONE_SUB_TEXT)

    countdown.nodeFired.connect(on_node_fired)
    countdown.finished.connect(on_finished)
    countdown.resumed.connect(banner.hide_all)
    countdown.stopped.connect(banner.hide_all)

    # 主控「手动提示」 -> 本机自己算自己的剩余时间，再弹出来
    # （需求：客户端自动计算自己还剩多少时间并弹出提示）
    def on_prompt():
        text = countdown.prompt_text()
        if not text:
            log.info("收到手动提示，但本机当前没有进行中的倒计时，忽略")
            return
        log.info("手动提示：%s", text)
        banner.show_prompt(text)

    net.sigPrompt.connect(on_prompt)

    # 主控「自定义提示」 -> 原样显示主控输入的文字
    # （和手动提示不同：这个跟倒计时状态无关，空闲时也要能弹）
    def on_custom(text):
        log.info("自定义提示：%s", text)
        banner.show_custom(text)

    net.sigCustom.connect(on_custom)

    # 倒计时 -> 托盘 + 心跳
    def on_state_changed(payload):
        state = payload.get("state", "idle")
        remaining = payload.get("remaining", 0)
        tray.set_countdown(state, remaining, payload.get("total", 0),
                           payload.get("label", ""))
        bridge.updated.emit(state, remaining)

    countdown.stateChanged.connect(on_state_changed)

    # 托盘 -> 网络
    tray.sigReconnect.connect(net.reconnect_now)

    def on_rename_requested():
        """托盘「设置本机名称」：改完立刻向所有主控重新注册，不用重启。"""
        cur = net.name
        text, accepted = QInputDialog.getText(
            None, "%s 客户端 - 设置本机名称" % APP_TITLE,
            "新的名字（主控端会立刻看到）：\n", QLineEdit.Normal, cur)
        if not accepted:
            return
        new_name = (text or "").strip()[:CLIENT_NAME_MAX]
        if not new_name or new_name == cur:
            return
        net.set_name(new_name)

    tray.sigRenameRequested.connect(on_rename_requested)

    # ---------------------------------------------------- 5. 退出
    def do_quit():
        log.info("客户端退出")
        try:
            QMetaObject.invokeMethod(net, "shutdown", Qt.BlockingQueuedConnection)
        except Exception:
            pass
        net_thread.quit()
        net_thread.wait(2000)
        tray.hide()
        if lock is not None:
            lock.close()
        app.quit()

    tray.sigQuit.connect(do_quit)

    # ---------------------------------------------------- 6. 开跑
    net_thread.started.connect(net.start)
    net_thread.start()

    tray.show_balloon("%s 客户端" % APP_TITLE,
                      "已在后台运行，正在寻找主控。\n右键托盘图标可查看状态。")

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
