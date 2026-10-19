# -*- coding: utf-8 -*-
"""屏幕提示横幅：**完全透明背景 + 描边文字**。

当前形态（需求确认）
--------------------
* 横跨整个屏幕宽度、垂直居中、高度约 25% 屏高
* **没有底色块、没有边框线** —— 屏幕上只有文字本身
* 文字颜色 ``#27F2F5``（青），文字描边颜色 ``#F56C27``（橙）
* 文字太长时**自动缩小字号**，保证不会溢出屏幕

四种用途
--------
========  ====================  ==========  ========
kind      触发                  消失方式    鼠标
========  ====================  ==========  ========
node      自动跨过 10/5/3/1 分钟  5 秒自动    穿透
prompt    主控「手动提示」按钮    5 秒自动    穿透
custom    主控「自定义提示」按钮  5 秒自动    穿透
done      倒计时归零              人工关闭    仅文字可点
========  ====================  ==========  ========

为什么必须用 QPainterPath 而不是 drawText
----------------------------------------
``drawText`` 只能给字**填充**一个颜色，画不出"填充色 + 另一色描边"的效果。
描边要先把文字转成**矢量轮廓路径**（``QPainterPath.addText``），
然后让 QPainter 用同一个路径同时描边（pen）和填充（brush）。

去掉背景后必须处理的一个副作用
------------------------------
原来整条横幅有底色，用户清楚"哪一块能点"。背景去掉后，屏幕上那一条
（全宽 × 25% 屏高）变成了**完全看不见的区域**——如果它仍然接收鼠标，
用户在屏幕中间点击就会被凭空吃掉，而且不知道该点哪里才能关掉。
所以「时间到」横幅用 ``setMask`` 把可点击区域**收缩到文字本身**，
其余地方一律穿透（见 ``_apply_text_mask``）。

其它技术要点（都是踩过的坑）
----------------------------
1. ``Qt.WindowDoesNotAcceptFocus`` + ``WA_ShowWithoutActivating``
   —— 弹出时不会激活窗口，全屏放映的 PPT 不被打断。
2. **抢占式置顶**：PowerPoint 放映窗口自身也是 topmost，所以弹出后走 Win32
   ``SetWindowPos(HWND_TOPMOST, SWP_NOACTIVATE)`` 强制置顶，并每秒重刷一次。
   ``SWP_NOACTIVATE`` 保证这个操作本身不抢焦点。
3. **淡入淡出用 QTimer 手动推**，不用 Qt 动画框架 —— 后者依赖 QUnifiedTimer，
   实测在本机不推进（动画"跑完"值却不变），横幅会停在 alpha=0 看不见。
   而且 ``setWindowOpacity`` 与 ``WA_TranslucentBackground`` 冲突，也不能用。
"""
import ctypes
import sys

from PyQt5.QtCore import (
    QObject,
    QPointF,
    QRect,
    Qt,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
    QRegion,
)
from PyQt5.QtWidgets import QApplication, QWidget

from common.const import (
    BANNER_ACCENT,
    BANNER_ACCENT_DONE,
    BANNER_ACCENT_PROMPT,
    BANNER_AUTO_HIDE_MS,
    BANNER_BG,
    BANNER_BG_DONE,
    BANNER_BLINK_MS,
    BANNER_DONE_CLICK_TEXT_ONLY,
    BANNER_DONE_PERSIST,
    BANNER_DRAW_BACKGROUND,
    BANNER_DRAW_BORDER,
    BANNER_ESC_ENABLED,
    BANNER_HEIGHT_RATIO,
    BANNER_TEXT_COLOR,
    BANNER_TEXT_FONT_RATIO,
    BANNER_TEXT_MIN_PX,
    BANNER_TEXT_OUTLINE,
    BANNER_TEXT_OUTLINE_MIN_PX,
    BANNER_TEXT_OUTLINE_RATIO,
    BANNER_TEXT_WIDTH_RATIO,
)

_IS_WINDOWS = sys.platform.startswith("win")

#: 淡入淡出的推进步长（毫秒）。不用 Qt 动画框架，见模块 docstring。
_FADE_STEP_MS = 20

#: 仅当 BANNER_DRAW_BORDER=True 时才用到的装饰线颜色
_ACCENT = {
    "node": BANNER_ACCENT,
    "prompt": BANNER_ACCENT_PROMPT,
    "custom": BANNER_ACCENT_PROMPT,
    "done": BANNER_ACCENT_DONE,
}
#: 日志里的中文叫法
_KIND_NAMES = {"node": "节点", "prompt": "手动提示", "custom": "自定义提示", "done": "到点"}

# --------------------------------------------------------------- Win32 桥
if _IS_WINDOWS:
    from ctypes import wintypes

    _user32 = ctypes.windll.user32

    _HWND_TOPMOST = -1
    _SWP_NOSIZE = 0x0001
    _SWP_NOMOVE = 0x0002
    _SWP_NOACTIVATE = 0x0010
    _SWP_SHOWWINDOW = 0x0040
    _VK_ESCAPE = 0x1B

    # 必须显式声明 argtypes：
    # HWND 在 64 位下是 8 字节，HWND_TOPMOST = (HWND)-1 若按默认的
    # 32 位 int 传递会被截断成 0x00000000FFFFFFFF（非法句柄），
    # SetWindowPos 会静默失败——这是"置顶没生效"最常见的坑。
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint,
    ]
    _user32.SetWindowPos.restype = wintypes.BOOL
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short

    def force_topmost(hwnd):
        """把窗口钉到最顶层，且不激活它（不抢焦点）。"""
        try:
            return bool(_user32.SetWindowPos(
                wintypes.HWND(int(hwnd)),
                wintypes.HWND(_HWND_TOPMOST),
                0, 0, 0, 0,
                _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
            ))
        except Exception:
            return False

    def esc_pressed():
        """轮询全局 ESC 状态。

        横幅是不接受焦点的窗口，收不到键盘事件，所以只能用
        GetAsyncKeyState 直接问系统"ESC 现在按着吗"。
        """
        try:
            return bool(_user32.GetAsyncKeyState(_VK_ESCAPE) & 0x8000)
        except Exception:
            return False

else:  # 非 Windows：功能不受影响，只是不置顶 / 无 ESC 全局热键
    def force_topmost(hwnd):
        return True

    def esc_pressed():
        return False


# ------------------------------------------------------------------ 横幅
def _flags(kind):
    flags = (
        Qt.Tool
        | Qt.FramelessWindowHint
        | Qt.WindowStaysOnTopHint
        | Qt.WindowDoesNotAcceptFocus
    )
    if kind != "done":
        # 节点 / 手动提示 / 自定义提示：鼠标完全穿透，不干扰任何操作
        flags |= Qt.WindowTransparentForInput
    return flags


class Banner(QWidget):
    """一块横幅（每块屏幕一个实例）。"""

    clicked = pyqtSignal()

    def __init__(self, screen, kind, text, sub_text="", parent=None):
        super().__init__(None, _flags(kind))
        self._kind = kind
        self._text = text
        self._sub_text = sub_text
        self._blink_on = True
        self._screen = screen
        self._fade_value = 0.0
        self._fade_timer = None

        # 排版结果（在 _relayout 里算）
        self._main_path = None
        self._main_px = BANNER_TEXT_MIN_PX
        self._sub_path = None
        self._sub_px = BANNER_TEXT_MIN_PX

        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("MeetingTimer")

        self._blink_timer = None
        if kind == "done" and BANNER_BLINK_MS > 0:
            self._blink_timer = QTimer(self)
            self._blink_timer.setInterval(BANNER_BLINK_MS)
            self._blink_timer.timeout.connect(self._on_blink)

        self.reposition()

    # ---------------------------------------------------- 淡入淡出属性
    # 为什么不用 setWindowOpacity？
    #   WA_TranslucentBackground 在 Windows 下走分层窗口（WS_EX_LAYERED）
    #   路径，和 windowOpacity 冲突——两者一起用时 Qt 不保证生效，实测
    #   windowOpacity 会卡在 0，横幅直接看不见。
    #   所以淡入淡出用这个自定义属性驱动，由 paintEvent 乘进各自的 alpha。
    def _get_fade(self):
        return self._fade_value

    def _set_fade(self, value):
        self._fade_value = max(0.0, min(1.0, float(value)))
        self.update()

    fade = pyqtProperty(float, _get_fade, _set_fade)

    # ------------------------------------------------------------ 几何 / 排版
    def reposition(self):
        geo = self._screen.geometry()
        height = max(120, int(geo.height() * BANNER_HEIGHT_RATIO))
        top = geo.y() + (geo.height() - height) // 2
        self.setGeometry(QRect(geo.x(), top, geo.width(), height))
        self._relayout()

    def _relayout(self):
        """算好字号（自动缩字）、文字矢量路径、以及可点击区域。"""
        width = self.width()
        height = self.height()
        max_text_w = width * BANNER_TEXT_WIDTH_RATIO

        top = int(height * 0.10)
        if self._sub_text:
            main_h = int(height * 0.66)
            main_rect = QRect(0, top, width, main_h)
            sub_rect = QRect(0, top + main_h, width, max(1, height - top - main_h))
        else:
            main_rect = QRect(0, top, width, max(1, height - 2 * top))
            sub_rect = None

        # ---- 主文字 ----
        preferred = max(28, int(height * BANNER_TEXT_FONT_RATIO))
        self._main_px = self._fit_px(self._text, preferred, max_text_w)
        self._main_path = self._outlined_path(
            self._text, self._make_font(self._main_px), main_rect)

        # ---- 副文字（仅到点横幅的"怎么关"提示）----
        self._sub_path = None
        if self._sub_text and sub_rect is not None:
            preferred_sub = max(14, int(self._main_px * 0.30))
            self._sub_px = self._fit_px(self._sub_text, preferred_sub, max_text_w)
            self._sub_path = self._outlined_path(
                self._sub_text, self._make_font(self._sub_px), sub_rect)

        self._apply_text_mask()

    @staticmethod
    def _make_font(px):
        font = QFont()
        font.setFamily("Microsoft YaHei UI")
        font.setPixelSize(max(1, int(px)))
        font.setBold(True)
        return font

    @staticmethod
    def _text_width(text, font):
        metrics = QFontMetricsF(font)
        try:
            return metrics.horizontalAdvance(text)
        except AttributeError:          # 老版本 Qt 没有 horizontalAdvance
            return metrics.width(text)

    def _fit_px(self, text, preferred_px, max_width):
        """文字太长就逐步缩字号，保证不溢出屏幕。"""
        px = max(BANNER_TEXT_MIN_PX, int(preferred_px))
        while px > BANNER_TEXT_MIN_PX:
            if self._text_width(text, self._make_font(px)) <= max_width:
                break
            px -= 2
        return px

    def _outlined_path(self, text, font, center_rect):
        """把文字转成矢量轮廓路径，并平移到 center_rect 正中。

        用路径而不是 drawText 的原因：路径可以同时被 pen 描边 + brush 填充，
        这样才能做出"青色字 + 橙色描边"的效果。
        """
        path = QPainterPath()
        path.addText(QPointF(0.0, 0.0), font, text)
        bounds = path.boundingRect()
        dx = center_rect.center().x() - (bounds.left() + bounds.width() / 2.0)
        dy = center_rect.center().y() - (bounds.top() + bounds.height() / 2.0)
        path.translate(dx, dy)
        return path

    def _apply_text_mask(self):
        """把「时间到」横幅的可点击/可绘制区域收缩到文字本身。

        背景去掉之后，整条横幅（全宽 × 25% 屏高）是透明的。若不收缩，
        用户在屏幕中间点击会**被一个看不见的窗口吃掉**，而且不知道该点哪里
        才能关掉它。setMask 之后：只有文字那一小块能点，其余全部穿透。
        """
        if self._kind != "done" or not BANNER_DONE_CLICK_TEXT_ONLY:
            self.clearMask()
            return
        region = QRegion()
        for path in (self._main_path, self._sub_path):
            if path is None:
                continue
            rect = path.boundingRect().toAlignedRect().adjusted(-16, -16, 16, 16)
            region = region.united(QRegion(rect))
        if region.isEmpty():
            self.clearMask()
            return
        try:
            self.setMask(region)
        except Exception:
            self.clearMask()

    def text_bounds(self):
        """返回文字区域（widget 坐标），供测试/探针判断"哪里能点"。"""
        rect = QRect()
        for path in (self._main_path, self._sub_path):
            if path is not None:
                rect = rect.united(path.boundingRect().toAlignedRect())
        return rect

    # ------------------------------------------------------------ 显示
    def show_banner(self):
        self._set_fade(0.0)
        self.show()
        self.raise_()
        self._pin()
        if self._blink_timer:
            self._blink_timer.start()
        self._animate_fade(to=1.0, ms=160)

    def _pin(self):
        """强制置顶（不激活）。"""
        try:
            force_topmost(int(self.winId()))
        except (RuntimeError, TypeError, ValueError):
            pass

    def keep_on_top(self):
        """周期调用：对抗 PPT 放映窗口的 topmost 抢位。"""
        if self.isVisible():
            self._pin()

    def _animate_fade(self, to, ms, on_done=None):
        """用 QTimer 手动推 fade 值做淡入淡出。

        为什么不用 QPropertyAnimation / QVariantAnimation？
            两者都依赖 Qt 的动画驱动（QUnifiedTimer），在本机实测**完全不推进**
            （动画"跑完了"值却一点没变，横幅停在 alpha=0 直接看不见）。
            而 QTimer 是稳稳工作的（倒计时全部逻辑就靠它）。
            所以这里退化成最朴素、最可控的做法：定步长推进 + 缓动曲线自己算。
        """
        if self._fade_timer is not None:
            self._fade_timer.stop()

        start = float(self._fade_value)
        end = float(to)
        steps = max(1, int(ms / _FADE_STEP_MS))
        state = {"step": 0}

        timer = QTimer(self)
        timer.setInterval(_FADE_STEP_MS)

        def tick():
            state["step"] += 1
            t = min(1.0, state["step"] / float(steps))
            eased = t * t * (3.0 - 2.0 * t)        # smoothstep 缓动
            self._set_fade(start + (end - start) * eased)
            if t >= 1.0:
                timer.stop()
                if on_done is not None:
                    on_done()

        timer.timeout.connect(tick)
        timer.start()
        self._fade_timer = timer

    def fade_out_and_close(self, on_done=None):
        if self._blink_timer:
            self._blink_timer.stop()
        self._animate_fade(to=0.0, ms=320, on_done=lambda: self._finish(on_done))

    def _finish(self, on_done):
        self.hide()
        if on_done is not None:
            on_done()
        self.close()

    def _on_blink(self):
        self._blink_on = not self._blink_on
        self.update()

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        fade = self._fade_value

        # ---- 可选：底色块（当前关闭）----
        if BANNER_DRAW_BACKGROUND:
            base = BANNER_BG_DONE if self._kind == "done" else BANNER_BG
            painter.fillRect(
                self.rect(),
                QColor(base[0], base[1], base[2], int(base[3] * fade)))

        # ---- 可选：上下装饰线（当前关闭）----
        if BANNER_DRAW_BORDER:
            accent = QColor(_ACCENT.get(self._kind, BANNER_ACCENT))
            accent.setAlpha(int(accent.alpha() * fade))
            pen = QPen(accent)
            pen.setWidth(3)
            painter.setPen(pen)
            painter.drawLine(0, 1, self.width(), 1)
            painter.drawLine(0, self.height() - 2, self.width(), self.height() - 2)

        # ---- 描边文字 ----
        alpha = 255
        if self._kind == "done" and not self._blink_on:
            alpha = 90                              # 闪烁的暗态

        self._paint_outlined(painter, self._main_path, self._main_px, fade, alpha)
        if self._sub_path is not None:
            # 副文字稍微淡一点，跟主文字拉开层次
            self._paint_outlined(painter, self._sub_path, self._sub_px, fade, 190)

    @staticmethod
    def _paint_outlined(painter, path, font_px, fade, alpha):
        if path is None:
            return
        a = max(0, min(255, int(alpha * fade)))
        outline = QColor(BANNER_TEXT_OUTLINE)
        outline.setAlpha(a)
        fill = QColor(BANNER_TEXT_COLOR)
        fill.setAlpha(a)

        pen = QPen(outline)
        pen.setWidthF(max(BANNER_TEXT_OUTLINE_MIN_PX,
                          font_px * BANNER_TEXT_OUTLINE_RATIO))
        pen.setJoinStyle(Qt.RoundJoin)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(fill)
        # 同一个路径同时描边 + 填充 = 青色字 + 橙色描边
        painter.drawPath(path)

    # ------------------------------------------------------------ 交互
    def mousePressEvent(self, event):
        # 节点/手动/自定义横幅是鼠标穿透的，根本收不到这里；能进来的只有到点横幅
        if self._kind == "done" and event.button() == Qt.LeftButton:
            self.clicked.emit()
        event.accept()


class BannerManager(QObject):
    """横幅生命周期管理：多屏同步显示、自动消失、到点常驻待确认。"""

    #: 到点横幅被用户关掉了
    doneClosed = pyqtSignal()
    sigLog = pyqtSignal(str)

    def __init__(self, screens_mode="all", parent=None):
        super().__init__(parent)
        self._mode = screens_mode or "all"
        self._banners = []
        self._auto_timer = None
        self._keep_timer = None
        self._esc_timer = None
        self._kind = None
        self._hiding = False

    # -------------------------------------------------- 屏幕选择
    def _target_screens(self):
        app = QApplication.instance()
        if app is None:
            return []
        primary = app.primaryScreen()
        if self._mode == "primary":
            return [primary] if primary else []
        if self._mode == "cursor":
            from PyQt5.QtGui import QCursor

            screen = app.screenAt(QCursor.pos()) or primary
            return [screen] if screen else []
        screens = app.screens()
        return screens or ([primary] if primary else [])

    # -------------------------------------------------- 对外接口
    def show_node(self, text):
        """自动节点提示（10/5/3/1 分钟）：5 秒后自动消失，鼠标穿透。"""
        self._spawn("node", text, "")

    def show_prompt(self, text):
        """主控「手动提示」：文案由客户端按自己剩余时间算好传进来。"""
        self._spawn("prompt", text, "")

    def show_custom(self, text):
        """主控「自定义提示」：主控输入什么就显示什么。"""
        self._spawn("custom", text, "")

    def show_done(self, text, sub_text=""):
        """到点提示：常驻，等用户点击文字或按 ESC。"""
        self._spawn("done", text, sub_text)

    def hide_all(self):
        """立即收起所有横幅（停止/重置/加时恢复计时时调用）。"""
        self._stop_timers()
        banners, self._banners = self._banners, []
        self._kind = None
        for banner in banners:
            banner.fade_out_and_close()
        if banners:
            self.sigLog.emit("横幅已收起")

    # -------------------------------------------------- 内部
    def _spawn(self, kind, text, sub_text):
        self._stop_timers()
        for banner in self._banners:
            banner.fade_out_and_close()
        self._banners = []
        self._kind = kind

        screens = self._target_screens()
        for screen in screens:
            banner = Banner(screen, kind, text, sub_text)
            banner.clicked.connect(self._on_user_close)
            self._banners.append(banner)
            banner.show_banner()

        px = self._banners[0]._main_px if self._banners else 0
        self.sigLog.emit("弹出提示[%s]：%s（%d 块屏幕，字号 %dpx）"
                         % (_KIND_NAMES.get(kind, kind), text, len(self._banners), px))

        # 每秒重刷置顶，防止被全屏放映的幻灯片盖住
        self._keep_timer = QTimer(self)
        self._keep_timer.setInterval(1000)
        self._keep_timer.timeout.connect(self._keep_on_top)
        self._keep_timer.start()

        if kind != "done":
            # 节点 / 手动提示 / 自定义提示：5 秒后自动消失
            self._auto_timer = QTimer(self)
            self._auto_timer.setSingleShot(True)
            self._auto_timer.setInterval(BANNER_AUTO_HIDE_MS)
            self._auto_timer.timeout.connect(self.hide_all)
            self._auto_timer.start()
        else:
            if BANNER_DONE_PERSIST and BANNER_ESC_ENABLED and _IS_WINDOWS:
                self._esc_timer = QTimer(self)
                self._esc_timer.setInterval(150)
                self._esc_timer.timeout.connect(self._poll_esc)
                self._esc_timer.start()
            if not BANNER_DONE_PERSIST:
                self._auto_timer = QTimer(self)
                self._auto_timer.setSingleShot(True)
                self._auto_timer.setInterval(BANNER_AUTO_HIDE_MS)
                self._auto_timer.timeout.connect(self.hide_all)
                self._auto_timer.start()

    def _keep_on_top(self):
        for banner in self._banners:
            banner.keep_on_top()

    def _poll_esc(self):
        if esc_pressed():
            self.sigLog.emit("检测到 ESC，关闭到点提示")
            self._on_user_close()

    def _on_user_close(self):
        if self._hiding:
            return
        self._hiding = True
        try:
            was_done = self._kind == "done"
            self.sigLog.emit("用户关闭了%s横幅"
                             % _KIND_NAMES.get(self._kind, self._kind))
            self.hide_all()
            if was_done:
                self.doneClosed.emit()
        finally:
            self._hiding = False

    def _stop_timers(self):
        for timer in (self._auto_timer, self._keep_timer, self._esc_timer):
            if timer is not None:
                timer.stop()
        self._auto_timer = None
        self._keep_timer = None
        self._esc_timer = None
