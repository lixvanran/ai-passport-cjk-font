"""校验：把生成的 C 源码**回读**并按 LVGL 的语义解码，与 FreeType 栅格化逐像素比对。

仓库文档的原话是「验证产出的字体，而不是产出字体的工具」。生成器写错了位图打包，
编译和链接都不会报错，只有在真机上才会表现为满屏噪点或错位。所以这里做两件事：

  1. 逐像素回读：重新解析 .c，按 LVGL 的连续位流规则解出 A8 灰度，和重新栅格化的
     结果比对。任何不一致直接让构建失败。
  2. 覆盖门禁：扫描应用源码里的非 ASCII 字符，断言它们都在字符清单里。以后新增一条
     带未映射字符的文案，会在静态检查阶段失败，而不是在设备上显示成方块。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .raster import Rasterizer, _quantize

_DSC_RE = re.compile(
    r"\{\.bitmap_index = (\d+), \.adv_w = (\d+), \.box_w = (\d+), "
    r"\.box_h = (\d+), \.ofs_x = (-?\d+), \.ofs_y = (-?\d+)\}"
)
_CMAP_RE = re.compile(
    r"\{\.range_start = (\d+), \.range_length = (\d+), \.glyph_id_start = (\d+), "
    r"\.unicode_list = (\w+), \.glyph_id_ofs_list = \w+, \.list_length = (\d+), "
    r"\.type = LV_FONT_FMT_TXT_CMAP_(\w+)\}"
)
_U16_RE = re.compile(r"static const uint16_t (\w+)\[\] = \{(.*?)\};", re.S)
_BITMAP_RE = re.compile(r"static const uint8_t glyph_bitmap\[\] = \{(.*?)\n\};", re.S)
_BPP_RE = re.compile(r"\.bpp = (\d+)")
_LH_RE = re.compile(r"\.line_height = (\d+)")
_BL_RE = re.compile(r"\.base_line = (-?\d+)")


@dataclass
class ParsedFont:
    name: str
    bitmap: bytes
    glyph_dsc: list[tuple[int, int, int, int, int, int]]
    cmaps: list[dict]
    bpp: int
    line_height: int
    base_line: int


def parse_font(c_path: Path) -> ParsedFont:
    src = c_path.read_text(encoding="utf-8")

    m = _BITMAP_RE.search(src)
    if not m:
        raise ValueError(f"{c_path}: 找不到 glyph_bitmap 数组")
    bitmap = bytes(int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{2})", m.group(1)))

    dsc = [
        (int(a), int(b), int(c), int(d), int(e), int(f))
        for a, b, c, d, e, f in _DSC_RE.findall(src)
    ]
    if not dsc:
        raise ValueError(f"{c_path}: 找不到 glyph_dsc 条目")

    lists: dict[str, list[int]] = {}
    for name, body in _U16_RE.findall(src):
        lists[name] = [int(x) for x in re.findall(r"\d+", body)]

    cmaps: list[dict] = []
    for start, length, gid_start, ulist, llen, kind in _CMAP_RE.findall(src):
        cmaps.append(
            {
                "range_start": int(start),
                "range_length": int(length),
                "glyph_id_start": int(gid_start),
                "unicode_list": None if ulist == "NULL" else lists.get(ulist, []),
                "list_length": int(llen),
                "type": kind,
            }
        )
    if not cmaps:
        raise ValueError(f"{c_path}: 找不到 cmaps 条目")

    bpp = int(_BPP_RE.search(src).group(1))
    return ParsedFont(
        name=c_path.stem,
        bitmap=bitmap,
        glyph_dsc=dsc,
        cmaps=cmaps,
        bpp=bpp,
        line_height=int(_LH_RE.search(src).group(1)),
        base_line=int(_BL_RE.search(src).group(1)),
    )


def lookup_glyph_id(font: ParsedFont, cp: int) -> int:
    """按 LVGL `get_glyph_dsc_id()` 的语义查字形号，四种 cmap 分别处理。"""
    for cmap in font.cmaps:
        start = cmap["range_start"]
        end = start + cmap["range_length"] - 1
        if not (start <= cp <= end):
            continue
        rel = cp - start
        kind = cmap["type"]
        if kind == "FORMAT0_TINY":
            return cmap["glyph_id_start"] + rel
        if kind == "SPARSE_TINY":
            ulist = cmap["unicode_list"] or []
            # 二分查找：LVGL 用的是有序偏移数组
            lo, hi = 0, len(ulist) - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if ulist[mid] == rel:
                    return cmap["glyph_id_start"] + mid
                if ulist[mid] < rel:
                    lo = mid + 1
                else:
                    hi = mid - 1
            continue
        raise ValueError(f"不支持的 cmap 类型 {kind}（本工具只产出 TINY 变体）")
    return 0


def decode_bitmap(font: ParsedFont, glyph_id: int) -> tuple[list[int], int, int]:
    """按 LVGL PLAIN 解码规则还原灰度，返回 (pixels, w, h)。"""
    _, _, box_w, box_h, _, _ = font.glyph_dsc[glyph_id]
    n = box_w * box_h
    out = [0] * n
    if n == 0:
        return out, box_w, box_h
    base = font.glyph_dsc[glyph_id][0]
    bpp = font.bpp
    data = font.bitmap
    if bpp == 4:
        for i in range(n):
            byte = data[base + (i >> 1)]
            out[i] = (byte >> 4) if (i & 1) == 0 else (byte & 0x0F)
    elif bpp == 1:
        for i in range(n):
            byte = data[base + (i >> 3)]
            out[i] = (byte >> (7 - (i & 7))) & 1
    elif bpp == 2:
        for i in range(n):
            byte = data[base + (i >> 2)]
            out[i] = (byte >> (6 - 2 * (i & 3))) & 3
    elif bpp == 8:
        out = list(data[base : base + n])
    else:
        raise ValueError(f"不支持的 bpp={bpp}")
    return out, box_w, box_h


def verify_pixels(
    c_path: Path,
    font_path: str,
    px: int,
    weight: int,
    leading: int | None,
    sample: int | None = None,
) -> dict:
    """逐像素比对。`sample=None` 时校验全部字形。"""
    font = parse_font(c_path)
    rz = Rasterizer(font_path, px, weight=weight, leading=leading)

    cps: list[int] = []
    for cmap in font.cmaps:
        if cmap["type"] == "FORMAT0_TINY":
            cps.extend(range(cmap["range_start"], cmap["range_start"] + cmap["range_length"]))
        else:
            cps.extend(cmap["range_start"] + off for off in (cmap["unicode_list"] or []))

    if sample is not None:
        step = max(1, len(cps) // sample)
        cps = cps[::step]

    # 用文件里记录的行高/基线重算，保证度量口径一致——否则抽样时重算出的基线不同，
    # 会把「度量不一致」误报成打包错误。
    expected, _metrics = rz.render(
        cps, font.bpp, line_height=font.line_height, base_line=font.base_line
    )
    mismatches: list[str] = []
    for exp in expected:
        gid = lookup_glyph_id(font, exp.cp)
        if gid == 0:
            mismatches.append(f"U+{exp.cp:04X} 查不到字形（cmap 漏了）")
            continue
        bitmap_index, adv_w, box_w, box_h, ofs_x, ofs_y = font.glyph_dsc[gid]
        if (box_w, box_h, ofs_x, ofs_y) != (exp.box_w, exp.box_h, exp.ofs_x, exp.ofs_y):
            mismatches.append(
                f"U+{exp.cp:04X} 度量不一致: 文件 {(box_w, box_h, ofs_x, ofs_y)} "
                f"vs 栅格 {(exp.box_w, exp.box_h, exp.ofs_x, exp.ofs_y)}"
            )
            continue
        if adv_w != exp.adv_w:
            mismatches.append(f"U+{exp.cp:04X} adv_w 不一致: {adv_w} vs {exp.adv_w}")
            continue
        # 直接比字节：位图长度、打包顺序、半字节高低位全在这条比较里
        got = font.bitmap[bitmap_index : bitmap_index + len(exp.bitmap)]
        if got != exp.bitmap:
            diff = sum(1 for a, b in zip(got, exp.bitmap) if a != b)
            mismatches.append(
                f"U+{exp.cp:04X} 位图不一致：{diff}/{len(exp.bitmap)} 字节不同"
            )

    return {
        "font": font.name,
        "checked": len(expected),
        "bpp": font.bpp,
        "line_height": font.line_height,
        "base_line": font.base_line,
        "mismatches": mismatches,
    }


def scan_sources(root: Path) -> set[int]:
    """扫 .c/.h 里的非 ASCII 字符，用于覆盖门禁。"""
    return set(scan_sources_located(root))


def scan_sources_located(root: Path) -> dict[int, list[tuple[str, int]]]:
    """同 `scan_sources`，但记录每个码位出现的文件与行号，报错时能直接定位。

    只扫源码里的字面量是有意的：注释里的中文不该逼你扩字符集，而字符串里的
    每一个字最终都会上屏。代价是 C 的块注释也可能被扫进来，可接受。
    """
    found: dict[int, list[tuple[str, int]]] = {}
    for path in sorted(list(root.rglob("*.c")) + list(root.rglob("*.h"))):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for ch in line:
                if ord(ch) > 0x7F:
                    found.setdefault(ord(ch), []).append((str(path), lineno))
    return found


def coverage_gate(c_path: Path, required: set[int]) -> list[int]:
    """返回字符清单里覆盖不到的码位（应为空）。"""
    font = parse_font(c_path)
    missing = []
    for cp in sorted(required):
        if lookup_glyph_id(font, cp) == 0:
            missing.append(cp)
    return missing


def check_project(src_dir: Path, font_paths: list[Path]) -> dict:
    """工程门禁：源码里出现的每个非 ASCII 字符，至少要被一个字体覆盖。"""
    located = scan_sources_located(src_dir)
    covered: set[int] = set()
    for fp in font_paths:
        font = parse_font(fp)
        for cmap in font.cmaps:
            if cmap["type"] == "FORMAT0_TINY":
                covered.update(
                    range(cmap["range_start"], cmap["range_start"] + cmap["range_length"])
                )
            else:
                covered.update(
                    cmap["range_start"] + off for off in (cmap["unicode_list"] or [])
                )

    missing = {cp: locs for cp, locs in located.items() if cp not in covered}
    return {
        "scanned_chars": len(located),
        "fonts": [p.name for p in font_paths],
        "missing": missing,
    }
