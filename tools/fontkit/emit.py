"""LVGL 9.x 字体 C 源码发射器。

结构对照 `src/font/fmt_txt/lv_font_fmt_txt.h`（v9.5.0）：

  glyph_dsc[0]        占位（LVGL 里 `if(!gid) return NULL;`，所以 0 号必须空着）
  cmap[0]  ASCII      FORMAT0_TINY   —— 连续码位，glyph_id = start + (cp - range_start)
  cmap[1+] 其余       SPARSE_TINY    —— 有序偏移数组二分查找，glyph_id = start + 命中下标

绝不用 FORMAT0_FULL：它需要逐码位的 `glyph_id_ofs_list`，稀疏字集传 NULL 不是
「退化成慢查找」而是直接崩——这是仓库文档里已经踩过的坑。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .raster import FontMetrics, Glyph

# LVGL 默认 LV_FONT_FMT_TXT_LARGE == 0 时，bitmap_index 只有 20 位，单个字体上限 1 MB。
BITMAP_INDEX_LIMIT = (1 << 20) - 1

ASCII_LO, ASCII_HI = 0x20, 0x7E


@dataclass
class CmapRange:
    range_start: int
    cps: list[int]  # 升序
    glyph_id_start: int
    kind: str  # "format0" | "sparse"

    @property
    def range_length(self) -> int:
        return self.cps[-1] - self.range_start + 1


def plan_cmaps(cps: list[int], max_span: int = 0xF000) -> list[tuple[str, list[int]]]:
    """把码位切成 cmap 段：ASCII 连续段用 FORMAT0_TINY，其余聚成稀疏段。"""
    cps = sorted(set(cps))
    plan: list[tuple[str, list[int]]] = []

    ascii_run = list(range(ASCII_LO, ASCII_HI + 1))
    if all(cp in set(cps) for cp in ascii_run):
        plan.append(("format0", ascii_run))
        cps = [cp for cp in cps if not (ASCII_LO <= cp <= ASCII_HI)]

    # 稀疏段：跨度超过 max_span 就断开，保证 unicode_list 的 uint16 偏移不溢出
    chunk: list[int] = []
    for cp in cps:
        if chunk and cp - chunk[0] > max_span:
            plan.append(("sparse", chunk))
            chunk = []
        chunk.append(cp)
    if chunk:
        plan.append(("sparse", chunk))
    return plan


def _hex_array(name: str, data: bytes, per_line: int = 16) -> str:
    lines = [f"static const uint8_t {name}[] = {{"]
    for i in range(0, len(data), per_line):
        chunk = data[i : i + per_line]
        lines.append("    " + "".join(f"0x{b:02X}," for b in chunk))
    lines.append("};")
    return "\n".join(lines)


def _u16_array(name: str, values: list[int], per_line: int = 12) -> str:
    lines = [f"static const uint16_t {name}[] = {{"]
    for i in range(0, len(values), per_line):
        chunk = values[i : i + per_line]
        lines.append("    " + ", ".join(str(v) for v in chunk) + ",")
    lines.append("};")
    return "\n".join(lines)


def emit(
    *,
    name: str,
    glyphs: list[Glyph],
    metrics: FontMetrics,
    bpp: int,
    source_note: str,
    charset_note: str,
    cmd_note: str,
    out_dir: Path,
) -> dict:
    """写出 `<name>.c` 与 `<name>.h`，返回体积统计。"""
    plan = plan_cmaps([g.cp for g in glyphs])

    # ---- 按 cmap 段重排字形，保证 glyph_id 连续 ----
    by_cp = {g.cp: g for g in glyphs}
    ordered: list[Glyph] = []
    ranges: list[CmapRange] = []
    next_id = 1  # 0 号留给占位
    for kind, cps in plan:
        rng = CmapRange(range_start=cps[0], cps=cps, glyph_id_start=next_id, kind=kind)
        ranges.append(rng)
        for cp in cps:
            ordered.append(by_cp[cp])
        next_id += len(cps)

    # ---- 位图与索引 ----
    bitmap = bytearray()
    dsc_rows: list[str] = []
    dsc_rows.append(
        "    {.bitmap_index = 0, .adv_w = 0, .box_w = 0, .box_h = 0, .ofs_x = 0, .ofs_y = 0},  /* 0 号占位 */"
    )
    for g in ordered:
        idx = len(bitmap)
        if idx > BITMAP_INDEX_LIMIT:
            raise ValueError(
                f"{name}: 位图 {idx} 字节超出 LVGL 默认 20 位 bitmap_index 上限 1 MB。\n"
                f"  两条路：① 缩小字符集/字号；② 在 sdkconfig 打开 CONFIG_LV_FONT_FMT_TXT_LARGE"
                f"（glyph_dsc 会从 8 字节涨到 12 字节）后重新生成。"
            )
        bitmap.extend(g.bitmap)
        dsc_rows.append(
            f"    {{.bitmap_index = {idx}, .adv_w = {g.adv_w}, .box_w = {g.box_w}, "
            f".box_h = {g.box_h}, .ofs_x = {g.ofs_x}, .ofs_y = {g.ofs_y}}},"
        )

    # ---- cmap 表 ----
    unicode_arrays: list[str] = []
    cmap_rows: list[str] = []
    for i, rng in enumerate(ranges):
        if rng.kind == "format0":
            cmap_rows.append(
                f"    {{.range_start = {rng.range_start}, .range_length = {len(rng.cps)}, "
                f".glyph_id_start = {rng.glyph_id_start}, .unicode_list = NULL, "
                f".glyph_id_ofs_list = NULL, .list_length = 0, "
                f".type = LV_FONT_FMT_TXT_CMAP_FORMAT0_TINY}},"
            )
        else:
            offsets = [cp - rng.range_start for cp in rng.cps]
            arr = f"unicode_list_{i}"
            unicode_arrays.append(_u16_array(arr, offsets))
            cmap_rows.append(
                f"    {{.range_start = {rng.range_start}, .range_length = {rng.range_length}, "
                f".glyph_id_start = {rng.glyph_id_start}, .unicode_list = {arr}, "
                f".glyph_id_ofs_list = NULL, .list_length = {len(rng.cps)}, "
                f".type = LV_FONT_FMT_TXT_CMAP_SPARSE_TINY}},"
            )

    total_px = sum(1 for _ in ordered)
    parts: list[str] = []
    parts.append(
        f"""/**
 * 自动生成，请勿手工修改。
 *
 * 源字体 : {source_note}
 * 字符集 : {charset_note}
 * 规格   : {metrics.px} px / {bpp} bpp / 行高 {metrics.line_height} px / 基线 {metrics.base_line} px
 * 字形数 : {total_px}
 * 重新生成: {cmd_note}
 */

#include "lvgl.h"

#if LVGL_VERSION_MAJOR < 8
#error "本字库面向 LVGL 9.x"
#endif

/*----------------
 *    位图
 *----------------*/
"""
    )
    parts.append(_hex_array("glyph_bitmap", bytes(bitmap)))
    parts.append("")
    parts.append(
        """/*----------------
 *    字形描述
 *----------------*/
static const lv_font_fmt_txt_glyph_dsc_t glyph_dsc[] = {"""
    )
    parts.append("\n".join(dsc_rows))
    parts.append("};")
    parts.append("")

    if unicode_arrays:
        parts.append(
            """/*----------------
 *    码位偏移表（相对 range_start）
 *----------------*/"""
        )
        parts.append("\n\n".join(unicode_arrays))
        parts.append("")

    parts.append(
        f"""/*----------------
 *    字符映射
 *----------------*/
static const lv_font_fmt_txt_cmap_t cmaps[] = {{
{chr(10).join(cmap_rows)}
}};

/*----------------
 *    字体数据
 *----------------*/
static const lv_font_fmt_txt_dsc_t font_dsc = {{
    .glyph_bitmap = glyph_bitmap,
    .glyph_dsc = glyph_dsc,
    .cmaps = cmaps,
    .kern_dsc = NULL,
    .kern_scale = 0,
    .cmap_num = {len(ranges)},
    .bpp = {bpp},
    .kern_classes = 0,
    .bitmap_format = LV_FONT_FMT_TXT_PLAIN,
    .stride = 0,          /* 0 = 连续位流，不逐行补齐 */
}};

/*----------------
 *    对外字体对象
 *----------------*/
const lv_font_t {name} = {{
    .get_glyph_dsc = lv_font_get_glyph_dsc_fmt_txt,
    .get_glyph_bitmap = lv_font_get_bitmap_fmt_txt,
    .release_glyph = NULL,
    .line_height = {metrics.line_height},
    .base_line = {metrics.base_line},
    .subpx = LV_FONT_SUBPX_NONE,
    .kerning = LV_FONT_KERNING_NONE,
    .static_bitmap = 0,   /* 与 LVGL 自带 Montserrat 一致：走 A8 转换路径。
                            置 1 会改走 lv_font_get_glyph_static_bitmap() 的
                            原始位图路径，省一次转换但依赖绘制层直接认 4bpp，
                             没在真机上验证过就不要开。 */
    .underline_position = {-max(1, metrics.px // 8)},
    .underline_thickness = 1,
    .dsc = &font_dsc,
    .fallback = NULL,
    .user_data = NULL,
}};
"""
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    c_path = out_dir / f"{name}.c"
    c_path.write_text("\n".join(parts), encoding="utf-8")

    h_path = out_dir / f"{name}.h"
    h_path.write_text(
        f"""/* 自动生成，请勿手工修改。 */
#ifndef {name.upper()}_H
#define {name.upper()}_H

#include "lvgl.h"

LV_FONT_DECLARE({name});

#endif /* {name.upper()}_H */
""",
        encoding="utf-8",
    )

    return {
        "name": name,
        "glyphs": total_px,
        "bitmap_bytes": len(bitmap),
        "dsc_bytes": (len(ordered) + 1) * 8,
        "cmap_count": len(ranges),
        "source_bytes": c_path.stat().st_size,
        "c_path": str(c_path),
        "h_path": str(h_path),
    }
