"""卡片渲染不可用时的纯文字回退。"""

from __future__ import annotations

from typing import Any, Sequence

from .config import Config
from .engine import fmt_money, fmt_pct, fmt_price


def quote_text(
    rows: Sequence[dict[str, Any]],
    page: int,
    pages: int,
    round_no: int,
    refresh_seconds: int,
    title: str = "本群行情",
) -> str:
    lines = [f"【{title} {page}/{pages}】"]
    for row in rows:
        name = row.get("name") or ""
        label = f"{row['code']} {name}".strip()
        price = row.get("price_text") or fmt_price(row, int(row["price"]))
        suffix = "" if row.get("kind") == "fx" else "元"
        if row.get("delisted"):
            lines.append(f"{label} 已停牌（{price}{suffix}）")
            continue
        lines.append(f"{label} {price}{suffix} {fmt_pct(row['pct'])}")
    lines.append(f"第 {round_no} 轮 · 每 {refresh_seconds} 秒刷新")
    return "\n".join(lines)


def position_text(summary: dict[str, Any], round_no: int) -> str:
    lines = [
        f"【持仓】第 {round_no} 轮",
        f"可用 {fmt_money(summary['cash'])} 元 · 冻结 {fmt_money(summary['frozen'])} 元",
        f"持仓市值 {fmt_money(summary['market_value'])} 元",
        f"总资产 {fmt_money(summary['total'])} 元",
        f"杠杆欠款 {fmt_money(summary['debt'])} 元",
        f"净资产 {fmt_money(summary['net'])} 元",
        f"累计已实现盈亏 {fmt_money(summary['realized'])} 元",
        f"浮动盈亏 {fmt_money(summary['floating'])} 元",
    ]
    rows = summary["positions"]
    if not rows:
        lines.append("持仓 0：暂无持仓")
        return "\n".join(lines)
    lines.append(f"持仓 {len(rows)}")
    for pos in rows:
        unit = pos.get("unit") or "股"
        lines.append(
            f"{pos.get('label') or pos['code']}：{pos['shares']} {unit}"
            f"（冻结 {pos['frozen']}）现价 {pos.get('price_text') or ''}"
        )
        lines.append(f"  成本 {fmt_money(pos['cost'])} 元 · 浮动盈亏 {fmt_money(pos['floating'])} 元")
        if pos.get("debt"):
            lines.append(f"  杠杆欠款 {fmt_money(pos['debt'])} 元（强平线以上）")
    return "\n".join(lines)


def chart_text(stock: dict[str, Any]) -> str:
    history = stock.get("history") or []
    prices = [int(p) for _, p in history] or [int(stock["price"])]
    latest = fmt_price(stock, prices[-1])
    return (
        f"【走势 {stock['code']}】最新 {latest}\n"
        f"区间最高 {fmt_price(stock, max(prices))} · 最低 {fmt_price(stock, min(prices))}\n"
        f"共 {len(prices)} 个数据点"
    )


def rank_text(rows: Sequence[dict[str, Any]], round_no: int) -> str:
    lines = [f"【本群资产排行】第 {round_no} 轮"]
    for index, row in enumerate(rows):
        lines.append(
            f"{index + 1}. {row['name']}：{fmt_money(row['total'])} 元"
            f"（可用 {fmt_money(row['cash'])}）"
        )
    lines.append("排名按净资产 = 可用资金 + 冻结资金 + 持仓市值 − 杠杆欠款")
    return "\n".join(lines)


def profit_text(data: dict[str, list[dict[str, Any]]], round_no: int) -> str:
    lines = [f"【本群盈亏榜】第 {round_no} 轮"]

    def block(title: str, rows: list[dict[str, Any]]) -> None:
        lines.append(title)
        if not rows:
            lines.append("  暂无")
            return
        for index, row in enumerate(rows):
            lines.append(
                f"  {index + 1}. {row['name']}：{fmt_money(row['total'])} 元"
                f"（已实现 {fmt_money(row['realized'])} / 浮动 {fmt_money(row['floating'])}）"
            )

    block("赚钱榜", data["gainers"])
    block("亏钱榜", data["losers"])
    lines.append("只统计买卖盈亏，签到/补助/give/抢劫/db 不计入")
    return "\n".join(lines)


def source_rule_text(cfg: Config) -> str:
    """帮助文案里的行情来源说明。"""
    if cfg.data_source == "real":
        return (
            f"腾讯/新浪实时报价，{cfg.refresh_seconds} 秒内复用同一帧；"
            f"单日涨跌 ≥{cfg.alert_pct:g}% 记一条异动新闻。\n"
            "港股/美股按报价数值以元记账，不做汇率换算。"
        )
    return f"本地模拟行情：每 {cfg.round_seconds} 秒一轮，每轮涨跌 ≤{cfg.max_change_pct:g}%。"


def delist_rule_text(cfg: Config) -> str:
    """帮助文案里的退市规则，关闭时给一句说明。"""
    if cfg.data_source == "real":
        return "真实行情模式不设退市：股票停牌/代码失效时沿用最后一个有效报价。"
    if not cfg.delist_enabled:
        return "本群未开启退市机制。"
    span = delist_span_text(cfg)
    first = (
        f"连续 {cfg.delist_rounds} 轮（约 {span}）低于 {fmt_money(cfg.delist_price)} 元即退市，"
        "持仓按最后价强制平仓。"
    )
    if not cfg.auto_relist:
        return first + "之后由管理员复牌。"
    # 换行让帮助卡分成两行，避免单行超宽
    return first + (
        f"\n停牌 {cfg.relist_after_rounds} 轮"
        f"（约 {round_span_text(cfg.relist_after_rounds, cfg.round_seconds)}）后自动复牌，"
        f"复牌价 = 退市价 × {cfg.relist_multiplier:g}；管理员可提前复牌。"
    )


def news_rule_text(cfg: Config) -> str:
    """帮助文案里的新闻规则。"""
    if cfg.data_source == "real":
        return (
            f"真实行情下新闻是异动播报：单日涨跌 ≥{cfg.alert_pct:g}%，每品种每天一条。"
        )
    if cfg.news_probability <= 0:
        return "本群未开启新闻系统。"
    scope = {
        "stock": "新闻只影响被点名的股票",
        "sector": "重要及以上带动同板块，黑天鹅波及全市场",
        "market": "所有新闻都会带动全市场",
    }.get(cfg.news_scope, "重要及以上带动同板块，黑天鹅波及全市场")
    # 拆成两行，避免帮助卡单行超宽被裁掉
    return (
        f"新闻分普通/重要/重大/黑天鹅四档，越高越罕见、幅度越大（最高 {cfg.news_max_pct:g}%）；\n"
        f"{scope}；重大/黑天鹅可突破单轮涨跌上限。"
    )


def round_span_text(rounds: int, round_seconds: int) -> str:
    """把轮数换算成「6 小时 / 30 分钟」这样的口语时长。"""
    seconds = int(rounds) * int(round_seconds)
    hours = seconds / 3600
    if hours >= 1:
        return f"{hours:.1f} 小时".replace(".0 ", " ")
    return f"{seconds // 60} 分钟"


def delist_span_text(cfg: Config) -> str:
    return round_span_text(cfg.delist_rounds, cfg.round_seconds)


def help_text(cfg: Config) -> str:
    return (
        "【群内股市】全部为虚拟资金，本群独立账户\n"
        "/股市 开户\n"
        "/股市 行情 [页码] · 行情 SH600519 · 汇市 [页码]\n"
        "/股市 买入 SH600519 100 · 卖出 SH600519 100\n"
        "/股市 买入 FX_USDCNY 1000（外汇买卖同语法）\n"
        "/股市 融资买入 SH600519 100 2 · 杠杆 · 还债\n"
        "/股市 买单 SH600519 100 1430.00 · 卖单 SH600519 100 1500.00\n"
        "/股市 挂单 · 撤单 1 · 成交\n"
        "/股市 新闻 · 走势 SH600519 · 持仓\n"
        "/股市 签到 · 排行 [N] · 盈亏 [N] · 补助\n"
        "/股市 db 1000 · 抢劫 @对方\n"
        "/股市 transfer @对方 1000\n\n"
        f"开户 {fmt_money(cfg.initial_capital)} 元。\n"
        f"行情：{source_rule_text(cfg)}\n"
        f"新闻：{news_rule_text(cfg)}\n"
        f"退市：{delist_rule_text(cfg)}\n"
        f"买卖手续费各 {cfg.fee_rate * 100:.3f}%（向上取整到分），全天交易，整数单位，不支持做空。\n"
        f"杠杆最高 {cfg.max_leverage:g} 倍，欠款日息 {cfg.margin_daily_rate * 100:.3f}%，"
        f"权益/市值低于 {cfg.margin_call_ratio * 100:.0f}% 自动强平；"
        "强平后仍欠的钱保留，签到/补助/抢劫等收入优先还债。\n"
        f"限价单冻结资金/持仓，{cfg.order_ttl_hours} 小时失效；达到限价即按当时更优报价全额成交。\n"
        f"签到每天一次（基础 {fmt_money(cfg.daily_bonus)} 元，连续签到有加成）；"
        f"总资产低于 {fmt_money(cfg.relief_threshold)} 元可领补助，{cfg.relief_cooldown_hours} 小时一次。\n"
        "转账免手续费，双方需已开户。\n"
        f"db 是纯随机赌博（下注 X 元，回报 0～2X 均匀随机）；"
        f"抢劫只抢对方可用资金 {cfg.rob_min_pct:g}%～{cfg.rob_max_pct:g}%（成功率按双方可用资金差计算），"
        f"失败会被反抢，{cfg.rob_cooldown_minutes} 分钟冷却。\n"
        "无主动推送，请用挂单、成交、新闻查询结果。"
    )
