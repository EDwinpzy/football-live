#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从“球迹直播”品牌源图生成 Android TV 与移动端资源。

源图固定为 tools/assets/qiuji-live-app-icon-1024.png，并通过 SHA-256 校验，
避免资源在无意间被替换后仍生成出不可追溯的图标。

输出（两个 Android 工程）：
  mipmap-xxxhdpi/ic_launcher_fg.png  432x432 自适应图标前景
  mipmap-xhdpi/ic_launcher_fg.png    216x216 自适应图标前景
  drawable/my_tv_logo.png            透明底白色“球迹直播”启动字标
  drawable/ic_banner.png             640x360 TV banner（左侧图标、右侧品牌名）
"""
from __future__ import annotations

import hashlib
import os
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parent.parent
SOURCE_ICON = ROOT / "tools" / "assets" / "qiuji-live-app-icon-1024.png"
SOURCE_SHA256 = "8e50a882a7189006f5c8b85db77e6bbccc692f5249086a3b8d5da74795af818d"
BRAND_NAME = "球迹直播"

RES_ROOTS = (
    ROOT / "android" / "app" / "src" / "main" / "res",
    ROOT / "internal" / "mobile" / "app" / "src" / "main" / "res",
)

ICON_BG = (8, 38, 26, 255)  # 与 values/colors.xml 的 #08261A 保持一致
ICON_BG_TOP = (7, 31, 20, 255)
ICON_BG_BOTTOM = (10, 57, 30, 255)
RIM = (96, 245, 125, 255)
WORDMARK = (255, 255, 255, 255)

# 源图中央球场圆环与足球/播放键完整保留，只裁掉最外层草皮边缘。
SOURCE_CROP = (160, 128, 864, 832)
ICON_SAFE_RATIO = 0.64
SUPERSAMPLE = 4

OUTPUT_SIZES = {
    "mipmap-xxxhdpi/ic_launcher_fg.png": (432, 432),
    "mipmap-xhdpi/ic_launcher_fg.png": (216, 216),
    "drawable/my_tv_logo.png": None,  # 尺寸由字体排版决定
    "drawable/ic_banner.png": (640, 360),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def load_source() -> Image.Image:
    if not SOURCE_ICON.is_file():
        raise FileNotFoundError(f"缺少品牌源图：{SOURCE_ICON}")
    actual = sha256_file(SOURCE_ICON)
    if actual != SOURCE_SHA256:
        raise RuntimeError(
            "品牌源图校验失败：\n"
            f"  路径：{SOURCE_ICON}\n"
            f"  期望：{SOURCE_SHA256}\n"
            f"  实际：{actual}"
        )
    image = Image.open(SOURCE_ICON).convert("RGBA")
    if image.size != (1024, 1024):
        raise ValueError(f"品牌源图必须为 1024x1024，实际为 {image.size[0]}x{image.size[1]}")
    return image


@lru_cache(maxsize=8)
def load_cjk_bold_font(size: int) -> ImageFont.FreeTypeFont:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = (
        windir / "Fonts" / "msyhbd.ttc",
        windir / "Fonts" / "msyh.ttc",
        windir / "Fonts" / "simhei.ttf",
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    )
    for font_path in candidates:
        if font_path.is_file():
            try:
                return ImageFont.truetype(str(font_path), size=size, index=0)
            except OSError:
                continue
    raise FileNotFoundError(
        "未找到可用的中文字体。请安装微软雅黑、苹方或 Noto Sans CJK 后重试。"
    )


def make_white_wordmark(font_size: int = 128) -> Image.Image:
    """生成透明底、纯白色的“球迹直播”字标。"""
    font = load_cjk_bold_font(font_size)
    probe = ImageDraw.Draw(Image.new("RGBA", (2, 2)))
    bbox = probe.textbbox((0, 0), BRAND_NAME, font=font)
    pad_x = round(font_size * 0.12)
    pad_y = round(font_size * 0.14)
    width = bbox[2] - bbox[0] + pad_x * 2
    height = bbox[3] - bbox[1] + pad_y * 2
    canvas = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    ImageDraw.Draw(canvas).text(
        (pad_x - bbox[0], pad_y - bbox[1]),
        BRAND_NAME,
        font=font,
        fill=WORDMARK,
    )
    return canvas


def make_circular_badge(size: int) -> Image.Image:
    """从源图裁出完整球场徽章，并加一圈细亮边。"""
    work_size = size * SUPERSAMPLE
    crop = load_source().crop(SOURCE_CROP).convert("RGB")
    crop = crop.resize((work_size, work_size), Image.Resampling.LANCZOS)

    mask = Image.new("L", (work_size, work_size), 0)
    ImageDraw.Draw(mask).ellipse(
        (0, 0, work_size - 1, work_size - 1), fill=255
    )

    badge = Image.new("RGBA", (work_size, work_size), (0, 0, 0, 0))
    badge.paste(crop, (0, 0), mask)

    rim = Image.new("RGBA", (work_size, work_size), (0, 0, 0, 0))
    rim_width = max(2, round(work_size * 0.008))
    ImageDraw.Draw(rim).ellipse(
        (0, 0, work_size - 1, work_size - 1),
        outline=RIM,
        width=rim_width,
    )
    badge.alpha_composite(rim)
    return badge.resize((size, size), Image.Resampling.LANCZOS)


def make_icon_foreground(size: int) -> Image.Image:
    """生成自适应图标前景；徽章整体保持在工作区内切安全区。"""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    badge_size = round(size * ICON_SAFE_RATIO)
    badge = make_circular_badge(badge_size)
    offset = (size - badge_size) // 2
    canvas.alpha_composite(badge, (offset, offset))
    return canvas


def make_gradient(size: tuple[int, int]) -> Image.Image:
    width, height = size
    canvas = Image.new("RGBA", size)
    draw = ImageDraw.Draw(canvas)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(
            round(ICON_BG_TOP[channel] * (1.0 - ratio) + ICON_BG_BOTTOM[channel] * ratio)
            for channel in range(4)
        )
        draw.line((0, y, width, y), fill=color)
    return canvas


def fit_image(image: Image.Image, max_width: int, max_height: int) -> Image.Image:
    ratio = min(max_width / image.width, max_height / image.height)
    return image.resize(
        (max(1, round(image.width * ratio)), max(1, round(image.height * ratio))),
        Image.Resampling.LANCZOS,
    )


def make_banner(wordmark: Image.Image, width: int = 640, height: int = 360) -> Image.Image:
    """640x360 TV banner：深草绿底，左侧品牌徽章，右侧白色品牌名。"""
    canvas = make_gradient((width, height))

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((8, 16, 330, 344), fill=(79, 242, 112, 50))
    glow = glow.filter(ImageFilter.GaussianBlur(42))
    canvas.alpha_composite(glow)

    badge = make_circular_badge(272)
    badge_x = 30
    badge_y = (height - badge.height) // 2
    canvas.alpha_composite(badge, (badge_x, badge_y))

    text_left = badge_x + badge.width + 28
    text_right = width - 28
    fitted = fit_image(wordmark, text_right - text_left, 112)
    text_x = text_left + (text_right - text_left - fitted.width) // 2
    text_y = (height - fitted.height) // 2
    canvas.alpha_composite(fitted, (text_x, text_y))
    return canvas


def save_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)


def open_png(path: Path) -> Image.Image:
    image = Image.open(path)
    image.load()
    return image


def validate_outputs() -> None:
    for path in (SOURCE_ICON,):
        if not path.is_file():
            raise AssertionError(f"缺少文件：{path}")

    relative_files = tuple(OUTPUT_SIZES)
    expected_hashes: dict[str, str] = {}
    for relative in relative_files:
        project_hashes = []
        for res_root in RES_ROOTS:
            path = res_root / relative
            if not path.is_file():
                raise AssertionError(f"缺少生成文件：{path}")
            image = open_png(path)
            expected_size = OUTPUT_SIZES[relative]
            if expected_size is not None and image.size != expected_size:
                raise AssertionError(
                    f"尺寸错误：{path} 期望 {expected_size}，实际 {image.size}"
                )
            if image.mode != "RGBA":
                raise AssertionError(f"PNG 必须为 RGBA：{path} 实际 {image.mode}")
            project_hashes.append(sha256_file(path))

        if project_hashes[0] != project_hashes[1]:
            raise AssertionError(
                f"两端文件哈希不一致：{relative}\n"
                f"  TV: {project_hashes[0]}\n"
                f"  Mobile: {project_hashes[1]}"
            )
        expected_hashes[relative] = project_hashes[0]

    for res_root in RES_ROOTS:
        for relative in (
            "mipmap-xxxhdpi/ic_launcher_fg.png",
            "mipmap-xhdpi/ic_launcher_fg.png",
        ):
            image = open_png(res_root / relative)
            bbox = image.getchannel("A").getbbox()
            if bbox is None:
                raise AssertionError(f"图标前景完全透明：{res_root / relative}")
            left, top, right, bottom = bbox
            width, height = image.size
            if not (
                left >= round(width * 0.16)
                and top >= round(height * 0.16)
                and right <= round(width * 0.84)
                and bottom <= round(height * 0.84)
            ):
                raise AssertionError(
                    f"图标主体超出自适应安全区：{res_root / relative} bbox={bbox}"
                )

        wordmark = open_png(res_root / "drawable" / "my_tv_logo.png")
        alpha = wordmark.getchannel("A")
        alpha_min, alpha_max = alpha.getextrema()
        if alpha_min != 0 or alpha_max != 255:
            raise AssertionError("启动字标必须同时包含透明像素和不透明像素")
        rgba = wordmark.tobytes()
        solid_pixels = 0
        for offset in range(0, len(rgba), 4):
            if rgba[offset + 3] == 0:
                continue
            solid_pixels += 1
            if rgba[offset : offset + 3] != b"\xff\xff\xff":
                raise AssertionError("启动字标必须为透明底白色字")
        if solid_pixels == 0:
            raise AssertionError("启动字标必须包含可见文字")

    print(f"源图 SHA-256: {SOURCE_SHA256}")
    print(f"品牌名: {BRAND_NAME}")
    print("生成文件尺寸与两端哈希验证通过：")
    for relative, digest in expected_hashes.items():
        size = OUTPUT_SIZES[relative]
        size_text = f"{size[0]}x{size[1]}" if size else "透明字标"
        print(f"  {relative} | {size_text} | sha256={digest}")


def generate() -> None:
    source = load_source()
    if source.size != (1024, 1024):
        raise AssertionError("品牌源图尺寸必须为 1024x1024")

    icon_432 = make_icon_foreground(432)
    icon_216 = icon_432.resize((216, 216), Image.Resampling.LANCZOS)
    wordmark = make_white_wordmark()
    banner = make_banner(wordmark)

    for res_root in RES_ROOTS:
        save_png(icon_432, res_root / "mipmap-xxxhdpi" / "ic_launcher_fg.png")
        save_png(icon_216, res_root / "mipmap-xhdpi" / "ic_launcher_fg.png")
        save_png(wordmark, res_root / "drawable" / "my_tv_logo.png")
        save_png(banner, res_root / "drawable" / "ic_banner.png")

    validate_outputs()


if __name__ == "__main__":
    try:
        generate()
    except Exception as exc:
        print(f"图标生成失败：{exc}", file=sys.stderr)
        raise