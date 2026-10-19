# -*- coding: utf-8 -*-
"""主控端网络服务（多主控版）。

职责
----
* 占一组端口（UDP 发现 / TCP 控制），支持**同一台电脑开多个主控**
* 维护"所有连上来的客户端"注册表，并把占用状态（谁认领了它）镜像出来
* 认领 / 释放
* 把指令**只发给已选中的客户端**

为什么占用状态由客户端说了算
---------------------------
客户端才是"被抢的那台机器"，谁先认领、谁被拒，只有它自己有最终话语权。
如果主控之间互相协商，两个主控同时点认领就会出现双主：
    主控A: 认领成功（本地标记）
    主控B: 认领成功（本地标记）
    客户端: 我听谁的？
所以这里主控只做两件事：**发起认领** + **镜像客户端回报的状态**。
"""
from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

#: 超过这么久没收到客户端任何数据就认为它没了。
#: 客户端 2 秒一次心跳，20 秒足够宽松；客户端被强杀/断网时 TCP 未必立刻报错，
#: 不主动清理就会在列表里留下"幽灵客户端"。
CONN_IDLE_MS = 20000
from PyQt5.QtNetwork import QAbstractSocket, QHostAddress, QTcpServer, QTcpSocket, QUdpSocket

from common.const import (
    DEFAULT_NODE_MINUTES,
    MSG_ACK,
    MSG_ADD_TIME,
    MSG_BYE,
    MSG_CLAIM,
    MSG_CLAIM_RESULT,
    MSG_CLAIM_STATE,
    MSG_CUSTOM,
    MSG_DISCOVER,
    MSG_HEARTBEAT,
    MSG_HELLO,
    MSG_HERE,
    MSG_PROMPT,
    MSG_REGISTER,
    MSG_RELEASE,
    MSG_RESET,
    MSG_START,
    MSG_STOP,
    PORT_GROUP_COUNT,
    CUSTOM_TEXT_MAX,
    port_group,
)
from common.logutil import setup_logger
from common.netutil import get_primary_ip
from common.protocol import FrameParser, decode, encode

#: 客户端超过这么久没心跳，界面上标"离线"
CLIENT_OFFLINE_MS = 8000


class _Conn(object):
    """一条客户端连接。"""

    __slots__ = ("sock", "parser", "client_id", "name", "ip", "mac",
                 "state", "remaining", "claimed_by", "last_rx", "label", "key")

    def __init__(self, sock, label):
        self.sock = sock
        self.parser = FrameParser()
        self.client_id = ""
        self.name = ""
        self.ip = ""
        self.mac = ""
        self.state = "idle"
        self.remaining = 0
        self.claimed_by = None
        self.last_rx = 0
        self.label = label
        #: 本连接的唯一键（client_id@ip:port）。
        #: **不能用 client_id 当键**：同机多开、或把客户端目录整份复制到另一台
        #: 机器时 client_id 会重复，用它当键会让列表里多台机器互相覆盖
        #: （实测开 3 个客户端，主控只剩最后一行有内容）。
        self.key = label


class MasterServer(QObject):
    #: (ip, tcp_port, udp_port, group_index) —— 端口组是运行时决定的，必须报实际值
    sigStarted = pyqtSignal(str, int, int, int)
    sigListenFailed = pyqtSignal(str)
    sigClients = pyqtSignal(object)
    sigLog = pyqtSignal(str)
    #: 认领结果 {client_id, ok, reason, owner_name}
    sigClaim = pyqtSignal(object)

    def __init__(self, master_id="", master_name="", parent=None):
        super().__init__(parent)
        self.master_id = master_id
        self.master_name = master_name
        self.nodes = tuple(DEFAULT_NODE_MINUTES)
        self._targets = set()          # 已选中的 client_id
        self._conns = {}               # id(conn) -> _Conn
        self._by_key = {}              # conn.key(唯一) -> _Conn
        self._udp = None
        self._tcp = None
        self._tcp_port = 0
        self._udp_port = 0
        self._group = -1
        self._log = setup_logger("master.net", "master.log")
        self._sweep = QTimer(self)
        self._sweep.setInterval(3000)
        self._sweep.timeout.connect(self._on_sweep)

    # ================================================================ 启动
    @pyqtSlot()
    def start(self):
        last_error = ""
        for index in range(PORT_GROUP_COUNT):
            udp_port, tcp_port = port_group(index)
            tcp = QTcpServer(self)
            if not tcp.listen(QHostAddress.AnyIPv4, tcp_port):
                last_error = "TCP %d 被占用（%s）" % (tcp_port, tcp.errorString())
                tcp.deleteLater()
                continue
            udp = QUdpSocket(self)
            if not udp.bind(QHostAddress.AnyIPv4, udp_port,
                            QUdpSocket.ShareAddress | QUdpSocket.ReuseAddressHint):
                last_error = "UDP %d 被占用" % udp_port
                tcp.close()
                tcp.deleteLater()
                udp.deleteLater()
                continue

            self._tcp, self._udp = tcp, udp
            self._tcp_port, self._udp_port, self._group = tcp_port, udp_port, index
            tcp.newConnection.connect(self._on_new_connection)
            udp.readyRead.connect(self._on_udp)
            self._sweep.start()
            ip = get_primary_ip()
            self._emit_log("主控已启动：TCP %s:%d / UDP %d（端口组 %d，最多支持 %d 个主控同机运行）"
                           % (ip, tcp_port, udp_port, index + 1, PORT_GROUP_COUNT))
            self.sigStarted.emit(ip, tcp_port, udp_port, index + 1)
            return

        msg = ("没有可用端口：%s\n\n" % last_error
               + "可能已经开了 %d 个主控，或者端口被别的程序占用。"
               % PORT_GROUP_COUNT)
        self._log.error(msg)
        self.sigListenFailed.emit(msg)

    @pyqtSlot()
    def shutdown(self):
        self._sweep.stop()
        for conn in list(self._conns.values()):
            self._drop(conn, notify=True)
        self._conns.clear()
        self._by_key.clear()
        if self._udp is not None:
            try:
                self._udp.close()
            except RuntimeError:
                pass
            self._udp = None
        if self._tcp is not None:
            try:
                self._tcp.close()
            except RuntimeError:
                pass
            self._tcp = None

    # ================================================================ 发现
    def _on_udp(self):
        while self._udp is not None and self._udp.hasPendingDatagrams():
            datagram = self._udp.receiveDatagram()
            try:
                msg = decode(bytes(datagram.data()))
            except ValueError:
                continue
            if msg.get("type") != MSG_DISCOVER:
                continue
            try:
                self._udp.writeDatagram(
                    encode(MSG_HERE,
                           master_id=self.master_id,
                           master_name=self.master_name,
                           tcp_port=self._tcp_port),
                    datagram.senderAddress(), datagram.senderPort())
            except (RuntimeError, ValueError):
                pass

    # ================================================================ 连接
    def _on_new_connection(self):
        while self._tcp is not None and self._tcp.hasPendingConnections():
            sock = self._tcp.nextPendingConnection()
            if sock is None:
                continue
            peer = "%s:%d" % (sock.peerAddress().toString(), sock.peerPort())
            conn = _Conn(sock, peer)
            self._conns[id(conn)] = conn
            sock.readyRead.connect(lambda c=conn: self._on_ready(c))
            sock.disconnected.connect(lambda c=conn: self._on_gone(c))
            sock.stateChanged.connect(lambda st, c=conn: self._on_state(c, st))
            # 受理连接后立刻自报身份。
            # 主控重启后 master_id 变了但端口不变，客户端如果抢在自我介绍之前
            # 注册，就会在"新主控"名下多出一行（随后才被清理），列表会闪一下重复。
            self._send(conn, MSG_HELLO,
                       master_id=self.master_id, master_name=self.master_name)
            self._emit_log("客户端接入：%s" % peer)

    def _on_state(self, conn, state):
        if state == QAbstractSocket.UnconnectedState:
            self._on_gone(conn)

    def _on_sweep(self):
        """每 3 秒：刷新列表 + 清掉长时间没心跳的僵尸连接。"""
        now = self._now()
        dead = []
        for conn in list(self._conns.values()):
            if conn.last_rx <= 0:
                continue
            if now - conn.last_rx > CONN_IDLE_MS:
                dead.append(conn)
        for conn in dead:
            label = conn.name or conn.label
            self._on_gone(conn)
            self._emit_log("「%s」超过 %d 秒无心跳，已移除"
                           % (label, CONN_IDLE_MS // 1000))
        self._emit_clients()

    def _on_gone(self, conn):
        if self._conns.pop(id(conn), None) is None:
            return
        self._by_key.pop(conn.key, None)
        self._targets.discard(conn.key)
        name = conn.name or conn.label
        self._drop(conn, notify=False)
        self._emit_log("客户端下线：%s" % name)
        self._emit_clients()

    def _drop(self, conn, notify=False):
        if notify:
            self._send(conn, MSG_BYE)
        sock, conn.sock = conn.sock, None
        if sock is not None:
            try:
                sock.abort()
                sock.deleteLater()
            except RuntimeError:
                pass

    def _on_ready(self, conn):
        if conn.sock is None:
            return
        try:
            data = bytes(conn.sock.readAll())
        except RuntimeError:
            return
        if not data:
            return
        conn.last_rx = self._now()
        for msg in conn.parser.feed(data):
            self._dispatch(conn, msg)

    # ================================================================ 收
    def _dispatch(self, conn, msg):
        mtype = msg.get("type")

        if mtype == MSG_REGISTER:
            conn.client_id = str(msg.get("client_id") or conn.client_id or conn.label)
            conn.name = str(msg.get("name") or "")
            conn.ip = str(msg.get("ip") or "") or conn.label.split(":")[0]
            conn.mac = str(msg.get("mac") or "")
            conn.claimed_by = msg.get("claimed_by")
            conn.key = "%s@%s" % (conn.client_id, conn.label)
            self._by_key[conn.key] = conn
            # 把主控自己的身份一起回给客户端。
            # 主控重启后 master_id 会变、端口不变，客户端只靠端口重连会在同一台
            # 主控上留两条连接（旧 id + 新 id），列表里就会出现重复的同一台客户端。
            # 客户端拿到真实身份后能自己纠正（见 client/net.py 的 _adopt_master_id）。
            self._send(conn, MSG_ACK, what=MSG_REGISTER,
                       master_id=self.master_id, master_name=self.master_name)
            # 同一个身份 ID 出现在两条连接上：要么同机多开，要么配置文件被整份
            # 复制到了另一台机器。这种情况下必须让用户在列表里分得清，而不是
            # 悄悄合并成一台。
            dup = sum(1 for c in self._conns.values()
                      if c.client_id == conn.client_id and c is not conn)
            if dup:
                self._emit_log(
                    "注意：「%s」与另一台客户端用了同一个身份 ID（%s…）——"
                    "可能是同机多开或复制了客户端目录；列表里会把它们分开显示。"
                    % (conn.name or "未命名", conn.client_id[:8]))
            self._emit_log("客户端注册：%s（%s）" % (conn.name or "未命名", conn.ip))
            self._emit_clients()
            return

        if mtype == MSG_HEARTBEAT:
            conn.state = str(msg.get("state") or "idle")
            conn.remaining = int(msg.get("remaining") or 0)
            conn.claimed_by = msg.get("claimed_by")
            # 必须回一个 ACK。
            # 客户端靠"收到过数据"来判断主控还活着；主控平时不发任何东西，
            # 若不回心跳，客户端 8 秒后就认为主控死了 -> 断开重连 -> 无限循环，
            # 表现就是"主控界面里一台客户端都没有"。
            self._send(conn, MSG_ACK, what=MSG_HEARTBEAT)
            self._emit_clients()
            return

        if mtype == MSG_CLAIM_STATE:
            conn.claimed_by = msg.get("claimed_by")
            self._emit_clients()
            return

        if mtype == MSG_CLAIM_RESULT:
            self._on_claim_result(conn, msg)
            return

        if mtype == MSG_ACK:
            if not msg.get("ok", True):
                reason = msg.get("reason") or ""
                if reason == "not_owner":
                    owner = (msg.get("owner") or {}).get("name") or "其他主控"
                    self._emit_log(
                        "「%s」拒绝了本次指令：它正被「%s」占用（请先认领）"
                        % (conn.name or conn.label, owner))
            return

        if mtype == MSG_BYE:
            self._on_gone(conn)
            return

    def _on_claim_result(self, conn, msg):
        ok = bool(msg.get("ok"))
        owner = msg.get("owner") or {}
        cid = conn.client_id or str(msg.get("client_id") or "")
        if ok:
            self._emit_log("已认领「%s」" % (conn.name or conn.label))
        else:
            reason = str(msg.get("reason") or "")
            owner_name = str(owner.get("name") or "")
            if reason == "busy":
                self._emit_log("认领「%s」失败：已被「%s」占用"
                               % (conn.name or conn.label, owner_name or "其他主控"))
            else:
                self._emit_log("认领「%s」失败：%s"
                               % (conn.name or conn.label, reason or "未知原因"))
        self.sigClaim.emit({"key": conn.key, "client_id": cid, "ok": ok,
                            "reason": str(msg.get("reason") or ""),
                            "owner_name": str(owner.get("name") or ""),
                            "name": conn.name})
        self._emit_clients()

    # ================================================================ 发
    def _send(self, conn, mtype, **payload):
        if conn is None or conn.sock is None:
            return False
        try:
            conn.sock.write(encode(mtype, **payload))
            conn.sock.flush()
            return True
        except (RuntimeError, ValueError):
            return False

    def _target_conns(self):
        out = []
        for key in list(self._targets):
            conn = self._by_key.get(key)
            if conn is not None and conn.sock is not None:
                out.append(conn)
        return out

    def _broadcast(self, mtype, **payload):
        """发给选中集合；没选任何客户端就什么都不发。"""
        sent = 0
        for conn in self._target_conns():
            if self._send(conn, mtype, **payload):
                sent += 1
        return sent

    # ================================================================ 槽
    @pyqtSlot(object)
    def set_targets(self, keys):
        # 这里的元素是 conn.key（连接唯一键），不是 client_id
        self._targets = set(str(c) for c in (keys or []))
        self._emit_log("控制目标：%d 台" % len(self._targets))

    @pyqtSlot(str)
    def claim(self, key):
        conn = self._by_key.get(key)
        if conn is None:
            return
        self._send(conn, MSG_CLAIM,
                   master_id=self.master_id, master_name=self.master_name)
        self._emit_log("请求认领「%s」…" % (conn.name or conn.label))

    @pyqtSlot(str)
    def release(self, key):
        conn = self._by_key.get(key)
        if conn is None:
            return
        self._send(conn, MSG_RELEASE, master_id=self.master_id)
        self._targets.discard(key)
        self._emit_log("释放「%s」" % (conn.name or conn.label))

    @pyqtSlot(object)
    def set_nodes(self, nodes):
        if not nodes:
            return
        self.nodes = tuple(int(n) for n in nodes)
        self._emit_log("提醒节点：%s 分钟" % " / ".join(str(n) for n in self.nodes))
        # 正在倒计时的客户端需要立刻用新节点（重发当前项）
        for conn in self._target_conns():
            if conn.state == "running" and conn.remaining > 0:
                self._send(conn, MSG_START,
                           total=max(60, conn.remaining),
                           target_ms=self._now() + conn.remaining * 1000,
                           label="", nodes=list(self.nodes))

    @pyqtSlot("qint64")
    def start_countdown(self, total_seconds):
        self.start_countdown_until(total_seconds, 0, "")

    @pyqtSlot("qint64", "qint64", str)
    def start_countdown_until(self, total_seconds, target_ms=0, label=""):
        total = int(total_seconds)
        target = int(target_ms) or (self._now() + total * 1000)
        sent = self._broadcast(MSG_START, total=total, target_ms=target,
                               label=label or "", nodes=list(self.nodes))
        self._emit_log("下发倒计时 %d 秒%s -> %d 台客户端"
                       % (total, ("（%s）" % label) if label else "", sent))

    @pyqtSlot("qint64")
    def add_time(self, delta_seconds):
        delta = int(delta_seconds)
        sent = self._broadcast(MSG_ADD_TIME, delta=delta,
                               target_ms=self._now() + delta * 1000)
        self._emit_log("加时 +%d 秒 -> %d 台客户端" % (delta, sent))

    @pyqtSlot()
    def send_prompt(self):
        sent = self._broadcast(MSG_PROMPT)
        self._emit_log("手动提示 -> %d 台客户端" % sent)

    @pyqtSlot(str)
    def send_custom(self, text):
        text = " ".join(str(text or "").split())[:CUSTOM_TEXT_MAX]
        if not text:
            return
        sent = self._broadcast(MSG_CUSTOM, text=text)
        self._emit_log("自定义提示「%s」-> %d 台客户端" % (text, sent))

    @pyqtSlot()
    def stop_countdown(self):
        sent = self._broadcast(MSG_STOP)
        self._emit_log("停止 -> %d 台客户端" % sent)

    @pyqtSlot()
    def reset_countdown(self):
        sent = self._broadcast(MSG_RESET)
        self._emit_log("重置 -> %d 台客户端" % sent)

    # ================================================================ 输出
    def clients(self):
        now = self._now()
        out = []
        # 统计每个身份 ID 出现了几次，重复的要在名字上标出来
        counts = {}
        for conn in self._conns.values():
            if conn.client_id:
                counts[conn.client_id] = counts.get(conn.client_id, 0) + 1
        for conn in list(self._conns.values()):
            if not conn.client_id:
                continue
            owner = conn.claimed_by or {}
            owner_id = str(owner.get("id") or "")
            online = (now - conn.last_rx) < CLIENT_OFFLINE_MS if conn.last_rx else True
            name = conn.name or conn.label
            if counts.get(conn.client_id, 0) > 1:
                # 同身份多台：后缀连接端口，否则用户看到两行一模一样的名字
                name = "%s（%s）" % (name, conn.label.rsplit(":", 1)[-1])
            out.append({
                "key": conn.key,
                "id": conn.client_id,
                "name": name,
                "ip": conn.ip or conn.label.split(":")[0],
                "mac": conn.mac,
                "state": conn.state,
                "remaining": conn.remaining,
                "online": online,
                "owner_id": owner_id,
                "owner_name": str(owner.get("name") or ""),
                "mine": owner_id == self.master_id,
                "free": not owner_id,
                "selected": conn.key in self._targets,
            })
        out.sort(key=lambda c: (not c["online"], c["name"]))
        return out

    def _emit_clients(self):
        self.sigClients.emit(self.clients())

    def _now(self):
        from common.protocol import now_ms
        return now_ms()

    def _emit_log(self, text):
        self._log.info("%s", text)
        self.sigLog.emit(text)