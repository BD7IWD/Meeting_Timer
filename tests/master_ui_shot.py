# -*- coding: utf-8 -*-
"""把主控端界面渲染成 PNG —— 离屏渲染，不弹窗口、不抢焦点。

    python tests/master_ui_shot.py tests/master_ui.png

为什么用 `WA_DontShowOnScreen` + `grab()` 而不是屏幕截图：
    这个脚本只要出一张界面图给人看，没必要在你桌面上真的开一个窗口
    （反复弹窗很打扰）。`WA_DontShowOnScreen` 让 Qt 认为"已经显示了"——
    布局会正常计算、尺寸正常分配——但窗口永远不会出现在屏幕上，
    之后 `grab()` 就能拿到一张渲染好的位图。

顺便把用户那张真实的培训议程填进去，方便肉眼检查排版和中文显示。
"""
import os
import socket
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from common import agenda as ag  # noqa: E402
from common.const import TCP_CONTROL_PORT  # noqa: E402
from master.ui_master import MasterWindow  # noqa: E402

_APP = None

#: 用户真实的会议议程（截图演示用）
#: (开始, 结束, 活动内容, 是否"休息"——休息项不提醒客户端)
ROWS = [
    ("11:30", "13:30", "各自午餐并按定位到现场签到", True),
    ("13:30", "15:00", "硬件培训", False),
    ("15:00", "15:15", "中场休息（下午茶）", True),
    ("15:15", "18:00", "软件培训", False),
    ("18:00", "18:15", "中场休息", True),
    ("18:15", "19:00", "画质扫盲", False),
    ("19:00", "22:00", "晚餐+自由活动", True),
]

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    global _APP
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "master_ui.png")

    # 主控界面启动时会去绑定控制端口，若已经有主控在跑就会弹一个错误框，
    # 那个框是模态的、会真的出现在屏幕上并卡住渲染。先探一下端口。
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    busy = probe.connect_ex(("127.0.0.1", TCP_CONTROL_PORT)) == 0
    probe.close()
    if busy:
        print("端口 %d 已被占用（可能有主控正在运行）。" % TCP_CONTROL_PORT)
        print("请先关掉它再渲染界面图，否则会弹出错误框打断渲染。")
        return 2

    # 别动用户真实的 agenda.json
    real_path = ag.agenda_path
    tmp_path = os.path.join(HERE, "_shot_agenda.json")
    ag.agenda_path = lambda: tmp_path

    _APP = QApplication(sys.argv)
    window = MasterWindow()

    # 把时钟钉在 13:35（正好落在第 2 项「硬件培训」里），这样截图上能同时看到
    # 大字剩余时间、状态行的当前项/下一项、表格里的"▶ 进行中"标记
    window._now = lambda: datetime(2026, 10, 16, 13, 35, 0)
    window.agenda = ag.Agenda([
        ag.AgendaItem(ag.parse_hhmm(a), ag.parse_hhmm(b), t, is_break=brk)
        for a, b, t, brk in ROWS
    ])
    window.chk_agenda.setChecked(True)
    window._agenda_tick()
    window._refresh_clock()
    window._refresh_agenda_table()
    window._append_log("（本图是离屏渲染的界面预览，不是真实运行截图）")

    # 关键：让 Qt 以为窗口已显示（布局才会算好），但不真的上屏
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.show()
    _APP.processEvents()

    # 填入示例客户端，让截图上能看到认领列表长什么样
    demo = [
        {"key": "d1", "id": "d1", "name": "301会议室", "ip": "192.168.0.31", "state": "running",
         "remaining": 1234, "online": True, "owner_id": window.master_id,
         "owner_name": window.master_name, "mine": True, "free": False},
        {"key": "d2", "id": "d2", "name": "302会议室", "ip": "192.168.0.32", "state": "running",
         "remaining": 1234, "online": True, "owner_id": window.master_id,
         "owner_name": window.master_name, "mine": True, "free": False},
        {"key": "d3", "id": "d3", "name": "401 季度汇报", "ip": "192.168.0.41", "state": "idle",
         "remaining": 0, "online": True, "owner_id": "another-master",
         "owner_name": "小王主控", "mine": False, "free": False},
        {"key": "d4", "id": "d4", "name": "402 待命", "ip": "192.168.0.42", "state": "idle",
         "remaining": 0, "online": True, "owner_id": "",
         "owner_name": "", "mine": False, "free": True},
    ]
    window._refresh_table(demo)
    _APP.processEvents()

    pixmap = window.grab()
    ok = pixmap.save(out)
    print("离屏渲染 %dx%d -> %s %s"
          % (pixmap.width(), pixmap.height(), out, "成功" if ok else "失败"))

    window.hide()
    window.close()
    ag.agenda_path = real_path
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
