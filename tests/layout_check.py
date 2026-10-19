# -*- coding: utf-8 -*-
"""主控界面排版检查：底部按钮有没有被压到边框上 / 被挤出可见区域。

    python tests/layout_check.py

为什么需要它：
    界面排版问题**测不出来**——单元测试从不真的显示窗口，几何尺寸全是 0。
    但排版恰恰是最容易被后续改动搞坏的东西（实测用户就报了"按钮压到边框上"）。

做法：
    `WA_DontShowOnScreen` 让 Qt 认为窗口已显示（布局正常计算、尺寸正常分配），
    但窗口永远不会出现在屏幕上，所以可以放心在无人值守的测试里跑。

这次抓到的真实 bug：
    `QTabWidget` 的最小高度**只按"当前页"计算**。两个页高度不同（手动页很矮、
    议程页很高）时，切到议程页整页会被压扁，最下面那排按钮就被挤出可见区域，
    看起来正是"压在边框上/被裁掉"。
    修法：显式把 tabs 的最小高度设成"两页里需要得更高的那个 + 标签栏高度"。

⚠️ 量坐标时注意：这些按钮的 parent 就是页面本身，所以要用
    `b.mapTo(page, QPoint(0, 0))` 取左上角；顺手把 `b.geometry().topLeft()`
    再喂给 mapTo 会重复叠加一次父偏移，数值直接翻倍（我一开始就这么量错了，
    差点把好好的布局判成 FAIL）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 用不常见的端口，避免和"正在运行的主控"抢 47820/47821
os.environ.setdefault("MEETINGTIMER_TCP_PORT", "48921")
os.environ.setdefault("MEETINGTIMER_UDP_PORT", "48920")

from PyQt5.QtCore import QPoint, Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from common import agenda as ag  # noqa: E402
from master.ui_master import MasterWindow  # noqa: E402

#: 底部按钮距离页面下边缘至少要留出的像素（少于此数就是"压到边框上"）
MIN_BOTTOM_GAP = 6

#: 议程页底部这一整排按钮
BOTTOM_BUTTONS = [
    "btn_agenda_add", "btn_agenda_edit", "btn_agenda_del",
    "btn_agenda_up", "btn_agenda_down",
    "btn_agenda_shift", "btn_agenda_next",
]

APP = None
_failures = []


def check(name, ok, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  " + str(detail)) if detail else ""))
    if not ok:
        _failures.append(name)


def measure(page, widget):
    """返回 (top, bottom, height) —— 都在 page 坐标系里。"""
    pos = widget.mapTo(page, QPoint(0, 0))
    return pos.y(), pos.y() + widget.height(), widget.height()


def main():
    global APP
    # 别碰用户真实的 agenda.json
    real_path = ag.agenda_path
    tmp = os.path.join(tempfile.gettempdir(), "_mt_layout.json")
    ag.agenda_path = lambda: tmp

    APP = QApplication(sys.argv)
    window = MasterWindow()
    window.setAttribute(Qt.WA_DontShowOnScreen, True)     # 关键：布局照算，但不上屏
    window.show()
    APP.processEvents()

    print("\n[1] 会议议程页（内容最高的一页，最容易压边框）")
    window.tabs.setCurrentIndex(1)
    APP.processEvents()
    page = window.tabs.widget(1)
    page.layout().activate()
    APP.processEvents()
    print("      窗口 %dx%d   议程页 %dx%d   tabs 最小高=%d"
          % (window.width(), window.height(), page.width(), page.height(),
             window.tabs.minimumHeight()))

    heights = []
    worst_gap = 10 ** 6
    for name in BOTTOM_BUTTONS:
        btn = getattr(window, name)
        top, bottom, height = measure(page, btn)
        gap = page.height() - 1 - bottom
        heights.append(height)
        worst_gap = min(worst_gap, gap)
        check("%s 完整落在页面内" % name, bottom <= page.height(),
              "y=%d..%d / 页高 %d" % (top, bottom, page.height()))

    check("底部按钮高度一致（不会参差不齐）", len(set(heights)) == 1,
          sorted(set(heights)))
    check("底部按钮与页面下边缘有足够间距（不压边框）",
          worst_gap >= MIN_BOTTOM_GAP,
          "最小间距 %d px（要求 >= %d）" % (worst_gap, MIN_BOTTOM_GAP))

    # 表格和按钮行不能重叠
    t_top, t_bottom, _ = measure(page, window.tbl_agenda)
    b_top = measure(page, window.btn_agenda_add)[0]
    check("议程表格与按钮行不重叠", t_bottom <= b_top,
          "表格 y=%d..%d / 按钮行 y=%d" % (t_top, t_bottom, b_top))

    print("\n[2] 手动模式页")
    window.tabs.setCurrentIndex(0)
    APP.processEvents()
    mpage = window.tabs.widget(0)
    mpage.layout().activate()
    APP.processEvents()
    m_top, m_bottom, _ = measure(mpage, window.btn_start)
    check("手动页按钮完整落在页面内", m_bottom <= mpage.height(),
          "y=%d..%d / 页高 %d" % (m_top, m_bottom, mpage.height()))
    check("手动页底部留白足够",
          mpage.height() - 1 - m_bottom >= MIN_BOTTOM_GAP,
          "%d px" % (mpage.height() - 1 - m_bottom))

    print("\n[3] 切回议程页再量一次（切页后布局要重新算对）")
    window.tabs.setCurrentIndex(1)
    APP.processEvents()
    page.layout().activate()
    APP.processEvents()
    top, bottom, _ = measure(page, window.btn_agenda_next)
    check("反复切页后按钮仍在可见区域内", bottom <= page.height(),
          "y=%d..%d / 页高 %d" % (top, bottom, page.height()))

    window.hide()
    window.close()
    ag.agenda_path = real_path
    if os.path.isfile(tmp):
        os.remove(tmp)

    print("\n[4] 客户端认领列表（多主控版）")
    table = window.table
    check("客户端表格 6 列", table.columnCount() == 6, table.columnCount())
    heads = [table.horizontalHeaderItem(i).text()
             for i in range(table.columnCount())]
    check("表头文字正确",
          heads == ["控制", "名称", "IP 地址", "状态", "占用者", "剩余"], heads)

    def has_flag(value, flag):
        """位测试。用 __and__ 是为了避免这个文件里出现位运算符。"""
        return bool(int(value).__and__(int(flag)))

    window._refresh_table([])
    check("没有客户端时「开始下发」禁用", not window.btn_start.isEnabled())
    check("顶部提示要先勾选",
          "勾选" in window.lbl_clients.text(), window.lbl_clients.text())

    # 注意：主控的行标识用的是连接唯一键 key（不是 id），控件也只认 key。
    fake = [
        {"key": "c1", "id": "c1", "name": "301会议室", "ip": "192.168.0.31",
         "state": "idle", "remaining": 0, "online": True,
         "owner_id": window.master_id, "owner_name": window.master_name,
         "mine": True, "free": False},
        {"key": "c2", "id": "c2", "name": "401 季度汇报", "ip": "192.168.0.41",
         "state": "idle", "remaining": 0, "online": True,
         "owner_id": "other-master", "owner_name": "小王主控",
         "mine": False, "free": False},
        {"key": "c3", "id": "c3", "name": "402 待命", "ip": "192.168.0.42",
         "state": "idle", "remaining": 0, "online": True,
         "owner_id": "", "owner_name": "", "mine": False, "free": True},
    ]
    window._refresh_table(fake)
    check("自己认领的那台已勾上",
          table.item(0, 0).checkState() == Qt.Checked)
    check("别人占用的那台勾不动",
          not has_flag(table.item(1, 0).flags(), Qt.ItemIsUserCheckable),
          table.item(1, 0).toolTip())
    check("空闲的那台可以勾选",
          has_flag(table.item(2, 0).flags(), Qt.ItemIsUserCheckable))
    check("「占用者」列显示谁占着",
          table.item(1, 4).text() == "小王主控", table.item(1, 4).text())
    check("自己有客户端时「开始下发」可用", window.btn_start.isEnabled())
    check("已选集合只包含我认领的那台", window._targets == {"c1"}, window._targets)
    check("顶部显示已选台数与名字",
          "已选 1 台" in window.lbl_clients.text()
          and "301会议室" in window.lbl_clients.text(),
          window.lbl_clients.text())
    window._refresh_table([])

    total = len(_failures)
    print("\n" + "=" * 60)
    if total:
        print("排版检查失败 %d 项：" % total)
        for name in _failures:
            print("  FAIL: " + name)
    else:
        print("排版检查全部通过")
    print("=" * 60)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())