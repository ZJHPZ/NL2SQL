"""把覆盖率扫描结果画成曲线（M4 实验一的图，供论文实验章使用）。

用 Pillow 直接画，不引入 matplotlib：项目只需要一张图，
没必要为一张图给全仓加一个绘图依赖。中文用系统字体（雅黑/黑体）。

用法:
    python scripts/plot_coverage.py data/_experiments/coverage/coverage.json docs/assets/coverage_curve.png
"""
from __future__ import annotations

import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)

W, H = 1280, 760
BG = (255, 255, 255)
FG = (34, 34, 34)
GRID = (226, 226, 226)
RULE = (37, 99, 235)       # 规则规划器：实测
CEIL = (120, 120, 120)     # 语义天花板
LLM = (234, 88, 12)        # 模型规划器（有 Key 时）
AREA = (120, 70, 1180, 590)  # x0, y0, x1, y1


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _dashed(draw, p1, p2, fill, dash=12, gap=8, width=2):
    x1, y1 = p1
    x2, y2 = p2
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if length == 0:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    pos = 0.0
    while pos < length:
        end = min(pos + dash, length)
        draw.line([(x1 + ux * pos, y1 + uy * pos),
                   (x1 + ux * end, y1 + uy * end)], fill=fill, width=width)
        pos = end + gap


def plot_coverage(json_path: str, png_path: str) -> str:
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    sweep = sorted(data.get("sweep", []), key=lambda p: p["target"])
    if not sweep:
        raise SystemExit("结果里没有 sweep 数据，无法画图")

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_title = _font(34)
    f_sub = _font(20)
    f_axis = _font(22)
    f_tick = _font(18)
    f_label = _font(20)
    f_note = _font(18)

    x0, y0, x1, y1 = AREA
    pw, ph = x1 - x0, y1 - y0
    total = data["total_cases"]
    xs = [p["target"] for p in sweep]
    xmin, xmax = min(xs), max(xs)

    def px(target: int) -> float:
        return x0 + (target - xmin) / (xmax - xmin) * pw

    def py(value: float) -> float:
        return y1 - (value / total) * ph

    d.text((x0, 26), "语义层覆盖率决定准确率上限", font=f_title, fill=FG)
    d.text((x0, 60), "横轴 = 语义模型保留的指标数（满配 17）；纵轴 = 黄金集 30 题通过数",
           font=f_sub, fill=(90, 90, 90))

    # 网格 + y 轴刻度（0..30，每 5 题）
    for v in range(0, total + 1, 5):
        y = py(v)
        d.line([(x0, y), (x1, y)], fill=GRID, width=1)
        label = str(v)
        w = d.textlength(label, font=f_tick)
        d.text((x0 - 12 - w, y - 10), label, font=f_tick, fill=(110, 110, 110))
    d.line([(x0, y0), (x0, y1)], fill=(180, 180, 180), width=2)
    d.line([(x0, y1), (x1, y1)], fill=(180, 180, 180), width=2)

    # x 轴刻度
    for target in xs:
        x = px(target)
        d.line([(x, y1), (x, y1 + 6)], fill=(180, 180, 180), width=1)
        label = str(target)
        w = d.textlength(label, font=f_tick)
        d.text((x - w / 2, y1 + 10), label, font=f_tick, fill=(110, 110, 110))
    axis_label = "语义模型中的指标数"
    w = d.textlength(axis_label, font=f_axis)
    d.text((x0 + pw / 2 - w / 2, y1 + 46), axis_label, font=f_axis, fill=FG)

    # 天花板（虚线）
    ceil_pts = [(px(p["target"]), py(p["coverage_ceiling"])) for p in sweep]
    for a, b in zip(ceil_pts, ceil_pts[1:]):
        _dashed(d, a, b, CEIL, width=2)
    for p, (x, y) in zip(sweep, ceil_pts):
        d.ellipse([x - 4, y - 4, x + 4, y + 4], outline=CEIL, width=2, fill=BG)

    # 规则规划器实测（实线 + 圆点）
    rule_pts = [(px(p["target"]), py(p["pass"])) for p in sweep]
    d.line(rule_pts, fill=RULE, width=4, joint="curve")
    for p, (x, y) in zip(sweep, rule_pts):
        d.ellipse([x - 7, y - 7, x + 7, y + 7], fill=RULE, outline=BG, width=2)
        if p["target"] in (xmin, 5, 11, 17, xmax):
            txt = f"{p['pass']}"
            w = d.textlength(txt, font=f_note)
            d.text((x - w / 2, y - 32), txt, font=f_note, fill=RULE)

    # 模型规划器（有数据才画）
    llm_pts = []
    for e in data.get("tiers", []):
        llm = e.get("llm")
        if llm and "skipped" not in llm:
            llm_pts.append((px(e["kept_count"]), py(llm["pass"]), llm["pass"]))
    if llm_pts:
        d.line([(x, y) for x, y, _ in llm_pts], fill=LLM, width=4, joint="curve")
        for x, y, _ in llm_pts:
            d.ellipse([x - 7, y - 7, x + 7, y + 7], fill=LLM, outline=BG, width=2)

    # 三档标注（满配/中配/低配）
    tier_labels = {"满配": "满配", "中配": "中配", "低配": "低配"}
    for e in data.get("tiers", []):
        x = px(e["kept_count"])
        label = tier_labels.get(e["label"], e["label"])
        w = d.textlength(label, font=f_label)
        d.text((x - w / 2, y1 - 8), label, font=f_label, fill=(80, 80, 80))

    # 图例
    lx, ly = x0 + 20, y0 + 10
    d.line([(lx, ly + 10), (lx + 40, ly + 10)], fill=RULE, width=4)
    d.text((lx + 50, ly), "规则规划器（实测）", font=f_note, fill=FG)
    d.line([(lx + 240, ly + 10), (lx + 280, ly + 10)], fill=CEIL, width=2)
    d.text((lx + 290, ly), "语义天花板（所需指标是否还在模型里）", font=f_note, fill=FG)
    if llm_pts:
        d.line([(lx + 660, ly + 10), (lx + 700, ly + 10)], fill=LLM, width=4)
        d.text((lx + 710, ly), "模型规划器", font=f_note, fill=FG)

    # 底部注脚
    note = (f"数据: {os.path.basename(json_path)}；"
            f"语义层可移植性 {data.get('portability', 0):.0%}；"
            "判分口径 = golden_set/evaluate.py rows_equal（全仓唯一）")
    d.text((x0, H - 44), note, font=f_note, fill=(120, 120, 120))

    os.makedirs(os.path.dirname(os.path.abspath(png_path)), exist_ok=True)
    img.save(png_path)
    return png_path


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path = plot_coverage(sys.argv[1], sys.argv[2])
    print(f"图: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
