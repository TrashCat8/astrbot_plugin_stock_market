"""卡片渲染不可用时的纯文字回退。"""

from __future__ import annotations

from typing import Any, Sequence

from .config import Config
from .engine import fmt_money, fmt_pct


def quote_text(
    rows: Sequence[dict[str, Any]],
    page: int,
    pages: int,
    round_no: int,
    refresh_seconds: int,
) -> str:
    lines = [f"【本群行情 {page}/{pages}】第 {round_no} 轮"]
    for row in rows:
        name = row.get("name") or ""
        label = f"{row['code']} {name}".strip()
        lines.append(f"{label} {fmt_money(row['price'], comma=False)}元 {fmt_pct(row['pct'])}")
    lines.append(f"每 {refresh_seconds} 秒更新")
    return "\n".join(lines)


def position_text(summary: dict[str, Any], round_no: int) -> str:
    lines = [
        f"【持仓】第 {round_no} 轮",
        f"可用 {fmt_money(summary['cash'])} 元 · 冻结 {fmt_money(summary['frozen'])} 元",
        f"股票市值 {fmt_money(summary['market_value'])} 元",
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
        lines.append(
            f"{pos.get('label') or pos['code']}：{pos['shares']}股（冻结 {pos['frozen']}）"
        )
        lines.append(f"  成本 {fmt_money(pos['cost'])} 元 · 浮动盈亏 {fmt_money(pos['floating'])} 元")
        if pos.get("debt"):
            lines.append(f"  杠杆欠款 {fmt_money(pos['debt'])} 元（强平线以上）")
    return "\n".join(lines)


def chart_text(stock: dict[str, Any]) -> str:
    history = stock.get("history") or []
    prices = [int(p) for _, p in history] or [int(stock["price"])]
    return (
        f"【走势 {stock['code']}】最新 {fmt_money(prices[-1], comma=False)} 元\n"
        f"区间最高 {fmt_money(max(prices), comma=False)} 元 · 最低 {fmt_money(min(prices), comma=False)} 元\n"
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


def help_text(cfg: Config) -> str:
    return (
        "【群内模拟股市】全部为虚拟货币，本群独立账户\n"
        "/股市 开户\n"
        "/股市 行情 [页码] · 行情 ST001\n"
        "/股市 买入 ST001 100 · 卖出 ST001 100\n"
        "/股市 融资买入 ST001 100 2 · 杠杆 · 还债\n"
        "/股市 买单 ST001 100 9.50 · 卖单 ST001 100 12.00\n"
        "/股市 挂单 · 撤单 1 · 成交\n"
        "/股市 新闻 · 走势 ST001 · 持仓 [页码]\n"
        "/股市 签到 · 排行 [N] · 盈亏 [N] · 补助\n"
        "/股市 db 1000 · 抢劫 @对方\n"
        "/股市 transfer @对方 1000\n\n"
        f"开户 {fmt_money(cfg.initial_capital)} 元；每 {cfg.round_seconds} 秒更新，"
        f"每轮涨跌 ≤{cfg.max_change_pct:g}%。\n"
        f"买卖手续费各 {cfg.fee_rate * 100:.3f}%（向上取整到分），全天交易，整数股，不支持做空。\n"
        f"杠杆最高 {cfg.max_leverage:g} 倍，欠款日息 {cfg.margin_daily_rate * 100:.3f}%，"
        f"权益/市值低于 {cfg.margin_call_ratio * 100:.0f}% 自动强平；"
        "强平后仍欠的钱保留，签到/补助/抢劫等收入优先还债。\n"
        f"限价单冻结资金/股份，{cfg.order_ttl_hours} 小时失效；达到限价即按当时更优报价全额成交。\n"
        f"签到每天一次（基础 {fmt_money(cfg.daily_bonus)} 元，连续签到有加成）；"
        f"总资产低于 {fmt_money(cfg.relief_threshold)} 元可领补助，{cfg.relief_cooldown_hours} 小时一次。\n"
        "转账免手续费，双方需已开户。\n"
        f"db 是纯随机赌博（下注 X 元，回报 0～2X 均匀随机）；"
        f"抢劫只抢对方可用资金 {cfg.rob_min_pct:g}%～{cfg.rob_max_pct:g}%（成功率按双方可用资金差计算），"
        f"失败会被反抢，{cfg.rob_cooldown_minutes} 分钟冷却。\n"
        "无主动推送，请用挂单、成交、新闻查询结果。"
    )
