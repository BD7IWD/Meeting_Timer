# -*- coding: utf-8 -*-
"""系统托盘图标：无界面客户端唯一的"操作入口"和状态出口。

图标优先用 assets/client.ico（打包时用 --add-data 一起带上）；
万一资源缺失，会自动退回 QPainter 现画的圆点，不会变成空白托盘图标。
"""
from PyQt5.QtCore import QObject, QUrl, Qt, pyqtSignal
from PyQt5.QtGui import (QColor, QDesktopServices, QIcon, QPainter, QPen,
                         QPixmap)
from PyQt5.QtWidgets import QAction, QMenu, QMessageBox, QSystemTrayIcon

from common.assets import asset_path, icon_exists
from common.const import (APP_TITLE, AUTHOR_LINE, AUTHOR_URL,
                          BANNER_DONE_TEXT, VERSION)

_STATE_COLORS = {
    "idle": (96, 125, 139),        # 蓝灰：未在倒计时
    "running": (67, 160, 71),      # 绿：倒计时中
    "finished": (211, 47, 47),     # 红：已超时
    "offline": (158, 158, 158),    # 灰：未连接主控
}


_ICON_CACHE = {}

#: 运行中 / 已结束：右下角加个小圆点，不破坏原图，又能一眼看出状态
_DOT_COLORS = {
    "running": (76, 175, 80),
    "finished": (211, 47, 47),
}


def _client_pixmap():
    """加载 assets/client.ico 里**最大的那层**；取不到返回 None（交给兜底绘制）。

    ⚠️ 不能用 QPixmap 直接读 .ico —— 它只取第一个层（16×16），
    放大到 64 的托盘图标会糊。QIcon 能按尺寸挑层，这里直接要 256×256。
    """
    if "base" in _ICON_CACHE:
        return _ICON_CACHE["base"]
    pm = None
    try:
        if icon_exists("client.ico"):
            pm = QIcon(asset_path("client.ico")).pixmap(256, 256)
            if pm.isNull():
                pm = None
    except Exception:
        pm = None
    _ICON_CACHE["base"] = pm
    return pm


def _draw_fallback_icon(state):
    """资源缺失时的兜底：画一个圆点，绝不出现空白托盘图标。"""
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)

    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)

    color = QColor(*_STATE_COLORS.get(state, _STATE_COLORS["idle"]))
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(2, 2, size - 4, size - 4)

    pen = QPen(QColor(255, 255, 255, 240))
    pen.setWidth(5)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    center = size // 2
    painter.drawLine(center, center, center, center - 15)
    painter.drawLine(center, center, center + 12, center)
    painter.end()
    return QIcon(pm)


def make_icon(state="idle"):
    """托盘图标：用 assets/client.ico。

    * 正常：直接用原图
    * 运行中 / 已结束：右下角加绿 / 红小圆点
    * 未连接：整图压到 45% 不透明度（变灰压暗），一眼看出"没连上主控"
    """
    base = _client_pixmap()
    if base is None:
        return _draw_fallback_icon(state)

    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

    scaled = base.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    if state == "offline":
        painter.setOpacity(0.45)
    painter.drawPixmap((size - scaled.width()) // 2,
                       (size - scaled.height()) // 2, scaled)
    painter.setOpacity(1.0)

    dot = _DOT_COLORS.get(state)
    if dot:
        painter.setPen(QPen(QColor(255, 255, 255, 235), 2))
        painter.setBrush(QColor(*dot))
        d = 20
        painter.drawEllipse(size - d - 2, size - d - 2, d, d)
    painter.end()
    return QIcon(pm)


def _fmt_mmss(seconds):
    seconds = max(0, int(seconds))
    return "%02d:%02d" % (seconds // 60, seconds % 60)


class TrayIcon(QObject):
    """托盘图标 + 右键菜单。"""

    sigReconnect = pyqtSignal()
    #: 用户在菜单里点了「设置本机名称」
    sigRenameRequested = pyqtSignal()
    sigQuit = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "idle"
        self._status = {}
        self._remaining = 0
        self._total = 0
        self._owner = ""

        self._tray = QSystemTrayIcon(make_icon("idle"), parent)
        self._menu = QMenu()

        self._act_status = QAction("查看状态", self._menu)
        self._act_status.triggered.connect(self.show_status)
        self._menu.addAction(self._act_status)
        self._menu.addSeparator()

        self._act_rename = QAction("设置本机名称", self._menu)
        self._act_rename.setToolTip("主控端列表里显示的名字")
        self._act_rename.triggered.connect(self.sigRenameRequested.emit)
        self._menu.addAction(self._act_rename)

        self._act_reconnect = QAction("立即重连", self._menu)
        self._act_reconnect.triggered.connect(self.sigReconnect.emit)
        self._menu.addAction(self._act_reconnect)
        self._menu.addSeparator()

        # 作者署名：菜单里放一项，点了直接用浏览器打开主页
        self._menu.addSeparator()
        self._act_author = QAction(AUTHOR_LINE, self._menu)
        self._act_author.setToolTip("点击打开作者主页")
        self._act_author.triggered.connect(self._open_author)
        self._menu.addAction(self._act_author)
        self._menu.addSeparator()

        self._act_quit = QAction("退出", self._menu)
        self._act_quit.triggered.connect(self.sigQuit.emit)
        self._menu.addAction(self._act_quit)

        self._tray.setContextMenu(self._menu)
        self._tray.setToolTip("%s 客户端\n%s" % (APP_TITLE, AUTHOR_LINE))
        self._tray.activated.connect(self._on_activated)

    def set_owner(self, master_id, master_name):
        """被谁占用（空字符串 = 空闲）。

        ⚠️ 这个方法是被 Qt 信号调用的（net.sigClaim -> tray.set_owner）。
        PyQt5 里槽函数抛出未捕获异常会**直接 abort 整个进程**（连托盘图标一起消失），
        所以方法名必须写对 —— 之前这里误写成 _refresh_tooltip()，真名是 _refresh()，
        结果主控一认领客户端，客户端就"自己关掉了"。
        """
        self._owner = master_name if master_id else ""
        self._refresh()

    def _open_author(self):
        """点菜单里的作者项 -> 用系统默认浏览器打开主页。"""
        try:
            QDesktopServices.openUrl(QUrl(AUTHOR_URL))
        except Exception:
            pass

    # ------------------------------------------------------------ 显示
    def show(self):
        self._tray.show()

    def hide(self):
        self._tray.hide()

    def show_balloon(self, title, text):
        try:
            self._tray.showMessage(title, text, make_icon(self._state), 4000)
        except Exception:
            pass

    def _on_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_status()

    # ------------------------------------------------------------ 状态
    def set_status(self, status):
        self._status = dict(status or {})
        self._refresh()

    def set_countdown(self, state, remaining, total=0, label=""):
        self._state = state
        self._remaining = int(remaining or 0)
        self._total = int(total or 0)
        self._label = str(label or "")      # 议程模式下的议题名
        self._refresh()

    def _refresh(self):
        connected = bool(self._status.get("connected"))
        if not connected:
            icon_state = "offline"
        else:
            icon_state = self._state
        self._tray.setIcon(make_icon(icon_state))

        lines = []
        if connected:
            lines.append("已连接 %s" % (self._status.get("master") or "主控"))
        else:
            lines.append(self._status.get("detail") or "未连接主控")
        if self._state == "running":
            # 议程模式下带上议题名，托盘悬停就知道"是哪一项还剩多久"
            prefix = ("%s " % self._label) if self._label else ""
            lines.append("倒计时中，%s剩余 %s" % (prefix, _fmt_mmss(self._remaining)))
        elif self._state == "finished":
            lines.append(BANNER_DONE_TEXT)
        self._tray.setToolTip("%s\n%s\n\n%s"
                              % (APP_TITLE, "\n".join(lines), AUTHOR_LINE))

    # ------------------------------------------------------------ 状态窗
    def show_status(self):
        status = self._status or {}
        state_text = {
            "idle": "空闲（未在倒计时）",
            "running": "倒计时进行中",
            "finished": BANNER_DONE_TEXT,
        }.get(self._state, self._state)

        lines = [
            "客户端名称：%s" % (status.get("name") or "-"),
            "本机地址：%s" % (status.get("ip") or "-"),
            "网卡 MAC：%s" % (status.get("mac") or "-"),
            "版本号：v%s" % VERSION,
            "",
            "连接状态：%s" % ("已连接主控" if status.get("connected") else "未连接"),
            "主控地址：%s" % (status.get("master") or "自动发现中…"),
            "详细状态：%s" % (status.get("detail") or "-"),
            "",
            "倒计时状态：%s" % state_text,
            "剩余时间：%s" % _fmt_mmss(self._remaining),
            "",
            "当前控制：%s" % (self._owner or "空闲（未被任何主控认领）"),
            "",
            AUTHOR_LINE,
        ]
        if self._total:
            lines.append("本轮总时长：%s" % _fmt_mmss(self._total))

        box = QMessageBox()
        box.setWindowTitle("%s 客户端 - 状态" % APP_TITLE)
        box.setIcon(QMessageBox.Information)
        box.setText("会议倒计时客户端正在后台运行")
        box.setInformativeText("\n".join(lines))
        box.setStandardButtons(QMessageBox.Ok)
        box.exec_()
