# -*- coding: utf-8 -*-
"""会议倒计时提醒系统 —— 源码直跑入口（主控端）。

    python run_master.py            正常启动
    python run_master.py --console  同时把日志打到控制台，方便排障
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from master.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
