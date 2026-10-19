# -*- coding: utf-8 -*-
"""横幅几何断言 + 真实抓图（不依赖任何"看图"能力，全部用数字和文件说话）。

    python tests/banner_shot.py node  out_node.png
    python tests/banner_shot.py done  out_done.png

做三件事：
  1. 断言横幅宽度 == 屏幕宽度、高度 ≈ 25% 屏高、垂直居中；
  2. 断言置顶 / 不抢焦点 / （节点横幅）鼠标穿透 的窗口属性真的生效；
  3. 抓真屏并裁剪出横幅区域存 PNG，用数字证明它长什么样。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QCoreApplication, QEventLoop, QTimer, Qt  # noqa: E402
from PyQt5.QtGui import QRegion  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from client.banner import BannerManager  # noqa: E402
from common.const import (  # noqa: E402
    BANNER_DONE_SUB_TEXT,
    BANNER_DONE_TEXT,
    BANNER_DRAW_BACKGROUND,
    BANNER_DRAW_BORDER,
    BANNER_HEIGHT_RATIO,
    BANNER_TEXT_COLOR,
    BANNER_TEXT_OUTLINE,
    BANNER_TEXT_OUTLINE_MIN_PX,
    BANNER_TEXT_OUTLINE_RATIO,
)

_APP = None


def pump(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def wait_for(cond, timeout_ms=6000, poll_ms=50):
    """轮询等待条件成立。

    为什么不固定 sleep：本机实测 Qt 事件循环启动后头 ~1.1 秒会被"首次绘制
    大号中文字体"阻塞（CJK 字体初次加载），计时器集体饿死，之后才瞬间追上。
    固定等待会误判成"淡入没生效"，必须轮询。
    """
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


def main():
    global _APP
    # --text "要显示的文字"（仅 custom 模式用）先摘出来，免得被当成模式/输出路径
    argv = sys.argv[1:]
    custom_text = None
    if "--text" in argv:
        idx = argv.index("--text")
        custom_text = argv[idx + 1] if idx + 1 < len(argv) else None
        del argv[idx:idx + 2]

    args = [a for a in argv if not a.startswith("--")]
    mode = args[0] if args else "done"
    out = args[1] if len(args) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "banner_%s.png" % mode)

    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    _APP = QApplication(sys.argv)

    screen = _APP.primaryScreen()
    geo = screen.geometry()
    print("=" * 66)
    print("屏幕几何: %dx%d @ (%d,%d)  DPR=%.2f"
          % (geo.width(), geo.height(), geo.x(), geo.y(), screen.devicePixelRatio()))

    manager = BannerManager("all")
    manager.sigLog.connect(lambda t: print("  [banner] " + t))

    t_before = time.time()
    if mode == "node":
        manager.show_node("倒计时 10 分钟")
    elif mode == "prompt":
        manager.show_prompt("剩余 12 分钟")
    elif mode == "custom":
        manager.show_custom(custom_text or "请关闭手机，保持安静")
    else:
        manager.show_done(BANNER_DONE_TEXT, BANNER_DONE_SUB_TEXT)

    # 自动消失的计时器是在 _spawn 末尾（构造 + show 完成之后）才启动的，
    # 所以基准要取在这里。否则会把"构造 Banner"的时间算进去——首次构造要
    # 预热 CJK 字体（约 1 秒），看起来会像"自动消失变慢了一秒"。
    t_armed = time.time()

    banners = manager._banners
    faded = wait_for(lambda: banners[0].fade > 0.95, timeout_ms=6000)
    print("-" * 66)
    print("横幅实例数：%d" % len(banners))
    if not faded:
        print("警告：等待淡入超时，当前 fade=%.3f" % banners[0].fade)
    rect = banners[0].geometry()

    expect_h = max(120, int(geo.height() * BANNER_HEIGHT_RATIO))
    expect_y = geo.y() + (geo.height() - expect_h) // 2
    print("横幅几何: %dx%d @ (%d,%d)" % (rect.width(), rect.height(), rect.x(), rect.y()))
    print("自适应字号: %dpx   文字区域: %s"
          % (banners[0]._main_px, banners[0].text_bounds()))

    # 描边粗细：按字号比例算，再被下限兜底
    outline_px = max(BANNER_TEXT_OUTLINE_MIN_PX,
                     banners[0]._main_px * BANNER_TEXT_OUTLINE_RATIO)
    old_px = max(2.0, banners[0]._main_px * 0.062)      # 改动前的算法
    print("描边宽度: %.2f px（字号 %dpx × 比例 %.4f）"
          % (outline_px, banners[0]._main_px, BANNER_TEXT_OUTLINE_RATIO))
    print("  改动前同字号下是 %.2f px -> 现在为 %.2f 倍（目标 1/3 ≈ 0.33）"
          % (old_px, outline_px / old_px))

    checks = [
        ("宽度横跨整个屏幕宽", rect.width() == geo.width(),
         "%d vs %d" % (rect.width(), geo.width())),
        ("高度 ≈ 25%% 屏高", abs(rect.height() - expect_h) <= 1,
         "%d vs %d" % (rect.height(), expect_h)),
        ("垂直居中", abs(rect.y() - expect_y) <= 1, "%d vs %d" % (rect.y(), expect_y)),
        ("水平起点与屏幕对齐", rect.x() == geo.x(), "%d vs %d" % (rect.x(), geo.x())),
        ("已置顶 (WindowStaysOnTopHint)",
         bool(banners[0].windowFlags() & Qt.WindowStaysOnTopHint), ""),
        ("不抢焦点 (WindowDoesNotAcceptFocus)",
         bool(banners[0].windowFlags() & Qt.WindowDoesNotAcceptFocus), ""),
        ("不激活显示 (WA_ShowWithoutActivating)",
         banners[0].testAttribute(Qt.WA_ShowWithoutActivating), ""),
        ("淡入完成（自绘 alpha 属性已到 1.0）",
         banners[0].fade > 0.95, "%.2f" % banners[0].fade),
        ("无底色块（屏幕上看不到色块，只有文字）",
         BANNER_DRAW_BACKGROUND is False, ""),
        ("无边框线", BANNER_DRAW_BORDER is False, ""),
        ("文字色 #27F2F5 / 描边色 #F56C27",
         BANNER_TEXT_COLOR.upper() == "#27F2F5"
         and BANNER_TEXT_OUTLINE.upper() == "#F56C27",
         "%s / %s" % (BANNER_TEXT_COLOR, BANNER_TEXT_OUTLINE)),
        ("文字已转成矢量路径（才能描边）",
         banners[0]._main_path is not None and not banners[0]._main_path.isEmpty(), ""),
        ("文字未溢出屏幕", banners[0].text_bounds().width() <= rect.width(),
         "%d <= %d" % (banners[0].text_bounds().width(), rect.width())),
        ("描边已压缩到原来的 1/3 左右",
         abs(outline_px / old_px - 1.0 / 3.0) < 0.06,
         "%.2f 倍" % (outline_px / old_px)),
        ("结束语 = 「时间到」", BANNER_DONE_TEXT == "时间到", BANNER_DONE_TEXT),
        ("节点/手动/自定义横幅鼠标穿透 (WindowTransparentForInput)" if mode != "done"
         else "到点横幅接收鼠标（不穿透）",
         (not bool(banners[0].windowFlags() & Qt.WindowTransparentForInput))
         == (mode == "done"),
         ""),
    ]
    failed = 0
    for name, ok, extra in checks:
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
        if not ok:
            failed += 1

    # ---- 抓真屏并裁剪 ----
    pixmap = screen.grabWindow(0)
    scale = pixmap.width() / float(geo.width()) if geo.width() else 1.0
    crop = QRegion(int(rect.x() * scale), int(rect.y() * scale),
                   int(rect.width() * scale), int(rect.height() * scale)).boundingRect()
    shot = pixmap.copy(crop)
    ok_save = shot.save(out, "PNG")
    print("-" * 66)
    print("抓屏: 全屏 %dx%d (scale=%.3f) -> 裁剪 %dx%d"
          % (pixmap.width(), pixmap.height(), scale, shot.width(), shot.height()))
    print("已保存: %s (%s)" % (out, "OK" if ok_save else "失败"))

    # ---- 实测自动消失耗时（不只看常量，看真实行为）----
    if mode != "done":
        gone = wait_for(lambda: not manager._banners, timeout_ms=9000)
        stamp = time.time()
        armed_ms = (stamp - t_armed) * 1000.0
        total_ms = (stamp - t_before) * 1000.0
        print("自动消失: %s" % ("是" if gone else "否"))
        print("  计时器启动后实测 %.0f ms（期望约 5000）" % armed_ms)
        print("  含构造/字体预热的全程 %.0f ms（首次会多约 1 秒，属正常）" % total_ms)
        auto_ok = gone and 4800 <= armed_ms <= 6000
        print("  [%s] 自动消失时间符合 5 秒设置" % ("PASS" if auto_ok else "FAIL"))
        if not auto_ok:
            failed += 1
    else:
        print("到点横幅：常驻不自动消失（需人工关闭）")

    manager.hide_all()
    pump(200)
    print("=" * 66)
    print("几何/属性检查失败项：%d" % failed)
    QTimer.singleShot(0, _APP.quit)
    _APP.exec_()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
