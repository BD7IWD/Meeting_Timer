# -*- coding: utf-8 -*-
"""多主控 / 多客户端 端到端自测。

    python tests/selftest_multi.py

在**同一台电脑**上跑 2 个主控 + 2 个客户端（靠端口组隔离），验证：
  * 端口组不冲突，两个主控各自起来
  * 每个客户端都能被**两个**主控看到（名字正确）
  * 认领独占：M1 认领 C1 后，M2 再认领 C1 必须失败并说明被谁占用
  * 指令只发给选中的那台（C2 收不到）
  * 越权指令被拒（M2 不是占用者 -> not_owner）
  * 释放后别人立刻能认领
  * 占用者失联 -> 客户端自动解锁
  * 改名实时同步到所有主控

注意：这里所有对象都在**同一个线程**里跑（都靠 Qt 事件循环驱动），
生产环境里主控/客户端各自在自己的 worker 线程，socket 行为一致。
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import QApplication  # noqa: E402

import client.net as cnet  # noqa: E402
from client.net import ClientNet  # noqa: E402
from common import const  # noqa: E402
from common.protocol import now_ms  # noqa: E402
from master.server import MasterServer  # noqa: E402

APP = None
_results = []
_TMP = tempfile.gettempdir()


def check(name, ok, detail=""):
    _results.append((name, bool(ok)))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  " + str(detail)) if detail else ""))


def pump(ms):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        APP.processEvents()
        time.sleep(0.01)


def wait_until(pred, timeout_ms=8000):
    end = time.time() + timeout_ms / 1000.0
    while time.time() < end:
        APP.processEvents()
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.02)
    return False


def key_of(master, client_id):
    """按 client_id 找到那台客户端的连接键（认领/下发要用 key，不是 client_id）。

    注意 key 形如 client_id@对端ip:对端端口，**每个主控看到的不一样**
    （客户端对每个主控各开一条连接，本地端口不同），所以必须按主控分别取。
    """
    for c in master.clients():
        if c.get("id") == client_id:
            return c["key"]
    return ""


class Recorder(object):
    """把客户端的回调都收下来，方便断言。"""

    def __init__(self, net):
        self.starts = []
        self.prompts = 0
        self.customs = []
        self.claims = []
        net.sigStart.connect(self._start)
        net.sigPrompt.connect(self._prompt)
        net.sigCustom.connect(self._custom)
        net.sigClaim.connect(self._claim)

    def _start(self, total, target, label, nodes):
        self.starts.append((int(total), int(target), label, nodes))

    def _prompt(self):
        self.prompts += 1

    def _custom(self, text):
        self.customs.append(text)

    def _claim(self, mid, name):
        self.claims.append((mid, name))


def main():
    global APP
    APP = QApplication(sys.argv)

    print("\n[1] 端口组")
    udp0, tcp0 = const.port_group(0)
    udp1, tcp1 = const.port_group(1)
    check("两组端口不重叠", (udp0, tcp0) != (udp1, tcp1), (udp0, tcp0, udp1, tcp1))
    check("默认认领超时 15 秒", const.CLAIM_TIMEOUT_MS == 15000, const.CLAIM_TIMEOUT_MS)

    print("\n[2] 起两个主控")
    m1 = MasterServer("m1-test", "主控A")
    m2 = MasterServer("m2-test", "主控B")
    started = []
    m1.sigStarted.connect(lambda ip, tcp, udp, grp: started.append(("A", tcp, udp, grp)))
    m2.sigStarted.connect(lambda ip, tcp, udp, grp: started.append(("B", tcp, udp, grp)))
    failed = []
    m1.sigListenFailed.connect(lambda r: failed.append(("A", r)))
    m2.sigListenFailed.connect(lambda r: failed.append(("B", r)))
    m1.start()
    m2.start()
    pump(300)
    check("两个主控都监听成功", not failed, failed)
    check("两个主控占了不同端口",
          len(started) == 2 and started[0][1] != started[1][1], started)
    check("两个主控用了不同端口组",
          len(started) == 2 and started[0][3] != started[1][3], started)
    # 端口组是运行时挑的：本机若已有别的主控占着第 1 组（比如用户自己开着），
    # 本测试会自动退到后面的组。所以断言"端口与它报的端口组一致"，
    # 而不是写死"必须用第 1 组"。
    by_label = {}
    for item in started:
        by_label[item[0]] = item
    for label, srv in (("A", m1), ("B", m2)):
        if label not in by_label:
            check("主控%s 上报了启动信息" % label, False, started)
            continue
        _lab, tcp, udp, grp = by_label[label]
        check("主控%s 的端口与端口组号自洽" % label,
              (udp, tcp) == const.port_group(grp - 1),
              "组%d expected=%r actual=%r" % (grp, const.port_group(grp - 1), (udp, tcp)))
        check("主控%s 实际监听的端口与上报一致" % label,
              (srv._udp_port, srv._tcp_port) == (udp, tcp),
              "上报=%r 实际=%r" % ((udp, tcp), (srv._udp_port, srv._tcp_port)))

    print("\n[3] 起两个客户端")
    def make_cfg(cid, name):
        return {"client_id": cid, "name": name,
                "_source": os.path.join(_TMP, "_mt_multi_%s.json" % cid)}

    c1 = ClientNet(make_cfg("c1-test", "301会议室"))
    c2 = ClientNet(make_cfg("c2-test", "302会议室"))
    r1, r2 = Recorder(c1), Recorder(c2)
    c1.start()
    c2.start()

    ok = wait_until(lambda: len(m1.clients()) == 2 and len(m2.clients()) == 2, 15000)
    check("两个主控都看到了 2 台客户端", ok,
          "A=%d B=%d" % (len(m1.clients()), len(m2.clients())))

    names = sorted(c["name"] for c in m1.clients())
    check("主控A 看到的名字正确", names == ["301会议室", "302会议室"], names)
    names_b = sorted(c["name"] for c in m2.clients())
    check("主控B 也看到同样的名字", names_b == ["301会议室", "302会议室"], names_b)

    print("\n[4] 认领独占")
    claims_a, claims_b = [], []
    m1.sigClaim.connect(lambda info: claims_a.append(info))
    m2.sigClaim.connect(lambda info: claims_b.append(info))

    m1.claim(key_of(m1, "c1-test"))
    ok = wait_until(lambda: any(c["mine"] for c in m1.clients()), 6000)
    check("主控A 成功认领 301会议室", ok,
          [(c["name"], c["mine"]) for c in m1.clients()])
    check("客户端侧记住了占用者", c1._owner is not None and c1._owner[0] == "m1-test",
          c1._owner)

    m2.claim(key_of(m2, "c1-test"))
    ok = wait_until(lambda: any(not i["ok"] for i in claims_b), 6000)
    check("主控B 认领同一台被拒绝", ok, claims_b)
    if ok:
        denied = [i for i in claims_b if not i["ok"]][-1]
        check("拒绝原因是被占用", denied["reason"] == "busy", denied)
        check("拒绝时带出占用者名字", denied["owner_name"] == "主控A",
              denied["owner_name"])

    m2.clients()
    check("主控B 的列表里这台显示为别人占用",
          any(c["id"] == "c1-test" and c["owner_name"] == "主控A" and not c["mine"]
              for c in m2.clients()),
          [(c["name"], c["owner_name"], c["mine"]) for c in m2.clients()])

    print("\n[5] 指令只发给选中的客户端")
    m1.set_targets([key_of(m1, "c1-test")])
    pump(200)
    deadline = now_ms() + 600 * 1000
    m1.start_countdown_until(600, deadline, "硬件培训")
    ok = wait_until(lambda: len(r1.starts) == 1, 6000)
    check("301会议室 收到了倒计时", ok, r1.starts)
    check("302会议室 没有收到（没被选中）", len(r2.starts) == 0, r2.starts)
    if r1.starts:
        check("下发的议题名正确", r1.starts[0][2] == "硬件培训", r1.starts[0][2])
        check("下发的是绝对目标时间戳", r1.starts[0][1] == deadline,
              (r1.starts[0][1], deadline))

    print("\n[6] 越权指令被拒")
    m2.set_targets([key_of(m2, "c1-test")])
    pump(200)
    before = r1.prompts
    m2.send_prompt()
    pump(1200)
    check("被占用者拒绝非占用主控的操作", r1.prompts == before,
          "prompts %d -> %d" % (before, r1.prompts))

    print("\n[7] 释放后别人能认领")
    m1.release(key_of(m1, "c1-test"))
    ok = wait_until(lambda: c1._owner is None, 6000)
    check("释放后客户端回到空闲", ok, c1._owner)
    m2.claim(key_of(m2, "c1-test"))
    ok = wait_until(lambda: any(c["mine"] for c in m2.clients()), 6000)
    check("主控B 立刻认领成功", ok,
          [(c["name"], c["mine"]) for c in m2.clients()])

    print("\n[8] 占用者失联 -> 自动解锁")
    # 把阈值临时改成 3 秒，否则要等 15 秒；真实值单独断言过
    saved = cnet.CLAIM_TIMEOUT_MS
    cnet.CLAIM_TIMEOUT_MS = 3000
    try:
        m2.shutdown()
        ok = wait_until(lambda: c1._owner is None, 12000)
        check("占用它的主控下线后自动解锁", ok, c1._owner)
    finally:
        cnet.CLAIM_TIMEOUT_MS = saved

    print("\n[9] 改名实时同步")
    ok = wait_until(lambda: len(m1.clients()) >= 1, 8000)
    c1.set_name("Q3季度汇报")
    ok = wait_until(
        lambda: any(c["name"] == "Q3季度汇报" for c in m1.clients()), 8000)
    check("改名后主控立刻看到新名字", ok,
          [c["name"] for c in m1.clients()])

    print("\n[10] 托盘集成（回归：槽函数抛异常会让客户端秒退）")
    from client.tray import TrayIcon
    tray = TrayIcon()
    tray.show()
    rail = []
    try:
        tray.set_status({"connected": True, "phase": "claimed",
                         "detail": "已被「主控A」控制", "master": "主控A",
                         "owner": "主控A", "name": "301会议室",
                         "ip": "127.0.0.1", "mac": "aa:bb:cc:dd:ee:ff"})
        tray.set_owner("m1-test", "主控A")
        tray.set_owner("", "")
        tray.set_countdown("running", 1234, 3600, "硬件培训")
        rail.append("ok")
    except Exception as exc:
        rail.append(repr(exc))
    check("托盘的 set_owner / set_status 不抛异常（否则客户端会被 abort）",
          rail == ["ok"], rail)
    tray.hide()

    print("\n[11] 两台客户端用同一个身份 ID（回归：列表不能塌缩成一行）")
    # 真实成因：同一份 client_config.json 被多个实例使用（同机多开，或把客户端
    # 目录整份复制到另一台机器）。此时两台 client_id 完全相同，如果主控拿
    # client_id 当行键，就会互相覆盖 —— 实测 3 台只剩最后一行有内容。
    c3 = ClientNet({"client_id": "dup-test", "name": "Q3季度会议",
                    "_source": os.path.join(_TMP, "_mt_dup_a.json")})
    c4 = ClientNet({"client_id": "dup-test", "name": "Q3季度会议",
                    "_source": os.path.join(_TMP, "_mt_dup_b.json")})
    c3.start()
    c4.start()

    def dup_rows():
        return [c for c in m1.clients() if c.get("id") == "dup-test"]

    ok = wait_until(lambda: len(dup_rows()) == 2, 15000)
    rows = dup_rows()
    check("同一个 client_id 的两台都出现在列表里", ok and len(rows) == 2,
          [(c["name"], c["key"]) for c in rows])
    if len(rows) == 2:
        check("两台的连接键不同（不会互相覆盖）",
              rows[0]["key"] != rows[1]["key"], [c["key"] for c in rows])
        check("两台的显示名被自动区分开（带端口后缀）",
              rows[0]["name"] != rows[1]["name"],
              [c["name"] for c in rows])

        # 认领其中一台，另一台必须不受影响
        target = rows[0]["key"]
        other = rows[1]["key"]
        m1.claim(target)
        ok = wait_until(lambda: any(c["key"] == target and c["mine"]
                                    for c in m1.clients()), 6000)
        check("能单独认领其中一台", ok,
              [(c["name"], c["mine"]) for c in m1.clients()])
        check("另一台没有被连带认领",
              not any(c["key"] == other and c["mine"] for c in m1.clients()),
              [(c["name"], c["mine"]) for c in m1.clients()])

    for obj in (c3, c4):
        try:
            obj.shutdown()
        except Exception:
            pass

    print("\n[12] 主控重启后不能出现重复客户端（回归：启动几次就多几行）")
    # 真实现象：主控重启后 master_id 变了但监听端口不变。客户端按端口重连的那条
    # 连接实际连上了新主控，UDP 发现又为新 id 建了一条 -> 同一台客户端显示多行。
    # 只统计"重启测试"这一台：前面步骤的客户端还在重连，会混进新主控
    def rows(m):
        return [c for c in m.clients() if c.get("id") == "restart-test"]

    mA = MasterServer("restart-a", "重启前主控")
    tcp_a = []
    mA.sigStarted.connect(lambda ip, tcp, udp, grp: tcp_a.append(tcp))
    mA.start()
    pump(500)

    cR = ClientNet({"client_id": "restart-test", "name": "重启测试",
                    "_source": os.path.join(_TMP, "_mt_restart.json")})
    cR.start()

    ok = wait_until(lambda: len(rows(mA)) == 1, 15000)
    check("重启前：列表里 1 台", ok and len(rows(mA)) == 1,
          [(c["name"], c["key"]) for c in rows(mA)])

    port_before = mA._tcp_port
    mA.shutdown()
    pump(1500)

    # 新主控：新 master_id，端口组会被重新占用 -> 同一个端口（模拟真实重启）
    mB = MasterServer("restart-b", "重启后主控")
    tcp_b = []
    mB.sigStarted.connect(lambda ip, tcp, udp, grp: tcp_b.append(tcp))
    mB.start()
    pump(500)
    check("重启后端口与原主控相同（确实是端口复用场景）",
          tcp_b and tcp_b[0] == port_before, (port_before, tcp_b))

    ok = wait_until(lambda: len(rows(mB)) == 1, 20000)
    check("重启后仍然只显示 1 台", ok and len(rows(mB)) == 1,
          [(c["name"], c["key"]) for c in rows(mB)])

    # 关键：不能出现"一闪而过"的重复行。
    # 上一版是在注册之后才发现身份不对再清理，列表里会短暂出现两行同一客户端。
    # 现在改成 HELLO 握手后才注册，这里密集采样 12 秒，最大行数必须始终是 1。
    max_seen = 0
    end = time.time() + 12
    while time.time() < end:
        APP.processEvents()
        max_seen = max(max_seen, len(rows(mB)))
        time.sleep(0.02)
    check("重启后全程（含瞬间）都没有重复行", max_seen <= 1,
          "采样期间最大行数 = %d" % max_seen)

    # 再连着重启一次，模拟"反复启动主控"
    mB.shutdown()
    pump(1500)
    mC = MasterServer("restart-c", "第二次重启")
    mC.start()
    ok = wait_until(lambda: len(rows(mC)) == 1, 20000)
    check("第二次重启后还是 1 台（不会累积）",
          ok and len(rows(mC)) == 1,
          [(c["name"], c["key"]) for c in rows(mC)])

    pump(6000)
    check("再过 6 秒也没有额外重复", len(rows(mC)) == 1,
          [(c["name"], c["key"]) for c in rows(mC)])
    cR.shutdown()
    mC.shutdown()

    print("\n[13] 收拾现场")
    for obj in (c1, c2):
        try:
            obj.shutdown()
        except Exception:
            pass
    for obj in (m1, m2):
        try:
            obj.shutdown()
        except Exception:
            pass
    pump(300)
    for cid in ("c1-test", "c2-test"):
        path = os.path.join(_TMP, "_mt_multi_%s.json" % cid)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass

    total = len(_results)
    failed_n = sum(1 for _n, ok in _results if not ok)
    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项（共 %d 项）" % (total - failed_n, failed_n, total))
    if failed_n:
        for name, ok in _results:
            if not ok:
                print("  FAIL: " + name)
    print("=" * 60)
    return 1 if failed_n else 0


if __name__ == "__main__":
    sys.exit(main())