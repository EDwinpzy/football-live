#!/usr/bin/env python3
# 生成足球直播品牌资源
# 输出（TV 与移动 Android 工程）：
#   res/drawable/my_tv_logo.png             启动屏字标（白字透明底，保留资源名兼容引用）
#   res/mipmap-xxxhdpi/ic_launcher_fg.png   自适应图标前景（足球 + 播放键 + 直播灯）
#   res/mipmap-xhdpi/ic_launcher_fg.png
#   res/drawable/ic_banner.png              TV 桌面 banner 640×360（图标 + 字标）
# 自适应图标 XML（ic_launcher.xml / ic_launcher_bg 颜色）为仓库内静态文件，无需改动。
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
RES_ROOTS = (
    ROOT / "android" / "app" / "src" / "main" / "res",
    ROOT / "internal" / "mobile" / "app" / "src" / "main" / "res",
)
FONT_DIR = Path(r"C:\Windows\Fonts")
FONT_FOOTBALL = FONT_DIR / "msyh.ttc"  # Microsoft YaHei，支持中文品牌字样
SIZE = 200
ICON_BG = (8, 21, 31, 255)
ICON_LIME = (91, 255, 153, 255)
ICON_INK = (8, 30, 42, 255)
ICON_BALL = (246, 250, 247, 255)
ICON_LIVE = (255, 75, 82, 255)


def make_wordmark() -> Image.Image:
    """白字透明底「足球直播」字标。"""
    font = ImageFont.truetype(str(FONT_FOOTBALL), SIZE)
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    bbox = probe.textbbox((0, 0), "足球直播", font=font)
    pad = 16
    canvas = Image.new(
        "RGBA",
        (bbox[2] - bbox[0] + pad * 2, bbox[3] - bbox[1] + pad * 2),
        (255, 255, 255, 0),
    )
    ImageDraw.Draw(canvas).text(
        (pad - bbox[0], pad - bbox[1]), "足球直播", font=font, fill=(255, 255, 255, 255)
    )
    return canvas


def whiten(img: Image.Image) -> Image.Image:
    """保留 alpha，把所有颜色改纯白（图标前景/深底场景统一白标）"""
    a = img.getchannel("A")
    white = Image.new("RGBA", img.size, (255, 255, 255, 0))
    white.putalpha(a)
    return white


def make_icon_foreground(size: int = 432) -> Image.Image:
    """足球轮廓与播放键，位于自适应图标安全区。"""
    scale = 4
    canvas = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    def u(value: float) -> int:
        return round(value * size * scale)

    def points(coords):
        return [(u(x), u(y)) for x, y in coords]

    # A clean ball silhouette remains recognizable at launcher size.
    draw.ellipse((u(.225), u(.225), u(.775), u(.775)), fill=ICON_BALL)
    draw.arc((u(.225), u(.225), u(.775), u(.775)), 145, 330,
             fill=ICON_LIME, width=u(.018))
    draw.polygon(points([(.285, .335), (.37, .275), (.405, .34), (.35, .405), (.285, .39)]), fill=ICON_INK)
    draw.polygon(points([(.59, .27), (.68, .31), (.72, .38), (.65, .40), (.60, .34)]), fill=ICON_INK)
    draw.polygon(points([(.255, .52), (.32, .485), (.37, .55), (.34, .625), (.28, .625)]), fill=ICON_INK)
    draw.polygon(points([(.64, .49), (.73, .48), (.74, .55), (.70, .62), (.65, .61)]), fill=ICON_INK)
    draw.polygon(points([(.41, .69), (.46, .64), (.54, .64), (.59, .69), (.55, .76), (.45, .76)]), fill=ICON_INK)
    # The central play button communicates live video without tiny text.
    draw.ellipse((u(.365), u(.365), u(.635), u(.635)), fill=ICON_INK)
    draw.polygon(points([(.465, .425), (.465, .575), (.575, .50)]), fill=ICON_LIME)
    # On-air indicator remains legible without relying on typography.
    draw.ellipse((u(.69), u(.245), u(.77), u(.325)), fill=ICON_BG)
    draw.ellipse((u(.702), u(.257), u(.758), u(.313)), fill=ICON_LIVE)
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def make_banner(logo: Image.Image, w: int = 640, h: int = 360) -> Image.Image:
    """TV banner：深色底、足球直播图标与品牌字标。"""
    canvas = Image.new("RGBA", (w, h), ICON_BG)
    icon = make_icon_foreground(260)
    canvas.alpha_composite(icon, (36, (h - icon.height) // 2))
    wl = whiten(logo)
    target_w = int(w * 0.46)
    fg = wl.resize((target_w, int(target_w * wl.height / wl.width)), Image.LANCZOS)
    canvas.alpha_composite(fg, (w - fg.width - 34, (h - fg.height) // 2))
    return canvas


logo = make_wordmark()
for res in RES_ROOTS:
    (res / "drawable").mkdir(parents=True, exist_ok=True)
    logo.save(res / "drawable" / "my_tv_logo.png")
    for density, size in (("mipmap-xxxhdpi", 432), ("mipmap-xhdpi", 216)):
        (res / density).mkdir(parents=True, exist_ok=True)
        make_icon_foreground(size).save(res / density / "ic_launcher_fg.png")
    make_banner(logo).save(res / "drawable" / "ic_banner.png")
print(f"足球直播足球播放图标、启动屏字标和 TV banner 已生成到 {len(RES_ROOTS)} 个 Android 工程")
