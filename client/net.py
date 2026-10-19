# -*- coding: utf-8 -*-
"""客户端网络层（多主控版）。

与单主控版本的根本差别
----------------------
1. **一个客户端连所有主控**（不是一个）。
   每个主控的列表里都要能看到"全部"客户端及其占用状态。如果客户端只连占用它的
   那个主控，主控 A 就看不到"401 已被小王占用"，会以为那台机器不存在。

2. **只有占用者能下发指令**。
   客户端记住 claimed_by；收到非占用者的 START / PROMPT / CUSTOM … 一律
   回 ACK{ok:false, reason:"not_owner"} 并忽略，同时在日志里写明是谁被谁挡下了。

3. **占用会过期**。
   客户端 2 秒一次心跳，占用者 15 秒内没有任何回应就认为它崩了/断网了，
   自动解除占用并广播给其他主控 —— 否则主控崩溃会留下永远解不开的"幽灵锁"。

4. **全部异步**。
   每条连接一个 QTcpSocket（Qt 自己处理非阻塞 connect/断线），
   worker 线程里跑三个 QTimer（发现 / 心跳 / 超时检查），
   queued 投递过来的槽（set_runtime / set_name / reconnect_now）照常能收到。
"""
import os

from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtNetwork import QAbstractSocket, QTcpSocket, QUdpSocket, QHostAddress

from common.const import (
    CLAIM_TIMEOUT_MS,
    CLIENT_NAME_MAX,
    HEARTBEAT_INTERVAL_MS,
    HELLO_WAIT_MS,
    MSG_ACK,
    MSG_HELLO,
    MSG_ADD_TIME,
    MSG_BYE,
    MSG_CLAIM,
    MSG_CLAIM_RESULT,
    MSG_CLAIM_STATE,
    MSG_CUSTOM,
    MSG_DISCOVER,
    MSG_HEARTBEAT,
    MSG_HERE,
    MSG_PROMPT,
    MSG_REGISTER,
    MSG_RELEASE,
    MSG_RESET,
    MSG_START,
    MSG_STOP,
    all_udp_ports,
)
from common.config import save_client_config
from common.identity import default_client_name, ensure_client_id
from common.logutil import setup_logger
from common.netutil import get_broadcast_addrs, get_mac, get_primary_ip
from common.protocol import FrameParser, decode, encode

#: 主控多久没应答就认为这条连接死了（非占用者用这个）
LINK_IDLE_MS = 8000
#: 断开后多久重连
RECONNECT_MS = 3000
#: 发现广播间隔
DISCOVER_MS = 2000
#: 超时检查节拍
WATCHDOG_MS = 500

#: 认领类指令 —— 只有这些不校验归属
_OWNER_FREE = (MSG_CLAIM, MSG_RELEASE, MSG_ACK)


class _Link(object):
    """到某一个主控的一条连接。"""

    __slots__ = ("master_id", "name", "ip", "tcp_port", "sock", "parser",
                 "connected", "last_rx", "next_retry",
                 "await_hello", "hello_deadline")

    def __init__(self, master_id, name, ip, tcp_port):
        self.master_id = master_id
        self.name = name
        self.ip = ip
        self.tcp_port = int(tcp_port)
        self.sock = None
        self.parser = FrameParser()
        self.connected = False
        self.last_rx = 0
        self.next_retry = 0
        #: 已连上但还没确认主控身份（等 HELLO）
        self.await_hello = False
        self.hello_deadline = 0

    def label(self):
        return self.name or ("%s:%d" % (self.ip, self.tcp_port))


class ClientNet(QObject):
    """客户端网络层。API 与单主控版保持一致，另加认领相关的两个信号。"""

    sigLog = pyqtSignal(str)
    sigStatus = pyqtSignal(dict)
    # 注意：必须用 qint64！pyqtSignal(int, int) 会映射到 C++ 32 位 int，
    # 毫秒时间戳会被截断（单主控版本踩过这个坑）。
    sigStart = pyqtSignal("qint64", "qint64", str, object)
    sigAddTime = pyqtSignal("qint64", "qint64")
    sigPrompt = pyqtSignal()
    sigCustom = pyqtSignal(str)
    sigStop = pyqtSignal()
    sigReset = pyqtSignal()
    #: 占用状态变化 (master_id, master_name)；空字符串 = 已释放
    sigClaim = pyqtSignal(str, str)
    #: 主控清单变化（排障/托盘展示用）
    sigMasters = pyqtSignal(object)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg or {}
        self.name = default_client_name(self.cfg)
        self.client_id = ensure_client_id(self.cfg)
        # 会话 ID：默认就是 client_id。但如果按下面的开关**允许同机多开**（测试用），
        # 多开出来的实例会共用同一份配置 -> client_id 相同，主控那边就会把它们
        # 当成同一台。所以多开模式下带上进程号，保证每个实例都能被单独认领。
        self.session_id = self.client_id
        if os.environ.get("MEETINGTIMER_ALLOW_MULTI_CLIENT") == "1":
            self.session_id = "%s-p%d" % (self.client_id[:8], os.getpid())
        self.local_ip = get_primary_ip()
        self.mac = get_mac()

        self._links = {}              # master_id -> _Link
        self._udp = None
        self._running = False
        self._owner = None            # (master_id, master_name)
        self._runtime = {"state": "idle", "remaining": 0}
        self._phase = "idle"
        self._detail = "启动中"
        self._last_discover = 0

        self._log = setup_logger("client.net", "client.log")

        self._t_discover = QTimer(self)
        self._t_discover.setInterval(DISCOVER_MS)
        self._t_discover.timeout.connect(self._broadcast_discover)

        self._t_heart = QTimer(self)
        self._t_heart.setInterval(HEARTBEAT_INTERVAL_MS)
        self._t_heart.timeout.connect(self._heartbeat)

        self._t_watch = QTimer(self)
        self._t_watch.setInterval(WATCHDOG_MS)
        self._t_watch.timeout.connect(self._watchdog)

    # ================================================================ 生命周期
    @pyqtSlot()
    def start(self):
        if self._running:
            return
        self._running = True

        self._udp = QUdpSocket(self)
        self._udp.bind(QHostAddress.AnyIPv4, 0)
        self._udp.setSocketOption(self._udp.MulticastTtlOption, 1)
        self._udp.readyRead.connect(self._on_udp_ready)
        self._emit_log("本机身份：%s（ID %s…）" % (self.name, self.client_id[:8]))
        self._set_phase("discovering", "正在搜索局域网内的主控…")

        self._broadcast_discover()
        self._t_discover.start()
        self._t_heart.start()
        self._t_watch.start()

    @pyqtSlot()
    def shutdown(self):
        """退出前收尾：给所有主控发 BYE，然后断开。"""
        self._running = False
        for timer in (self._t_discover, self._t_heart, self._t_watch):
            timer.stop()
        for link in list(self._links.values()):
            self._send(link, MSG_BYE)
            self._close_link(link)
        self._links.clear()
        if self._udp is not None:
            try:
                self._udp.close()
            except RuntimeError:
                pass
            self._udp = None

    @pyqtSlot()
    def reconnect_now(self):
        """托盘「立即重连」：全部推倒重来。"""
        self._emit_log("手动触发重连")
        for link in list(self._links.values()):
            self._close_link(link)
        self._links.clear()
        self._owner = None
        self.sigClaim.emit("", "")
        self._broadcast_discover()
        self._set_phase("discovering", "正在重新搜索主控…")

    # ================================================================ 运行态
    @pyqtSlot(str, "qint64")
    def set_runtime(self, state, remaining):
        self._runtime["state"] = state
        self._runtime["remaining"] = int(remaining)

    @pyqtSlot(str)
    def set_name(self, name):
        """改名：存盘 + 立刻向所有主控重新注册（不用重启）。"""
        name = (name or "").strip()[:CLIENT_NAME_MAX]
        if not name or name == self.name:
            return
        self.name = name
        self.cfg["name"] = name
        self.cfg["client_name"] = name
        try:
            save_client_config(self.cfg, self.cfg.get("_source") or None)
        except (OSError, ValueError):
            pass
        self._register_all()
        self.sigStatus.emit(self.status())
        self._emit_log("本机名称已改为「%s」" % name)

    # ================================================================ 发现
    def _broadcast_discover(self):
        if not self._running or self._udp is None:
            return
        payload = encode(MSG_DISCOVER, client_id=self.session_id, name=self.name)
        addrs = get_broadcast_addrs() or ["255.255.255.255"]
        for port in all_udp_ports():
            for addr in addrs:
                try:
                    self._udp.writeDatagram(payload, QHostAddress(addr), port)
                except (RuntimeError, TypeError):
                    continue
        self._piggyback_retry()

    def _on_udp_ready(self):
        while self._udp is not None and self._udp.hasPendingDatagrams():
            datagram = self._udp.receiveDatagram()
            data = bytes(datagram.data())
            try:
                msg = decode(data)
            except ValueError:
                continue
            if msg.get("type") != MSG_HERE:
                continue
            mid = str(msg.get("master_id") or "")
            if not mid:
                continue
            ip = self._sender_ip(datagram)
            port = int(msg.get("tcp_port") or 0)
            if not ip or not port:
                continue
            name = str(msg.get("master_name") or "")
            link = self._links.get(mid)
            if link is None:
                link = _Link(mid, name, ip, port)
                self._links[mid] = link
                self._open_link(link)
                self._emit_log("发现主控「%s」%s:%d" % (link.label(), ip, port))
                self.sigMasters.emit(self.masters())
            else:
                link.name = name or link.name
                if not link.connected and (link.ip != ip or link.tcp_port != port):
                    link.ip, link.tcp_port = ip, port

    @staticmethod
    def _sender_ip(datagram):
        try:
            return datagram.senderAddress().toString()
        except (AttributeError, RuntimeError):
            return ""

    def _piggyback_retry(self):
        """发现节拍里顺手做重连，避免再开一个定时器。"""
        now = self._now()
        for link in list(self._links.values()):
            if link.connected or now < link.next_retry:
                continue
            self._open_link(link)

    # ================================================================ 连接
    def _open_link(self, link):
        if link.sock is not None:
            self._close_link(link)
        link.next_retry = self._now() + RECONNECT_MS
        sock = QTcpSocket(self)
        link.sock = sock
        mid = link.master_id
        # 注意：这里必须绑定**连接对象**而不是 master_id。
        # 主控重启后我们会把 link.master_id 改成新身份（_adopt_master_id），
        # 如果用 id 做键，改完之后所有信号都会查不到这条连接 -> 数据全丢 ->
        # 表现为"身份同步"之后 8 秒又"长时间无响应"。
        sock.connected.connect(lambda l=link: self._on_connected(l))
        sock.stateChanged.connect(lambda st, l=link: self._on_state(l, st))
        sock.readyRead.connect(lambda l=link: self._on_ready_read(l))
        sock.connectToHost(link.ip, link.tcp_port)

    def _close_link(self, link):
        sock, link.sock = link.sock, None
        link.connected = False
        link.parser.reset()
        if sock is not None:
            try:
                sock.abort()
                sock.deleteLater()
            except RuntimeError:
                pass

    def _on_connected(self, link):
        if self._links.get(link.master_id) is not link or link.sock is None:
            return
        link.connected = True
        link.last_rx = self._now()
        # 先不注册：等主控的 HELLO 报出真实身份再注册。
        # 主控重启后端口不变、身份变了，抢跑注册会在新主控名下多出一行。
        link.await_hello = True
        link.hello_deadline = self._now() + HELLO_WAIT_MS
        self._emit_log("已连接主控「%s」%s:%d"
                       % (link.label(), link.ip, link.tcp_port))
        self.sigMasters.emit(self.masters())
        self._set_phase("connected", self._phase_detail())

    def _adopt_master_id(self, link, real_id, real_name):
        """把"重启前的老连接"纠正成主控的真实身份，或者让它让位。

        场景：主控重启后 **master_id 变了但监听端口没变**。
        * 客户端按端口重连的旧连接，实际连上的是**新主控**（但它以为还是老主控）；
        * UDP 发现又为新 master_id 建了一条连接；
        两条连接指向同一台主控，主控就会把同一台客户端显示成多行
        —— 实测"主控启动几次就多几行"。

        纠正办法：谁先绑到这个真实身份谁留下，另一条直接关掉。
        """
        other = self._links.get(real_id)
        if other is not None and other is not link:
            self._close_link(link)
            self._links.pop(link.master_id, None)
            self._emit_log("清理遗留连接 %s:%d（该主控已重启）"
                           % (link.ip, link.tcp_port))
        else:
            self._links.pop(link.master_id, None)
            link.master_id = real_id
            if real_name:
                link.name = real_name
            self._links[real_id] = link
            self._emit_log("主控身份已更新：「%s」（重启后端口复用）" % link.name)
        self.sigMasters.emit(self.masters())
        self._set_phase("connected", self._phase_detail())

    def _on_state(self, link, state):
        if state != QAbstractSocket.UnconnectedState:
            return
        if self._links.get(link.master_id) is not link:
            return
        was = link.connected
        link.connected = False
        link.parser.reset()
        link.next_retry = self._now() + RECONNECT_MS
        if was:
            self._emit_log("与主控「%s」的连接断开，稍后自动重连" % link.label())
            self.sigMasters.emit(self.masters())
            self._set_phase("discovering", self._phase_detail())

    def _on_ready_read(self, link):
        if self._links.get(link.master_id) is not link or link.sock is None:
            return
        try:
            data = bytes(link.sock.readAll())
        except RuntimeError:
            return
        if not data:
            return
        link.last_rx = self._now()
        for msg in link.parser.feed(data):
            self._dispatch(link, msg)

    def _now(self):
        from common.protocol import now_ms
        return now_ms()

    # ================================================================ 收发
    def _send(self, link, mtype, **payload):
        if link is None or link.sock is None or not link.connected:
            return False
        try:
            link.sock.write(encode(mtype, **payload))
            link.sock.flush()
            return True
        except (RuntimeError, ValueError):
            return False

    def _register_all(self):
        for link in self._links.values():
            if link.connected:
                self._register(link)

    def _register(self, link):
        self._send(link, MSG_REGISTER,
                   client_id=self.session_id,
                   name=self.name,
                   ip=self.local_ip,
                   mac=self.mac,
                   claimed_by=self._owner_info())

    def _ack(self, link, what, ok=True, **extra):
        payload = {"what": what, "ok": bool(ok)}
        payload.update(extra)
        self._send(link, MSG_ACK, **payload)

    def _owner_info(self):
        if not self._owner:
            return None
        return {"id": self._owner[0], "name": self._owner[1]}

    def _broadcast_claim_state(self):
        for link in self._links.values():
            if link.connected:
                self._send(link, MSG_CLAIM_STATE, claimed_by=self._owner_info())

    def _heartbeat(self):
        for link in self._links.values():
            if link.connected:
                self._send(link, MSG_HEARTBEAT,
                           state=self._runtime.get("state", "idle"),
                           remaining=int(self._runtime.get("remaining", 0)),
                           claimed_by=self._owner_info())

    # ================================================================ 超时
    def _watchdog(self):
        """占用者失联 -> 自动解锁；非占用者长期无响应 -> 判定断线重连。"""
        if not self._running:
            return
        now = self._now()
        owner_id = self._owner[0] if self._owner else None

        # 兜底：万一主控没发 HELLO（旧版本），等超时后照常注册
        for lk in list(self._links.values()):
            if lk.connected and lk.await_hello and now >= lk.hello_deadline:
                lk.await_hello = False
                self._register(lk)

        if self._owner:
            link = self._links.get(owner_id)
            dead = (link is None or not link.connected
                    or (now - link.last_rx) > CLAIM_TIMEOUT_MS)
            if dead:
                name = self._owner[1]
                self._owner = None
                self._broadcast_claim_state()
                self.sigClaim.emit("", "")
                self._emit_log(
                    "「%s」主控失联超过 %d 秒，已自动解除占用（可以重新认领）"
                    % (name, CLAIM_TIMEOUT_MS // 1000))
                self._set_phase("connected", self._phase_detail())

        for link in list(self._links.values()):
            if link.connected and link.master_id != owner_id:
                if (now - link.last_rx) > LINK_IDLE_MS:
                    self._emit_log("主控「%s」长时间无响应，断开重连" % link.label())
                    self._close_link(link)
                    self.sigMasters.emit(self.masters())

    # ================================================================ 分发
    def _dispatch(self, link, msg):
        mtype = msg.get("type")

        if mtype == MSG_HELLO:
            # 主控受理连接时自报身份；确认身份后再注册
            mid = str(msg.get("master_id") or "")
            if mid and mid != link.master_id:
                self._adopt_master_id(link, mid,
                                      str(msg.get("master_name") or ""))
            if self._links.get(link.master_id) is link:
                link.await_hello = False
                self._register(link)
            return

        if mtype == MSG_ACK:
            # 主控在 REGISTER 的 ACK 里自报身份，用来纠正"重启后端口复用"造成的
            # 重复连接（详见 _adopt_master_id）
            mid = str(msg.get("master_id") or "")
            if mid and mid != link.master_id:
                self._adopt_master_id(link, mid,
                                      str(msg.get("master_name") or ""))
            return

        if mtype == MSG_CLAIM:
            self._on_claim(link, msg)
            return
        if mtype == MSG_RELEASE:
            self._on_release(link, msg)
            return

        # ---- 以下都是控制指令：必须来自占用者 ----
        if not self._owner or self._owner[0] != link.master_id:
            self._ack(link, mtype, ok=False, reason="not_owner",
                      owner=self._owner_info())
            owner_desc = ("「%s」" % self._owner[1]) if self._owner else "其他主控"
            self._emit_log("已拒绝主控「%s」的 %s 指令：本机被%s占用"
                           % (link.label(), mtype, owner_desc))
            return

        if mtype == MSG_START:
            total = int(msg.get("total") or msg.get("total_s") or 0)
            target = int(msg.get("target_ms") or 0)
            label = str(msg.get("label") or "")
            nodes = msg.get("nodes")
            self._ack(link, mtype)
            self.sigStart.emit(total, target, label, nodes)
        elif mtype == MSG_ADD_TIME:
            delta = int(msg.get("delta") or 0)
            target = int(msg.get("target_ms") or 0)
            self._ack(link, mtype)
            self.sigAddTime.emit(delta, target)
        elif mtype == MSG_PROMPT:
            self._ack(link, mtype)
            self.sigPrompt.emit()
        elif mtype == MSG_CUSTOM:
            text = str(msg.get("text") or "")
            if not text.strip():
                return
            self._ack(link, mtype)
            self.sigCustom.emit(text)
        elif mtype == MSG_STOP:
            self._ack(link, mtype)
            self.sigStop.emit()
        elif mtype == MSG_RESET:
            self._ack(link, mtype)
            self.sigReset.emit()

    def _on_claim(self, link, msg):
        mid = str(msg.get("master_id") or link.master_id)
        mname = str(msg.get("master_name") or link.name)
        if self._owner and self._owner[0] != mid:
            self._send(link, MSG_CLAIM_RESULT, ok=False, reason="busy",
                       owner=self._owner_info(), master_id=mid)
            self._emit_log("拒绝「%s」的认领请求：本机已被「%s」占用"
                           % (mname, self._owner[1]))
            return
        self._owner = (mid, mname)
        link.name = mname or link.name
        self._send(link, MSG_CLAIM_RESULT, ok=True,
                   owner=self._owner_info(), master_id=mid)
        self._broadcast_claim_state()
        self.sigClaim.emit(mid, mname)
        self._set_phase("claimed", self._phase_detail())
        self._emit_log("已被主控「%s」认领" % mname)

    def _on_release(self, link, msg):
        if not self._owner:
            self._send(link, MSG_CLAIM_RESULT, ok=True,
                       owner=None, master_id=link.master_id)
            return
        if self._owner[0] != link.master_id:
            self._send(link, MSG_CLAIM_RESULT, ok=False, reason="not_owner",
                       owner=self._owner_info(), master_id=link.master_id)
            return
        name = self._owner[1]
        self._owner = None
        self._send(link, MSG_CLAIM_RESULT, ok=True,
                   owner=None, master_id=link.master_id)
        self._broadcast_claim_state()
        self.sigClaim.emit("", "")
        self._set_phase("connected", self._phase_detail())
        self._emit_log("主控「%s」已释放本机" % name)

    # ================================================================ 状态
    def masters(self):
        """给托盘/测试看的清单。"""
        return [
            {"id": l.master_id, "name": l.name, "ip": l.ip,
             "port": l.tcp_port, "connected": l.connected,
             "owner": bool(self._owner and self._owner[0] == l.master_id)}
            for l in self._links.values()
        ]

    def _phase_detail(self):
        if self._owner:
            return "已被「%s」控制" % self._owner[1]
        total = len(self._links)
        online = sum(1 for l in self._links.values() if l.connected)
        if online:
            return "已连接 %d 个主控，等待认领" % online
        if total:
            return "主控离线，正在重连…"
        return "未发现主控"

    def _set_phase(self, phase, detail):
        self._phase = phase
        self._detail = detail
        self.sigStatus.emit(self.status())

    def status(self):
        masters = self.masters()
        online = [m for m in masters if m["connected"]]
        return {
            "connected": bool(online),
            "phase": self._phase,
            "detail": self._detail,
            "master": ("%s（%d 个主控在线）" % (self._owner[1], len(online))
                       if self._owner else
                       ("%d 个主控在线" % len(online) if online else "")),
            "masters": len(online),
            "owner": self._owner[1] if self._owner else "",
            "name": self.name,
            "ip": self.local_ip,
            "mac": self.mac,
        }

    def _emit_log(self, text):
        self._log.info("%s", text)
        self.sigLog.emit(text)