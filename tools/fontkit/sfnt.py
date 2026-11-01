"""最小 sfnt（TTF/OTF）解析器。

只做三件事，不依赖 fontTools：
  1. 读 `head.unitsPerEm`
  2. 读 `OS/2` 的垂直度量（sTypoAscender / sTypoDescender）
  3. 读 `cmap`（format 4 / 12），拿到字体真正覆盖的码位集合

第 3 点很关键：源字体没画的字，写进字符清单也变不出字形。
生成链必须先做一次「清单 ∩ 字体 cmap」，把缺字在构建期暴露出来。
"""

from __future__ import annotations

import struct


class SfntError(RuntimeError):
    pass


class Sfnt:
    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as fh:
            self.data = fh.read()
        self._read_directory()

    # ------------------------------------------------------------------ 目录
    def _read_directory(self) -> None:
        d = self.data
        if len(d) < 12:
            raise SfntError(f"{self.path}: 文件过短，不是合法字体")
        tag = d[:4]
        if tag == b"ttcf":
            raise SfntError(f"{self.path}: 这是 TTC 字体集合，请先用工具拆出单个字体")
        if tag not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttf "):
            raise SfntError(f"{self.path}: 未知 sfnt 版本 {tag!r}")

        num_tables = struct.unpack(">H", d[4:6])[0]
        self.tables: dict[str, tuple[int, int]] = {}
        for i in range(num_tables):
            off = 12 + i * 16
            name, _checksum, offset, length = struct.unpack(">4sIII", d[off : off + 16])
            self.tables[name.decode("latin-1")] = (offset, length)

    def _table(self, name: str) -> tuple[int, int]:
        if name not in self.tables:
            raise SfntError(f"{self.path}: 缺少 {name} 表")
        return self.tables[name]

    # ------------------------------------------------------------------- head
    @property
    def units_per_em(self) -> int:
        off, _ = self._table("head")
        return struct.unpack(">H", self.data[off + 18 : off + 20])[0]

    # ------------------------------------------------------------------- OS/2
    @property
    def typo_metrics(self) -> tuple[int, int, int]:
        """返回 (sTypoAscender, sTypoDescender, sTypoLineGap)，单位为 font units。"""
        off, length = self._table("OS/2")
        if length < 74:
            raise SfntError(f"{self.path}: OS/2 表过短")
        asc, desc, gap = struct.unpack(">hhh", self.data[off + 68 : off + 74])
        return asc, desc, gap

    # ------------------------------------------------------------------- cmap
    def codepoints(self) -> set[int]:
        off, _ = self._table("cmap")
        d = self.data
        _version, num = struct.unpack(">HH", d[off : off + 4])

        subtables: list[tuple[int, int, int]] = []  # (platform, encoding, offset)
        for i in range(num):
            p = off + 4 + i * 8
            platform, encoding, sub_off = struct.unpack(">HHI", d[p : p + 8])
            subtables.append((platform, encoding, off + sub_off))

        # 优先取 Unicode 全覆盖子表：format 12 > format 4
        best: tuple[int, int] | None = None
        for platform, encoding, sub_off in subtables:
            fmt = struct.unpack(">H", d[sub_off : sub_off + 2])[0]
            score = 0
            if fmt == 12:
                score = 30
            elif fmt == 4:
                score = 20
            if score == 0:
                continue
            if platform == 3 and encoding == 10:
                score += 5
            elif platform == 3 and encoding == 1:
                score += 3
            elif platform == 0:
                score += 4
            if best is None or score > best[0]:
                best = (score, sub_off)

        if best is None:
            raise SfntError(f"{self.path}: cmap 里没有 format 4/12 子表")

        sub_off = best[1]
        fmt = struct.unpack(">H", d[sub_off : sub_off + 2])[0]
        return _parse_cmap4(d, sub_off) if fmt == 4 else _parse_cmap12(d, sub_off)


def _parse_cmap4(d: bytes, off: int) -> set[int]:
    _fmt, _len, _lang, seg_x2 = struct.unpack(">HHHH", d[off : off + 8])
    seg = seg_x2 // 2
    p = off + 14
    end_codes = struct.unpack(f">{seg}H", d[p : p + seg * 2])
    p += seg * 2 + 2  # 跳过 reservedPad
    start_codes = struct.unpack(f">{seg}H", d[p : p + seg * 2])
    p += seg * 2
    id_deltas = struct.unpack(f">{seg}h", d[p : p + seg * 2])
    p += seg * 2
    id_range_offsets_pos = p
    id_range_offsets = struct.unpack(f">{seg}H", d[p : p + seg * 2])

    out: set[int] = set()
    for i in range(seg):
        start, end = start_codes[i], end_codes[i]
        if start == 0xFFFF and end == 0xFFFF:
            continue
        for cp in range(start, end + 1):
            if id_range_offsets[i] == 0:
                gid = (cp + id_deltas[i]) & 0xFFFF
            else:
                addr = (
                    id_range_offsets_pos
                    + i * 2
                    + id_range_offsets[i]
                    + (cp - start) * 2
                )
                if addr + 2 > len(d):
                    continue
                gid = struct.unpack(">H", d[addr : addr + 2])[0]
                if gid != 0:
                    gid = (gid + id_deltas[i]) & 0xFFFF
            if gid != 0:
                out.add(cp)
    return out


def _parse_cmap12(d: bytes, off: int) -> set[int]:
    _fmt, _res, _len, _lang, n_groups = struct.unpack(">HHIII", d[off : off + 16])
    out: set[int] = set()
    p = off + 16
    for _ in range(n_groups):
        start, end, start_gid = struct.unpack(">III", d[p : p + 12])
        p += 12
        if start_gid == 0:
            continue
        for cp in range(start, end + 1):
            out.add(cp)
    return out
