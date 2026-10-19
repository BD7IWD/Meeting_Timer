# -*- coding: utf-8 -*-
"""会议倒计时提醒系统 —— 源码直跑入口（客户端）。

    python run_client.py            静默后台启动
    python run_client.py --console  同时把日志打到控制台（本机联调很有用）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
