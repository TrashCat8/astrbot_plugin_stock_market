"""用 Pillow 本地绘制行情卡片，不依赖外部文转图服务。

可选背景图：把 bg.jpg / bg.png 放进插件目录的 assets/ 下即可自动叠加。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFont

WIDTH = 760

BG = (20, 21, 31)
PANEL = (33, 35, 50)
PANEL_ALT = (40, 43, 62)
TEXT = (238, 240, 246)
MUTED = (152, 158, 175)
DIM = (108, 114, 132)
RED = (255, 96, 96)
GREEN = (64, 208, 148)
ACCENT = (150, 130, 250)
LINE = (58, 62, 84)

_FONT_CANDIDATES = [
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
    ("C:/Windows/Fonts/msyh.ttc", 0),
    ("C:/Windows/Fonts/simhei.ttf", 0),
    ("/System/Library/Fonts/PingFang.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
]
_BOLD_CANDIDATES = [
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
    ("C:/Windows/Fonts/msyhbd.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("C:/Windows/Fonts/msyh.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
]

_FONT_CACHE: dict[tuple[int, bool], Any] = {}
_ASSET_DIR: Path | None = None
_BG_CACHE: dict[tuple[int, int], Image.Image | None] = {}


def set_asset_dir(path: Path | None) -> None:
    global _ASSET_DIR
    _ASSET_DIR = Path(path) if path else None
    _BG_CACHE.clear()


def _font(size: int, bold: bool = False) -> Any:
    key = (size, bold)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    for path, index in _BOLD_CANDIDATES if bold else _FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        try:
            font = ImageFont.truetype(path, size, index=index)
            _FONT_CACHE[key] = font
            return font
        except OSError:
            continue
    font = ImageFont.load_default(size)
    _FONT_CACHE[key] = font
    return font


def _cover(img: Image.Image, width: int, height: int) -> Image.Image:
    ratio = max(width / img.width, height / img.height)
    size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
    resized = img.resize(size, Image.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _background(width: int, height: int) -> Image.Image | None:
    key = (width, height)
    if key in _BG_CACHE:
        return _BG_CACHE[key]
    result: Image.Image | None = None
    if _ASSET_DIR is not None:
        for name in ("bg.jpg", "bg.png", "bg.webp"):
            path = _ASSET_DIR / name
            if not path.exists():
                continue
            try:
                source = Image.open(path).convert("RGB")
                covered = _cover(source, width, height)
                result = Image.blend(covered, Image.new("RGB", (width, height), BG), 0.78)
            except OSError:
                result = None
            break
    _BG_CACHE[key] = result
    return result


def _canvas(height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    background = _background(WIDTH, height)
    img = background.copy() if background else Image.new("RGB", (WIDTH, height), BG)
    return img, ImageDraw.Draw(img)


def _price(cents: int) -> str:
    return f"{Decimal(int(cents)) / 100:.2f}"


def _pct_color(pct: float) -> tuple[int, int, int]:
    if pct > 0:
        return RED
    if pct < 0:
        return GREEN
    return MUTED


def _save(img: Image.Image, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out


# --------------------------------------------------------------------------- #
# 行情卡
# --------------------------------------------------------------------------- #


def render_quote(
    rows: Sequence[dict[str, Any]],
    page: int,
    pages: int,
    round_no: int,
    updated_at: str,
    *,
    refresh_seconds: int,
    fee_rate: float,
    out: Path,
) -> Path:
    row_h = 46
    header_h = 118
    height = 24 + header_h + row_h * len(rows) + 58
    img, draw = _canvas(height)

    draw.text((36, 52), f"【本群行情 {page}/{pages}】", font=_font(29, True), fill=TEXT, anchor="lm")
    draw.text(
        (36, 90),
        f"第 {round_no} 轮 · 每 {refresh_seconds} 秒更新 · {updated_at}",
        font=_font(16),
        fill=MUTED,
        anchor="lm",
    )

    top = header_h + 12
    bottom = top + row_h * len(rows) + 12
    draw.rounded_rectangle((24, top, WIDTH - 24, bottom), radius=18, fill=PANEL)

    head_y = top + 24
    draw.text((44, head_y), "代码", font=_font(15), fill=DIM, anchor="lm")
    draw.text((146, head_y), "名称", font=_font(15), fill=DIM, anchor="lm")
    draw.text((566, head_y), "现价", font=_font(15), fill=DIM, anchor="rm")
    draw.text((716, head_y), "较上轮", font=_font(15), fill=DIM, anchor="rm")

    for index, row in enumerate(rows):
        y = top + 48 + index * row_h
        if index % 2 == 1:
            draw.rounded_rectangle((32, y - row_h // 2, WIDTH - 32, y + row_h // 2), radius=10, fill=PANEL_ALT)
        draw.text((44, y), row["code"], font=_font(19, True), fill=TEXT, anchor="lm")
        name = row.get("name") or "—"
        draw.text((146, y), name, font=_font(19), fill=TEXT, anchor="lm")
        draw.text((566, y), _price(row["price"]), font=_font(21, True), fill=TEXT, anchor="rm")
        pct = float(row.get("pct", 0.0))
        draw.text((716, y), f"{pct:+.2f}%", font=_font(19, True), fill=_pct_color(pct), anchor="rm")

    draw.text(
        (36, height - 34),
        f"红涨绿跌 · 手续费 {fee_rate * 100:.3f}% · 最低 0.01 元",
        font=_font(15),
        fill=DIM,
        anchor="lm",
    )
    return _save(img, out)


# --------------------------------------------------------------------------- #
# 持仓卡
# --------------------------------------------------------------------------- #


def render_position(
    summary: dict[str, Any],
    round_no: int,
    updated_at: str,
    *,
    out: Path,
) -> Path:
    rows = summary["positions"]
    items = [
        ("可用资金", f"{_money(summary['cash'])} 元", TEXT),
        ("冻结资金", f"{_money(summary['frozen'])} 元", TEXT),
        ("股票市值", f"{_money(summary['market_value'])} 元", TEXT),
        ("总资产", f"{_money(summary['total'])} 元", TEXT),
        ("杠杆欠款", f"{_money(summary['debt'])} 元", _pct_color(-summary["debt"])),
        ("净资产", f"{_money(summary['net'])} 元", TEXT),
        (
            "累计已实现盈亏",
            f"{_signed_money(summary['realized'])} 元",
            _pct_color(summary["realized"]),
        ),
    ]
    panel_top = 122
    panel_bottom = panel_top + len(items) * 40 + 16
    list_top = panel_bottom + 22
    rows_top = list_top + 42
    height = rows_top + max(1, len(rows)) * 62 + 26
    img, draw = _canvas(height)

    draw.text((36, 52), "【持仓】", font=_font(29, True), fill=TEXT, anchor="lm")
    draw.text(
        (36, 90),
        f"第 {round_no} 轮 · {updated_at}",
        font=_font(16),
        fill=MUTED,
        anchor="lm",
    )

    draw.rounded_rectangle((24, panel_top, WIDTH - 24, panel_bottom), radius=18, fill=PANEL)

    for index, (label, value, color) in enumerate(items):
        y = panel_top + 30 + index * 40
        draw.text((48, y), label, font=_font(18), fill=MUTED, anchor="lm")
        draw.text((WIDTH - 48, y), value, font=_font(20, True), fill=color, anchor="rm")

    draw.text(
        (36, list_top),
        f"持仓 {len(rows)}",
        font=_font(19, True),
        fill=TEXT,
        anchor="lm",
    )
    draw.text(
        (WIDTH - 36, list_top),
        f"浮动盈亏 {_signed_money(summary['floating'])} 元",
        font=_font(18, True),
        fill=_pct_color(summary["floating"]),
        anchor="rm",
    )

    if not rows:
        draw.text((36, list_top + 52), "暂无持仓，去「/股市 行情」看看。", font=_font(18), fill=DIM, anchor="lm")
        return _save(img, out)

    for index, pos in enumerate(rows):
        y = list_top + 42 + index * 62
        draw.rounded_rectangle((24, y, WIDTH - 24, y + 54), radius=14, fill=PANEL)
        left = pos.get("label") or pos["code"]
        draw.text((42, y + 18), left, font=_font(19, True), fill=TEXT, anchor="lm")
        draw.text(
            (WIDTH - 42, y + 18),
            f"{pos['shares']} 股（冻结 {pos['frozen']}）",
            font=_font(17),
            fill=MUTED,
            anchor="rm",
        )
        draw.text(
            (42, y + 40),
            f"成本 {_money(pos['cost'])} 元 · 市值 {_money(pos['value'])} 元"
            + (f" · 负债 {_money(pos['debt'])} 元" if pos.get("debt") else ""),
            font=_font(16),
            fill=DIM,
            anchor="lm",
        )
        floating = int(pos["floating"])
        draw.text(
            (WIDTH - 42, y + 40),
            f"{_signed_money(floating)} 元 ({floating / pos['cost'] * 100:+.2f}%)" if pos["cost"] else f"{_signed_money(floating)} 元",
            font=_font(16, True),
            fill=_pct_color(floating),
            anchor="rm",
        )

    return _save(img, out)


# --------------------------------------------------------------------------- #
# 资产排行卡
# --------------------------------------------------------------------------- #


def render_rank(
    rows: Sequence[dict[str, Any]],
    round_no: int,
    updated_at: str,
    *,
    out: Path,
) -> Path:
    row_h = 56
    header_h = 118
    top = header_h + 12
    bottom = top + 44 + row_h * max(1, len(rows)) + 12
    height = bottom + 56
    img, draw = _canvas(height)

    draw.text((36, 52), "【本群资产排行】", font=_font(29, True), fill=TEXT, anchor="lm")
    draw.text(
        (36, 90),
        f"第 {round_no} 轮 · {updated_at} · 共 {len(rows)} 人",
        font=_font(16),
        fill=MUTED,
        anchor="lm",
    )

    draw.rounded_rectangle((24, top, WIDTH - 24, bottom), radius=18, fill=PANEL)

    head_y = top + 22
    draw.text((48, head_y), "排名", font=_font(15), fill=DIM, anchor="lm")
    draw.text((108, head_y), "昵称", font=_font(15), fill=DIM, anchor="lm")
    draw.text((580, head_y), "总资产", font=_font(15), fill=DIM, anchor="rm")
    draw.text((716, head_y), "可用资金", font=_font(15), fill=DIM, anchor="rm")

    for index, row in enumerate(rows):
        y = top + 44 + row_h // 2 + index * row_h
        if index % 2 == 1:
            draw.rounded_rectangle(
                (32, y - row_h // 2, WIDTH - 32, y + row_h // 2), radius=10, fill=PANEL_ALT
            )
        color = ACCENT if index < 3 else MUTED
        draw.text((48, y), str(index + 1), font=_font(19, True), fill=color, anchor="lm")
        name = str(row.get("name") or row.get("user") or "")
        if len(name) > 9:
            name = name[:8] + "…"
        draw.text((108, y), name, font=_font(19), fill=TEXT, anchor="lm")
        draw.text((580, y), _money(row["total"]), font=_font(20, True), fill=TEXT, anchor="rm")
        draw.text((716, y), _money(row["cash"]), font=_font(17), fill=MUTED, anchor="rm")

    draw.text(
        (36, height - 26),
        "总资产 = 可用资金 + 冻结资金 + 持仓市值",
        font=_font(15),
        fill=DIM,
        anchor="lm",
    )
    return _save(img, out)


# --------------------------------------------------------------------------- #
# 盈亏榜卡（赚钱榜 / 亏钱榜）
# --------------------------------------------------------------------------- #


def render_profit(
    data: dict[str, list[dict[str, Any]]],
    round_no: int,
    updated_at: str,
    *,
    out: Path,
) -> Path:
    gainers = data["gainers"]
    losers = data["losers"]
    row_h = 62
    header_h = 118
    section_head = 44
    top = header_h + 12
    gain_h = section_head + row_h * max(1, len(gainers)) + 14
    lose_top = top + gain_h + 18
    lose_h = section_head + row_h * max(1, len(losers)) + 14
    bottom = lose_top + lose_h
    height = bottom + 54
    img, draw = _canvas(height)

    draw.text((36, 52), "【本群盈亏榜】", font=_font(29, True), fill=TEXT, anchor="lm")
    draw.text(
        (36, 90),
        f"第 {round_no} 轮 · {updated_at} · 只统计买卖盈亏",
        font=_font(16),
        fill=MUTED,
        anchor="lm",
    )

    def section(title: str, rows: list[dict[str, Any]], panel_top: int, panel_h: int) -> None:
        draw.rounded_rectangle((24, panel_top, WIDTH - 24, panel_top + panel_h), radius=18, fill=PANEL)
        accent = RED if title == "赚钱榜" else GREEN
        draw.text((44, panel_top + 28), title, font=_font(20, True), fill=accent, anchor="lm")
        if not rows:
            draw.text(
                (44, panel_top + section_head + 30),
                "暂无",
                font=_font(17),
                fill=DIM,
                anchor="lm",
            )
            return
        for index, row in enumerate(rows):
            y = panel_top + section_head + row_h // 2 + index * row_h
            if index % 2 == 1:
                draw.rounded_rectangle(
                    (32, y - row_h // 2 + 4, WIDTH - 32, y + row_h // 2 - 4),
                    radius=10,
                    fill=PANEL_ALT,
                )
            draw.text((48, y), str(index + 1), font=_font(19, True), fill=MUTED, anchor="lm")
            name = str(row.get("name") or row.get("user") or "")
            if len(name) > 9:
                name = name[:8] + "…"
            draw.text((96, y - 9), name, font=_font(18, True), fill=TEXT, anchor="lm")
            draw.text(
                (96, y + 14),
                f"已实现 {_signed_money(row['realized'])} · 浮动 {_signed_money(row['floating'])}",
                font=_font(14),
                fill=DIM,
                anchor="lm",
            )
            total = int(row["total"])
            draw.text(
                (WIDTH - 48, y),
                f"{_signed_money(total)} 元",
                font=_font(21, True),
                fill=RED if total > 0 else GREEN,
                anchor="rm",
            )

    section("赚钱榜", gainers, top, gain_h)
    section("亏钱榜", losers, lose_top, lose_h)

    draw.text(
        (36, height - 26),
        "签到 · 补助 · give · 抢劫 · db 的资金变动不计入盈亏",
        font=_font(15),
        fill=DIM,
        anchor="lm",
    )
    return _save(img, out)


def _money(cents: int) -> str:
    return f"{Decimal(int(cents)) / 100:,.2f}"


def _signed_money(cents: int) -> str:
    value = Decimal(int(cents)) / 100
    return f"{value:+,.2f}"


# --------------------------------------------------------------------------- #
# 走势卡
# --------------------------------------------------------------------------- #


def render_chart(stock: dict[str, Any], *, out: Path) -> Path:
    history = [(int(r), int(p)) for r, p in stock.get("history", [])]
    if not history:
        history = [(0, int(stock["price"]))]

    height = 468
    img, draw = _canvas(height)

    name = stock.get("name") or stock["code"]
    draw.text((36, 52), f"【走势 {stock['code']}】", font=_font(28, True), fill=TEXT, anchor="lm")
    prev = history[0][1]
    pct = 0.0 if prev <= 0 else (history[-1][1] - prev) / prev * 100
    draw.text((36, 92), name, font=_font(17), fill=MUTED, anchor="lm")
    draw.text((WIDTH - 36, 92), f"最新 {_price(history[-1][1])} 元", font=_font(20, True), fill=TEXT, anchor="rm")
    draw.text((WIDTH - 36, 120), f"区间 {pct:+.2f}%", font=_font(17, True), fill=_pct_color(pct), anchor="rm")

    left, right, top, bottom = 48, WIDTH - 84, 158, 396
    prices = [p for _, p in history]
    low, high = min(prices), max(prices)
    spread = high - low
    pad = max(1, int(spread * 0.15)) if spread else max(1, high // 20)
    y_low, y_high = low - pad, high + pad
    if y_high <= y_low:
        y_high = y_low + 1

    def map_x(index: int) -> float:
        if len(history) == 1:
            return left
        return left + (right - left) * index / (len(history) - 1)

    def map_y(price: int) -> float:
        return bottom - (bottom - top) * (price - y_low) / (y_high - y_low)

    for step in range(5):
        y = top + (bottom - top) * step / 4
        draw.line((left, y, right, y), fill=LINE, width=1)
        price = y_high - (y_high - y_low) * step / 4
        draw.text((right + 12, y), f"{Decimal(int(price)) / 100:.2f}", font=_font(14), fill=DIM, anchor="lm")

    points = [(map_x(i), map_y(p)) for i, (_, p) in enumerate(history)]
    if len(points) == 1:
        points = [points[0], (right, points[0][1])]

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    polygon = points + [(points[-1][0], bottom), (points[0][0], bottom)]
    odraw.polygon(polygon, fill=(*ACCENT, 54))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)
    draw.line(points, fill=ACCENT, width=3, joint="curve")

    last_x, last_y = points[-1]
    draw.ellipse((last_x - 5, last_y - 5, last_x + 5, last_y + 5), fill=ACCENT)

    draw.text((left, bottom + 22), f"第 {history[0][0]} 轮", font=_font(14), fill=DIM, anchor="lm")
    draw.text((right, bottom + 22), f"第 {history[-1][0]} 轮", font=_font(14), fill=DIM, anchor="rm")
    draw.text(
        (left, height - 18),
        f"共 {len(history)} 个数据点 · 每轮涨跌上限由插件配置决定",
        font=_font(14),
        fill=DIM,
        anchor="lm",
    )
    return _save(img, out)


# --------------------------------------------------------------------------- #
# 帮助卡
# --------------------------------------------------------------------------- #

_HELP_LINES: list[tuple[str, str]] = [
    ("【群内模拟股市】", "title"),
    ("全部为虚拟货币，本群独立账户。", "muted"),
    ("", "gap"),
    ("/股市 开户", "cmd"),
    ("/股市 行情 [页码]     /股市 行情 ST001", "cmd"),
    ("/股市 买入 ST001 100", "cmd"),
    ("/股市 卖出 ST001 100", "cmd"),
    ("/股市 融资买入 ST001 100 2  /股市 杠杆  /股市 还债", "cmd"),
    ("/股市 买单 ST001 100 9.50", "cmd"),
    ("/股市 卖单 ST001 100 12.00", "cmd"),
    ("/股市 挂单  /股市 撤单 1  /股市 成交", "cmd"),
    ("/股市 新闻  /股市 走势 ST001", "cmd"),
    ("/股市 持仓 [页码]", "cmd"),
    ("/股市 签到  /股市 排行 [N]", "cmd"),
    ("/股市 盈亏 [N]  /股市 补助", "cmd"),
    ("/股市 db 1000  /股市 抢劫 @对方", "cmd"),
    ("/股市 transfer @对方 1000", "cmd"),
    ("", "gap"),
    ("开户 {capital} 元；每 {round} 秒更新，每轮涨跌 ≤{max_pct}%，股价不会跌破 {floor} 元。", "text"),
    ("买卖手续费各 {fee}%（向上取整到分），全天交易，整数股，不支持做空借贷。", "text"),
    ("限价单冻结资金/股份，{ttl} 小时失效；达到限价即按当时更优报价全额成交，可撤销未成交单。", "text"),
    ("签到每天一次（基础 {daily} 元，连续签到有加成）；总资产低于 {relief} 元可领补助。", "text"),
    ("db：下注 X 元，回报 0～2X 均匀随机；抢劫只抢对方可用资金，失败会被反抢。", "text"),
    ("杠杆：自有保证金 + 系统垫付，欠款按天计息；权益/市值过低自动强平，", "text"),
    ("强平后仍欠的钱保留，签到/补助/抢劫等收入优先还债。", "text"),
    ("无主动推送，请用挂单、成交、新闻查询结果。", "muted"),
]


def render_help(values: dict[str, str], *, out: Path) -> Path:
    lines = [(text.format(**values) if text else text, kind) for text, kind in _HELP_LINES]
    line_h = 34
    height = 40 + len(lines) * line_h + 30
    img, draw = _canvas(height)
    top = 36
    draw.rounded_rectangle((20, top - 12, WIDTH - 20, height - 20), radius=18, fill=PANEL)

    y = top + 16
    for text, kind in lines:
        if kind == "gap":
            y += 12
            continue
        if kind == "title":
            draw.text((40, y), text, font=_font(26, True), fill=TEXT, anchor="lm")
        elif kind == "cmd":
            draw.text((52, y), text, font=_font(18, True), fill=ACCENT, anchor="lm")
        elif kind == "muted":
            draw.text((40, y), text, font=_font(16), fill=MUTED, anchor="lm")
        else:
            draw.text((40, y), text, font=_font(16), fill=TEXT, anchor="lm")
        y += line_h
    return _save(img, out)
