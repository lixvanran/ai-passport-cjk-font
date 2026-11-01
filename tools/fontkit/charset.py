"""字符集分层。

设备侧的现实是：8 MB Flash、无 PSRAM、240×320 屏。把整套 CJK 全塞进去没有意义，
但也绝不能只塞「当前界面用到的字」——那样用户叫「李龘」就会看到方框。

所以按**字号**分层：
  小字号（正文/姓名/列表）覆盖到 8105 全表，保证任意姓名、任意网络文本不缺字；
  大字号（标题/数字）只覆盖一级常用字，省下的 Flash 留给小字号。

`通用规范汉字表`（2013 年国务院发布）是这里的分层依据：
  一级 3500 = 常用字；二级 3000 = 次常用；三级 1605 = 人名地名与专业用字。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# 符号区段。全部按「码位区间」声明，再与源字体的 cmap 求交，字体没有的自然被过滤掉。
#
# 三档按「一个 16 px 字体能塞下多少」来切，不是拍脑袋分的。实测 8105 个汉字在
# 16 px / 4bpp 下已经占 971.5 KB，而 LVGL 默认 bitmap_index 只有 20 位（上限 1 MB），
# 留给符号的预算只有约 52 KB。下表是各区段在 16 px 下的真实开销：
#
#   minimal  32.9 KB  中文标点、全角字符、货币、Latin-1 —— 只要显示中文就必须有
#   ui       +16.5 KB 箭头、几何图形、方块、类字母、数字形式、CJK 兼容形式
#   full     +44.4 KB 带圈数字、数学运算符、制表符、杂项符号 —— 会超 1 MB，慎用
SYMBOL_RANGES: dict[str, list[tuple[int, int]]] = {
    "none": [],
    # 必备：没有这些，中文正文的标点和全角字符就是方块
    "minimal": [
        (0x00A0, 0x00FF),  # Latin-1 补充：× ÷ ° ± § © ®
        (0x2000, 0x206F),  # 通用标点：— … ‘’ “” • ‰ † ‡ €
        (0x20A0, 0x20BF),  # 货币符号
        (0x3000, 0x303F),  # 中文标点：　、。〃〈〉《》「」『』【】〔〕
        (0xFF00, 0xFFEF),  # 半角及全角形式：！＂＃…ＡＢＣ１２３
    ],
    # UI 常用：加上箭头、进度条方块、几何图形、序号
    "ui": [
        (0x00A0, 0x00FF),
        (0x2000, 0x206F),
        (0x20A0, 0x20BF),
        (0x2100, 0x214F),  # 类字母符号：™ Ω №
        (0x2150, 0x218F),  # 数字形式：⅓ Ⅳ
        (0x2190, 0x21FF),  # 箭头：← → ↑ ↓
        (0x2580, 0x259F),  # 方块元素（进度条）
        (0x25A0, 0x25FF),  # 几何图形：● ○ ◆ ■ □ ▲ △
        (0x3000, 0x303F),
        (0xFE30, 0xFE4F),  # CJK 兼容形式
        (0xFF00, 0xFFEF),
    ],
    # 全量：再加带圈数字、数学运算符、制表符、杂项符号
    "full": [
        (0x00A0, 0x00FF),
        (0x2000, 0x206F),
        (0x20A0, 0x20BF),
        (0x2100, 0x214F),
        (0x2150, 0x218F),
        (0x2190, 0x21FF),
        (0x2200, 0x22FF),  # 数学运算符：≤ ≥ ≠ ≈ ∞ ∑ √
        (0x2460, 0x24FF),  # 带圈字母数字：① ② ⑴
        (0x2500, 0x257F),  # 制表符
        (0x2580, 0x259F),
        (0x25A0, 0x25FF),
        (0x2600, 0x26FF),  # 杂项符号：★ ☆ ☀ ☁
        (0x3000, 0x303F),
        (0xFE30, 0xFE4F),
        (0xFF00, 0xFFEF),
    ],
}

_LEVEL_RE = re.compile(r"^#\s*(.級字表|.级字表)")


@dataclass
class CharsetSpec:
    """一个字符集档位的定义。"""

    name: str
    levels: tuple[int, ...] = (1, 2, 3)
    symbols: str = "ui"
    ascii: bool = True  # 拉丁字母、数字、基础标点永远要有，否则 "ABC 123" 都显示不出来
    extra: str = ""  # 额外补充的字符（用于覆盖项目自己的文案）

    def describe(self) -> str:
        lv = "+".join(str(x) for x in self.levels) or "无"
        return f"{self.name}: 汉字级别={lv}, 符号={self.symbols}"


@dataclass
class Charset:
    """解析后的字符集：分层保存，便于按级别统计。"""

    by_level: dict[int, list[int]] = field(default_factory=dict)
    symbols: list[int] = field(default_factory=list)
    ascii: list[int] = field(default_factory=list)
    extra: list[int] = field(default_factory=list)

    @property
    def hanzi(self) -> list[int]:
        out: list[int] = []
        for level in sorted(self.by_level):
            out.extend(self.by_level[level])
        return out

    def all(self) -> list[int]:
        s = set(self.hanzi) | set(self.symbols) | set(self.ascii) | set(self.extra)
        return sorted(s)


def load_general_table(path: Path) -> dict[int, list[int]]:
    """解析《通用规范汉字表》。文件格式：`# 一级字表` 标题行 + 每行一个字。"""
    levels: dict[int, list[int]] = {1: [], 2: [], 3: []}
    current = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if "一" in line:
                current = 1
            elif "二" in line:
                current = 2
            elif "三" in line:
                current = 3
            continue
        if current == 0 or len(line) != 1:
            continue
        levels[current].append(ord(line))
    return levels


def expand_symbols(kind: str, coverage: set[int]) -> list[int]:
    """展开符号区间，并只保留源字体真正有字形的码位。"""
    ranges = SYMBOL_RANGES.get(kind)
    if ranges is None:
        raise ValueError(f"未知符号集 {kind!r}，可选：{sorted(SYMBOL_RANGES)}")
    out: set[int] = set()
    for lo, hi in ranges:
        for cp in range(lo, hi + 1):
            if cp in coverage:
                out.add(cp)
    return sorted(out)


def build(spec: CharsetSpec, coverage: set[int], table: dict[int, list[int]]) -> Charset:
    """按 spec 构造字符集；与源字体 cmap 求交，缺字不静默通过。"""
    by_level: dict[int, list[int]] = {}
    for level in spec.levels:
        by_level[level] = [cp for cp in table.get(level, []) if cp in coverage]

    symbols = expand_symbols(spec.symbols, coverage)
    ascii_cps = [cp for cp in range(0x20, 0x7F) if cp in coverage] if spec.ascii else []
    extra = sorted({ord(c) for c in spec.extra if ord(c) in coverage})
    return Charset(by_level=by_level, symbols=symbols, ascii=ascii_cps, extra=extra)


def missing_from_font(spec: CharsetSpec, coverage: set[int], table: dict[int, list[int]]) -> list[int]:
    """清单里有、源字体没有的码位。构建时应视为错误而不是警告。"""
    want: set[int] = set()
    for level in spec.levels:
        want.update(table.get(level, []))
    return sorted(want - coverage)


def to_text(codepoints: list[int], per_line: int = 40) -> str:
    """把码位清单写成可读文本，一行 40 字，便于 diff 和人工抽查。"""
    lines = []
    for i in range(0, len(codepoints), per_line):
        lines.append("".join(chr(cp) for cp in codepoints[i : i + per_line]))
    return "\n".join(lines) + "\n"
