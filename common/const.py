# -*- coding: utf-8 -*-
"""会议倒计时提醒系统 —— 公共常量与路径工具。

所有可调参数集中在这里，改行为只需要动这个文件。
"""
import os
import sys

# ---------------------------------------------------------------- 应用标识
# 多主控版用**独立的数据目录**，否则会和单主控版共用
# %LOCALAPPDATA%\MeetingTimer 下的 agenda.json / 日志，互相覆盖。
APP_NAME = "MeetingTimerMulti"
APP_TITLE = "会议倒计时"
VERSION = "3.0.0"

#: 作者署名。**只在这一处定义**，主控状态栏 / 客户端托盘 / 启动日志 /
#: exe 文件属性（build\version_*.txt）都引用这一份，改名不用满工程找。
AUTHOR = "野怼怼"
AUTHOR_URL = "https://www.bd7iwd.com"
AUTHOR_LINE = "作者：%s；%s" % (AUTHOR, AUTHOR_URL)

# ---------------------------------------------------------------- 网络参数
#: 帧魔数，用于粘包/乱流时的重新同步
PROTO_MAGIC = b"MTMR"
#: 单帧最大长度，防御性上限（超过即判定为垃圾数据）
MAX_FRAME_SIZE = 64 * 1024

#: UDP 自动发现端口（客户端广播 DISCOVER，主控单播 HERE 应答）
#: TCP 控制通道端口（主控 listen，客户端 connect）
#:
#: ⚠️ 两个端口都**可以用环境变量覆盖**，这样"端口撞了"不用改代码再重新打包 exe：
#:
#:     set MEETINGTIMER_TCP_PORT=48821 && set MEETINGTIMER_UDP_PORT=48820 && MeetingTimerMaster.exe
#:
#: 主控和客户端要设成同一组值。顺带好处：自动化测试能在另一套端口上跑，
#: 不会和"正在运行的主控"抢 47820/47821。
# ---------------------------------------------------------------- 端口组
# 为什么不沿用固定端口：多主控版本允许**同一台电脑同时开多个主控**
# （自己测试、或者一个窗口管 301 另一个窗口管 302）。一个端口只能被绑一次，
# 所以每个主控实例占一组：
#
#     第 i 个主控：UDP = BASE + i*10      （发现）
#                  TCP = BASE + i*10 + 1  （控制）
#
# 客户端会挨个探测这几组 UDP 端口，把**所有**应答的主控都收下来。
PORT_BASE = int(os.environ.get("MEETINGTIMER_PORT_BASE") or 47820)
PORT_GROUP_STEP = 10
PORT_GROUP_COUNT = 6


def port_group(index):
    """第 index 个主控实例使用的 (udp_port, tcp_port)。"""
    base = PORT_BASE + int(index) * PORT_GROUP_STEP
    return base, base + 1


def all_udp_ports():
    """客户端探测用的全部 UDP 端口。"""
    return [port_group(i)[0] for i in range(PORT_GROUP_COUNT)]


#: 兼容单主控版本的常量（= 第 1 组）。环境变量 MEETINGTIMER_UDP/TCP_PORT 仍然有效。
UDP_DISCOVERY_PORT = int(os.environ.get("MEETINGTIMER_UDP_PORT") or port_group(0)[0])
TCP_CONTROL_PORT = int(os.environ.get("MEETINGTIMER_TCP_PORT") or port_group(0)[1])

#: 客户端连续广播 DISCOVER 的间隔
DISCOVERY_INTERVAL_MS = 3000
#: 一轮广播后等待 HERE 应答的时间
DISCOVERY_REPLY_WAIT_MS = 1400
#: 心跳间隔
HEARTBEAT_INTERVAL_MS = 2000
#: 主控超过该时长收不到心跳即判定客户端离线
HEARTBEAT_TIMEOUT_MS = 6000
#: 客户端名称最大长度（弹窗输入限制）
CLIENT_NAME_MAX = 40
#: 客户端判定"主控已死"的阈值：客户端 2 秒一次心跳，15 秒收不到占用者的
#: 任何应答就认为它崩了/断网了，自动解除占用（避免"这机器被谁锁住了"的幽灵占用）。
CLAIM_TIMEOUT_MS = 15000
#: TCP 断开后的重连间隔
RECONNECT_DELAY_MS = 2000
#: 自动发现与手填 IP 都失败后的轮询间隔
FALLBACK_RETRY_MS = 5000

# ---------------------------------------------------------------- 报文类型
MSG_DISCOVER = "DISCOVER"      # C -> 广播        探测主控
MSG_HERE = "HERE"              # M -> C 单播      我在这里（携带 tcp_port）
MSG_REGISTER = "REGISTER"      # C -> M          注册（主机名/IP/MAC/版本）
MSG_START = "START"            # M -> C          开始倒计时（总秒数 + 绝对目标时间戳）
MSG_ADD_TIME = "ADD_TIME"      # M -> C          加时
MSG_PROMPT = "PROMPT"          # M -> C          手动提示（客户端自己算剩余时间）
MSG_CUSTOM = "CUSTOM"          # M -> C          自定义文字提示（主控输入什么就显示什么）
MSG_STOP = "STOP"              # M -> C          停止
MSG_RESET = "RESET"            # M -> C          重置
MSG_HEARTBEAT = "HEARTBEAT"    # C -> M          心跳（携带状态与剩余秒数）
MSG_ACK = "ACK"                # C -> M          关键指令确认
MSG_BYE = "BYE"                # C -> M          主动下线
MSG_CLAIM = "CLAIM"            # M -> C          认领（独占这台客户端）
MSG_CLAIM_RESULT = "CLAIM_RESULT"  # C -> M      认领结果 ok / reason / owner
MSG_RELEASE = "RELEASE"        # M -> C          释放
MSG_CLAIM_STATE = "CLAIM_STATE"    # C -> 所有 M  占用状态变化广播（让别人列表立刻变灰）
MSG_SET_NAME = "SET_NAME"      # C -> M          改名后重新同步
MSG_HELLO = "HELLO"            # M -> C          受理连接时自报身份（master_id / master_name）

#: 客户端连上后等主控自报身份的时限；超时就按老流程直接注册（兼容旧主控）
HELLO_WAIT_MS = 2000

# ---------------------------------------------------------------- 倒计时逻辑
#: 默认提醒节点（分钟）。**只是默认值**——主控端可以改，改完随 START 报文
#: 一起下发给客户端，客户端用收到的节点而不是这里的常量（主控是唯一真相）。
DEFAULT_NODE_MINUTES = (10, 5, 3, 1)
#: 兼容旧名字（早期代码按固定节点写死）
NODE_MINUTES = DEFAULT_NODE_MINUTES
#: 节点个数上限（防御：避免主控误输入上百个节点把客户端弹爆）
NODE_MAX_COUNT = 12

#: UI 刷新节拍
TICK_MS = 200
#: 提示横幅自动消失时间（节点提示 / 手动提示 / 自定义提示共用）
BANNER_AUTO_HIDE_MS = 5000
#: 加时后，若剩余时间"重新超过"某节点阈值，该节点是否重新变为可弹
#:   True  = 会再提醒一次（默认，符合"会议顺延后重新提醒"的直觉）
#:   False = 本轮内已弹过的节点不再弹（避免重复打扰）
ADD_TIME_RESETS_NODE_FLAG = True

MIN_MINUTES = 1
MAX_MINUTES = 180
DEFAULT_MINUTES = 30

# ---------------------------------------------------------------- 横幅外观
#: 横幅高度占屏幕高度的比例（需求确认：约 25%）
BANNER_HEIGHT_RATIO = 0.25

#: 文字颜色（青）
BANNER_TEXT_COLOR = "#27F2F5"
#: 文字描边颜色（橙）
BANNER_TEXT_OUTLINE = "#F56C27"
#: 描边粗细 = 字号(px) * 该比例
#:   原先 0.062 觉得太粗，按需求压缩到 1/3 -> 约 0.0207
BANNER_TEXT_OUTLINE_RATIO = 0.0207
#: 描边最小粗细（px）。小字号时兜底，保证描边仍然看得见。
#: 注意别设太大，否则会把"按比例算出来的细描边"顶回去（下限会盖过比例）。
BANNER_TEXT_OUTLINE_MIN_PX = 1.0
#: 主文字"首选"最大字号 = 横幅高度 * 该比例
BANNER_TEXT_FONT_RATIO = 0.40
#: 文字太长时自动缩字的下限（px）
BANNER_TEXT_MIN_PX = 22
#: 文字可用宽度 = 横幅宽度 * 该比例
BANNER_TEXT_WIDTH_RATIO = 0.96

#: 是否画半透明底色块 / 上下装饰线。
#:   False（当前需求）= **完全透明**，屏幕上只留描边文字，最不遮挡放映内容
#:   True            = 恢复"色块 + 上下装饰线"的旧样式（下面几个颜色才生效）
BANNER_DRAW_BACKGROUND = False
BANNER_DRAW_BORDER = False
#: 仅当 BANNER_DRAW_BACKGROUND=True 时生效
BANNER_BG = (18, 18, 18, 216)
BANNER_BG_DONE = (168, 28, 28, 226)
#: 仅当 BANNER_DRAW_BORDER=True 时生效：节点=蓝、手动提示=琥珀、到点=黄
BANNER_ACCENT = "#4FC3F7"
BANNER_ACCENT_PROMPT = "#FFB300"
BANNER_ACCENT_DONE = "#FFD54F"

#: 到点横幅文字闪烁周期（0 = 不闪）
BANNER_BLINK_MS = 700
#: 到点横幅是否常驻（需人工关闭）
BANNER_DONE_PERSIST = True
#: 倒计时归零时的横幅主文字（"结束语"）。
#: 主控状态标签、托盘提示也都引用它，保证同一件事全程序说法一致。
BANNER_DONE_TEXT = "时间到"
#: 归零横幅的第二行：操作提示（小字）。
#: 因为背景去掉后只有文字能点，所以这里说"点击文字"而不是"点击横幅"。
#: 设成 "" 就只显示主文字。
BANNER_DONE_SUB_TEXT = "点击文字或按 ESC 关闭"
#: 是否启用"按 ESC 关闭常驻横幅"（无焦点窗口用 GetAsyncKeyState 轮询实现）
BANNER_ESC_ENABLED = True
#: 到点横幅是否把"可点击区域"收缩到文字本身。
#:   背景去掉之后，整条横幅变成透明的；若不收缩，屏幕中间那一整条（全宽 × 25% 高）
#:   会变成一个**看不见的、会吃掉鼠标点击**的区域。收缩后只有文字能点，其余穿透。
BANNER_DONE_CLICK_TEXT_ONLY = True

#: 横幅显示在哪块屏幕：all / primary / cursor
#:   all     = 每块屏幕都显示（会议室多屏/投影场景最稳，默认）
#:   primary = 只在主屏
#:   cursor  = 只在鼠标所在屏
BANNER_SCREENS_DEFAULT = "all"

#: 主控「自定义提示」单条最大字数（超出截断，防止文字缩到看不清）
CUSTOM_TEXT_MAX = 100

# ---------------------------------------------------------------- 会议议程
#: 议程与主控设置的持久化文件名
AGENDA_FILENAME = "agenda.json"
#: 议程调度节拍：每秒钟对一次系统时钟，判断"当前该跑哪一项"
AGENDA_TICK_MS = 1000
#: 议程模式下，离结束还剩这么多秒以内就不再重发 START（避免重复下发）
AGENDA_RESEND_GUARD_S = 3

# ---------------------------------------------------------------- 路径工具
def app_base_dir():
    """返回程序基准目录：打包后是 exe 所在目录，否则是工程根目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    # common/const.py -> 上一级即工程根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_data_dir():
    """返回可写数据目录（日志、配置）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path
