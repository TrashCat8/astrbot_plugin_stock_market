"""市场新闻：按轮次确定性生成，同一轮影响该股票的价格。"""

from __future__ import annotations

import random
from typing import Any

from .config import Config

MAX_NEWS = 20

POSITIVE = [
    "{name}发布业绩预告，净利润大幅增长。",
    "{name}宣布获得大额订单，机构集体看多。",
    "{name}新产品上线首日售罄，市场情绪高涨。",
    "传{name}即将被纳入本群核心指数，资金抢筹。",
    "{name}与头部平台达成合作，想象空间打开。",
]

NEGATIVE = [
    "{name}被曝产品质量问题，投资者恐慌抛售。",
    "{name}高管减持套现，股价承压。",
    "{name}核心项目延期交付，市场信心受挫。",
    "{name}遭做空机构点名，抛压明显。",
    "{name}财报不及预期，资金加速离场。",
]


def maybe_generate(state: dict[str, Any], round_no: int, cfg: Config) -> dict[str, Any] | None:
    """按概率生成一条新闻。用轮次做种子，补算与实时推进结果一致。"""
    if cfg.news_probability <= 0:
        return None
    rng = random.Random(f"{state['group']}:news:{round_no}")
    if rng.random() >= cfg.news_probability:
        return None

    # 退市停牌的股票不再出新闻（价格已凝固，新闻也不会影响它）
    tradable = [c for c, s in state["stocks"].items() if not s.get("delisted")]
    if not tradable:
        return None
    code = rng.choice(tradable)
    stock = state["stocks"][code]
    name = stock.get("name") or code
    positive = rng.random() < 0.5
    template = rng.choice(POSITIVE if positive else NEGATIVE)
    pct = round(rng.uniform(2.0, 8.0), 2)
    if not positive:
        pct = -pct

    item = {
        "round": round_no,
        "ts": int(state["epoch"]) + round_no * cfg.round_seconds,
        "code": code,
        "pct": pct,
        "text": ("【利好】" if positive else "【利空】") + template.format(name=name),
    }
    news = state.setdefault("news", [])
    news.append(item)
    if len(news) > MAX_NEWS:
        del news[: len(news) - MAX_NEWS]
    return item
