"""v1.32 兜底占位像素契约：必须真透明。

根因（2026-09-19 老板报障「黄色正方形兜底太丑」）：旧 1×1 像素 base64 实为
RGBA(255,255,0,127) 半透明黄色。App 端 coil 对 200 响应判 onSuccess → 隐藏
首字兜底，1px 黄点被 ContentScale.Fit 拉伸成整格黄色方块。
修复：像素替换为真 RGBA(0,0,0,0)；App 侧另按 intrinsicWidth 1..2px 判占位
继续显示首字。本测试锁死「像素永远真透明」这一资产级契约。
"""
import base64
import pathlib
import re
import struct
import sys
import unittest
import zlib

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


def _png_rgba(png_bytes):
    """极简 PNG 解码：返回 (width, height, (r,g,b,a))。仅支持本契约的 1×1 RGBA。"""
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos = 8
    w = h = None
    idat = b""
    while pos < len(png_bytes):
        ln = struct.unpack(">I", png_bytes[pos:pos + 4])[0]
        typ = png_bytes[pos + 4:pos + 8]
        data = png_bytes[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, bit_depth, color_type = struct.unpack(">IIBB", data[:10])
            assert bit_depth == 8 and color_type == 6, "expect 8-bit RGBA"
        elif typ == b"IDAT":
            idat += data
        pos += 12 + ln
    raw = zlib.decompress(idat)
    # scanline: filter byte + RGBA
    return w, h, tuple(raw[1:5])


class PlaceholderPixelContractTest(unittest.TestCase):
    def test_pixel_is_fully_transparent(self):
        src = (BACKEND / "proxy.py").read_text(encoding="utf-8")
        m = re.search(r'def _send_pixel_png\(self\):.*?base64\.b64decode\("([^"]+)"\)', src, re.S)
        self.assertIsNotNone(m, "_send_pixel_png 的 base64 像素未找到")
        w, h, rgba = _png_rgba(base64.b64decode(m.group(1)))
        self.assertEqual((w, h), (1, 1))
        self.assertEqual(rgba, (0, 0, 0, 0),
                         "兜底像素必须真透明 RGBA(0,0,0,0)——任何非零颜色都会被 "
                         "App 拉伸成色块（2026-09-19 黄色方块事故）")


if __name__ == "__main__":
    unittest.main()
