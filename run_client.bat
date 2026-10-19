@echo off
chcp 65001 >nul
rem 客户端 —— 源码直跑（开发/调试用，会显示一个控制台窗口看日志）
cd /d "%~dp0"
python run_client.py --console
if errorlevel 1 pause
