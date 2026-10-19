@echo off
chcp 65001 >nul
rem 主控端 —— 源码直跑（开发/调试用，会显示一个控制台窗口看日志）
cd /d "%~dp0"
python run_master.py --console
if errorlevel 1 pause
