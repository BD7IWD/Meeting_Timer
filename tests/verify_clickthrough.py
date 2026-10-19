# -*- coding: utf-8 -*-
"""鼠标穿透 / 点击接收 的操作系统级验证。

不靠"看起来对不对"，直接问 Windows：
    WindowFromPoint(x, y) 在横幅正中心的坐标上，返回的是哪一个窗口？

* 到点横幅：必须返回**横幅自己**的 HWND —— 证明它能收到鼠标点击
* 节点横幅：必须返回**它下面的那个窗口** —— 证明 WS_EX_TRANSPARENT 生效，
  鼠标完全穿透，培训/PPT 上的点击不会被横幅吃掉

顺带用 QTest 模拟一次真实左键点击，验证"点击关闭常驻横幅"的链路真的通。
"""
import ctypes
import os
import sys
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QCoreApplication, QEventLoop, QPoint, QTimer, Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

from client.banner import BannerManager  # noqa: E402
from common.const import BANNER_DONE_SUB_TEXT, BANNER_DONE_TEXT  # noqa: E402

_APP = None
_user32 = ctypes.windll.user32
_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL

_results = []


def check(name, ok, extra=""):
    _results.append((name, ok))
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))


def wait_for(cond, timeout_ms=6000, poll_ms=50):
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(poll_ms)
    poll.timeout.connect(lambda: cond() and loop.quit())
    poll.start()
    hard = QTimer()
    hard.setSingleShot(True)
    hard.timeout.connect(loop.quit)
    hard.start(timeout_ms)
    if cond():
        loop.quit()
    loop.exec_()
    poll.stop()
    return bool(cond())


def hwnd_center(hwnd):
    """窗口在**物理像素**坐标系下的中心点（WindowFromPoint 用的是物理像素）。"""
    rect = wintypes.RECT()
    _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return QPoint((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)


def window_at(point):
    return int(_user32.WindowFromPoint(wintypes.POINT(point.x(), point.y())) or 0)


def describe(hwnd):
    """给一个 HWND 打出"身份证"：类名 / 标题 / 位置，便于判断探到的是谁。"""
    if not hwnd:
        return "<null>"
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    cls = buf.value
    _user32.GetWindowTextW(wintypes.HWND(hwnd), buf, 256)
    title = buf.value
    rect = wintypes.RECT()
    _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    visible = bool(_user32.IsWindowVisible(wintypes.HWND(hwnd)))
    return "0x%X class=%r title=%r rect=(%d,%d %dx%d) visible=%s" % (
        hwnd, cls, title, rect.left, rect.top,
        rect.right - rect.left, rect.bottom - rect.top, visible)


def main():
    global _APP
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    _APP = QApplication(sys.argv)

    # ---- 铺一个"背景窗口"，用来代表横幅下面的 PPT/培训画面 ----
    # 铺满整屏：这样横幅覆盖的任意一点，下方都必然有这个窗口兜底
    # （桌面上还可能有别的窗口，见下面的多点探测）
    screen = _APP.primaryScreen()
    ref = QWidget(None, Qt.FramelessWindowHint | Qt.Tool)
    ref.setStyleSheet("background:#0B3D2E;")
    ref.setGeometry(screen.geometry())
    ref.show()
    wait_for(lambda: ref.isVisible() and int(ref.winId()) != 0, timeout_ms=4000)
    ref_hwnd = int(ref.winId())
    print("背景窗口 HWND = 0x%X" % ref_hwnd)

    manager = BannerManager("primary")
    logs = []
    manager.sigLog.connect(lambda t: (logs.append(t), print("  [banner] " + t)))

    # ================================================== 1. 到点横幅：接收鼠标
    print("\n[1] 到点横幅（常驻、需人工关闭）")
    manager.show_done(BANNER_DONE_TEXT, BANNER_DONE_SUB_TEXT)
    banner = manager._banners[0]
    wait_for(lambda: banner.fade > 0.95, timeout_ms=6000)
    bw_hwnd = int(banner.winId())
    print("到点横幅 HWND = 0x%X，fade=%.2f" % (bw_hwnd, banner.fade))

    center = hwnd_center(bw_hwnd)
    at = window_at(center)
    check("系统层面命中横幅本身（能收到鼠标点击）", at == bw_hwnd,
          "WindowFromPoint(%d,%d)=0x%X，期望 0x%X" % (center.x(), center.y(), at, bw_hwnd))

    # ---- 背景去掉后必须验证：可点击区域收缩到了文字本身 ----
    # 否则屏幕上那一条（全宽 × 25% 屏高）就是个**看不见、还会吃掉鼠标点击**的黑洞。
    bounds = banner.text_bounds()
    rect = wintypes.RECT()
    _user32.GetWindowRect(wintypes.HWND(bw_hwnd), ctypes.byref(rect))
    scale = (rect.right - rect.left) / float(max(1, banner.width()))
    print("  文字区域(widget) = %s   窗口(物理) = (%d,%d %dx%d)  scale=%.2f"
          % (bounds, rect.left, rect.top,
             rect.right - rect.left, rect.bottom - rect.top, scale))

    check("横幅已设置遮罩（可点击区域被收缩过）",
          not banner.mask().isEmpty(),
          "mask 是否为空=%s" % banner.mask().isEmpty())

    probe_wx = max(4, bounds.left() - 40)
    probe_wy = bounds.center().y()
    px = rect.left + int(probe_wx * scale)
    py = rect.top + int(probe_wy * scale)
    at_blank = window_at(QPoint(px, py))
    check("文字之外的空白区**不再**被横幅吃掉（穿透到下层）",
          at_blank != bw_hwnd,
          "WindowFromPoint(%d,%d)=0x%X，横幅自己=0x%X" % (px, py, at_blank, bw_hwnd))

    text_center_widget = bounds.center()
    px2 = rect.left + int(text_center_widget.x() * scale)
    py2 = rect.top + int(text_center_widget.y() * scale)
    at_text = window_at(QPoint(px2, py2))
    check("文字本身依然能点到（点文字关闭这条路径可用）",
          at_text == bw_hwnd,
          "WindowFromPoint(%d,%d)=0x%X，横幅自己=0x%X" % (px2, py2, at_text, bw_hwnd))

    # 模拟真实左键点击 ----
    local = QPoint(banner.width() // 2, banner.height() // 2)
    QTest.mouseClick(banner, Qt.LeftButton, Qt.NoModifier, local)
    closed = wait_for(lambda: not manager._banners, timeout_ms=3000)
    check("点击横幅后它真的被关掉了", closed,
          "剩余横幅=%d" % len(manager._banners))
    check("关闭动作有日志记录",
          any("关闭" in t for t in logs), "logs=%r" % logs)

    # 关键：必须等这个窗口**真正从屏幕上消失**（还有 320ms 淡出），
    # 否则它会继续盖在探测点上，让下一步的"穿透命中谁"探到它身上。
    gone = wait_for(lambda: not banner.isVisible(), timeout_ms=4000)
    check("旧横幅窗口已完全消失（为下一步探测清场）", gone,
          "isVisible=%s" % banner.isVisible())

    # ================================================== 2. 节点横幅：鼠标穿透
    print("\n[2] 节点横幅（10 秒自动消失、鼠标必须穿透）")
    logs.clear()
    manager.show_node("倒计时 10 分钟")
    banner2 = manager._banners[0]
    wait_for(lambda: banner2.fade > 0.95, timeout_ms=6000)
    b2_hwnd = int(banner2.winId())
    print("节点横幅 HWND = 0x%X，fade=%.2f" % (b2_hwnd, banner2.fade))

    center2 = hwnd_center(b2_hwnd)
    print("  节点横幅矩形（物理像素）: %s" % describe(b2_hwnd))

    # 多点探测：横幅横跨整屏，而桌面上可能还有别的窗口压在我的背景窗口上面
    # （实测这个聊天窗口就盖在屏幕中央）。所以不能只探一个点就断言"命中背景窗口"。
    # 断言拆成两条：
    #   ① 任何一点都**不能**命中横幅自己  -> 鼠标完全穿透，这是核心
    #   ② 至少有一点命中了下面的背景窗口 -> 点击确实被传到了下层，不是凭空消失
    rect2 = wintypes.RECT()
    _user32.GetWindowRect(wintypes.HWND(b2_hwnd), ctypes.byref(rect2))
    probe_y = (rect2.top + rect2.bottom) // 2
    probe_xs = [rect2.left + 200, rect2.left + 600,
                (rect2.left + rect2.right) // 2, rect2.right - 200]

    hit_self = 0
    hit_ref = 0
    hit_other_window = 0
    print("  多点探测（y=%d）：" % probe_y)
    for x in probe_xs:
        at = window_at(QPoint(x, probe_y))
        if at == b2_hwnd:
            hit_self += 1
        elif at == ref_hwnd:
            hit_ref += 1
        elif at:
            hit_other_window += 1
        print("    x=%-5d -> 0x%-8X %s"
              % (x, at, "【横幅自己】" if at == b2_hwnd
                 else ("【参照窗口】" if at == ref_hwnd else "【其它窗口】")))
        if at not in (b2_hwnd, ref_hwnd):
            print("             %s" % describe(at))

    check("所有探测点都没有命中横幅自己（鼠标完全穿透）",
          hit_self == 0, "命中自己 %d/%d 次" % (hit_self, len(probe_xs)))
    # 注意：不能断言"一定命中我的参照窗口"——桌面上可能还有别的窗口把它盖住
    # （实测这个聊天窗口就铺满了整屏）。真正要证明的是：点击**落到了横幅之外的
    # 某个真实窗口上**，即穿透生效、不是被凭空吃掉。
    check("每个探测点都落到了横幅之外的某窗口上（穿透生效，不是被吃掉）",
          hit_self == 0 and (hit_ref + hit_other_window) == len(probe_xs),
          "参照窗口 %d 次 / 其它窗口 %d 次 / 共 %d 次"
          % (hit_ref, hit_other_window, len(probe_xs)))
    if hit_ref == 0:
        print("  （说明：参照窗口这次被桌面上其它窗口整个盖住了，"
              "所以命中的是那些窗口，不影响结论）")

    # 节点横幅不该被点击关闭（它压根收不到点击），只能等自动消失
    QTest.mouseClick(banner2, Qt.LeftButton, Qt.NoModifier, local)
    check("节点横幅不会因为误点而消失（点击被忽略）",
          len(manager._banners) == 1,
          "剩余横幅=%d" % len(manager._banners))

    manager.hide_all()
    wait_for(lambda: not manager._banners, timeout_ms=3000)
    ref.close()

    failed = sum(1 for _n, ok in _results if not ok)
    print("\n" + "=" * 66)
    print("通过 %d 项，失败 %d 项" % (len(_results) - failed, failed))
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
