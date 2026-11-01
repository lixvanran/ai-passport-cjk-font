"""栅格化：把矢量字形变成 LVGL 需要的位图 + 度量。

为什么不用 lv_font_conv：仓库文档里已经踩过坑——1.5.3 在 Node v24 上写出的位图是坏的
（同一 OTF、同一组参数，加不加 `--no-prefilter` 字节完全相同，解出来是噪点）。
这里直接用 FreeType 栅格化并自己写 LVGL 位图格式，把工具链从信任链里拿掉。

两个容易写错、写错就整屏错位的细节，这里显式处理：
  · 位图是**连续位流**，不逐行字节对齐。4bpp 时字节数 = ceil(w*h/2)，高半字节在左。
  · 每个字形起点按字节对齐（bitmap_index 是字节下标），所以长度要向上取整到字节。
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont


@dataclass
class Glyph:
    cp: int
    bitmap: bytes
    adv_w: int  # 8.4 定点，真实宽度 * 16
    box_w: int
    box_h: int
    ofs_x: int
    ofs_y: int

    @property
    def is_empty(self) -> bool:
        return self.box_w == 0 or self.box_h == 0


@dataclass
class FontMetrics:
    px: int
    line_height: int
    base_line: int
    max_ascent: int
    max_descent: int
    leading: int
    clipped: int = 0


def _is_core(cp: int) -> bool:
    """决定行高/基线的「核心字符」：ASCII 与真正的汉字。

    不能简单用 `cp >= 0x2E80` —— 中文标点区里混着 `〱〲` 这类竖排重复符号，
    它们设计上就占满一整行（16 px 字号下墨迹高达 22 px、下伸 9 px），
    会把行高从 19 px 撑到 31 px，一屏 4 行的布局直接崩掉。
    """
    return (
        0x20 <= cp <= 0x7E
        or 0x3400 <= cp <= 0x4DBF  # CJK 扩展 A
        or 0x4E00 <= cp <= 0x9FFF  # CJK 基本区
        or 0xF900 <= cp <= 0xFAFF  # CJK 兼容汉字
    )


def _quantize(value: int, bpp: int) -> int:
    if bpp == 8:
        return value
    if bpp == 4:
        return (value * 15 + 127) // 255
    if bpp == 2:
        return (value * 3 + 127) // 255
    if bpp == 1:
        return 1 if value >= 128 else 0
    raise ValueError(f"不支持的 bpp={bpp}，可选 1/2/4/8")


def pack_bitmap(pixels: list[int], bpp: int) -> bytes:
    """连续位流打包，高位在前，末尾不足一字节时右补 0。"""
    per_byte = 8 // bpp
    out = bytearray()
    acc = 0
    n = 0
    for v in pixels:
        acc = (acc << bpp) | v
        n += 1
        if n == per_byte:
            out.append(acc)
            acc = 0
            n = 0
    if n:
        out.append(acc << (bpp * (per_byte - n)))
    return bytes(out)


class Rasterizer:
    """单个字号的栅格化器。

    `leading` 是行高之外额外加的行距。仓库文档的约定是「行高 = 字号 + 行距」，
    16 px 字号配 3 px 行距得 19 px 行高，一屏正好 4 行；若直接用字体自然行高
    （Noto Sans SC 在 16 px 时约 24 px），每页最后一行会被裁掉。
    """

    def __init__(self, font_path: str, px: int, weight: int = 400, leading: int | None = None):
        self.px = px
        self.weight = weight
        self.leading = leading if leading is not None else max(1, round(px * 0.1875))
        self.font = ImageFont.truetype(font_path, px, layout_engine=ImageFont.Layout.BASIC)
        try:
            self.font.set_variation_by_axes([weight])
        except Exception:  # 非可变字体
            pass

    # ------------------------------------------------------------------ 单字
    def _ink(self, ch: str) -> tuple[int, int, int, int, float]:
        """返回 (x0, y0, x1, y1, advance)，坐标以基线左端为原点，y 向上为负。"""
        bbox = self.font.getbbox(ch, anchor="ls")
        adv = self.font.getlength(ch)
        if bbox is None:
            return 0, 0, 0, 0, adv
        x0, y0, x1, y1 = bbox
        if x1 <= x0 or y1 <= y0:
            return 0, 0, 0, 0, adv
        return x0, y0, x1, y1, adv

    def render(
        self,
        cps: list[int],
        bpp: int,
        line_height: int | None = None,
        base_line: int | None = None,
    ) -> tuple[list[Glyph], FontMetrics]:
        """两遍：先量出所有字形的墨迹范围，再定行高/基线，最后写位图。

        先量后定是必须的——基线如果拍脑袋定，`g` 这种下伸字母或带尾巴的汉字就会被
        行框裁掉，而且只在大字号或个别字上出现，极难排查。

        但「量」不能取全字符集的最大值：符号区段里有个别装饰字形（℅、￣、箭头之类）
        的墨迹会远远跑出字身框，把行高从 19 px 撑到 31 px，一屏 4 行的布局直接崩。
        所以行高/基线只按**核心字符**（汉字 + ASCII）量，其余字形超出部分就地裁掉，
        并把裁掉的数量报出来。
        """
        # 第一遍：量
        raw: list[tuple[int, int, int, int, int, int]] = []  # cp,x0,y0,x1,y1,adv16
        core_ascent = 0
        core_descent = 0
        for cp in cps:
            x0, y0, x1, y1, adv = self._ink(chr(cp))
            adv16 = max(0, round(adv * 16))
            if x1 > x0 and y1 > y0:
                if _is_core(cp):
                    core_ascent = max(core_ascent, -y0)
                    core_descent = max(core_descent, y1)
            raw.append((cp, x0, y0, x1, y1, adv16))

        if line_height is None or base_line is None:
            line_height = max(self.px + self.leading, core_ascent + core_descent)
            slack = line_height - core_ascent - core_descent
            base_line = core_descent + max(0, slack // 2)

        baseline_y = line_height - base_line  # 基线距行框顶部

        # 第二遍：画 + 打包
        glyphs: list[Glyph] = []
        clipped = 0
        for cp, x0, y0, x1, y1, adv16 in raw:
            w, h = x1 - x0, y1 - y0
            if w <= 0 or h <= 0:
                glyphs.append(Glyph(cp, b"", adv16, 0, 0, 0, 0))
                continue

            img = Image.new("L", (w, h), 0)
            ImageDraw.Draw(img).text(
                (-x0, -y0), chr(cp), font=self.font, fill=255, anchor="ls"
            )

            ofs_y = baseline_y + y0
            # 就地裁剪到行框内：宁可我们自己裁掉 1 px，也不要让 LVGL 在运行时裁
            top = max(0, -ofs_y)
            bottom = max(0, ofs_y + h - line_height)
            if top or bottom:
                clipped += 1
                box = (0, top, w, h - bottom)
                if box[3] <= box[1] or box[2] <= box[0]:
                    glyphs.append(Glyph(cp, b"", adv16, 0, 0, 0, 0))
                    continue
                img = img.crop(box)
                w, h = img.size
                ofs_y += top

            pixels = [_quantize(v, bpp) for v in img.tobytes()]
            glyphs.append(
                Glyph(
                    cp=cp,
                    bitmap=pack_bitmap(pixels, bpp),
                    adv_w=adv16,
                    box_w=w,
                    box_h=h,
                    ofs_x=x0,
                    ofs_y=ofs_y,
                )
            )

        metrics = FontMetrics(
            px=self.px,
            line_height=line_height,
            base_line=base_line,
            max_ascent=core_ascent,
            max_descent=core_descent,
            leading=self.leading,
            clipped=clipped,
        )
        return glyphs, metrics
