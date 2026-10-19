# -*- coding: utf-8 -*-
"""倒计时控制器（跑在 UI 线程，因为它要弹窗）。

核心设计：**绝对目标时间戳**
---------------------------
不用"每秒减 1"的累加计数器，而是记下 `target_ms`（主控下发的绝对 epoch
毫秒），每次 tick 用 ``剩余 = target_ms - 当前系统时间`` 现算。

原因：QTimer 累加在系统休眠 / 笔记本合盖 / CPU 满载 / 调试断点 时都会漂移，
会议提醒漂个几分钟就彻底失去意义了。用绝对时间戳，哪怕进程被挂起 10 分钟，
醒来第一件事就是把剩余时间算对，并且该弹的节点一个不漏地补上。

节点判定规则（需求确认 ③）
--------------------------
* 节点 n 只有在 ``总时长 > n`` 时才是"有效的"——设 8 分钟时 10 分钟节点
  压根没跨过，不弹；
* 从剩余时间**第一次 <= n` 的那一刻弹，且每轮只弹一次（fired 集合）。
  设 10 分钟时 t=0 剩余正好等于 10 分钟，不满足 `总时长 > 10`，所以起始
  瞬间不会糊脸。
"""
from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from common.const import (
    ADD_TIME_RESETS_NODE_FLAG,
    DEFAULT_NODE_MINUTES,
    NODE_MAX_COUNT,
    TICK_MS,
)
from common.protocol import now_ms


def sanitize_nodes(nodes, default=DEFAULT_NODE_MINUTES):
    """校验主控下发的节点列表（防御式：主控出错也不能让客户端弹爆）。

    规则：只收 1..1440 的整数、去重、降序、最多 NODE_MAX_COUNT 个；
    一个都不合法就回退到默认节点。
    """
    values = []
    for raw in (nodes or ()):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 1440:
            values.append(value)
    values = sorted(set(values), reverse=True)[:NODE_MAX_COUNT]
    return tuple(values) or tuple(default)


def _humanize_seconds(seconds):
    """秒数转成人话：不足 1 分钟报秒，否则四舍五入到分钟。

    这里不用内置 round()：Python 的 round 是**银行家舍入**，
    round(12.5) 会得 12、round(2.5) 得 2，跟"还剩多少时间"的直觉不符。
    用 floor(x + 0.5) 才是我们想要的四舍五入。
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%d 秒" % seconds
    return "%d 分钟" % max(1, int(seconds / 60.0 + 0.5))


class CountdownController(QObject):
    """维护倒计时状态机，并在跨过节点时发信号。"""

    #: 状态变化：{"state":..., "remaining":int, "total":int}
    stateChanged = pyqtSignal(dict)
    #: 跨过某节点（单位：分钟）
    nodeFired = pyqtSignal(int)
    #: 倒计时归零
    finished = pyqtSignal()
    #: 从"已归零"状态被加时救回来
    resumed = pyqtSignal()
    #: 被停止 / 重置
    stopped = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = "idle"        # idle / running / finished
        self.total_s = 0
        self.target_ms = 0
        self.fired = set()
        #: 提醒节点（分钟）。**由主控下发**，不在客户端写死——这样主控改了
        #: 节点设置（比如把 10,5,3,1 改成 5,1），客户端立刻跟着变。
        self.nodes = tuple(DEFAULT_NODE_MINUTES)
        #: 当前项的名称（议程模式下是议题名，手动模式下为空）。
        #: 有它时节点提醒会带上前缀，例如「硬件培训 剩余 10 分钟」。
        self.label = ""

        self._tick = QTimer(self)
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self._on_tick)

    # ------------------------------------------------------------ 查询
    def remaining_ms(self):
        if self.state == "idle" or not self.target_ms:
            return 0
        return max(0, self.target_ms - now_ms())

    def remaining_s(self):
        return int(self.remaining_ms() // 1000)

    # -------------------------------------------------------- 手动提示文案
    def node_text(self, minutes):
        """节点提醒的横幅文字。

        议程模式（有 label）：``硬件培训 剩余 10 分钟`` —— 同一场里会弹好几次
        "剩余 N 分钟"，不带议题名没人知道是哪一项要结束了。
        手动模式（无 label）：``倒计时 10 分钟``（维持原样）。
        """
        if self.label:
            return "%s 剩余 %d 分钟" % (self.label, minutes)
        return "倒计时 %d 分钟" % minutes

    def prompt_text(self):
        """主控点了「手动提示」时，本机要弹的文案。

        由客户端**自己按绝对目标时间戳现算**，所以每台机器弹出来的数字都是
        它自己的真实剩余时间——这就是需求里"客户端自动计算自己还剩多少时间"。

        * 倒计时中 -> "硬件培训 剩余 12 分钟" / "剩余 45 秒"
        * 已超时   -> "已超时 3 分钟"（target_ms 留在过去的时刻，正好能算超时多久）
        * 空闲     -> None（没有倒计时，没什么可提示的，不弹）
        """
        if self.state == "idle" or not self.target_ms:
            return None
        now = now_ms()
        if self.state == "running":
            body = "剩余 " + _humanize_seconds((self.target_ms - now) // 1000)
            return "%s %s" % (self.label, body) if self.label else body
        return "已超时 " + _humanize_seconds((now - self.target_ms) // 1000)

    # ------------------------------------------------------------ 指令
    @pyqtSlot("qint64", "qint64", str, object)
    def start(self, total_s, target_ms, label="", nodes=None):
        """主控下发开始（或重连后补发当前进度）。

        * 槽签名必须写 qint64 —— 即使信号是 64 位，槽声明成 int 也会在接收侧
          再截断一次（epoch 毫秒会被砍掉高 32 位）。
        * ``nodes`` 用 ``object`` 传（元组/列表），PyQt 会原样转发。
          主控没给/给的不合法时沿用上一次的节点，不会突然变成"不提醒"。
        """
        self.total_s = max(0, int(total_s))
        self.target_ms = int(target_ms) or (now_ms() + self.total_s * 1000)
        self.label = str(label or "").strip()
        if nodes:
            self.nodes = sanitize_nodes(nodes, self.nodes)
        self.fired = set()
        self.state = "running"

        # 重连补发场景：已经越过的节点不应该再补弹一遍（避免重连就糊脸）
        remain_ms = self.remaining_ms()
        for minutes in self.nodes:
            if remain_ms <= minutes * 60 * 1000:
                self.fired.add(minutes)

        self._tick.start()
        self._emit()

    @pyqtSlot("qint64", "qint64")
    def add_time(self, delta_s, target_ms):
        """加时。target_ms 由主控给出（权威），本地同步。"""
        if self.state == "idle":
            return
        delta_s = int(delta_s)
        self.total_s += delta_s
        self.target_ms = int(target_ms) or (self.target_ms + delta_s * 1000)

        if ADD_TIME_RESETS_NODE_FLAG:
            # 加时后剩余时间可能重新超过某些节点 -> 这些节点重新变为可弹
            remain_ms = self.remaining_ms()
            for minutes in list(self.fired):
                if remain_ms > minutes * 60 * 1000:
                    self.fired.discard(minutes)

        was_finished = self.state == "finished"
        self.state = "running"
        self._tick.start()
        if was_finished:
            self.resumed.emit()
        self._emit()

    @pyqtSlot()
    def stop(self):
        if self.state == "idle":
            return
        self._tick.stop()
        self.state = "idle"
        self.target_ms = 0
        self.fired = set()
        self.label = ""
        self._emit()
        self.stopped.emit()

    @pyqtSlot()
    def reset(self):
        self._tick.stop()
        self.state = "idle"
        self.total_s = 0
        self.target_ms = 0
        self.fired = set()
        self.label = ""
        self._emit()
        self.stopped.emit()

    # ------------------------------------------------------------ 节拍
    def _on_tick(self):
        remain_ms = self.remaining_ms()

        if remain_ms <= 0:
            if self.state != "finished":
                self.state = "finished"
                self._tick.stop()
                self.fired = set()
                self._emit()
                self.finished.emit()
            return

        for minutes in sorted(self.nodes, reverse=True):
            threshold_ms = minutes * 60 * 1000
            if self.total_s * 1000 <= threshold_ms:
                continue                      # 总时长就没超过这个节点，不弹
            if minutes in self.fired:
                continue
            if remain_ms <= threshold_ms:
                self.fired.add(minutes)
                self.nodeFired.emit(minutes)

        self._emit()

    def _emit(self):
        self.stateChanged.emit({
            "state": self.state,
            "remaining": self.remaining_s(),
            "total": self.total_s,
            "label": self.label,
            "nodes": self.nodes,
        })
