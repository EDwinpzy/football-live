"""量「会员」页头与顶栏标签胶囊的垂直位置，判断重叠/间距。

输出：
  1. 顶栏标签胶囊的文字带（取 x 中间区域，避开返回钮）
  2. 「会员」二字字形带 + 高度
  3. 侧栏说明文字带 + 高度
用法：python tools/dev/measure_paywall_top.py <png>
"""
import sys
from pathlib import Path

from PIL import Image


def neutral_bright(px):
    r, g, b = px[:3]
    return r > 125 and g > 125 and b > 125 and (max(px[:3]) - min(px[:3])) < 28


def bands(prof, thr=2, min_run=3):
    """prof: list of (y, [(x,...)]);  聚连续行成带，返回 (y0,y1,h,xmin,xmax,peak)"""
    res = []
    start = None
    for y, xs in prof:
        if len(xs) >= thr:
            if start is None:
                start, acc, xmin, xmax = y, 0, 10**9, -1
            acc += len(xs)
            xmin = min(xmin, min(xs))
            xmax = max(xmax, max(xs))
            last = y
            peak = max(acc, len(xs))
        else:
            if start is not None and last - start >= min_run - 1:
                res.append((start, last, last - start + 1, xmin, xmax))
            start = None
    if start is not None:
        res.append((start, last, last - start + 1, xmin, xmax))
    return res


def scan(img, x0, x1, y0, y1):
    pix = img.load()
    out = []
    for y in range(y0, y1):
        xs = [x for x in range(x0, x1) if neutral_bright(pix[x, y])]
        out.append((y, xs))
    return out


def main():
    path = Path(sys.argv[1])
    img = Image.open(path).convert("RGB")
    W, H = img.size
    print(f"图 {path.name}  {W}x{H}")

    print("\n=== ① 顶栏 tab 文字带（x 180..1000，避开返回钮与搜索图标）===")
    for a, b, h, xmin, xmax in bands(scan(img, 180, 1000, 0, 300)):
        print(f"  y {a:4d} .. {b:4d}   h={h:3d}   x {xmin}..{xmax}")

    print("\n=== ② 「会员」页头（x 0..300，y 100..300）===")
    for a, b, h, xmin, xmax in bands(scan(img, 0, 300, 100, 300)):
        print(f"  y {a:4d} .. {b:4d}   h={h:3d}   x {xmin}..{xmax}")

    print("\n=== ③ 说明行（x 0..1000，y 230..340）===")
    for a, b, h, xmin, xmax in bands(scan(img, 0, 1000, 230, 340)):
        print(f"  y {a:4d} .. {b:4d}   h={h:3d}   x {xmin}..{xmax}")

    # 顶栏胶囊底沿：胶囊背景是 rgba(30,30,30,.5) 叠渐变，靠颜色不好判，
    # 改用「tab 文字带底沿」近似 + 已知胶囊几何推算。
    print("\n=== ④ 关键间距 ===")
    tab = bands(scan(img, 180, 1000, 0, 300))
    mem = bands(scan(img, 0, 300, 100, 300))
    if tab and mem:
        tab_bottom = tab[0][1]
        mem_top = mem[-1][0]
        print(f"  tab 文字底沿 y={tab_bottom}；「会员」顶沿 y={mem_top}；净空 {mem_top - tab_bottom}px")
    print("  已知几何：胶囊 top=48 设计px 高 68 → 底沿 y=116 设计px"
          "（s=0.5486 → 63.6dp → 63.6dp*2.625px/dp=167px）")


if __name__ == "__main__":
    main()
