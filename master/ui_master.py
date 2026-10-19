# -*- coding: utf-8 -*-
"""主控端界面。

布局从上到下：

    ┌──────────────────────────────────────────────┐
    │                 12:34                         │  剩余时间大字
    │  议程中「硬件培训」13:30~15:00  下一项：…      │  状态行
    ├──────────────────────────────────────────────┤
    │ [手动模式] [会议议程]                          │  两个标签页
    ├──────────────────────────────────────────────┤
    │ 自定义提示 [_______________] [下发提示]        │  两种模式共用
    ├──────────────────────────────────────────────┤
    │  在线客户端列表 / 运行日志                      │
    └──────────────────────────────────────────────┘

两种工作模式
------------
* **手动模式**：设一个时长，点「开始下发」——从这一刻起算 N 分钟。
* **议程模式**：预先录入一张日程表（时间 + 活动内容），打开开关后主控
  **自动跟着系统时钟跑**——到点把当前项下发下去，倒计时到该项的结束时刻；
  项与项之间静默切换；跑完议程自动回到静默。

议程模式下主控不下发"整场倒计时"，而是**逐项**下发：每进入一项就发一次
``START``，目标时刻 = 该项的结束时间，并带上议题名。这样客户端弹出来的
节点提醒就自动变成「硬件培训 剩余 10 分钟」。

主控自己不弹窗，它只是"发令枪"，同时在本地镜像一份剩余时间用于显示。
"""
from datetime import datetime

from PyQt5.QtCore import QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon

from common.assets import asset_path
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from common import agenda as ag
from common.const import (
    AGENDA_TICK_MS,
    APP_TITLE,
    AUTHOR,
    AUTHOR_LINE,
    AUTHOR_URL,
    BANNER_DONE_TEXT,
    CUSTOM_TEXT_MAX,
    DEFAULT_MINUTES,
    DEFAULT_NODE_MINUTES,
    MAX_MINUTES,
    MIN_MINUTES,
    TCP_CONTROL_PORT,
    UDP_DISCOVERY_PORT,
    VERSION,
)
from common.config import load_master_config
from common.identity import master_name as master_name_of
from common.identity import new_master_id
from common.logutil import setup_logger
from common.protocol import now_ms
from master.server import MasterServer

_STATE_TEXT = {
    "idle": "空闲",
    "running": "倒计时中",
    "finished": "已超时",
}

_CLIENT_STATE_TEXT = {
    "idle": "待机",
    "running": "倒计时中",
    "finished": "已超时",
}


def _fmt_mmss(seconds):
    seconds = max(0, int(seconds))
    return "%02d:%02d" % (seconds // 60, seconds % 60)


def _now_min():
    now = datetime.now()
    return now.hour * 60 + now.minute


# ================================================================= 录入弹窗
class AgendaItemDialog(QDialog):
    """录入/编辑一条议程：开始时间 + 结束时间 + 议题。"""

    def __init__(self, parent=None, item=None, default_start=None):
        super().__init__(parent)
        self.setWindowTitle("编辑议程" if item else "添加议程")
        self.setMinimumWidth(460)
        self._item = item

        if default_start is None:
            default_start = _now_min()
        start_text = ag.format_hhmm(item.start_min if item else default_start)
        end_text = ag.format_hhmm(item.end_min if item else default_start + 60)

        form = QFormLayout(self)
        form.setSpacing(10)

        self.edit_start = QLineEdit(start_text)
        self.edit_start.setPlaceholderText("例如 13:30")
        form.addRow("开始时间", self.edit_start)

        self.edit_end = QLineEdit(end_text)
        self.edit_end.setPlaceholderText("例如 15:00")
        form.addRow("结束时间", self.edit_end)

        self.edit_title = QLineEdit(item.title if item else "")
        self.edit_title.setPlaceholderText("例如 硬件培训")
        self.edit_title.setMaxLength(ag.TITLE_MAX)
        self.edit_title.returnPressed.connect(self._on_ok)
        form.addRow("活动内容", self.edit_title)

        self.chk_break = QCheckBox("这是休息（整个时段不提醒客户端）")
        self.chk_break.setChecked(bool(item.is_break) if item else False)
        self.chk_break.setToolTip(
            "勾上之后，这一段**不下发倒计时、也不弹任何节点提醒**。\n"
            "适合中场休息 / 茶歇 / 午餐这类不需要催时间的时段。\n"
            "主控端自己仍然显示剩余时间，方便主持人掌握节奏。")
        form.addRow("提醒", self.chk_break)

        hint = QLabel("时间可写 13:30 / 1330；结束早于开始表示跨午夜（如 23:30~01:00）")
        hint.setStyleSheet("color:#78909C;font-size:12px;")
        hint.setWordWrap(True)
        form.addRow("", hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        form.addRow("", buttons)

    def _on_ok(self):
        start = ag.parse_hhmm(self.edit_start.text())
        end = ag.parse_hhmm(self.edit_end.text())
        if start is None:
            QMessageBox.warning(self, APP_TITLE,
                                "开始时间格式不对。\n\n请填 13:30 这样的格式（或 1330）。")
            self.edit_start.setFocus()
            return
        if end is None:
            QMessageBox.warning(self, APP_TITLE,
                                "结束时间格式不对。\n\n请填 15:00 这样的格式（或 1500）。")
            self.edit_end.setFocus()
            return
        if start % ag.MINUTES_PER_DAY == end % ag.MINUTES_PER_DAY:
            QMessageBox.warning(self, APP_TITLE,
                                "开始时间和结束时间不能相同。\n\n"
                                "如果这一项跨午夜，请把结束时间填成次日的时刻"
                                "（例如 23:30 ~ 01:00）。")
            return
        title = self.edit_title.text().strip()
        if not title:
            answer = QMessageBox.question(
                self, APP_TITLE, "「活动内容」是空的，仍然添加吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                self.edit_title.setFocus()
                return
        self._result_item = ag.AgendaItem(start, end, title,
                                          is_break=self.chk_break.isChecked())
        self.accept()

    def result_item(self):
        return getattr(self, "_result_item", None)


# =================================================================== 主窗口
class MasterWindow(QMainWindow):
    """主控端主窗口。"""

    # 发往 worker 线程（自动变成 queued connection）
    # 必须和 worker 侧的 @pyqtSlot(...) 签名一致，否则 PyQt 直接抛
    # "decorated slot has no signature compatible with ..."。
    sigStartCountdown = pyqtSignal("qint64")
    sigStartUntil = pyqtSignal("qint64", "qint64", str)   # 总秒数, 目标时刻, 议题名
    sigSetNodes = pyqtSignal(object)                      # 提醒节点列表
    sigSetTargets = pyqtSignal(object)                    # 选中的客户端 id 列表
    sigClaimRequest = pyqtSignal(str)                     # 认领某台客户端
    sigReleaseRequest = pyqtSignal(str)                   # 释放某台客户端
    sigAddTime = pyqtSignal("qint64")
    sigPrompt = pyqtSignal()
    sigCustom = pyqtSignal(str)
    sigStopCountdown = pyqtSignal()
    sigResetCountdown = pyqtSignal()
    sigShutdown = pyqtSignal()

    def __init__(self, console=False):
        super().__init__()
        self._console = console
        # 界面日志同时落盘用。worker 那边是 "master.net"，这里分开好区分来源。
        self.log = setup_logger("master.ui", "master.log")
        self.state = "idle"
        self.target_ms = 0
        self.total_s = 0
        self._rows = {}
        self._clients = []
        # ---- 多主控：认领与目标集合 ----
        # 注意：这里存的是 server 给的 conn.key（连接唯一键），**不是 client_id**。
        # 用 client_id 会让"同机多开/复制配置"导致的重复 ID 互相覆盖。
        self._targets = set()        # 已认领（=已勾选）的 conn.key
        self._pending = set()        # 已发认领、等回复的 conn.key
        self._sent_targets = None
        self._suspend_table = False  # 程序化刷新表格时抑制 itemChanged
        self.mcfg = load_master_config()
        self.master_id = new_master_id()
        self.master_name = master_name_of(self.mcfg)

        # ---- 议程状态 ----
        self.agenda = ag.Agenda()
        self.node_minutes = tuple(DEFAULT_NODE_MINUTES)
        self.agenda_enabled = True
        self._agenda_index = None      # 当前正在跑第几项；None = 空闲
        self._load_agenda()

        self.setWindowTitle("%s 主控端 v%s" % (APP_TITLE, VERSION))
        # 标题栏 / 任务栏图标用 assets/master.ico
        self.setWindowIcon(QIcon(asset_path("master.ico")))
        self.resize(1000, 940)

        self._build_ui()

        # ---- 网络 worker ----
        self._thread = QThread()
        self._thread.setObjectName("master-net")
        self._server = MasterServer(self.master_id, self.master_name)
        self._server.moveToThread(self._thread)

        self.sigStartCountdown.connect(self._server.start_countdown)
        self.sigStartUntil.connect(self._server.start_countdown_until)
        self.sigSetNodes.connect(self._server.set_nodes)
        self.sigSetTargets.connect(self._server.set_targets)
        self.sigClaimRequest.connect(self._server.claim)
        self.sigReleaseRequest.connect(self._server.release)
        self.sigAddTime.connect(self._server.add_time)
        self.sigPrompt.connect(self._server.send_prompt)
        self.sigCustom.connect(self._server.send_custom)
        self.sigStopCountdown.connect(self._server.stop_countdown)
        self.sigResetCountdown.connect(self._server.reset_countdown)
        self.sigShutdown.connect(self._server.shutdown)

        self._server.sigStarted.connect(self._on_started)
        self._server.sigListenFailed.connect(self._on_listen_failed)
        self._server.sigClients.connect(self._on_clients)
        self._server.sigClaim.connect(self._on_claim_result)
        self._server.sigLog.connect(self._append_log)

        # ---- 本地显示节拍（镜像自己下发的目标时间戳）----
        self._tick = QTimer(self)
        self._tick.setInterval(300)
        self._tick.timeout.connect(self._refresh_clock)
        self._tick.start()

        # ---- 议程调度节拍（每秒对一次系统时钟）----
        self._agenda_timer = QTimer(self)
        self._agenda_timer.setInterval(AGENDA_TICK_MS)
        self._agenda_timer.timeout.connect(self._agenda_tick)
        self._agenda_timer.start()

        self._thread.started.connect(self._server.start)
        self._thread.start()

        # 把持久化的节点设置推给 worker 线程。不做这一步的话，重启主控后
        # worker 还抱着它自己的默认值，客户端收到的会是 10,5,3,1，
        # 而不是用户上次设的那一套（状态为 idle 时不会广播，安全）。
        self.sigSetNodes.emit(self.node_minutes)

        self._append_log(AUTHOR_LINE)
        self._append_log("主控端已启动，正在监听端口…")
        self._append_log("提示：首次运行若弹出 Windows 防火墙询问，请务必点「允许访问」。")
        self._refresh_agenda_table()
        if self.agenda_enabled and len(self.agenda):
            self._append_log("已载入 %d 条议程，议程模式开启中（会自动跟系统时钟跑）"
                             % len(self.agenda))
        self._refresh_buttons()

    # ================================================================ 议程存储
    def _load_agenda(self):
        data = ag.load_agenda()
        self.agenda = ag.Agenda(data.get("items") or [])
        raw = data.get("node_minutes") or []
        if raw:
            self.node_minutes = ag.parse_nodes(
                ",".join(str(n) for n in raw), DEFAULT_NODE_MINUTES)
        else:
            self.node_minutes = tuple(DEFAULT_NODE_MINUTES)
        self.agenda_enabled = bool(data.get("enabled", True))

    def _save_agenda(self):
        ag.save_agenda(self.agenda, self.node_minutes, self.agenda_enabled)

    # =================================================================== UI
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.setSpacing(12)

        # ---- 剩余时间 ----
        self.lbl_clock = QLabel("--:--")
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        font.setPixelSize(96)
        font.setBold(True)
        self.lbl_clock.setFont(font)
        self.lbl_clock.setAlignment(Qt.AlignCenter)
        self.lbl_clock.setStyleSheet("color:#1B5E20;")
        layout.addWidget(self.lbl_clock)

        self.lbl_state = QLabel("空闲 —— 设置时长后点击「开始下发」")
        self.lbl_state.setAlignment(Qt.AlignCenter)
        self.lbl_state.setStyleSheet("color:#546E7A;font-size:14px;")
        self.lbl_state.setWordWrap(True)
        layout.addWidget(self.lbl_state)

        # ---- 控制区（两个标签页）----
        box = QGroupBox("倒计时与提示控制")
        box_outer = QVBoxLayout(box)
        box_outer.setSpacing(10)

        # ---- 通用设置：提醒节点（手动模式 / 会议议程 都生效）----
        # 原来放在「会议议程」页里，但手动模式下发倒计时同样按这些节点提醒，
        # 藏在议程页里既不合理也找不到，所以提到标签页外面。
        row_nodes = QHBoxLayout()
        row_nodes.setSpacing(10)
        row_nodes.addWidget(QLabel("提醒节点"))
        self.edit_nodes = QLineEdit(ag.format_nodes(self.node_minutes))
        self.edit_nodes.setMaximumWidth(150)
        self.edit_nodes.setPlaceholderText("10,5,3,1")
        self.edit_nodes.setToolTip(
            "距倒计时结束还剩这么多分钟时弹提醒，逗号分隔。\n"
            "默认 10,5,3,1。想少弹几次就改成 5,1。\n"
            "手动模式和议程模式都按这个来；回车或点「应用」生效，\n"
            "并会立刻下发给所有已勾选的客户端。")
        self.edit_nodes.returnPressed.connect(self._on_nodes_applied)
        row_nodes.addWidget(self.edit_nodes)

        self.btn_nodes = QPushButton("应用")
        self.btn_nodes.clicked.connect(self._on_nodes_applied)
        row_nodes.addWidget(self.btn_nodes)

        self.lbl_nodes_hint = QLabel("手动模式与议程模式通用")
        self.lbl_nodes_hint.setStyleSheet("color: #777;")
        row_nodes.addWidget(self.lbl_nodes_hint)
        row_nodes.addStretch(1)
        box_outer.addLayout(row_nodes)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_manual_tab(), "手动模式")
        self.tabs.addTab(self._build_agenda_tab(), "会议议程")
        box_outer.addWidget(self.tabs)

        # ⚠️ QTabWidget 的最小高度只按「当前页」计算。两个页高度不同时，
        # 切到内容更高的「会议议程」页，整页会被压扁，最下面那排按钮就被
        # 挤出可见区域（表现就是按钮压到边框上、看起来被裁掉）。
        # 这里显式把最小高度给够：取两页里需要得更高的那个。
        for _i in range(self.tabs.count()):
            _pg = self.tabs.widget(_i)
            if _pg.layout() is not None:
                _pg.layout().activate()
        _need = max(self.tabs.widget(_i).minimumSizeHint().height()
                    for _i in range(self.tabs.count()))
        self.tabs.setMinimumHeight(
            _need + self.tabs.tabBar().sizeHint().height() + 14)
        # ---- 自定义提示（两种模式共用，放在标签页外面）----
        row2 = QHBoxLayout()
        row2.setSpacing(10)
        row2.addWidget(QLabel("自定义提示"))
        self.edit_custom = QLineEdit()
        self.edit_custom.setPlaceholderText(
            "输入要显示在客户端屏幕上的文字，回车或点「下发提示」即可，例如："
            "请关闭手机 / 休息 10 分钟 / 请保持安静")
        self.edit_custom.setMaxLength(CUSTOM_TEXT_MAX)
        self.edit_custom.setMinimumHeight(32)
        self.edit_custom.returnPressed.connect(self._on_custom_clicked)
        row2.addWidget(self.edit_custom, 1)

        self.btn_custom = QPushButton("下发提示")
        self.btn_custom.setMinimumHeight(38)
        self.btn_custom.setToolTip(
            "把这行文字发给所有在线客户端，屏幕中央弹一次（5 秒后自动消失）。\n"
            "和「手动提示」不同：这个不需要正在倒计时，任何时刻都能发。")
        self.btn_custom.clicked.connect(self._on_custom_clicked)
        row2.addWidget(self.btn_custom)
        box_outer.addLayout(row2)

        layout.addWidget(box)

        # ---- 客户端列表 ----
        self.lbl_clients = QLabel("在线客户端：0 台")
        self.lbl_clients.setStyleSheet("font-weight:bold;")
        layout.addWidget(self.lbl_clients)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["控制", "名称", "IP 地址", "状态", "占用者", "剩余"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setToolTip(
            "勾选 = 认领这台客户端（之后所有下发只发给已勾选的）\n"
            "取消勾选 = 释放\n"
            "别人认领中的会灰显、勾不动")
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self._on_table_item_changed)
        self.table.setMinimumHeight(140)
        layout.addWidget(self.table, 1)

        # ---- 日志 ----
        log_box = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_box)
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(500)
        self.txt_log.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.txt_log)
        layout.addWidget(log_box, 1)

        # ---- 状态栏 ----
        self.setStatusBar(QStatusBar())
        self._lbl_listen = QLabel("监听：启动中…")
        self.statusBar().addWidget(self._lbl_listen)
        self._lbl_nodes = QLabel()
        self._refresh_nodes_label()
        self.statusBar().addPermanentWidget(self._lbl_nodes)

        # 作者署名放在状态栏最右侧：常驻可见，又不占用主界面空间。
        # 用富文本 + setOpenExternalLinks，点链接直接用系统浏览器打开主页。
        self._lbl_author = QLabel()
        self._lbl_author.setTextFormat(Qt.RichText)
        self._lbl_author.setOpenExternalLinks(True)
        self._lbl_author.setText(
            '作者：%s；<a href="%s" style="color:#1B5E20;text-decoration:none;">%s</a>'
            % (AUTHOR, AUTHOR_URL, AUTHOR_URL))
        self._lbl_author.setToolTip("点击链接打开作者主页")
        self.statusBar().addPermanentWidget(self._lbl_author)

    # ------------------------------------------------------------ 手动模式页
    def _build_manual_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(6, 10, 6, 10)
        outer.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(10)

        row1.addWidget(QLabel("时长"))
        self.spin_minutes = QSpinBox()
        self.spin_minutes.setRange(MIN_MINUTES, MAX_MINUTES)
        self.spin_minutes.setValue(DEFAULT_MINUTES)
        self.spin_minutes.setSuffix(" 分钟")
        self.spin_minutes.setMinimumWidth(120)
        row1.addWidget(self.spin_minutes)

        self.btn_start = QPushButton("开始下发")
        self.btn_start.setMinimumHeight(38)
        self.btn_start.clicked.connect(self._on_start_clicked)
        row1.addWidget(self.btn_start)

        self.btn_prompt = QPushButton("手动提示")
        self.btn_prompt.setMinimumHeight(38)
        self.btn_prompt.setToolTip(
            "立即让所有客户端弹一次提示。\n"
            "提示内容是每台客户端按自己的剩余时间自动算出来的，\n"
            "所以各机器显示的数字和它的真实剩余时间一致。")
        self.btn_prompt.clicked.connect(self._on_prompt_clicked)
        row1.addWidget(self.btn_prompt)

        row1.addSpacing(16)

        self.btn_add5 = QPushButton("+5 分钟")
        self.btn_add5.clicked.connect(lambda: self._on_add_clicked(5))
        row1.addWidget(self.btn_add5)

        self.btn_add10 = QPushButton("+10 分钟")
        self.btn_add10.clicked.connect(lambda: self._on_add_clicked(10))
        row1.addWidget(self.btn_add10)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.clicked.connect(self._on_stop_clicked)
        row1.addWidget(self.btn_stop)

        self.btn_reset = QPushButton("重置")
        self.btn_reset.clicked.connect(self._on_reset_clicked)
        row1.addWidget(self.btn_reset)

        row1.addStretch(1)
        outer.addLayout(row1)

        hint = QLabel("从点击的那一刻起算 N 分钟。需要整天自动执行请用右边「会议议程」。")
        hint.setStyleSheet("color:#78909C;font-size:12px;")
        outer.addWidget(hint)
        outer.addStretch(1)
        return page

    # ------------------------------------------------------------ 议程页
    def _build_agenda_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(6, 10, 6, 10)
        outer.setSpacing(8)

        # ---- 第一行：启用开关 + 提醒节点 ----
        row1 = QHBoxLayout()
        row1.setSpacing(10)

        self.chk_agenda = QCheckBox("启用议程（自动跟系统时钟执行）")
        self.chk_agenda.setChecked(self.agenda_enabled)
        self.chk_agenda.toggled.connect(self._on_agenda_toggled)
        row1.addWidget(self.chk_agenda)

        row1.addStretch(1)
        outer.addLayout(row1)

        # ---- 议程表格 ----
        self.tbl_agenda = QTableWidget(0, 5)
        self.tbl_agenda.setHorizontalHeaderLabels(
            ["开始", "结束", "活动内容", "提醒", "状态"])
        self.tbl_agenda.verticalHeader().setVisible(False)
        self.tbl_agenda.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_agenda.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_agenda.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl_agenda.setAlternatingRowColors(True)
        self.tbl_agenda.doubleClicked.connect(lambda _i: self._on_agenda_edit())
        a_header = self.tbl_agenda.horizontalHeader()
        a_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        a_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        a_header.setSectionResizeMode(2, QHeaderView.Stretch)
        a_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        a_header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.tbl_agenda.setMinimumHeight(150)
        outer.addWidget(self.tbl_agenda, 1)

        # ---- 第二行：增删改排序 ----
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        self.btn_agenda_add = QPushButton("＋ 添加议程")
        self.btn_agenda_add.setMinimumHeight(34)
        self.btn_agenda_add.setToolTip("弹窗录入：开始时间 / 结束时间 / 活动内容")
        self.btn_agenda_add.clicked.connect(self._on_agenda_add)
        row2.addWidget(self.btn_agenda_add)

        self.btn_agenda_edit = QPushButton("编辑")
        self.btn_agenda_edit.setMinimumHeight(34)
        self.btn_agenda_edit.clicked.connect(self._on_agenda_edit)
        row2.addWidget(self.btn_agenda_edit)

        self.btn_agenda_del = QPushButton("删除")
        self.btn_agenda_del.setMinimumHeight(34)
        self.btn_agenda_del.clicked.connect(self._on_agenda_delete)
        row2.addWidget(self.btn_agenda_del)

        self.btn_agenda_up = QPushButton("↑")
        self.btn_agenda_up.setFixedWidth(38)
        self.btn_agenda_up.setMinimumHeight(34)
        self.btn_agenda_up.setToolTip("与前一行交换时间")
        self.btn_agenda_up.clicked.connect(lambda: self._on_agenda_move(-1))
        row2.addWidget(self.btn_agenda_up)

        self.btn_agenda_down = QPushButton("↓")
        self.btn_agenda_down.setFixedWidth(38)
        self.btn_agenda_down.setMinimumHeight(34)
        self.btn_agenda_down.setToolTip("与后一行交换时间")
        self.btn_agenda_down.clicked.connect(lambda: self._on_agenda_move(1))
        row2.addWidget(self.btn_agenda_down)

        row2.addSpacing(20)

        self.btn_agenda_shift = QPushButton("+10 分钟（顺延）")
        self.btn_agenda_shift.setMinimumHeight(34)
        self.btn_agenda_shift.setToolTip(
            "讲师讲超时：当前项的结束时间 +10 分钟，\n"
            "并且后面每一项的起止时间都整体往后推 10 分钟。")
        self.btn_agenda_shift.clicked.connect(lambda: self._on_agenda_shift(10))
        row2.addWidget(self.btn_agenda_shift)

        self.btn_agenda_next = QPushButton("立即进入下一项")
        self.btn_agenda_next.setMinimumHeight(34)
        self.btn_agenda_next.setToolTip(
            "讲师提前讲完：当前项截断到现在，立刻切到下一项。\n"
            "下一项会「吸收」多出来的时间，再后面的项不动。")
        self.btn_agenda_next.clicked.connect(self._on_agenda_next)
        row2.addWidget(self.btn_agenda_next)

        row2.addStretch(1)
        outer.addLayout(row2)

        self.lbl_agenda_note = QLabel("")
        self.lbl_agenda_note.setStyleSheet("color:#B26A00;font-size:12px;")
        self.lbl_agenda_note.setWordWrap(True)
        outer.addWidget(self.lbl_agenda_note)
        return page

    # ============================================================ 网络回调
    def _on_started(self, ip, tcp_port, udp_port, group):
        # 端口组是运行时挑的（前一组被别的程序/上次残留的 TIME_WAIT 占着时会往后顺延），
        # 所以这里必须报**实际**端口，不能报常量 —— 否则状态栏显示的和真相不一致。
        self._lbl_listen.setText(
            "监听：%s:%d（UDP 发现 %d，端口组 %d）"
            % (ip, tcp_port, udp_port, group))
        self._append_log(
            "监听就绪：TCP %d / UDP %d（端口组 %d）。客户端会自动探测所有端口组，无需配置。"
            % (tcp_port, udp_port, group))

    def _on_listen_failed(self, reason):
        self._lbl_listen.setText("监听失败")
        self._append_log("错误：%s" % reason)
        QMessageBox.critical(
            self, APP_TITLE,
            "%s\n\n可能原因：端口被其它程序占用，或安全软件拦截。\n"
            "可关闭重复运行的主控端后重试。" % reason)

    def _on_clients(self, clients):
        self._clients = clients
        self._refresh_table(clients)

    def _refresh_table(self, clients):
        keys = [c["key"] for c in clients]
        self._suspend_table = True
        try:
            if keys != list(self._rows.keys()):
                self._rows = {}
                self.table.setRowCount(len(clients))
                for row, client in enumerate(clients):
                    self._rows[client["key"]] = row
                    for col in range(6):
                        item = QTableWidgetItem("")
                        if col == 0:
                            item.setFlags(Qt.ItemIsEnabled)
                            item.setCheckState(Qt.Unchecked)
                        self.table.setItem(row, col, item)

            self._targets = set()
            for client in clients:
                row = self._rows.get(client["key"])
                if row is None:
                    continue
                mine = bool(client.get("mine"))
                free = bool(client.get("free"))
                pending = client["key"] in self._pending

                box = self.table.item(row, 0)
                if box is not None:
                    box.setCheckState(
                        Qt.Checked if (mine or pending) else Qt.Unchecked)
                    flags = Qt.ItemIsEnabled
                    if mine or free or pending:
                        flags |= Qt.ItemIsUserCheckable
                    box.setFlags(flags)
                    if mine or pending:
                        box.setToolTip("已认领：取消勾选即释放")
                    elif free:
                        box.setToolTip("勾选以认领这台客户端")
                    else:
                        box.setToolTip("已被「%s」占用，勾不动"
                                       % (client.get("owner_name") or "其他主控"))
                    item_font = box.font()
                    item_font.setBold(mine)
                    box.setFont(item_font)

                self._set_cell(row, 1, client.get("name") or client.get("ip") or "-")
                self._set_cell(row, 2, client.get("ip") or "-")
                state = _CLIENT_STATE_TEXT.get(client.get("state"), "待机")
                if not client.get("online"):
                    state = "离线"
                self._set_cell(row, 3, state)
                if mine:
                    owner = "我"
                elif client.get("owner_name"):
                    owner = client.get("owner_name")
                else:
                    owner = "--"
                self._set_cell(row, 4, owner)
                self._set_cell(row, 5, _fmt_mmss(client.get("remaining", 0)))

                # 灰显别人的行，一眼能看出"这台轮不到我"
                for col in range(1, 6):
                    item = self.table.item(row, col)
                    if item is not None:
                        item.setForeground(QColor("#9E9E9E") if
                                           (not free and not mine) else QColor("#212121"))
                if mine:
                    self._targets.add(client["key"])
        finally:
            self._suspend_table = False

        self._update_clients_label(clients)
        self._emit_targets()

    def _update_clients_label(self, clients):
        online = sum(1 for c in clients if c.get("online"))
        mine = [c for c in clients
                if c.get("mine") or c["key"] in self._pending]
        text = "在线客户端：%d 台" % online
        if mine:
            names = "、".join((c.get("name") or c.get("ip") or "?") for c in mine[:4])
            if len(mine) > 4:
                names += " 等"
            text += "    已选 %d 台（%s）" % (len(mine), names)
        else:
            text += "    ⚠ 请先勾选要控制的客户端（勾选 = 认领）"
        self.lbl_clients.setText(text)

    def _on_table_item_changed(self, item):
        if self._suspend_table or item.column() != 0:
            return
        client_id = None
        for key, row in self._rows.items():
            if row == item.row():
                client_id = key
                break
        if not client_id:
            return
        if item.checkState() == Qt.Checked:
            self._pending.add(client_id)
            self.sigClaimRequest.emit(client_id)
        else:
            self._pending.discard(client_id)
            self._targets.discard(client_id)
            self.sigReleaseRequest.emit(client_id)
            self._emit_targets()

    def _emit_targets(self):
        targets = sorted(self._targets)
        if targets == self._sent_targets:
            return
        self._sent_targets = targets
        self.sigSetTargets.emit(targets)
        self._refresh_buttons()
        self._refresh_state_line()

    def _on_claim_result(self, info):
        key = str(info.get("key") or "")
        self._pending.discard(key)
        name = info.get("name") or str(info.get("client_id") or "")[:8]
        if info.get("ok"):
            self._append_log("已认领「%s」，可以下发了" % name)
        else:
            owner = info.get("owner_name") or "其他主控"
            self._append_log("认领「%s」失败：已被「%s」占用" % (name, owner))
            QMessageBox.warning(
                self, APP_TITLE,
                "认领「%s」失败。\n\n它正被「%s」占用。\n"
                "要控制这台机器，请让对方先释放。" % (name, owner))

    def _set_cell(self, row, col, text):
        item = self.table.item(row, col)
        if item is None:
            self.table.setItem(row, col, QTableWidgetItem(text))
        elif item.text() != text:
            item.setText(text)

    # ============================================================ 手动模式操作
    def _on_start_clicked(self):
        if not self._targets:
            QMessageBox.information(
                self, APP_TITLE,
                "还没有选客户端。\n\n"
                "请在下方列表里**勾选**要控制的机器（勾选即认领），再开始下发。")
            return
        seconds = self.spin_minutes.value() * 60
        self.state = "running"
        self.total_s = seconds
        self.target_ms = now_ms() + seconds * 1000
        self.sigStartCountdown.emit(seconds)
        self._refresh_buttons()

    def _on_add_clicked(self, minutes):
        if self.state == "idle":
            return
        delta = minutes * 60
        self.total_s += delta
        self.target_ms += delta * 1000
        self.state = "running"
        self.sigAddTime.emit(delta)
        self._append_log("加时 +%d 分钟，全部客户端剩余时间同步顺延" % minutes)
        self._refresh_buttons()

    def _on_prompt_clicked(self):
        """手动提示：不做任何本地计时改动，只让客户端各自弹一次剩余时间。"""
        if self.state == "idle":
            return
        self.sigPrompt.emit()
        self._append_log("已下发手动提示：各客户端将按自己的剩余时间弹出提示")

    def _on_custom_clicked(self):
        """自定义提示：把输入框里的文字原样下发给所有客户端。

        不做本地计时改动，也不需要正在倒计时（空闲时也能发）。
        """
        text = self.edit_custom.text().strip()
        if not text:
            self._append_log("自定义提示内容为空，未下发")
            return
        if not self._targets:
            self._append_log("还没有勾选客户端，未下发")
            return
        self.sigCustom.emit(text)
        self._append_log("已下发自定义提示：%s" % text)
        self.edit_custom.clear()

    def _on_stop_clicked(self):
        # 议程开着的时候，单纯"停止"没意义——下一秒调度器又会把当前项拉起来。
        # 所以停止 = 同时关掉议程开关，由开关的处理函数统一收尾。
        if self.agenda_enabled and self.chk_agenda.isChecked():
            self._append_log("停止：同时关闭议程模式（否则下一秒会被自动拉起）")
            self.chk_agenda.setChecked(False)
            return
        self.state = "idle"
        self.target_ms = 0
        self.total_s = 0
        self.sigStopCountdown.emit()
        self._append_log("已停止")
        self._refresh_buttons()

    def _on_reset_clicked(self):
        self.state = "idle"
        self.target_ms = 0
        self.total_s = 0
        self._agenda_index = None
        self.sigResetCountdown.emit()
        self._append_log("已重置")
        self._refresh_buttons()

    # ============================================================== 议程操作
    def _on_agenda_add(self):
        default_start = None
        selected = self._selected_agenda_row()
        if selected is not None and 0 <= selected < len(self.agenda):
            # 开始时间预填"上一条的结束时间"，连着录 7 条基本只要改结束和标题
            default_start = self.agenda.items[selected].end_min
        elif len(self.agenda):
            default_start = self.agenda.items[-1].end_min
        else:
            default_start = self._now_min()
            default_start = (default_start // 30) * 30      # 对齐到整半点

        dialog = AgendaItemDialog(self, default_start=default_start)
        if dialog.exec_() != QDialog.Accepted:
            return
        item = dialog.result_item()
        if item is None:
            return
        index = self.agenda.add(item)
        self._save_agenda()
        self._refresh_agenda_table()
        self.tbl_agenda.selectRow(index)
        self._append_log("已添加议程：%s" % item.describe())
        self._refresh_buttons()

    def _on_agenda_edit(self):
        index = self._selected_agenda_row()
        if index is None:
            self._append_log("请先在表格里选中一行再点「编辑」")
            return
        old = self.agenda.items[index]
        dialog = AgendaItemDialog(self, item=old)
        if dialog.exec_() != QDialog.Accepted:
            return
        item = dialog.result_item()
        if item is None:
            return
        self.agenda.replace(index, item)
        self._save_agenda()
        self._refresh_agenda_table()
        self._append_log("已修改议程：%s -> %s" % (old.describe(), item.describe()))

    def _on_agenda_delete(self):
        index = self._selected_agenda_row()
        if index is None:
            self._append_log("请先在表格里选中一行再点「删除」")
            return
        item = self.agenda.items[index]
        answer = QMessageBox.question(
            self, APP_TITLE, "删除这条议程？\n\n%s" % item.describe(),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.agenda.remove(index)
        self._save_agenda()
        self._refresh_agenda_table()
        self._append_log("已删除议程：%s" % item.describe())
        self._refresh_buttons()

    def _on_agenda_move(self, delta):
        index = self._selected_agenda_row()
        if index is None or len(self.agenda) < 2:
            return
        target = index + delta
        if not (0 <= target < len(self.agenda)):
            return
        items = self.agenda.items
        # 同时交换"时间"，而不是交换行位置 —— 因为表格是按时间排序显示的，
        # 只换位置会被 sort() 立刻还原，看起来"点了没反应"。
        items[index].start_min, items[target].start_min = (
            items[target].start_min, items[index].start_min)
        items[index].end_min, items[target].end_min = (
            items[target].end_min, items[index].end_min)
        self.agenda.sort()
        self._save_agenda()
        self._refresh_agenda_table()
        self.tbl_agenda.selectRow(target if delta < 0 else index)
        self._append_log("已调整议程顺序")

    def _on_agenda_shift(self, minutes):
        """顺延：当前项 +N 分钟，后面所有项整体后移。"""
        if len(self.agenda) == 0:
            self._append_log("议程表是空的，无法顺延")
            return
        index = self._agenda_index
        if index is None:
            index = self.agenda.index_at(self._now_min())
        if index is None:
            self._append_log("当前没有正在进行的议程项，无法顺延（可先点「启用议程」）")
            return
        is_break = self.agenda.items[index].is_break
        self.agenda.shift_from(index, minutes)
        if not is_break:
            # 只有在真的跑着倒计时时才动本地计时、才发加时。
            # 休息项本来就没下发，发 ADD_TIME 会让服务端记一条"尚未开始，忽略加时"。
            self.total_s += minutes * 60
            self.target_ms += minutes * 60 * 1000
            self.state = "running"
            self.sigAddTime.emit(minutes * 60)
        self._agenda_index = index
        self._save_agenda()
        self._refresh_agenda_table()
        self._append_log("顺延 +%d 分钟：「%s」及之后所有议程项整体后移（全场结束时间 +%d 分钟）"
                         % (minutes, self.agenda.items[index].title, minutes))
        self._refresh_buttons()

    def _on_agenda_next(self):
        """讲师提前讲完：截断当前项，立刻进入下一项。"""
        index = self._agenda_index
        if index is None:
            self._append_log("当前没有正在进行的议程项，「立即进入下一项」用不上")
            return
        now = self._now()
        title = self.agenda.items[index].title
        nxt = self.agenda.finish_now(index, self._now_min())
        self._save_agenda()
        self._refresh_agenda_table()
        if nxt is None:
            self._agenda_index = None
            self._stop_local()
            self._append_log("提前结束「%s」：这是最后一项，议程已全部结束，客户端回到静默"
                             % title)
            self._refresh_buttons()
            return
        self._agenda_index = nxt
        self._dispatch_agenda_item(nxt, now)
        self._append_log("提前结束「%s」，立刻进入「%s」"
                         % (title, self.agenda.items[nxt].title))
        self._refresh_buttons()

    def _on_nodes_applied(self):
        nodes = ag.parse_nodes(self.edit_nodes.text(), self.node_minutes)
        self.edit_nodes.setText(ag.format_nodes(nodes))
        if nodes == self.node_minutes:
            self._append_log("提醒节点未变化：%s 分钟"
                             % " / ".join(str(n) for n in nodes))
            return
        self.node_minutes = nodes
        self.sigSetNodes.emit(nodes)
        self._refresh_nodes_label()
        self._save_agenda()
        self._append_log("提醒节点已改为 %s 分钟，并已下发（正在倒计时则立刻生效）"
                         % " / ".join(str(n) for n in nodes))

    def _on_agenda_toggled(self, checked):
        self.agenda_enabled = bool(checked)
        self._save_agenda()
        if checked:
            if len(self.agenda) == 0:
                self._append_log("议程已启用，但议程表是空的 —— 请先点「＋ 添加议程」")
            else:
                self._append_log("议程已启用：按系统时钟自动执行（共 %d 项）"
                                 % len(self.agenda))
            self._agenda_index = None          # 下一个 tick 自己定位
        else:
            self._append_log("议程已停用")
            self._agenda_index = None
            if self.state != "idle":
                self._stop_local()
                self._append_log("客户端已回到静默")
        self._refresh_agenda_table()
        self._refresh_buttons()

    def _selected_agenda_row(self):
        rows = self.tbl_agenda.selectionModel().selectedRows() \
            if self.tbl_agenda.selectionModel() else []
        if rows:
            return rows[0].row()
        item = self.tbl_agenda.currentItem()
        return item.row() if item is not None else None

    # ============================================================ 时钟
    def _now(self):
        """当前时间。

        故意抽成一个方法：真实运行就是 ``datetime.now()``，但测试可以把它
        替换成一个固定时刻，于是"13:35 应该跑第几项""顺延后结束时刻变成几点"
        这类判断都能**确定性地**验证，不用靠碰运气等真实时间走到那个点。
        """
        return datetime.now()

    def _now_min(self):
        now = self._now()
        return now.hour * 60 + now.minute

    # ========================================================== 议程调度核心
    def _agenda_tick(self):
        """每秒对一次系统时钟：判断当前该跑哪一项。

        * 只在**项发生变化**时下发，所以同一项不会每秒钟重发一遍。
        * 主控中途才启动也能接上：第一个 tick 就会定位到"此刻正处于哪一项"。
        * 落到议程空隙 / 议程之外 -> 客户端静默。
        """
        if not self.agenda_enabled or len(self.agenda) == 0:
            if self._agenda_index is not None:
                self._agenda_index = None
            self._refresh_state_line()
            return

        now = self._now()
        index = self.agenda.index_at(now.hour * 60 + now.minute)

        if index != self._agenda_index:
            previous = self._agenda_index
            self._agenda_index = index
            if index is None:
                if previous is not None:
                    self._append_log("议程间歇：客户端回到静默（等下一项到点自动开始）")
                    self._stop_local()
            else:
                item = self.agenda.items[index]
                if item.is_break:
                    # 休息项：**不下发任何倒计时**，客户端自然保持静默。
                    # 如果上一项还在倒计时，这里要主动停掉（否则客户端会继续弹）。
                    if self.state != "idle":
                        self._stop_local()
                    self._append_log(
                        "「%s」%s 标记为休息：整个时段不提醒（客户端静默）"
                        % (item.title or "(未命名)", item.label()))
                else:
                    self._dispatch_agenda_item(index, now)
            self._refresh_agenda_table()
            self._refresh_buttons()

        self._refresh_state_line()

    def _dispatch_agenda_item(self, index, now):
        """把第 index 项下发下去：倒计时到该项的**结束时刻**。"""
        item = self.agenda.items[index]
        deadline = self.agenda.deadline_ms(index, now)
        # 第一个参数在协议里的语义是「本次倒计时的总时长」，客户端用它判断
        # "某个节点对本次倒计时有没有意义"。所以这里必须传该项的**完整时长**，
        # 不是剩余时间（传剩余时间会让"总时长 5 分钟"的项把 10 分钟节点也弹出来）。
        total_s = max(60, item.duration_min * 60)
        # 剩余时间从"注入的现在"算，而不是真实墙上时钟 —— 两者在生产环境里是同一个值，
        # 但抽成方法后测试能注入固定时钟，这里就不会和 deadline 打架。
        remain_s = max(0, int((deadline - int(now.timestamp() * 1000)) // 1000))
        self.state = "running"
        self.total_s = total_s
        self.target_ms = deadline
        self.sigStartUntil.emit(total_s, deadline, item.title)
        self._append_log(
            "议程项开始：「%s」%s，倒计时到 %s（该项共 %d 分钟，剩 %d 分钟）"
            % (item.title, item.label(), ag.format_hhmm(item.end_min),
               item.duration_min, remain_s // 60))

    def _stop_local(self):
        self.state = "idle"
        self.target_ms = 0
        self.total_s = 0
        self.sigStopCountdown.emit()

    # ============================================================ 显示刷新
    def _refresh_agenda_table(self):
        items = self.agenda.items
        current = self._agenda_index
        now_min = self._now_min()
        if self.tbl_agenda.rowCount() != len(items):
            self.tbl_agenda.setRowCount(len(items))
            for row in range(len(items)):
                for col in range(5):
                    self.tbl_agenda.setItem(row, col, QTableWidgetItem(""))

        for row, item in enumerate(items):
            self._set_agenda_cell(row, 0, ag.format_hhmm(item.start_min))
            self._set_agenda_cell(row, 1, ag.format_hhmm(item.end_min))
            self._set_agenda_cell(row, 2, item.title or "(未命名)")
            self._set_agenda_cell(row, 3, "不提醒" if item.is_break else "提醒")
            if row == current:
                mark = "▶ 进行中"
            elif not self.agenda_enabled:
                mark = ""
            elif current is not None:
                mark = "已过" if row < current else "未到"
            else:
                mark = "已过" if row < self._first_future(now_min) else "未到"
            self._set_agenda_cell(row, 4, mark)

        if current is not None and 0 <= current < len(items):
            self.tbl_agenda.selectRow(current)

        notes = self.agenda.validate()
        self.lbl_agenda_note.setText("　".join(notes[:3]) if notes else "")

    def _first_future(self, now_min):
        for i, item in enumerate(self.agenda.items):
            if item.start_min > now_min:
                return i
        return len(self.agenda.items)

    def _set_agenda_cell(self, row, col, text):
        item = self.tbl_agenda.item(row, col)
        if item is None:
            self.tbl_agenda.setItem(row, col, QTableWidgetItem(text))
        elif item.text() != text:
            item.setText(text)

    def _refresh_nodes_label(self):
        self._lbl_nodes.setText(
            "提醒节点：%s 分钟（默认 %s）"
            % (" / ".join(str(n) for n in self.node_minutes),
               " / ".join(str(n) for n in DEFAULT_NODE_MINUTES)))

    def _break_remaining_s(self):
        """当前项若是"休息"，返回它还剩多少秒；否则返回 None。

        休息项不发倒计时，但主持人仍然需要看到还剩多久，所以在本地算。
        """
        if not self.agenda_enabled or self._agenda_index is None:
            return None
        if not (0 <= self._agenda_index < len(self.agenda.items)):
            return None
        item = self.agenda.items[self._agenda_index]
        if not item.is_break:
            return None
        now = self._now()
        deadline = self.agenda.deadline_ms(self._agenda_index, now)
        return max(0, (deadline - int(now.timestamp() * 1000)) // 1000)

    def _refresh_clock(self):
        """每 300ms 刷新大字时钟。状态行交给 _refresh_state_line。"""
        if self.state == "idle" or not self.target_ms:
            rest = self._break_remaining_s()
            if rest is not None:
                # 紫色 + 剩余时间：一眼看出"这是休息，别催"
                self.lbl_clock.setText(_fmt_mmss(rest))
                self.lbl_clock.setStyleSheet("color:#5E35B1;")
                self._refresh_state_line()
                return
            self.lbl_clock.setText("--:--")
            self.lbl_clock.setStyleSheet("color:#90A4AE;")
            self._refresh_state_line()
            return

        remaining = max(0, (self.target_ms - now_ms()) // 1000)

        if remaining <= 0:
            if self.state != "finished":
                self.state = "finished"
                self._refresh_buttons()
            self.lbl_clock.setText("00:00")
            self.lbl_clock.setStyleSheet("color:#C62828;")
            self._refresh_state_line()
            return

        self.lbl_clock.setText(_fmt_mmss(remaining))
        color = "#1B5E20" if remaining > 180 else "#E65100"
        self.lbl_clock.setStyleSheet("color:%s;" % color)
        self._refresh_state_line()

    def _refresh_state_line(self):
        """状态行文案。议程模式优先，其次手动模式。"""
        if self.agenda_enabled and len(self.agenda):
            index = self._agenda_index
            if index is not None and 0 <= index < len(self.agenda):
                item = self.agenda.items[index]
                nxt = self.agenda.next_index(index)
                if nxt is None:
                    tail = "（最后一项）"
                else:
                    following = self.agenda.items[nxt]
                    tail = "　下一项「%s」%s" % (following.title,
                                              ag.format_hhmm(following.start_min))
                if item.is_break:
                    self.lbl_state.setText(
                        "休息中：「%s」%s　客户端静默不提醒%s"
                        % (item.title or "(未命名)", item.label(), tail))
                    return
                self.lbl_state.setText(
                    "议程进行中：「%s」%s%s" % (item.title, item.label(), tail))
                return
            now_min = self._now_min()
            upcoming = self._first_future(now_min)
            if upcoming < len(self.agenda):
                following = self.agenda.items[upcoming]
                self.lbl_state.setText(
                    "议程已启用 ｜ 间歇中，下一项「%s」%s 自动开始"
                    % (following.title, following.label()))
            else:
                self.lbl_state.setText("议程已启用 ｜ 今日议程已完成，客户端静默")
            return

        if self.state == "idle" or not self.target_ms:
            self.lbl_state.setText("空闲 —— 设置时长后点击「开始下发」"
                                   "（或用「会议议程」自动执行）")
            return
        if self.state == "finished":
            self.lbl_state.setText("%s（客户端横幅需人工关闭）" % BANNER_DONE_TEXT)
            return
        self.lbl_state.setText(
            "倒计时中 —— 总时长 %s，已下发到 %d 台客户端"
            % (_fmt_mmss(self.total_s), len(self._clients)))

    def _refresh_buttons(self):
        running = self.state != "idle"
        has_target = bool(self._targets)
        # 没勾选客户端 = 没有下发对象，所以除了"重置"其余一律禁用。
        self.btn_start.setEnabled(not running and has_target)
        self.btn_start.setToolTip("" if has_target else "请先在下方列表勾选要控制的客户端")
        self.spin_minutes.setEnabled(not running)
        self.btn_prompt.setEnabled(running and has_target)
        self.btn_add5.setEnabled(running and has_target)
        self.btn_add10.setEnabled(running and has_target)
        self.btn_custom.setEnabled(has_target)
        self.btn_custom.setToolTip(
            "把这行文字发给已勾选的客户端"
            if has_target else "请先在下方列表勾选要控制的客户端")
        self.btn_stop.setEnabled(running or (self.agenda_enabled
                                             and self.chk_agenda.isChecked()))
        self.btn_reset.setEnabled(running or self.total_s > 0)
        # 顺延/下一项只在"真的有一项在跑"时有意义
        has_current = self._agenda_index is not None
        self.btn_agenda_shift.setEnabled(has_current)
        self.btn_agenda_next.setEnabled(has_current)

    def _append_log(self, text):
        self.txt_log.appendPlainText(text)
        # 同时写进 master.log。
        # 只显示在界面上的日志等于没日志 —— 议程调度（"为什么这一项没提醒"
        # "休息项跳过了没有"）全是走的这条通道，出问题时用户机器上必须能查到。
        try:
            self.log.info("%s", text)
        except Exception:
            pass

    # ============================================================ 退出
    def closeEvent(self, event):
        self._tick.stop()
        self._agenda_timer.stop()
        self._save_agenda()
        try:
            self.sigShutdown.emit()
        except Exception:
            pass
        self._thread.quit()
        self._thread.wait(2000)
        event.accept()
