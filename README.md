# AI Passport 中文位图字库

面向 [AI Passport](https://ai-passport.folotoy.cn/)（ESP32-C3 / 8 MB Flash / 无 PSRAM / ST7789P3 240×320 / LVGL 9.5）的中文字库，覆盖《通用规范汉字表》全部 **8105** 字。

不是「再导出一份 lv_font_conv 产物」，而是把官方参考项目里已经踩过的三个坑一次性堵掉：位图打包写错、字符格式选错、子集只覆盖当前文案。

---

## 1. 为什么不用现成工具

官方仓库 `docs/reference/shinku-chen/lvgl-cjk-font-subsets.zh_CN.md` 已经记录了两条硬结论：

| 问题 | 后果 | 本项目的做法 |
| --- | --- | --- |
| `lv_font_conv` 1.5.3 在新版 Node 下写出的位图是坏的 | 整屏噪点，且很容易误判成「字体没选中」 | 直接用 FreeType 栅格化，自己写 LVGL 位图格式 |
| `FORMAT0_FULL` 的 cmap 对稀疏字集传 `NULL` 会崩 | 首次绘制中文标签前就崩在字形查找里 | 只产出 `FORMAT0_TINY`（ASCII）+ `SPARSE_TINY`（其余） |

第三条是自己加的：**子集只覆盖「当前文案」的字体，遇到用户叫「李燚」就会画方框**。所以这里按字号分层，小字号覆盖全表。

---

## 2. 覆盖范围

字符集来自《通用规范汉字表》（2013 年国务院发布），三级分档：

| 级别 | 字数 | 含义 |
| --- | --- | --- |
| 一级 | 3,500 | 常用字 |
| 二级 | 3,000 | 次常用字 |
| 三级 | 1,605 | 人名、地名、专业用字 |
| **合计** | **8,105** | |

符号按「一个 16 px 字体能塞下多少」分三档，不是拍脑袋分的（详见第 6 节）：

- `minimal` 32.9 KB —— 中文标点、全角字符、货币、Latin-1。**只要显示中文就必须有**
- `ui` +16.5 KB —— 箭头、几何图形、方块、类字母、数字形式、CJK 兼容形式
- `full` +44.4 KB —— 带圈数字、数学运算符、制表符、杂项符号

ASCII 可打印区（0x20–0x7E）恒定包含，且用 `FORMAT0_TINY` 连续段，查找零开销。

---

## 3. 成品

已生成并**逐像素校验通过**的字体：

| 字体 | 字号 | 汉字 | 符号 | 位图 | 字形描述 | 行高/基线 | 校验 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `ap_font_12` | 12 px | 8105（全表） | 589（`ui`） | 611.6 KB | 68.7 KB | 14 / 3 | 8789 字形全过 |
| `ap_font_16` | 16 px | 8105（全表） | 421（`minimal`） | 1008.9 KB | 67.4 KB | 20 / 5 | 8621 字形全过 |
| `ap_font_20_core` | 20 px | 3500（一级） | 589（`ui`） | 737.1 KB | 32.7 KB | 24 / 6 | 4184 字形全过 |

**合计 2.47 MB Flash**，只读、不占静态 RAM（LVGL 直接从 Flash 读字形位图）。

精简档 `lite`（12 + 16 px，各 3500 字 + `minimal` 符号）合计 **789.5 KB**，适合 Flash 预算极紧的玩法。

### 为什么没有 24 px

4bpp 下 24 px 的 3500 个汉字就要 966 KB，加任何符号都会突破 LVGL 默认 1 MB 的 `bitmap_index` 上限。240×320 的屏上 20 px 已经是「大标题」。确实需要时用 `--bpp 2`，或打开 `CONFIG_LV_FONT_FMT_TXT_LARGE`。

### 为什么 16 px 只能用 minimal 符号

8105 个汉字在 16 px / 4bpp 下占 971.5 KB。加 `ui` 档符号后是 **1,048,589 字节 —— 比 1 MB 上限多 13 字节**。字符优先于装饰符号，所以 16 px 只保留必备标点；`ui` 档的箭头和几何图形由 12 px 与 20 px 提供。

---

## 4. 快速开始

```bash
# 生成标准档（12/16 全表 + 20 一级），并逐像素回读校验
python tools/build.py --profile standard --verify

# 精简档
python tools/build.py --profile lite --verify

# 只重建某个字号
python tools/build.py --profile standard --only 16 --verify

# 工程门禁：扫描源码里的中文，缺字就让 CI 失败
python tools/build.py --check path/to/main
```

依赖只有 **Python 3 + Pillow**（Pillow 自带 FreeType）。不需要 Node，不需要 fontTools。

源字体默认取 `C:\Windows\Fonts\NotoSansSC-VF.ttf`，可用 `--font` 指向任意 TTF/OTF。可变字体用 `--weight 400/500/700` 取不同字重。

### 输出

```
fonts/ap_font_12.c / .h          直接加进工程编译
charsets/cjk-3500.txt            一级字表（按字表顺序，一行 40 字）
charsets/cjk-6500.txt            一+二级
charsets/cjk-8105.txt            全表
build-report.json                体积与覆盖率报告
```

字符清单**既入库也作为输入**：它让「从更小的变体重建」不会悄悄丢掉另一个变体还在用的字。

---

## 5. 接进 AI Passport 工程

### 5.1 放文件

把 `fonts/*.c` 和 `*.h` 复制到参考项目的 `assets/fonts/`（该目录目前不存在，需要新建），并在素材 README 里登记来源与授权。

### 5.2 编译

在 `main/CMakeLists.txt` 现有的 `idf_component_register(...)` **之后**追加，不要替换原有源文件列表：

```cmake
target_sources(${COMPONENT_LIB} PRIVATE
    "${CMAKE_CURRENT_LIST_DIR}/../assets/fonts/ap_font_12.c"
    "${CMAKE_CURRENT_LIST_DIR}/../assets/fonts/ap_font_16.c"
    "${CMAKE_CURRENT_LIST_DIR}/../assets/fonts/ap_font_20_core.c"
)
```

### 5.3 用

```c
#include "lvgl.h"

LV_FONT_DECLARE(ap_font_12);
LV_FONT_DECLARE(ap_font_16);
LV_FONT_DECLARE(ap_font_20_core);

/* 必须在 LVGL 初始化之后、创建控件之前调用一次。
   描述符是只读的，这里浅拷贝出一份可写的，用来挂 fallback。 */
static lv_font_t s_body_font;

void app_fonts_init(void)
{
    s_body_font = ap_font_16;
    /* 16 px 没有箭头和几何图形，缺字时退到 12 px 取符号。
       注意：两者行高不同（20 vs 14），只适合取单个符号，不要拿来做正文 fallback。 */
    s_body_font.fallback = &ap_font_12;
}

lv_obj_t *app_create_title(lv_obj_t *parent)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_obj_set_style_text_font(label, &ap_font_20_core, LV_PART_MAIN | LV_STATE_DEFAULT);
    lv_label_set_text(label, "AI 通行证");
    return label;
}
```

### 5.4 必须同时满足的四件事

字体文件正确，只是第一层。官方文档 `lvgl-chinese-fonts.zh_CN.md` 第 1 节列了五层，这里按优先级排：

```
CONFIG_LV_TXT_ENC_UTF8=y
CONFIG_LV_USE_FONT_PLACEHOLDER=y     # 开发期保留，让缺字可见而不是变空白
```

1. **源码是 UTF-8**，且不要按字节数截断——截在汉字中间就是乱码，跟字体无关。
2. **控件真正用了这个字体**。设置全局默认字体不会覆盖已经显式指定 Montserrat 的控件。
3. **行高要对齐布局预算**。16 px 字体的行高是 20 px（不是 19）。如果你的页面按固定行数排版，用 `--line-height 19` 重新生成，构建会报出有多少字形底部被裁 1 px。
4. **动态文本要有约定**。覆盖 8105 字不等于覆盖一切，见第 7 节。

---

## 6. 关键实现细节

这些是写错就会整屏错位、而且编译链接都不报错的地方。

### 位图是连续位流，不逐行对齐

`lv_font_get_bitmap_fmt_txt()` 在 `stride == 0` 时，位索引 `i` 跨行连续递增。所以 4bpp 字形的字节数是 `ceil(w*h/2)`，**不是** `ceil(w/2)*h`，高半字节在左。

这条不是猜的——拿官方 `lv_font_montserrat_16.c` 反推验证：157 个字形，连续位流假设 **157/157 吻合**，逐行对齐假设只有 100/157 吻合。

### cmap 只用 TINY 变体

```
ASCII 0x20-0x7E   FORMAT0_TINY   glyph_id = glyph_id_start + (cp - range_start)
其余（分段）       SPARSE_TINY    glyph_id = glyph_id_start + 二分查找(unicode_list, cp - range_start)
```

`unicode_list` 存的是**相对 `range_start` 的偏移**，`uint16` 上限，所以生成器会在跨度超过 0xF000 时自动断成新段。

### 行高与基线是排版参数，不是字体属性

```
line_height = 字号 + 行距（默认 0.1875 em）
base_line   = 核心字符最大下伸 + 剩余空间的一半
```

「核心字符」限定为 ASCII 与真正的汉字（`0x3400-0x4DBF` / `0x4E00-0x9FFF` / `0xF900-0xFAFF`）。不能简单用 `cp >= 0x2E80`——中文标点区里混着 `〱〲` 这类竖排重复符号，它们设计上就占满一整行，会把 16 px 的行高从 19 px 撑到 **31 px**，一屏 4 行的布局直接崩。

超出行框的装饰字形就地裁掉并计数，构建时打印 `裁剪 N 字形`。

### 1 MB 硬上限

LVGL 默认 `LV_FONT_FMT_TXT_LARGE == 0`，`bitmap_index` 只有 20 位，单个字体的位图不能超过 1,048,575 字节。生成器在超限时直接报错并给出两条出路，不会生成一个上机才发现崩的字体。

---

## 7. 已知边界

- **字表里有 196 个字在 Unicode 扩展 B/C/E 区**（`𬉼 𠳐 𬻗 𬇕 𫐄`…，码位 0x20164–0x2CE93），都是一些人名地名用字。源字体（Noto Sans SC）对这 196 个全部有字形，所以一并收进来了。代价是 16 px 字体因此多占约 24 KB——这也是 16 px 位图 1008.9 KB、离 1 MB 上限只剩 1.5% 的原因。如果需要更多余量，把它们剔掉是最省事的一刀（生成器会在超限时明确报错，不会静默产出坏字体）。
- **《通用规范汉字表》不含「龘」（U+9F98）、「羴」（U+7FB4）、「猋」、「驫」**。这不是字库的缺陷，是字表本身的范围。源字体有这些字形，需要时用 `--extra "龘羴"` 加进来。这个结论和官方文档里「U+9F98 刻意未收录」完全对得上。
- **「𠮷」（U+20BB7）源字体本身就没有**。这个字在人名里出现频率不低（𠮷野家之类），但 Noto Sans SC 的 cmap 里查不到，`--extra "𠮷"` 会被明确告警并跳过。要覆盖它必须换源字体（比如全字库版的思源黑体 OTC）。
- **125 个 GB2312 字符不在本字表内**，其中大部分是部首/笔画部件（`亻 冫 刂 勹 匚 卩 宀 屮 巛 丶 丿 亠`）——它们本来就不是独立汉字，字表不收是对的。但也有 `嬲 垅 坶 堀 咭 哜 佧` 这类真字，如果你的界面要用，同样用 `--extra` 补。
- **带重音符号的拉丁大写字母**（À Á Â È É …）有 27 个字形被裁掉 1–2 px 顶部：它们的重音超出字身框，而核心字不需要那么高的行框。实际中文界面里几乎不会用到。
- **覆盖 8105 字 ≠ 覆盖任意 Unicode**。用户输入的姓名如果落在扩展区且不在上述 196 字之内，仍然会显示占位方块——建议在输入侧就做校验，或显示「该字暂不支持」而不是静默丢字。
- **只生成简体**。繁体（`國` / `體`）不在《通用规范汉字表》里。

---

## 8. 验证

```bash
python tools/build.py --profile standard --verify
```

对每个字号做两件事：

1. **逐像素回读**：重新解析生成的 `.c`，按 LVGL 的连续位流规则解出每个字形，与 FreeType 栅格化结果**逐字节**比对。度量（`box_w/box_h/ofs_x/ofs_y/adv_w`）也一起比。
2. **覆盖门禁**：扫描源码里的非 ASCII 字符，断言全部有字形。

当前结果：

```
ap_font_12     8789 个字形逐像素比对，0 处不一致
ap_font_16     8621 个字形逐像素比对，0 处不一致
ap_font_20_core 4184 个字形逐像素比对，0 处不一致

python tools/build.py --check ../work/ai-passport/main
→ 非 ASCII 字符：455 个，覆盖检查通过
```

`demo/passport-240x320@3x.png` 不是用浏览器排的字——它是把 `.c` 里的位图按 LVGL 规则解码后逐字形贴出来的 240×320 界面，跑通这张图等于证明「文件里的字节确实能拼成正确的界面」。

`demo/index.html` 是一个可交互的覆盖率检查页：直接打开就能输入任意文本，实时标红当前字库覆盖不到的字。它读的 `demo/coverage.js` 是从生成的 `.c` 文件里反解出来的码位表，不是另写一份。

---

## 9. 目录

```
tools/
  build.py                入口：profile / 校验 / 门禁
  fontkit/
    sfnt.py               最小 sfnt 解析（cmap / head / OS-2），不依赖 fontTools
    charset.py            三级字表 + 符号分档，与源字体 cmap 求交
    raster.py             FreeType 栅格化 + 连续位流打包 + 行高基线推导
    emit.py               LVGL 9.x C 源码发射（TINY/SPARSE_TINY、1MB 守卫）
    verify.py             逐像素回读校验 + 源码覆盖门禁
charsets/
  通用规范汉字表.txt        原始字表（# 一级字表 / # 二级字表 / # 三级字表）
  cjk-3500/6500/8105.txt   生成的分级清单
fonts/                    生成的 C 源码 + OFL.txt
demo/
  index.html              可交互覆盖率检查页
  coverage.js             从 .c 反解出的码位表（供网页用）
  render_preview.py       用生成的字体渲染真机同像素预览
  passport-240x320@3x.png
  glyph-grid@2x.png
```

---

## 10. 授权

本项目**分层授权**，因为生成出来的位图是字体的衍生作品，不能由我单方面改成 MIT：

| 范围 | 授权 | 说明 |
| --- | --- | --- |
| 生成工具（`tools/`、`demo/`） | **MIT** | 见根目录 `LICENSE`，随便用 |
| 字库产物（`fonts/*.c`、`*.h`） | **SIL OFL 1.1** | 是 Noto Sans SC 的位图衍生作品，必须沿用 OFL |
| 字符表数据（`charsets/`） | 见下 | 公开字符集整理数据 |

- **字符表数据**：`charsets/通用规范汉字表.txt` 取自 [rime-aca/character_set](https://github.com/rime-aca/character_set)，是公开的字符集整理数据；《通用规范汉字表》本身为国务院 2013 年发布的规范性文件。
- **源字体**：Noto Sans SC，SIL Open Font License 1.1，允许商用、允许修改、允许再分发；衍生字体不能单独售卖，且需保留同一授权。`fonts/OFL.txt` 已随附全文。
- **为什么不能全 MIT**：OFL 要求衍生字体继续用 OFL，而位图字库在版权意义上就是字体的衍生作品。把 `fonts/*.c` 标成 MIT 会违反上游授权。
- 若改用其它源字体（MiSans、HarmonyOS Sans、阿里巴巴普惠体等），请自行核对其商用条款——`--font` 换一条路径就能重建，生成链不绑定具体字族。
