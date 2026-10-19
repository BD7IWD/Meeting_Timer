# -*- coding: utf-8 -*-
r"""把「白底方图」里的圆形图案抠成透明背景的多尺寸 .ico。

用法：
    python tests\make_icons.py                       # 用默认素材重生成两端图标
    python tests\make_icons.py 客户.png client.ico   # 指定源图/输出
    python tests\make_icons.py --tol 20              # 白底判定阈值调松

为什么需要它：设计给的图通常是**白底正方形**，直接转 ico 会带一圈白方块，
在任务栏 / 资源管理器里很难看。这个脚本按圆的外接框做抗锯齿圆形遮罩，
把圆外全部置为透明。
"""
import argparse
import os
import sys

from PIL import Image, ImageChops, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 默认素材目录（放 客户.png / 主控.png）；也可以命令行直接指定源图
DEFAULT_SRC = os.path.join(ROOT, "assets", "icons-src")
SIZES = [16, 24, 32, 48, 64, 128, 256]


def content_bbox(img, tol):
    """找非纯白内容的外接框。"""
    rgb = img.convert("RGB")
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, (255, 255, 255)))
    mask = diff.convert("L").point(lambda v: 255 if v > tol else 0)
    return mask.getbbox()


def circle_transparent(img, tol=10, pad_ratio=0.02, shrink=0):
    """按圆外接框做遮罩，圆外透明。"""
    bbox = content_bbox(img, tol)
    if not bbox:
        raise RuntimeError("整张图都是白的，没找到内容")
    l, t, r, b = bbox
    cw, ch = r - l, b - t
    side = max(cw, ch)
    cx, cy = (l + r) // 2, (t + b) // 2
    left, top = cx - side // 2, cy - side // 2
    box = (left, top, left + side, top + side)

    # 超采样画圆 -> 边缘抗锯齿，不会出现锯齿/白圈
    ss = 4
    m = Image.new("L", (side * ss, side * ss), 0)
    inset = int(shrink * ss)
    ImageDraw.Draw(m).ellipse(
        [inset, inset, side * ss - 1 - inset, side * ss - 1 - inset], fill=255)
    mask = m.resize((side, side), Image.LANCZOS)

    circle = img.convert("RGBA").crop(box)
    circle.putalpha(mask)

    pad = int(round(side * pad_ratio))
    canvas = Image.new("RGBA", (side + pad * 2, side + pad * 2), (0, 0, 0, 0))
    canvas.paste(circle, (pad, pad), circle)
    print("  bbox=%r side=%d pad=%d -> %dx%d" % (bbox, side, pad,
                                                 canvas.width, canvas.height))
    return canvas


def convert(src, dst, tol=10, pad_ratio=0.02, shrink=0):
    img = Image.open(src)
    icon = circle_transparent(img, tol=tol, pad_ratio=pad_ratio, shrink=shrink)
    icon.save(dst, format="ICO", sizes=[(s, s) for s in SIZES])
    print("  -> %s (%d 字节, %d 层)" % (dst, os.path.getsize(dst), len(SIZES)))
    return icon


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", help="源 PNG（默认批量处理两端）")
    ap.add_argument("dst", nargs="?", help="输出 .ico")
    ap.add_argument("--tol", type=int, default=10, help="白底阈值，默认 10")
    ap.add_argument("--pad", type=float, default=0.02, help="圆外留白比例，默认 0.02")
    ap.add_argument("--shrink", type=int, default=0, help="遮罩内缩像素（去白边用）")
    a = ap.parse_args(argv)

    if a.src and a.dst:
        print("处理 %s" % a.src)
        convert(a.src, a.dst, a.tol, a.pad, a.shrink)
        return 0

    # 默认：两端图标一起重生成
    pairs = [("客户.png", os.path.join(ROOT, "assets", "client.ico")),
             ("主控.png", os.path.join(ROOT, "assets", "master.ico"))]
    for name, dst in pairs:
        src = os.path.join(DEFAULT_SRC, name)
        if not os.path.exists(src):
            print("跳过（源图不存在）: %s" % src)
            continue
        print("处理 %s" % src)
        convert(src, dst, a.tol, a.pad, a.shrink)
    print("\n完成。重新打包 exe 后记得清一次 Windows 图标缓存：")
    print("  taskkill /f /im explorer.exe")
    print(r"  del /a %LOCALAPPDATA%\IconCache.db")
    print(r"  del /a %LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache*.db")
    print("  start explorer.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())