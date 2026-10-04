# -*- coding: utf-8 -*-
"""aria2bridge 图标 v2: 渐变圆角方 + 白色下载箭头 + 托盘"""
from PIL import Image, ImageDraw

A = "C:/Users/29195/WorkBuddy/容器/aria2bridge"


def rounded_gradient(ss: int) -> Image.Image:
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    grad = Image.new("RGBA", (ss, ss))
    c1 = (109, 100, 245)   # indigo
    c2 = (41, 98, 255)     # blue
    px = grad.load()
    for y in range(ss):
        for x in range(ss):
            t = (x * 0.35 + y * 0.65) / (ss - 1)
            px[x, y] = (
                int(c1[0] + (c2[0] - c1[0]) * t),
                int(c1[1] + (c2[1] - c1[1]) * t),
                int(c1[2] + (c2[2] - c1[2]) * t),
                255,
            )
    mask = Image.new("L", (ss, ss), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, ss - 1, ss - 1], radius=int(ss * 0.225), fill=255)
    img.paste(grad, (0, 0), mask)
    return img, mask


def make_icon(size: int) -> Image.Image:
    ss = size * 4
    u = ss / 256.0
    img, mask = rounded_gradient(ss)
    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)

    cx = ss / 2
    # 箭头杆
    shaft_w = 34 * u
    top = 46 * u
    shaft_bottom = 122 * u
    d.rounded_rectangle(
        [cx - shaft_w / 2, top, cx + shaft_w / 2, shaft_bottom],
        radius=shaft_w / 2, fill=white,
    )
    # 箭头头（三角）
    head_w = 104 * u
    head_h = 56 * u
    d.polygon([
        (cx - head_w / 2, shaft_bottom - 6 * u),
        (cx + head_w / 2, shaft_bottom - 6 * u),
        (cx, shaft_bottom - 6 * u + head_h),
    ], fill=white)
    # 托盘（圆角横条）
    tray_w = 140 * u
    tray_h = 24 * u
    tray_y = 196 * u
    d.rounded_rectangle(
        [cx - tray_w / 2, tray_y, cx + tray_w / 2, tray_y + tray_h],
        radius=tray_h / 2, fill=white,
    )

    out = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out.resize((size, size), Image.LANCZOS)


icon256 = make_icon(256)
icon64 = make_icon(64)
icon256.save(A + "/ICON_256.PNG")
icon64.save(A + "/ICON.PNG")
icon256.save(A + "/app/ui/images/icon_256.png")
icon64.save(A + "/app/ui/images/icon_64.png")
print("icons v2 written")
