"""市场新闻：按轮次确定性生成，分档影响股价（可带同板块 / 全市场传导）。

新闻分四档，权重越高越罕见、幅度越大：

- 普通（55%）：±2%～4%，只影响单只股票；
- 重要（30%）：±5%～8%，带动同板块；
- 重大（12%）：±10%～15%，可突破单轮常规涨跌上限，带动同板块；
- 黑天鹅（3%）：±18%～30%，除同板块外还会波及全市场（系数减半）。

影响范围由配置 `news_scope` 控制：stock 只影响单只、sector（默认）按上表、
market 则所有新闻都带动全市场。同板块 / 全市场的折扣系数为 `news_spillover`。
"""

from __future__ import annotations

import random
from typing import Any

from . import seeds
from .config import Config

MAX_NEWS = 20

# 档位表。weight 是相对权重，low/high 是影响幅度区间（百分比，符号由利好/利空决定）
TIERS: tuple[dict[str, Any], ...] = (
    {"key": "normal", "label": "普通", "weight": 55.0, "low": 2.0, "high": 4.0},
    {"key": "major", "label": "重要", "weight": 30.0, "low": 5.0, "high": 8.0},
    {"key": "heavy", "label": "重大", "weight": 12.0, "low": 10.0, "high": 15.0},
    {"key": "blackswan", "label": "黑天鹅", "weight": 3.0, "low": 18.0, "high": 30.0},
)
TIER_BY_KEY = {tier["key"]: tier for tier in TIERS}

NORMAL_POSITIVE = [
    "{name}发布业绩预告，净利润大幅增长。",
    "{name}宣布获得大额订单，机构集体看多。",
    "{name}新产品上线首日售罄，市场情绪高涨。",
    "传{name}即将被纳入本群核心指数，资金抢筹。",
    "{name}与头部平台达成合作，想象空间打开。",
]
NORMAL_NEGATIVE = [
    "{name}被曝产品质量问题，投资者恐慌抛售。",
    "{name}高管减持套现，股价承压。",
    "{name}核心项目延期交付，市场信心受挫。",
    "{name}遭做空机构点名，抛压明显。",
    "{name}财报不及预期，资金加速离场。",
]

MAJOR_POSITIVE = [
    "{name}中标重大项目，市场预期明显改善。",
    "{name}获国资战略入股，资金面得到支撑。",
    "{name}核心专利获批，行业地位进一步巩固。",
    "多家券商上调{name}评级，机构资金持续流入。",
    "行业扶持政策落地，{name}被列为重点受益标的。",
]
MAJOR_NEGATIVE = [
    "{name}收到监管问询函，短期不确定性上升。",
    "{name}核心高管突然离职，市场担忧经营稳定。",
    "{name}大股东质押比例过高，平仓风险引发抛售。",
    "{name}主要客户流失，订单能见度下降。",
    "行业政策收紧，{name}被列入重点整改名单。",
]

HEAVY_POSITIVE = [
    "{name}被行业巨头溢价收购，股价大幅跳涨。",
    "{name}重大资产重组获批，估值体系重塑。",
    "{name}净利润预增三倍以上，机构连夜上调目标价。",
    "{name}拿下独家特许经营权，未来现金流预期大增。",
]
HEAVY_NEGATIVE = [
    "{name}财报被出具保留意见，投资者大举出逃。",
    "{name}遭监管立案调查，多项业务被暂停。",
    "{name}发生债务违约，信用评级被下调。",
    "{name}主要工厂全面停产，交付能力受重创。",
]

SWAN_POSITIVE = [
    "{name}被海外巨头以数倍溢价全面要约收购，股价直接跳空。",
    "{name}核心技术取得颠覆性突破，全行业格局面临改写。",
    "{name}意外获得巨额政府补贴，基本面彻底逆转。",
]
SWAN_NEGATIVE = [
    "{name}财务造假被证监会立案调查，全市场恐慌蔓延。",
    "{name}实控人被采取强制措施，公司经营陷入瘫痪。",
    "{name}资金链断裂无法兑付，供应商集体上门。",
    "{name}被审计机构出具「无法表示意见」，退市风险骤增。",
]

TEXTS = {
    "normal": (NORMAL_POSITIVE, NORMAL_NEGATIVE),
    "major": (MAJOR_POSITIVE, MAJOR_NEGATIVE),
    "heavy": (HEAVY_POSITIVE, HEAVY_NEGATIVE),
    "blackswan": (SWAN_POSITIVE, SWAN_NEGATIVE),
}


def _pick_tier(rng: random.Random) -> dict[str, Any]:
    total = sum(tier["weight"] for tier in TIERS)
    roll = rng.random() * total
    for tier in TIERS:
        roll -= tier["weight"]
        if roll < 0:
            return tier
    return TIERS[0]


def _scale(pct: float, factor: float) -> float:
    value = round(pct * factor, 2)
    return 0.0 if abs(value) < 0.01 else value


def _spread(
    state: dict[str, Any], code: str, pct: float, tier_key: str, cfg: Config
) -> list[list[Any]]:
    """新闻影响清单：[[代码, 涨跌幅], ...]，第一条是被点名的股票。"""
    entries: list[list[Any]] = [[code, pct]]
    if cfg.news_scope == "stock":
        return entries

    others = [
        c for c, s in state["stocks"].items() if c != code and not s.get("delisted")
    ]
    factor = max(0.0, min(1.0, cfg.news_spillover))
    if cfg.news_scope == "market":
        entries.extend([c, _scale(pct, factor)] for c in others)
        return [entry for entry in entries if entry[1]]

    # 默认 sector：普通档只影响单只，重要及以上带动同板块，黑天鹅再波及全市场
    if tier_key == "normal":
        return entries
    sector = seeds.sector_of(code)
    same = [c for c in others if sector and seeds.sector_of(c) == sector]
    entries.extend([c, _scale(pct, factor)] for c in same)
    if tier_key == "blackswan":
        rest = [c for c in others if c not in same]
        entries.extend([c, _scale(pct, factor * 0.5)] for c in rest)
    return [entry for entry in entries if entry[1]]


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
    tier = _pick_tier(rng)
    pos_list, neg_list = TEXTS[tier["key"]]
    template = rng.choice(pos_list if positive else neg_list)
    pct = round(rng.uniform(tier["low"], tier["high"]), 2)
    if not positive:
        pct = -pct

    impacts = _spread(state, code, pct, tier["key"], cfg)
    item = {
        "round": round_no,
        "ts": int(state["epoch"]) + round_no * cfg.round_seconds,
        "code": code,
        "pct": pct,
        "tier": tier["key"],
        "tier_label": tier["label"],
        "impacts": impacts,
        "text": (
            f"【{'利好' if positive else '利空'}·{tier['label']}】"
            + template.format(name=name)
        ),
    }
    news = state.setdefault("news", [])
    news.append(item)
    if len(news) > MAX_NEWS:
        del news[: len(news) - MAX_NEWS]
    return item
