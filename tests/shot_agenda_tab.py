# -*- coding: utf-8 -*-
"""离屏渲染主控界面的「会议议程」页，确认提醒节点已移出。"""
import os
import sys

sys.path.insert(0, r"C:\Users\bd7iw\Downloads\timer-multi")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

APP = QApplication(sys.argv)

from master.ui_master import MasterWindow

out = sys.argv[1] if len(sys.argv) > 1 else "tests/_ui_agenda.png"

w = MasterWindow()
w.setAttribute(Qt.WA_DontShowOnScreen, True)
w.show()
APP.processEvents()
w.tabs.setCurrentIndex(1)
APP.processEvents()
pix = w.grab()
os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
ok = pix.save(out, "PNG")
print("渲染 %dx%d -> %s  %s" % (pix.width(), pix.height(), out,
                               "成功" if ok else "失败"))
w.close()
