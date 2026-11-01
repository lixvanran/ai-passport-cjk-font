#!/usr/bin/env python3
"""字库构建入口。

用法：
    python tools/build.py --profile standard
    python tools/build.py --profile lite --verify
    python tools/build.py --profile standard --only 16

输出：
    fonts/ap_font_*.c / .h     可直接放进 AI Passport 工程的 assets/fonts/
    charsets/*.txt             本次实际生成的字符清单（作为输入和门禁依据一起入库）
    build-report.json          体积与覆盖率报告
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from fontkit import charset as cs  # noqa: E402
from fontkit.emit import emit  # noqa: E402
from fontkit.raster import Rasterizer  # noqa: E402
from fontkit.sfnt import Sfnt  # noqa: E402
from fontkit.verify import check_project, verify_pixels  # noqa: E402

DEFAULT_FONT = r"C:\Windows\Fonts\NotoSansSC-VF.ttf"


@dataclass
class Job:
    px: int
    levels: tuple[int, ...] = (1, 2, 3)
    symbols: str = "ui"
    weight: int = 400
    bpp: int = 4
    leading: int | None = None
    tag: str = ""

    @property
    def name(self) -> str:
        return f"ap_font_{self.px}{self.tag}"


# 两档 profile。分层理由：小字号承担正文、姓名、列表，必须不缺字；大字号只用于标题，
# 一级常用字足够，省下的 Flash 留给小字号。
#
# 为什么没有 24 px：4bpp 下 24 px 的 3500 个汉字就要 966 KB，加上符号必然超过 LVGL
# 默认 1 MB 的 bitmap_index 上限。240×320 的屏上 20 px 已经是「大标题」，再大没有收益。
# 确实需要 24 px 时用 --bpp 2，或打开 CONFIG_LV_FONT_FMT_TXT_LARGE。
PROFILES: dict[str, list[Job]] = {
    # 最小可用：只覆盖一级常用字（3500），符号也精简，适合 Flash 预算极紧的玩法
    "lite": [
        Job(px=12, levels=(1,), symbols="minimal", tag="_core"),
        Job(px=16, levels=(1,), symbols="minimal", tag="_core"),
    ],
    # 推荐：12/16 px 覆盖全表 8105（任意姓名、任意网络文本都不缺字），20 px 走一级常用字。
    # 16 px 的符号只能用 minimal —— 8105 个汉字在 16 px / 4bpp 下占 971.5 KB，
    # 加 ui 档符号会到 1,048,589 字节，比 1 MB 上限多 13 字节。字符优先于装饰符号。
    "standard": [
        Job(px=12, levels=(1, 2, 3), symbols="ui"),
        Job(px=16, levels=(1, 2, 3), symbols="minimal"),
        Job(px=20, levels=(1,), symbols="ui", tag="_core"),
    ],
}


def human(n: int) -> str:
    return f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.2f} MB"


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 AI Passport 中文位图字库")
    ap.add_argument("--profile", default="standard", choices=sorted(PROFILES))
    ap.add_argument("--font", default=DEFAULT_FONT, help="源字体 TTF/OTF 路径")
    ap.add_argument("--weight", type=int, default=None, help="可变字体字重，默认取 profile")
    ap.add_argument("--bpp", type=int, default=None, choices=[1, 2, 4, 8])
    ap.add_argument("--only", type=int, default=None, help="只生成指定字号")
    ap.add_argument(
        "--extra",
        default="",
        help='额外补收的字符。例如 --extra "龘羴"（这些字不在《通用规范汉字表》里）',
    )
    ap.add_argument(
        "--line-height",
        type=int,
        default=None,
        help="强制行高（默认按字号+行距自动算；按固定行数排版时才需要指定）",
    )
    ap.add_argument("--base-line", type=int, default=None, help="强制基线（默认自动）")
    ap.add_argument("--verify", action="store_true", help="生成后逐像素回读校验")
    ap.add_argument("--verify-sample", type=int, default=None, help="只抽检 N 个字形")
    ap.add_argument("--out", default=str(ROOT / "fonts"))
    ap.add_argument("--charset-dir", default=str(ROOT / "charsets"))
    ap.add_argument(
        "--check",
        metavar="SRC_DIR",
        default=None,
        help="门禁模式：扫描该目录下 .c/.h 里的中文，检查是否被已生成的字体覆盖",
    )
    args = ap.parse_args()

    if args.check:
        return run_check(Path(args.check))

    font_path = Path(args.font)
    if not font_path.exists():
        print(f"找不到源字体：{font_path}", file=sys.stderr)
        return 2

    table_path = ROOT / "charsets" / "通用规范汉字表.txt"
    if not table_path.exists():
        print(f"找不到字符集文件：{table_path}", file=sys.stderr)
        return 2

    t0 = time.time()
    print(f"源字体 : {font_path}")
    sfnt = Sfnt(str(font_path))
    coverage = sfnt.codepoints()
    table = cs.load_general_table(table_path)
    asc, desc, _gap = sfnt.typo_metrics
    print(
        f"        家族={font_path.stem}  upem={sfnt.units_per_em}  "
        f"typo={asc}/{desc}  cmap 覆盖={len(coverage)} 码位"
    )
    print(
        f"字表   : 一级 {len(table[1])} / 二级 {len(table[2])} / 三级 {len(table[3])} "
        f"（合计 {sum(len(v) for v in table.values())}）"
    )

    jobs = PROFILES[args.profile]
    if args.only:
        jobs = [j for j in jobs if j.px == args.only]
        if not jobs:
            print(f"profile {args.profile} 里没有 {args.only} px", file=sys.stderr)
            return 2

    out_dir = Path(args.out)
    charset_dir = Path(args.charset_dir)
    charset_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    # 先把各级字符清单落盘：它既是输入也是门禁依据
    for level, label in ((1, "cjk-3500"), (2, "cjk-6500"), (3, "cjk-8105")):
        levels = tuple(range(1, level + 1))
        cps = sorted({cp for lv in levels for cp in table.get(lv, []) if cp in coverage})
        (charset_dir / f"{label}.txt").write_text(cs.to_text(cps), encoding="utf-8")
        print(f"清单   : {label}.txt  {len(cps)} 字")

    for job in jobs:
        weight = args.weight if args.weight is not None else job.weight
        bpp = args.bpp if args.bpp is not None else job.bpp
        spec = cs.CharsetSpec(
            name=job.name, levels=job.levels, symbols=job.symbols, extra=args.extra
        )

        missing = cs.missing_from_font(spec, coverage, table)
        if missing:
            print(
                f"  ! {job.name}: 源字体缺 {len(missing)} 个字："
                f"{''.join(chr(c) for c in missing[:20])}…",
                file=sys.stderr,
            )
        # --extra 里源字体也没有的字，必须明确报出来，不能静默吞掉
        lost = [c for c in args.extra if ord(c) not in coverage]
        if lost:
            print(
                f"  ! --extra 里这些字源字体没有字形，已跳过：{''.join(lost)}",
                file=sys.stderr,
            )

        built = cs.build(spec, coverage, table)
        cps = built.all()
        print(
            f"\n[{job.name}] {job.px}px / {bpp}bpp / wght {weight} / 行距 {job.leading}  "
            f"→ {len(cps)} 个码位（汉字 {len(built.hanzi)} + 符号 {len(built.symbols)}）"
        )

        rz = Rasterizer(str(font_path), job.px, weight=weight, leading=job.leading)
        glyphs, metrics = rz.render(
            cps, bpp, line_height=args.line_height, base_line=args.base_line
        )

        info = emit(
            name=job.name,
            glyphs=glyphs,
            metrics=metrics,
            bpp=bpp,
            source_note="Noto Sans SC Variable（SIL OFL 1.1）",
            charset_note=spec.describe(),
            cmd_note=f"python tools/build.py --profile {args.profile}",
            out_dir=out_dir,
        )
        info.update(
            {
                "px": job.px,
                "bpp": bpp,
                "weight": weight,
                "line_height": metrics.line_height,
                "base_line": metrics.base_line,
                "codepoints": len(cps),
                "hanzi": len(built.hanzi),
                "symbols": len(built.symbols),
                "levels": list(job.levels),
            }
        )
        print(
            f"  位图 {human(info['bitmap_bytes'])}  字形描述 {human(info['dsc_bytes'])}  "
            f"C 源码 {human(info['source_bytes'])}  行高 {metrics.line_height} 基线 {metrics.base_line}"
            + (f"  裁剪 {metrics.clipped} 字形" if metrics.clipped else "")
        )

        if args.verify:
            t1 = time.time()
            rep = verify_pixels(
                Path(info["c_path"]),
                str(font_path),
                job.px,
                weight,
                job.leading,
                sample=args.verify_sample,
            )
            ok = not rep["mismatches"]
            print(
                f"  校验 {'通过' if ok else '失败'}：{rep['checked']} 个字形逐像素比对，"
                f"{len(rep['mismatches'])} 处不一致（{time.time() - t1:.1f}s）"
            )
            for msg in rep["mismatches"][:10]:
                print(f"    - {msg}")
            info["verified"] = ok
            info["verified_glyphs"] = rep["checked"]
            if not ok:
                results.append(info)
                _write_report(results, args, font_path, time.time() - t0)
                return 1

        results.append(info)

    _write_report(results, args, font_path, time.time() - t0)

    total = sum(r["bitmap_bytes"] + r["dsc_bytes"] for r in results)
    print(f"\n合计 Flash 占用（位图 + 字形描述）：{human(total)}")
    print(f"报告：{ROOT / 'build-report.json'}")
    print(f"耗时：{time.time() - t0:.1f}s")
    return 0


def _write_report(results: list[dict], args, font_path: Path, elapsed: float) -> None:
    report = {
        "profile": args.profile,
        "source_font": str(font_path),
        "elapsed_sec": round(elapsed, 1),
        "fonts": results,
        "total_flash_bytes": sum(r["bitmap_bytes"] + r["dsc_bytes"] for r in results),
    }
    (ROOT / "build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run_check(src_dir: Path) -> int:
    """工程门禁：源码里出现的每个中文，都必须被已生成的字体覆盖。"""
    if not src_dir.exists():
        print(f"目录不存在：{src_dir}", file=sys.stderr)
        return 2
    fonts = sorted((ROOT / "fonts").glob("ap_font_*.c"))
    if not fonts:
        print("还没有生成任何字体，先跑一次 python tools/build.py", file=sys.stderr)
        return 2

    rep = check_project(src_dir, fonts)
    print(f"扫描目录：{src_dir}")
    print(f"字体     ：{', '.join(rep['fonts'])}")
    print(f"非 ASCII 字符：{rep['scanned_chars']} 个")

    if not rep["missing"]:
        print("覆盖检查通过：源码里的中文全部有字形。")
        return 0

    print(f"\n缺字 {len(rep['missing'])} 个（会显示成方块）：", file=sys.stderr)
    for cp in sorted(rep["missing"]):
        where = rep["missing"][cp][0]
        print(f"  {chr(cp)}  U+{cp:04X}  {where[0]}:{where[1]}", file=sys.stderr)
    print(
        "\n处理方式：把缺的字加进 --extra，或确认该字是否真的需要上屏。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
