#!/usr/bin/env python3
"""用**生成出来的 C 字库**渲染一张 240×320 的真机同像素预览图。

不是拿浏览器或 Pillow 直接排字——这里是把 .c 文件里的位图按 LVGL 的规则解码出来，
再逐字形贴到画布上。跑通这张图，等于证明了「文件里的字节确实能拼成正确的界面」。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from PIL import Image, ImageDraw  # noqa: E402

from fontkit.verify import ParsedFont, decode_bitmap, lookup_glyph_id, parse_font  # noqa: E402

W, H = 240, 320
SCALE = 3

BG = (14, 16, 22)
CARD = (26, 30, 40)
INK = (236, 240, 248)
DIM = (140, 150, 168)
ACCENT = (86, 200, 160)
LINE = (44, 50, 64)


class Font:
    """把 ParsedFont 包成「能往图上贴字」的对象。"""

    def __init__(self, c_path: Path):
        self.pf: ParsedFont = parse_font(c_path)
        self.name = self.pf.name

    @property
    def line_height(self) -> int:
        return self.pf.line_height

    def glyph(self, ch: str):
        cp = ord(ch)
        gid = lookup_glyph_id(self.pf, cp)
        if gid == 0:
            return None
        bitmap_index, adv_w, box_w, box_h, ofs_x, ofs_y = self.pf.glyph_dsc[gid]
        pixels, w, h = decode_bitmap(self.pf, gid)
        return pixels, w, h, ofs_x, ofs_y, (adv_w + 8) >> 4

    def measure(self, text: str) -> int:
        total = 0
        for ch in text:
            g = self.glyph(ch)
            total += g[5] if g else 0
        return total


def draw_text(img: Image.Image, font: Font, x: int, y: int, text: str, color) -> int:
    """在 (x, y) 处按行顶对齐画一行字，返回结束时的 x。"""
    draw = ImageDraw.Draw(img)
    pen = x
    for ch in text:
        g = font.glyph(ch)
        if g is None:
            draw.rectangle([pen, y + 2, pen + font.line_height - 6, y + font.line_height - 4], outline=color)
            pen += font.line_height // 2
            continue
        pixels, w, h, ofs_x, ofs_y, adv = g
        if w and h:
            for gy in range(h):
                for gx in range(w):
                    a = pixels[gy * w + gx]
                    if a:
                        px, py = pen + ofs_x + gx, y + ofs_y + gy
                        if 0 <= px < img.width and 0 <= py < img.height:
                            base = img.getpixel((px, py))
                            t = a / 15
                            img.putpixel(
                                (px, py),
                                tuple(int(base[i] * (1 - t) + color[i] * t) for i in range(3)),
                            )
        pen += adv
    return pen


def wrap(font: Font, text: str, width: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if font.measure(cur + ch) > width:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def build_mockup(f16: Font, f20: Font, f12: Font) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # 顶栏
    d.rectangle([0, 0, W, 26], fill=CARD)
    draw_text(img, f20, 10, 2, "AI 通行证", INK)
    d.rectangle([W - 46, 9, W - 10, 19], fill=(40, 46, 60))
    draw_text(img, f12, W - 44, 3, "在线", ACCENT)

    # 头像位
    d.ellipse([16, 40, 76, 100], fill=(38, 44, 58), outline=LINE)
    draw_text(img, f12, 32, 62, "头像", DIM)

    # 姓名 + 编号（「燚」是三级字，只有 8105 全表才覆盖）
    draw_text(img, f16, 90, 44, "李燚", INK)
    draw_text(img, f12, 90, 68, "编号 AI-8F3C-2026", DIM)
    draw_text(img, f12, 90, 84, "签发 2026-11-01", DIM)

    # 分隔线
    d.line([16, 112, W - 16, 112], fill=LINE)

    # 简介（16 px 正文，验证多行换行与行高）
    draw_text(img, f12, 16, 120, "自我介绍", ACCENT)
    body = "在风里读诗的旅行者，收集各地的方言与旧招牌，也喜欢把生僻字念错。"
    y = 138
    for line in wrap(f16, body, W - 32)[:4]:
        draw_text(img, f16, 16, y, line, INK)
        y += f16.line_height

    # 生僻字覆盖展示：这些是二级/三级字，一级常用字里没有，只有 8105 全表才覆盖
    d.line([16, y + 6, W - 16, y + 6], fill=LINE)
    draw_text(img, f12, 16, y + 14, "二级 / 三级字覆盖", ACCENT)
    draw_text(img, f16, 16, y + 30, "鑫 燚 淼 犇 骉 赟 燊 垚", INK)

    # 底部操作条
    d.rectangle([0, H - 24, W, H], fill=CARD)
    draw_text(img, f12, 10, H - 22, "▲▼ 选择", DIM)
    draw_text(img, f12, 86, H - 22, "确定 进入", DIM)
    draw_text(img, f12, 168, H - 22, "长按 返回", DIM)

    return img


def build_glyph_grid(f16: Font, chars: str, cols: int = 16) -> Image.Image:
    cell = 24
    rows = (len(chars) + cols - 1) // cols
    img = Image.new("RGB", (cols * cell + 16, rows * cell + 16), BG)
    for i, ch in enumerate(chars):
        x = 8 + (i % cols) * cell
        y = 8 + (i // cols) * cell
        draw_text(img, f16, x + 2, y + 2, ch, INK)
    return img


def export_coverage(fonts: dict[str, "Font"], out: Path) -> None:
    """导出每个字体覆盖的码位，供网页里做实时覆盖检测。"""
    import json

    data: dict[str, list[int]] = {}
    for name, f in fonts.items():
        cps: list[int] = []
        for cmap in f.pf.cmaps:
            if cmap["type"] == "FORMAT0_TINY":
                cps.extend(range(cmap["range_start"], cmap["range_start"] + cmap["range_length"]))
            else:
                cps.extend(cmap["range_start"] + o for o in (cmap["unicode_list"] or []))
        data[name] = sorted(cps)

    # 写成 .js 而不是 .json：file:// 下 fetch 会被 CORS 拦掉，<script> 不会
    payload = {
        "fonts": data,
        "lineHeight": {n: f.line_height for n, f in fonts.items()},
    }
    out.write_text(
        "window.COVERAGE = " + json.dumps(payload, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )
    print(f"覆盖清单   ：{out}（{sum(len(v) for v in data.values())} 条）")


def main() -> int:
    fonts = ROOT / "fonts"
    f12 = Font(fonts / "ap_font_12.c")
    f16 = Font(fonts / "ap_font_16.c")
    f20 = Font(fonts / "ap_font_20_core.c")

    out = ROOT / "demo"
    out.mkdir(exist_ok=True)

    mock = build_mockup(f16, f20, f12)
    mock.resize((W * SCALE, H * SCALE), Image.NEAREST).save(out / "passport-240x320@3x.png")
    mock.save(out / "passport-240x320.png")

    sample = (
        "永和九年岁在癸丑暮春之初会于会稽山阴之兰亭修禊事也群贤毕至少长咸集"
        "此地有崇山峻岭茂林修竹又有清流激湍映带左右引以为流觞曲水列坐其次"
        "鑫燚淼犇骉赟燊垚犇"
    )
    grid = build_glyph_grid(f16, sample[:128])
    grid.resize((grid.width * 2, grid.height * 2), Image.NEAREST).save(out / "glyph-grid@2x.png")

    export_coverage(
        {"ap_font_12": f12, "ap_font_16": f16, "ap_font_20_core": f20}, out / "coverage.js"
    )

    print(f"预览已生成：{out / 'passport-240x320@3x.png'}")
    print(f"字形网格   ：{out / 'glyph-grid@2x.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
