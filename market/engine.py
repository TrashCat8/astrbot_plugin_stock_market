"""群内股市核心逻辑：行情同步、交易、限价单、账本。

设计要点：
- 所有金额以「分」为单位存整数，手续费向上取整到分，避免浮点误差。
- 价格本身也是整数，但精度按品种区分：股票 100（分），外汇 1_000_000
  （百万分之一元），统一用 `value_cents(price × 数量)` 换算成金额。
- 真实行情模式（`data_source="real"`）下价格由外部行情快照覆盖，轮次只作为
  挂单有效期与图表横轴的节拍；模拟模式（`simulated`）保留按轮次确定性游走，
  作为离线兜底与单元测试基线。
"""

from __future__ import annotations

import math
import random
import time
from decimal import ROUND_CEILING, Decimal
from typing import Any

from . import news as news_mod
from .config import Config
from .instruments import SCALE_STOCK
from .seeds import SEED_STOCKS

MAX_HISTORY = 240
MAX_TRADES = 200
MAX_SHARES = 10_000_000


class MarketError(Exception):
    """可以直接展示给群成员的业务错误。"""


# --------------------------------------------------------------------------- #
# 格式化 / 价格精度
# --------------------------------------------------------------------------- #


def price_scale(stock: dict[str, Any] | None) -> int:
    """报价精度：股票 100（分），外汇 1_000_000。"""
    if not stock:
        return SCALE_STOCK
    try:
        return max(1, int(stock.get("price_scale") or SCALE_STOCK))
    except (TypeError, ValueError):
        return SCALE_STOCK


def price_digits(stock: dict[str, Any] | None) -> int:
    if not stock:
        return 2
    try:
        return max(0, min(8, int(stock.get("price_digits") or 2)))
    except (TypeError, ValueError):
        return 2


def fmt_price(stock: dict[str, Any] | None, price: int) -> str:
    """按品种精度格式化报价（股票 2 位、外汇 4/6 位）。"""
    return f"{Decimal(int(price)) / price_scale(stock):.{price_digits(stock)}f}"


def value_cents(stock: dict[str, Any] | None, price: int, units: int) -> int:
    """价格 × 数量 → 分。买卖共用一条公式，保证往返结算闭合。"""
    scale = price_scale(stock)
    return int(price) * int(units) * 100 // scale


def unit_label(stock: dict[str, Any] | None) -> str:
    return "单位" if (stock or {}).get("kind") == "fx" else "股"


def currency_of(stock: dict[str, Any] | None) -> str:
    return str((stock or {}).get("currency") or "CNY")


def fmt_money(cents: int, comma: bool = True) -> str:
    value = Decimal(int(cents)) / 100
    return f"{value:,.2f}" if comma else f"{value:.2f}"


def fmt_pct(pct: float) -> str:
    return f"{pct:+.2f}%"


def fmt_time(ts: float | int) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(int(ts) + 8 * 3600))


def calc_fee(amount: int, cfg: Config) -> int:
    if amount <= 0 or cfg.fee_rate <= 0:
        return 0
    value = Decimal(int(amount)) * Decimal(str(cfg.fee_rate))
    return int(value.to_integral_value(rounding=ROUND_CEILING))


# --------------------------------------------------------------------------- #
# 基础状态
# --------------------------------------------------------------------------- #


def current_round(state: dict[str, Any], now: float, cfg: Config) -> int:
    elapsed = max(0.0, float(now) - float(state["epoch"]))
    return int(elapsed // cfg.round_seconds)


def new_state(group: str, now: float, cfg: Config) -> dict[str, Any]:
    stocks: dict[str, dict[str, Any]] = {}
    if cfg.instruments:
        for inst in cfg.instruments:
            price = max(1, int(round(float(inst.ref_price or 1.0) * inst.price_scale)))
            stocks[inst.code] = {
                "code": inst.code,
                "name": inst.name,
                "kind": inst.kind,
                "symbol": inst.symbol,
                "sector": inst.sector,
                "currency": inst.currency,
                "price_scale": inst.price_scale,
                "price_digits": inst.price_digits,
                "price": price,
                "prev": price,
                "history": [[0, price]],
            }
    else:
        # 模拟模式：沿用内置种子（离线兜底与单元测试基线）
        for item in SEED_STOCKS:
            price = int(item["price"])
            stocks[item["code"]] = {
                "code": item["code"],
                "name": item.get("name", ""),
                "kind": "stock",
                "sector": item.get("sector", ""),
                "currency": "CNY",
                "price_scale": SCALE_STOCK,
                "price_digits": 2,
                "price": price,
                "prev": price,
                "history": [[0, price]],
            }
    return {
        "group": group,
        "epoch": int(now),
        "round": 0,
        "stocks": stocks,
        "accounts": {},
        "orders": {},
        "next_order_id": 1,
        "trades": [],
        "news": [],
        "created_at": int(now),
    }


def ensure_instruments(state: dict[str, Any], cfg: Config) -> int:
    """让账本的品种表跟上当前配置（真实行情代码/外汇），返回被清算的品种数。

    切换品种时旧持仓按最后价平仓、挂单撤销，资金回到现金，
    避免升级后持仓因为「找不到行情」而凭空归零。
    """
    if not cfg.instruments:
        return 0
    wanted = {inst.code: inst for inst in cfg.instruments}
    closed = 0
    for code in list(state.get("stocks", {})):
        if code in wanted:
            continue
        stock = state["stocks"][code]
        closed += _close_out(state, stock)
        state["stocks"].pop(code, None)

    for code, inst in wanted.items():
        stock = state["stocks"].get(code)
        if stock is None:
            price = max(1, int(round(float(inst.ref_price or 1.0) * inst.price_scale)))
            state["stocks"][code] = {
                "code": code,
                "name": inst.name,
                "kind": inst.kind,
                "symbol": inst.symbol,
                "sector": inst.sector,
                "currency": inst.currency,
                "price_scale": inst.price_scale,
                "price_digits": inst.price_digits,
                "price": price,
                "prev": price,
                "history": [[int(state.get("round", 0)), price]],
            }
            continue
        stock["kind"] = inst.kind
        stock["symbol"] = inst.symbol
        stock["sector"] = inst.sector
        stock["currency"] = inst.currency
        stock["price_scale"] = inst.price_scale
        stock["price_digits"] = inst.price_digits
        if not stock.get("name"):
            stock["name"] = inst.name
        # 老账本可能带着模拟时代的停牌标记，实时行情下不再适用
        stock.pop("delisted", None)
        stock.pop("delisted_round", None)
        stock.pop("delisted_at", None)
        stock["below_rounds"] = 0
    return closed


def _close_out(state: dict[str, Any], stock: dict[str, Any]) -> int:
    """下线一个品种：撤单、持仓按最后价无手续费平仓，返回涉及的账户数。"""
    code = stock["code"]
    price = int(stock.get("price") or 0)
    for oid, order in list(state.get("orders", {}).items()):
        if order.get("code") == code:
            _refund_order(state, order)
            state["orders"].pop(oid, None)

    holders = 0
    for acc in (state.get("accounts") or {}).values():
        pos = (acc.get("positions") or {}).get(code)
        if pos is None:
            continue
        shares = int(pos.get("shares") or 0)
        if shares > 0 and price > 0:
            amount = value_cents(stock, price, shares)
            debt = int(pos.get("debt") or 0)
            repay = min(debt, amount, int(acc.get("debt") or 0))
            acc["cash"] = int(acc.get("cash", 0)) + amount - repay
            acc["debt"] = max(0, int(acc.get("debt", 0)) - repay)
            acc["realized"] = (
                int(acc.get("realized", 0)) + amount - repay - int(pos.get("cost") or 0)
            )
            notices = acc.setdefault("notices", [])
            notices.append(
                f"🔄 市场切换：{display_label(stock)} 已按 {fmt_price(stock, price)} 结算，"
                f"到账 {fmt_money(amount - repay)} 元。"
            )
            if len(notices) > 5:
                del notices[: len(notices) - 5]
            holders += 1
        acc["positions"].pop(code, None)
    return holders


def advance(
    state: dict[str, Any],
    now: float,
    cfg: Config,
    quotes: dict[str, Any] | None = None,
) -> int:
    """把账本推进到当前时刻，返回本次推进的轮数。

    真实行情模式：价格来自 `quotes`，轮次只做节拍；模拟模式：按轮次确定性游走。
    杠杆欠款计息与强平检查两条路径都挂在这里，因此不需要后台任务。
    """
    target = current_round(state, now, cfg)
    advanced = 0
    if cfg.data_source == "real":
        advanced = max(0, target - int(state.get("round", 0)))
        if target > int(state.get("round", 0)):
            state["round"] = target
        if quotes:
            apply_quotes(state, quotes, now, cfg)
        _process_orders(state, int(state.get("round", 0)), cfg)
        _alert_moves(state, cfg, now)
    else:
        if target > state["round"]:
            start = state["round"] + 1
            if target - start + 1 > cfg.max_catchup:
                # 长期无人使用后不逐个补算，直接跳到最近的窗口，避免卡死
                start = max(1, target - cfg.max_catchup + 1)
                state["round"] = start - 1

            for round_no in range(start, target + 1):
                _apply_round(state, round_no, cfg)
                state["round"] = round_no
            advanced = target - start + 1

    _accrue_interest(state, now, cfg)
    _check_liquidations(state, now, cfg)
    return advanced


def apply_quotes(
    state: dict[str, Any], quotes: dict[str, Any], now: float, cfg: Config
) -> int:
    """把真实行情写进账本：最新价、昨收（涨跌基准）、开高低与走势点。"""
    round_no = int(state.get("round", 0))
    changed = 0
    for code, stock in state.get("stocks", {}).items():
        quote = quotes.get(code)
        if quote is None:
            continue
        scale = price_scale(stock)
        last = int(round(float(getattr(quote, "last", 0.0)) * scale))
        prev = int(round(float(getattr(quote, "prev_close", 0.0)) * scale)) or last
        if last <= 0:
            continue

        if not stock.get("name"):
            stock["name"] = str(getattr(quote, "name", "") or "")
        first_quote = not stock.get("quote_ts")
        stock["price"] = last
        stock["prev"] = prev
        for key, field in (("open", "open"), ("high", "high"), ("low", "low")):
            raw = float(getattr(quote, field, 0.0) or 0.0)
            if raw > 0:
                stock[key] = int(round(raw * scale))
        stock["quote_ts"] = float(getattr(quote, "ts", 0.0) or now)
        stock.pop("delisted", None)

        history = stock.setdefault("history", [])
        if first_quote:
            # 首次拿到真实报价：丢掉建仓时用参考价画的假起点，避免"区间"凭空多出高位
            history[:] = [[round_no, last]]
        elif not history or history[-1][1] != last or history[-1][0] != round_no:
            history.append([round_no, last])
            if len(history) > MAX_HISTORY:
                del history[: len(history) - MAX_HISTORY]
        changed += 1
    return changed


def _alert_moves(state: dict[str, Any], cfg: Config, now: float) -> None:
    """真实行情下把当日异动写成新闻播报，每品种每天只播一次。"""
    threshold = float(getattr(cfg, "alert_pct", 0.0) or 0.0)
    if threshold <= 0:
        return
    day = day_key(now)
    alerted = state.setdefault("alerted", {})
    news = state.setdefault("news", [])
    for code, stock in state.get("stocks", {}).items():
        pct = stock_pct(stock)
        if abs(pct) < threshold:
            continue
        key = f"{code}:{day}"
        if alerted.get(key):
            continue
        alerted[key] = 1
        direction = "上涨" if pct > 0 else "下跌"
        news.append(
            {
                "round": int(state.get("round", 0)),
                "ts": int(now),
                "code": code,
                "pct": round(pct, 2),
                "tier": "real",
                "tier_label": "行情异动",
                "text": (
                    f"【行情异动】{display_name(stock)} 当日{direction} {pct:+.2f}%，"
                    f"现价 {fmt_price(stock, int(stock['price']))}。"
                ),
            }
        )
    if len(news) > news_mod.MAX_NEWS:
        del news[: len(news) - news_mod.MAX_NEWS]
    if len(alerted) > 400:
        for key in list(alerted)[:200]:
            alerted.pop(key, None)


def _apply_round(state: dict[str, Any], round_no: int, cfg: Config) -> None:
    item = news_mod.maybe_generate(state, round_no, cfg)
    impacts: dict[str, float] = {}
    if item:
        # 重大/黑天鹅新闻会同时冲击同板块甚至全市场，impacts 里可能有多个代码
        entries = item.get("impacts") or [[item["code"], item["pct"]]]
        for code, pct in entries:
            stock = state["stocks"].get(code)
            if stock and stock["price"] > 0 and not is_delisted(stock):
                impacts[code] = float(pct)

    limit = cfg.max_change_pct / 100.0
    # 对数空间取对称区间 [-log(1+limit), +log(1+limit)]，几何均值不变（长期无漂移），
    # exp 后正好落在 [prev/(1+limit), prev*(1+limit)]，满足「单轮涨跌 ≤ limit」。
    log_up = math.log1p(limit)
    log_down = -log_up
    up = 1.0 + limit
    down = 1.0 / up
    for code, stock in state["stocks"].items():
        # 已退市的股票停牌，价格凝固在退市价，等管理员复牌
        if is_delisted(stock):
            continue
        rng = random.Random(f"{state['group']}:{code}:{round_no}")
        base = rng.uniform(log_down, log_up)
        pct = impacts.get(code)
        if pct is None:
            delta = max(log_down, min(log_up, base))
            stock_up, stock_down = up, down
        else:
            # 被新闻影响的股票：上限取「常规上限」和「新闻幅度」的较大者，
            # 所以重大/黑天鹅档可以突破 max_change_pct，但不会无限放大；
            # 上下仍按对数对称，避免放大波动带来长期漂移。
            cap_pct = min(cfg.news_max_pct, max(cfg.max_change_pct, abs(pct)))
            cap = cap_pct / 100.0
            cap_log = math.log1p(cap)
            stock_up = 1.0 + cap
            stock_down = 1.0 / stock_up
            delta = base + math.log1p(pct / 100.0)
            delta = max(-cap_log, min(cap_log, delta))

        # 乘数用 exp(δ)（对数正态）而不是 1+δ：后者算术均值虽为 1，几何均值却小于 1，
        # 长期会系统性阴跌（波动拖累），让股票集体沉到低价区。
        prev = int(stock["price"])
        low = max(cfg.min_price, int(math.ceil(prev * stock_down)))
        high = max(low, int(math.floor(prev * stock_up)))
        price = int(round(prev * math.exp(delta)))
        price = max(low, min(high, price))

        stock["prev"] = prev
        stock["price"] = price
        history = stock.setdefault("history", [])
        history.append([round_no, price])
        if len(history) > MAX_HISTORY:
            del history[: len(history) - MAX_HISTORY]

        if cfg.delist_enabled:
            # 低于退市线才累计；中途回到线上就重新计数
            if price < cfg.delist_price:
                stock["below_rounds"] = int(stock.get("below_rounds", 0)) + 1
            else:
                stock["below_rounds"] = 0

    if cfg.delist_enabled:
        for stock in state["stocks"].values():
            if is_delisted(stock):
                continue
            if int(stock.get("below_rounds", 0)) >= cfg.delist_rounds:
                _delist(state, stock, round_no, cfg)

    if cfg.auto_relist:
        # 停牌够久就自动重新上市，免得股票永久消失；管理员随时可以手动提前复牌
        for stock in state["stocks"].values():
            if not is_delisted(stock):
                continue
            if rounds_until_relist(stock, round_no, cfg) > 0:
                continue
            _relist_stock(
                state, stock, auto_relist_price(stock, cfg), round_no, cfg, auto=True
            )

    _process_orders(state, round_no, cfg)


def _delist(
    state: dict[str, Any], stock: dict[str, Any], round_no: int, cfg: Config
) -> dict[str, Any]:
    """退市清盘：撤销该股全部挂单，所有持仓按最后价强制卖出。

    与强平的区别在于这是「公司没了」，之后股票停牌、价格凝固，
    只有管理员用 /股市 复牌 才能恢复交易。
    """
    code = stock["code"]
    price = int(stock["price"])

    cancelled = 0
    for oid, order in list(state.get("orders", {}).items()):
        if order.get("code") != code:
            continue
        _refund_order(state, order)
        state["orders"].pop(oid, None)
        cancelled += 1

    holders = 0
    for user, acc in list((state.get("accounts") or {}).items()):
        pos = position_of(acc, code)
        if pos is None:
            continue
        # 挂单已撤，冻结股回到持仓，这里整仓卖出
        shares = int(pos.get("shares", 0))
        if shares <= 0:
            continue
        pos["frozen"] = 0
        result = _settle_sale(acc, pos, shares, price, cfg, stock)
        _record_trade(state, user, "delist", "sell", code, shares, price, result["fee"])
        holders += 1
        text = (
            f"📉 退市清盘：{display_label(stock)} 连续 {cfg.delist_rounds} 轮低于 "
            f"{fmt_money(cfg.delist_price)} 元，你的 {shares} 股已按 "
            f"{fmt_price(stock, price)} 元强制卖出，到账 {fmt_money(result['proceeds'])} 元。"
        )
        if result["repay"] > 0:
            text += f"其中 {fmt_money(result['repay'])} 元已偿还欠款。"
        left = int(acc.get("debt", 0))
        if left > 0:
            text += f"剩余欠款 {fmt_money(left)} 元，后续收入会自动优先还债。"
        notices = acc.setdefault("notices", [])
        notices.append(text)
        if len(notices) > 5:
            del notices[: len(notices) - 5]

    stock["delisted"] = True
    stock["delisted_round"] = round_no
    stock["delisted_at"] = int(state["epoch"]) + round_no * cfg.round_seconds
    stock["below_rounds"] = 0

    news = state.setdefault("news", [])
    news.append(
        {
            "round": round_no,
            "ts": stock["delisted_at"],
            "code": code,
            "pct": 0.0,
            "text": (
                f"【退市】{display_label(stock)} 连续 {cfg.delist_rounds} 轮低于 "
                f"{fmt_money(cfg.delist_price)} 元，已退市并强制平仓，"
                + (
                    f"停牌 {cfg.relist_after_rounds} 轮后自动复牌。"
                    if cfg.auto_relist
                    else "等待管理员复牌。"
                )
            ),
        }
    )
    if len(news) > news_mod.MAX_NEWS:
        del news[: len(news) - news_mod.MAX_NEWS]

    return {"stock": stock, "price": price, "cancelled": cancelled, "holders": holders}


def rounds_until_relist(stock: dict[str, Any], round_no: int, cfg: Config) -> int:
    """还要停牌多少轮才自动复牌；未退市返回 0。"""
    if not is_delisted(stock):
        return 0
    if not cfg.auto_relist:
        return -1  # 不会自动复牌
    since = round_no - int(stock.get("delisted_round", round_no))
    return max(0, cfg.relist_after_rounds - since)


def auto_relist_price(stock: dict[str, Any], cfg: Config) -> int:
    """自动复牌价 = 退市价 × 倍数，但不低于 relist_price，且必须高于退市线。"""
    base = int(stock["price"])
    price = int(math.ceil(base * cfg.relist_multiplier))
    price = max(price, cfg.relist_price, cfg.min_price)
    return max(price, cfg.delist_price + 1)


def _relist_stock(
    state: dict[str, Any],
    stock: dict[str, Any],
    price: int,
    round_no: int,
    cfg: Config,
    *,
    auto: bool,
) -> dict[str, Any]:
    """把停牌的股票重新上市：价格归位到 price，下一轮开始正常波动。"""
    stock["delisted"] = False
    stock.pop("delisted_round", None)
    stock["relisted_round"] = round_no
    stock["price"] = price
    stock["prev"] = price
    stock["below_rounds"] = 0
    history = stock.setdefault("history", [])
    history.append([round_no, price])
    if len(history) > MAX_HISTORY:
        del history[: len(history) - MAX_HISTORY]

    news = state.setdefault("news", [])
    if auto:
        text = (
            f"【复牌】{display_label(stock)} 停牌 {cfg.relist_after_rounds} 轮后自动重新上市，"
            f"起售价 {fmt_money(price, comma=False)} 元。"
        )
    else:
        text = (
            f"【复牌】{display_label(stock)} 恢复交易，起售价 "
            f"{fmt_money(price, comma=False)} 元。"
        )
    news.append(
        {
            "round": round_no,
            "ts": int(time.time()),
            "code": stock["code"],
            "pct": 0.0,
            "text": text,
        }
    )
    if len(news) > news_mod.MAX_NEWS:
        del news[: len(news) - news_mod.MAX_NEWS]

    return {"stock": stock, "label": display_label(stock), "price": price, "auto": auto}


def relist(
    state: dict[str, Any], key: str, price_text: Any, cfg: Config
) -> dict[str, Any]:
    """管理员手动复牌：解除停牌，从指定价格（默认复牌价）提前恢复交易。"""
    stock = find_stock(state, key)
    if not is_delisted(stock):
        raise MarketError(f"{display_label(stock)} 没有退市，无需复牌。")

    text = str(price_text or "").strip()
    price = _check_price(text) if text else auto_relist_price(stock, cfg)
    if price <= cfg.delist_price:
        raise MarketError(
            f"复牌价必须高于退市线 {fmt_money(cfg.delist_price)} 元，否则下一轮就会重新退市。"
        )

    return _relist_stock(state, stock, price, int(state["round"]), cfg, auto=False)


def _process_orders(state: dict[str, Any], round_no: int, cfg: Config) -> None:
    orders = sorted(state.get("orders", {}).values(), key=lambda o: int(o["id"]))
    for order in orders:
        stock = state["stocks"].get(order["code"])
        if stock is None or is_delisted(stock):
            # 退市时挂单已在 _delist 里撤掉，这里再兜一层，避免停牌价成交
            _refund_order(state, order)
            state["orders"].pop(str(order["id"]), None)
            continue
        if round_no > int(order["expire_round"]):
            _refund_order(state, order)
            state["orders"].pop(str(order["id"]), None)
            continue
        if _is_favorable(order["side"], stock["price"], int(order["limit"])):
            _execute_order(state, order, stock, int(stock["price"]), cfg)


# --------------------------------------------------------------------------- #
# 账户 / 持仓
# --------------------------------------------------------------------------- #


def ensure_account(state: dict[str, Any], user: str, cfg: Config) -> dict[str, Any]:
    accounts = state.setdefault("accounts", {})
    acc = accounts.get(user)
    if acc is None:
        acc = {
            "cash": int(cfg.initial_capital),
            "frozen": 0,
            "positions": {},
            "realized": 0,
            "debt": 0,
            "created_at": int(time.time()),
        }
        accounts[user] = acc
    acc.setdefault("positions", {})
    acc.setdefault("frozen", 0)
    acc.setdefault("realized", 0)
    acc.setdefault("debt", 0)
    return acc


def require_account(state: dict[str, Any], user: str) -> dict[str, Any]:
    acc = state.get("accounts", {}).get(user)
    if acc is None:
        raise MarketError("你还没有开户，请先发送「/股市 开户」。")
    return acc


def position_of(acc: dict[str, Any], code: str, create: bool = False) -> dict[str, int] | None:
    positions = acc.setdefault("positions", {})
    pos = positions.get(code)
    if pos is None:
        if not create:
            return None
        pos = {"shares": 0, "frozen": 0, "cost": 0, "debt": 0}
        positions[code] = pos
    pos.setdefault("shares", 0)
    pos.setdefault("frozen", 0)
    pos.setdefault("cost", 0)
    pos.setdefault("debt", 0)
    return pos


def is_known_code(code: str, cfg: Config | None = None) -> bool:
    key = (code or "").upper()
    if any(item["code"] == key for item in SEED_STOCKS):
        return True
    if cfg is not None and cfg.instruments:
        return any(inst.code == key for inst in cfg.instruments)
    return False


def find_stock(state: dict[str, Any], key: str) -> dict[str, Any]:
    key = (key or "").strip()
    if not key:
        raise MarketError("请提供股票代码，例如 ST001。")
    stock = state["stocks"].get(key.upper())
    if stock is not None:
        return stock
    for candidate in state["stocks"].values():
        if candidate.get("name") and candidate["name"] == key:
            return candidate
    raise MarketError(f"没有找到股票「{key}」。")


def is_delisted(stock: dict[str, Any]) -> bool:
    return bool(stock.get("delisted"))


def require_tradable(stock: dict[str, Any], cfg: Config) -> None:
    if not is_delisted(stock):
        return
    if cfg.auto_relist:
        tail = (
            f"停牌满 {cfg.relist_after_rounds} 轮后会自动复牌，"
            f"管理员也可用「/股市 复牌 {stock['code']}」提前恢复。"
        )
    else:
        tail = f"需管理员用「/股市 复牌 {stock['code']}」恢复。"
    raise MarketError(f"{display_label(stock)} 已退市停牌，无法交易；{tail}")


def stock_pct(stock: dict[str, Any]) -> float:
    prev = int(stock.get("prev") or stock["price"])
    if prev <= 0:
        return 0.0
    return (int(stock["price"]) - prev) / prev * 100


def display_name(stock: dict[str, Any]) -> str:
    return stock.get("name") or stock["code"]


def display_label(stock: dict[str, Any]) -> str:
    """「名称 代码」；名称还没填时只显示代码，避免出现 ST001 ST001。"""
    name = (stock.get("name") or "").strip()
    return f"{name} {stock['code']}" if name else stock["code"]


# --------------------------------------------------------------------------- #
# 交易
# --------------------------------------------------------------------------- #


def _check_shares(shares: Any, stock: dict[str, Any] | None = None) -> int:
    try:
        value = int(str(shares).strip())
    except (TypeError, ValueError):
        raise MarketError(f"{unit_label(stock)}数必须是正整数。")
    if value <= 0:
        raise MarketError(f"{unit_label(stock)}数必须是正整数。")
    if value > MAX_SHARES:
        raise MarketError(f"单笔{unit_label(stock)}数过大。")
    return value


def _check_price(price: Any, stock: dict[str, Any] | None = None) -> int:
    text = str(price).strip()
    try:
        value = Decimal(text)
    except Exception:
        digits = price_digits(stock)
        raise MarketError(f"价格格式不正确，例如 9.50 或 0.041805（最多 {digits} 位小数）。")
    scale = price_scale(stock)
    units = int((value * scale).to_integral_value(rounding=ROUND_CEILING))
    if units <= 0:
        raise MarketError("价格必须大于 0。")
    return units


def _consume_cost(pos: dict[str, int], shares: int) -> int:
    total = int(pos["shares"])
    if total <= 0:
        return 0
    cost_sold = int(pos["cost"]) * shares // total
    pos["cost"] = max(0, int(pos["cost"]) - cost_sold)
    return cost_sold


def _settle_sale(
    acc: dict[str, Any],
    pos: dict[str, Any],
    count: int,
    price: int,
    cfg: Config,
    stock: dict[str, Any] | None = None,
) -> dict[str, int]:
    """按市价卖出 count 单位并结算杠杆欠款，返回本次成交的各项金额。

    杠杆持仓卖出时按数量比例偿还欠款；极端情况下成交额不足以还清，
    差额留在账户欠款里，后续资金自动优先偿还。
    """
    held = int(pos["shares"])
    amount = value_cents(stock, price, count)
    fee = calc_fee(amount, cfg)
    proceeds = amount - fee

    debt_total = int(pos.get("debt", 0))
    if debt_total > 0:
        debt_due = debt_total if count >= held else debt_total * count // held
        debt_due = min(debt_due, debt_total)
    else:
        debt_due = 0

    cost_sold = _consume_cost(pos, count)
    pos["debt"] = debt_total - debt_due
    pos["shares"] = held - count
    if pos["shares"] <= 0:
        pos["shares"] = 0
        pos["frozen"] = 0
        pos["cost"] = 0
        pos["debt"] = 0

    # 签到/补助等收入可能已经提前还掉一部分欠款，因此偿还额不能超过账户实际欠款
    repay = min(debt_due, proceeds, int(acc.get("debt", 0)))
    acc["debt"] = max(0, int(acc.get("debt", 0)) - repay)
    acc["cash"] = int(acc.get("cash", 0)) + proceeds - repay
    pnl = proceeds - debt_due - cost_sold
    acc["realized"] = int(acc.get("realized", 0)) + pnl

    return {
        "amount": amount,
        "fee": fee,
        "proceeds": proceeds,
        "cost_sold": cost_sold,
        "debt_due": debt_due,
        "repay": repay,
        "pnl": pnl,
    }


def _record_trade(
    state: dict[str, Any],
    user: str,
    kind: str,
    side: str,
    code: str,
    shares: int,
    price: int,
    fee: int,
) -> None:
    trades = state.setdefault("trades", [])
    trades.append(
        {
            "ts": int(time.time()),
            "user": user,
            "kind": kind,
            "side": side,
            "code": code,
            "shares": shares,
            "price": price,
            "fee": fee,
        }
    )
    if len(trades) > MAX_TRADES:
        del trades[: len(trades) - MAX_TRADES]


def buy(state: dict[str, Any], user: str, key: str, shares: Any, cfg: Config) -> dict[str, Any]:
    stock = find_stock(state, key)
    require_tradable(stock, cfg)
    count = _check_shares(shares, stock)
    acc = require_account(state, user)

    price = int(stock["price"])
    amount = value_cents(stock, price, count)
    if amount <= 0:
        raise MarketError(
            f"成交金额不足 0.01 元，请增加{unit_label(stock)}数量。"
        )
    fee = calc_fee(amount, cfg)
    total = amount + fee
    if acc["cash"] < total:
        raise MarketError(
            f"可用资金不足：需要 {fmt_money(total)} 元，当前可用 {fmt_money(acc['cash'])} 元。"
        )

    acc["cash"] -= total
    pos = position_of(acc, stock["code"], create=True)
    pos["shares"] += count
    pos["cost"] += total
    _record_trade(state, user, "market", "buy", stock["code"], count, price, fee)

    return {
        "code": stock["code"],
        "name": display_name(stock),
        "label": display_label(stock),
        "shares": count,
        "price": price,
        "price_text": fmt_price(stock, price),
        "currency": currency_of(stock),
        "unit": unit_label(stock),
        "fee": fee,
        "total": total,
        "cash": acc["cash"],
    }


def sell(state: dict[str, Any], user: str, key: str, shares: Any, cfg: Config) -> dict[str, Any]:
    stock = find_stock(state, key)
    require_tradable(stock, cfg)
    count = _check_shares(shares, stock)
    acc = require_account(state, user)

    pos = position_of(acc, stock["code"])
    held = int(pos["shares"]) if pos else 0
    available = held - int(pos["frozen"]) if pos else 0
    if count > available:
        raise MarketError(
            f"可卖数量不足：持有 {held} {unit_label(stock)}，"
            f"可卖 {available} {unit_label(stock)}（不支持做空）。"
        )

    price = int(stock["price"])
    result = _settle_sale(acc, pos, count, price, cfg, stock)
    _record_trade(state, user, "market", "sell", stock["code"], count, price, result["fee"])

    return {
        "code": stock["code"],
        "name": display_name(stock),
        "label": display_label(stock),
        "shares": count,
        "price": price,
        "price_text": fmt_price(stock, price),
        "currency": currency_of(stock),
        "unit": unit_label(stock),
        "fee": result["fee"],
        "proceeds": result["proceeds"],
        "amount": result["amount"],
        "cash": acc["cash"],
        "realized": result["pnl"],
        "repaid": result["repay"],
        "debt": int(acc.get("debt", 0)),
    }


def _parse_leverage(text: Any, cfg: Config) -> float:
    """解析「2」「2x」「2倍」这类杠杆倍数输入。"""
    raw = str(text or "").strip().lower().rstrip("x倍")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise MarketError("杠杆倍数格式不正确，例如「2」表示 2 倍。")
    if value <= 1.0:
        raise MarketError("杠杆倍数必须大于 1，不加杠杆请直接用「/股市 买入」。")
    if value > cfg.max_leverage + 1e-9:
        raise MarketError(f"杠杆倍数不能超过 {cfg.max_leverage:g} 倍。")
    return value


def leverage_buy(
    state: dict[str, Any],
    user: str,
    key: str,
    shares: Any,
    leverage_text: Any,
    cfg: Config,
) -> dict[str, Any]:
    """倍数开仓：只冻结自有保证金，其余由系统垫付并记入账户欠款。

    盈亏按整笔市值计算，所以相当于按倍数放大；欠款按自然日计息，
    权益/市值跌破维持保证金比例时自动强平。
    """
    if not cfg.margin_enabled:
        raise MarketError("本群没有开启杠杆玩法。")
    stock = find_stock(state, key)
    require_tradable(stock, cfg)
    count = _check_shares(shares, stock)
    leverage = _parse_leverage(leverage_text, cfg)
    acc = require_account(state, user)

    debt = int(acc.get("debt", 0))
    if debt > 0:
        raise MarketError(
            f"你还有 {fmt_money(debt)} 元欠款，还清前不能继续融资买入（/股市 还债）。"
        )

    price = int(stock["price"])
    amount = value_cents(stock, price, count)
    if amount <= 0:
        raise MarketError(
            f"成交金额不足 0.01 元，请增加{unit_label(stock)}数量。"
        )
    fee = calc_fee(amount, cfg)
    margin = int(
        (Decimal(amount) / Decimal(str(leverage))).to_integral_value(rounding=ROUND_CEILING)
    )
    borrowed = amount - margin
    if acc["cash"] < margin + fee:
        raise MarketError(
            f"保证金不足：{leverage:g} 倍需要自有资金 {fmt_money(margin)} 元"
            f"（另加手续费 {fmt_money(fee)} 元），当前可用 {fmt_money(acc['cash'])} 元。"
        )

    acc["cash"] -= margin + fee
    acc["debt"] = debt + borrowed
    acc["debt_day"] = day_number(time.time())

    pos = position_of(acc, stock["code"], create=True)
    pos["shares"] += count
    pos["cost"] += margin + fee
    pos["debt"] = int(pos.get("debt", 0)) + borrowed
    _record_trade(state, user, "margin", "buy", stock["code"], count, price, fee)

    return {
        "code": stock["code"],
        "name": display_name(stock),
        "label": display_label(stock),
        "shares": count,
        "price": price,
        "price_text": fmt_price(stock, price),
        "currency": currency_of(stock),
        "unit": unit_label(stock),
        "leverage": leverage,
        "fee": fee,
        "margin": margin,
        "borrowed": borrowed,
        "cash": acc["cash"],
        "debt": int(acc["debt"]),
    }


def _is_favorable(side: str, market_price: int, limit: int) -> bool:
    if side == "buy":
        return market_price <= limit
    return market_price >= limit


def place_order(
    state: dict[str, Any],
    user: str,
    side: str,
    key: str,
    shares: Any,
    price_text: Any,
    cfg: Config,
) -> dict[str, Any]:
    stock = find_stock(state, key)
    require_tradable(stock, cfg)
    count = _check_shares(shares, stock)
    limit = _check_price(price_text, stock)
    acc = require_account(state, user)
    code = stock["code"]

    if side == "buy":
        notional = value_cents(stock, limit, count)
        frozen = notional + calc_fee(notional, cfg)
        if acc["cash"] < frozen:
            raise MarketError(
                f"可用资金不足：需要冻结 {fmt_money(frozen)} 元，当前可用 {fmt_money(acc['cash'])} 元。"
            )
        acc["cash"] -= frozen
        acc["frozen"] += frozen
    else:
        pos = position_of(acc, code)
        held = int(pos["shares"]) if pos else 0
        frozen_shares = int(pos["frozen"]) if pos else 0
        if count > held - frozen_shares:
            raise MarketError(
                f"可卖数量不足：持有 {held} {unit_label(stock)}，"
                f"可卖 {held - frozen_shares} {unit_label(stock)}。"
            )
        pos = position_of(acc, code, create=True)
        pos["frozen"] += count
        frozen = 0

    order_id = int(state.get("next_order_id", 1))
    state["next_order_id"] = order_id + 1
    order = {
        "id": order_id,
        "user": user,
        "side": side,
        "code": code,
        "shares": count,
        "limit": limit,
        "frozen": frozen,
        "created_round": int(state["round"]),
        "expire_round": int(state["round"]) + cfg.order_ttl_rounds,
    }
    state.setdefault("orders", {})[str(order_id)] = order

    executed_at = None
    market_price = int(stock["price"])
    if _is_favorable(side, market_price, limit):
        _execute_order(state, order, stock, market_price, cfg)
        executed_at = market_price

    return {"order": order, "executed_at": executed_at, "stock": stock}


def _execute_order(
    state: dict[str, Any],
    order: dict[str, Any],
    stock: dict[str, Any],
    price: int,
    cfg: Config,
) -> None:
    acc = state["accounts"][order["user"]]
    code = order["code"]
    count = int(order["shares"])
    amount = value_cents(stock, price, count)
    fee = calc_fee(amount, cfg)
    pos = position_of(acc, code, create=True)

    if order["side"] == "buy":
        cost = amount + fee
        refund = max(0, int(order["frozen"]) - cost)
        acc["frozen"] -= int(order["frozen"])
        acc["cash"] += refund
        pos["shares"] += count
        pos["cost"] += cost
    else:
        pos["frozen"] -= count
        if pos["frozen"] < 0:
            pos["frozen"] = 0
        _settle_sale(acc, pos, count, price, cfg, stock)

    state["orders"].pop(str(order["id"]), None)
    _record_trade(state, order["user"], "limit", order["side"], code, count, price, fee)


def _refund_order(state: dict[str, Any], order: dict[str, Any]) -> None:
    acc = state["accounts"].get(order["user"])
    if acc is None:
        return
    if order["side"] == "buy":
        frozen = int(order["frozen"])
        acc["cash"] += frozen
        acc["frozen"] = max(0, int(acc["frozen"]) - frozen)
    else:
        pos = position_of(acc, order["code"])
        if pos:
            pos["frozen"] = max(0, int(pos["frozen"]) - int(order["shares"]))


def cancel_order(state: dict[str, Any], user: str, order_id: Any) -> dict[str, Any]:
    try:
        oid = int(str(order_id).strip())
    except (TypeError, ValueError):
        raise MarketError("请提供挂单编号，例如「/股市 撤单 3」。")
    order = state.get("orders", {}).get(str(oid))
    if order is None:
        raise MarketError("没有找到该挂单，可能已成交或已撤销。")
    if order["user"] != user:
        raise MarketError("只能撤销自己的挂单。")
    _refund_order(state, order)
    state["orders"].pop(str(oid), None)
    return order


# --------------------------------------------------------------------------- #
# 玩法：签到 / 补助 / 排行 / 赌博 / 抢劫
# --------------------------------------------------------------------------- #

# 赌博和抢劫用系统真随机，不参与行情种子，避免被反推
_sysrand = random.SystemRandom()


def day_key(now: float | int) -> str:
    """UTC+8 的日期字符串，用于签到防重。"""
    return time.strftime("%Y%m%d", time.gmtime(int(now) + 8 * 3600))


def day_number(now: float | int) -> int:
    """UTC+8 的「第几天」整数，用于判断是否连续签到。"""
    return (int(now) + 8 * 3600) // 86400


def account_total(state: dict[str, Any], acc: dict[str, Any]) -> int:
    total = int(acc.get("cash", 0)) + int(acc.get("frozen", 0))
    for code, pos in (acc.get("positions") or {}).items():
        shares = int(pos.get("shares", 0))
        if shares <= 0:
            continue
        stock = state["stocks"].get(code)
        if stock:
            total += value_cents(stock, int(stock["price"]), shares)
    return total


def account_net(state: dict[str, Any], acc: dict[str, Any]) -> int:
    """净资产 = 可用 + 冻结 + 持仓市值 − 欠款。"""
    return account_total(state, acc) - int(acc.get("debt", 0))


def _credit(acc: dict[str, Any], amount: int) -> int:
    """入账：有杠杆欠款时优先还债，剩下的才进可用资金。返回本次还掉的金额。"""
    amount = max(0, int(amount))
    debt = int(acc.get("debt", 0))
    if debt <= 0 or amount <= 0:
        acc["cash"] = int(acc.get("cash", 0)) + amount
        return 0
    repay = min(debt, amount)
    acc["debt"] = debt - repay
    acc["cash"] = int(acc.get("cash", 0)) + amount - repay
    return repay


def pop_notices(acc: dict[str, Any]) -> list[str]:
    """取出待展示的强平/计息通知（取完即清，避免每次指令都重复提示）。"""
    notices = acc.pop("notices", None) or []
    return [str(item) for item in notices][:5]


def _accrue_interest(state: dict[str, Any], now: float, cfg: Config) -> None:
    """欠款按 UTC+8 自然日计息，利息并入欠款（不产生负现金）。"""
    if cfg.margin_daily_rate <= 0:
        return
    today = day_number(now)
    rate = Decimal(str(cfg.margin_daily_rate))
    for acc in (state.get("accounts") or {}).values():
        debt = int(acc.get("debt", 0))
        if debt <= 0:
            acc["debt_day"] = today
            continue
        last = int(acc.get("debt_day") or today)
        days = today - last
        if days <= 0:
            continue
        interest = int(
            (Decimal(debt) * rate * days).to_integral_value(rounding=ROUND_CEILING)
        )
        if interest > 0:
            debt += interest
            acc["debt"] = debt
            notices = acc.setdefault("notices", [])
            notices.append(
                f"📈 杠杆欠款计息 +{fmt_money(interest)} 元（{days} 天），当前欠款 {fmt_money(debt)} 元。"
            )
            if len(notices) > 5:
                del notices[: len(notices) - 5]
        acc["debt_day"] = today


def _cancel_sell_orders(state: dict[str, Any], user: str, code: str) -> None:
    """强平前撤掉该持仓上的卖单，把冻结股份放回来。"""
    acc = state["accounts"].get(user)
    pos = (acc or {}).get("positions", {}).get(code)
    for oid, order in list(state.get("orders", {}).items()):
        if order.get("user") != user or order.get("code") != code:
            continue
        if order.get("side") == "sell" and pos is not None:
            pos["frozen"] = max(0, int(pos.get("frozen", 0)) - int(order["shares"]))
        state["orders"].pop(oid, None)


def _check_liquidations(state: dict[str, Any], now: float, cfg: Config) -> None:
    """杠杆持仓的权益/市值跌破维持保证金比例时，按市价全部强平。

    成交额不够还清欠款的，差额留在账户欠款里，后续收入自动优先抵扣。
    """
    for user, acc in (state.get("accounts") or {}).items():
        for code, pos in list((acc.get("positions") or {}).items()):
            debt = int(pos.get("debt", 0))
            shares = int(pos.get("shares", 0))
            if debt <= 0 or shares <= 0:
                continue
            stock = state["stocks"].get(code)
            if stock is None or is_delisted(stock):
                continue
            price = int(stock["price"])
            value = value_cents(stock, price, shares)
            equity = value - debt
            if equity > 0 and equity >= value * cfg.margin_call_ratio:
                continue

            _cancel_sell_orders(state, user, code)
            result = _settle_sale(acc, pos, shares, price, cfg, stock)
            label = display_label(stock)
            text = (
                f"⚠️ 强平：{label} {shares} {unit_label(stock)}已按 "
                f"{fmt_price(stock, price)} 元强制卖出，"
                f"偿还欠款 {fmt_money(result['repay'])} 元。"
            )
            left = int(acc.get("debt", 0))
            if left > 0:
                text += f"剩余欠款 {fmt_money(left)} 元，后续收入会自动优先还债。"
            notices = acc.setdefault("notices", [])
            notices.append(text)
            if len(notices) > 5:
                del notices[: len(notices) - 5]
            _record_trade(state, user, "liquidate", "sell", code, shares, price, result["fee"])


def repay_debt(
    state: dict[str, Any], user: str, amount_text: Any, cfg: Config
) -> dict[str, Any]:
    """用可用资金还杠杆欠款；不填金额表示能还多少还多少。"""
    acc = require_account(state, user)
    debt = int(acc.get("debt", 0))
    if debt <= 0:
        raise MarketError("你没有欠款。")
    cash = int(acc.get("cash", 0))
    text = str(amount_text or "").strip()
    if text:
        amount = _parse_amount(text)
        if amount > cash:
            raise MarketError(f"可用资金不足：当前可用 {fmt_money(cash)} 元。")
    else:
        amount = min(cash, debt)
    if amount <= 0:
        raise MarketError("可用资金为 0，暂时没法还债。")
    pay = min(amount, debt)
    acc["cash"] = cash - pay
    acc["debt"] = debt - pay
    return {
        "amount": pay,
        "cash": acc["cash"],
        "debt": acc["debt"],
    }


def leverage_info(state: dict[str, Any], user: str, cfg: Config) -> dict[str, Any]:
    acc = require_account(state, user)
    debt = int(acc.get("debt", 0))
    rows = []
    for code, pos in sorted((acc.get("positions") or {}).items()):
        shares = int(pos.get("shares", 0))
        pos_debt = int(pos.get("debt", 0))
        if shares <= 0 or pos_debt <= 0:
            continue
        stock = state["stocks"].get(code)
        price = int(stock["price"]) if stock else 0
        value = value_cents(stock, price, shares) if stock else 0
        equity = value - pos_debt
        ratio = (equity / value * 100.0) if value > 0 else 0.0
        keep = max(0.0001, 1.0 - float(cfg.margin_call_ratio))
        scale = price_scale(stock)
        liq_price = int(
            (
                Decimal(pos_debt)
                * Decimal(scale)
                / (Decimal(str(shares * keep)) * Decimal(100))
            ).to_integral_value(rounding=ROUND_CEILING)
        )
        rows.append(
            {
                "code": code,
                "label": display_label(stock) if stock else code,
                "shares": shares,
                "price": price,
                "price_text": fmt_price(stock, price) if stock else "0.00",
                "liq_price_text": fmt_price(stock, liq_price) if stock else "0.00",
                "unit": unit_label(stock),
                "debt": pos_debt,
                "value": value,
                "equity": equity,
                "ratio": ratio,
                "liq_price": liq_price,
            }
        )
    total_assets = account_total(state, acc)
    return {
        "debt": debt,
        "cash": int(acc.get("cash", 0)),
        "total": total_assets,
        "net": total_assets - debt,
        "max_leverage": float(cfg.max_leverage),
        "daily_rate": float(cfg.margin_daily_rate),
        "call_ratio": float(cfg.margin_call_ratio),
        "positions": rows,
    }


def touch_name(state: dict[str, Any], user: str, name: str | None) -> None:
    """把群昵称记进账户，排行榜就不用显示一串 QQ 号。"""
    acc = state.get("accounts", {}).get(user)
    if acc is not None and name:
        acc["name"] = str(name)[:32]


def checkin(
    state: dict[str, Any], user: str, cfg: Config, now: float | None = None
) -> dict[str, Any]:
    now = time.time() if now is None else now
    acc = require_account(state, user)
    day = day_key(now)
    if acc.get("last_checkin") == day:
        raise MarketError("今天已经签到过了，明天再来吧。")

    today = day_number(now)
    streak = int(acc.get("streak") or 0) + 1 if acc.get("last_checkin_day") == today - 1 else 1
    bonus_pct = min(
        float(cfg.max_streak_bonus_pct),
        max(0.0, float(cfg.streak_bonus_pct)) * (streak - 1),
    )
    base = int(cfg.daily_bonus)
    amount = base + int(round(base * bonus_pct / 100.0))

    repaid = _credit(acc, amount)
    acc["last_checkin"] = day
    acc["last_checkin_day"] = today
    acc["streak"] = streak
    return {
        "amount": amount,
        "base": base,
        "bonus_pct": bonus_pct,
        "streak": streak,
        "cash": acc["cash"],
        "repaid": repaid,
        "debt": int(acc.get("debt", 0)),
    }


def relief(
    state: dict[str, Any], user: str, cfg: Config, now: float | None = None
) -> dict[str, Any]:
    now = time.time() if now is None else now
    acc = require_account(state, user)
    total = account_net(state, acc)
    if total >= int(cfg.relief_threshold):
        raise MarketError(
            f"净资产 {fmt_money(total)} 元，还没到补助线"
            f"（低于 {fmt_money(cfg.relief_threshold)} 元才能领）。"
        )
    wait = int(cfg.relief_cooldown_hours) * 3600
    last = float(acc.get("last_relief") or 0)
    if wait and last and now - last < wait:
        remain = max(1, int((wait - (now - last)) // 60) + 1)
        raise MarketError(f"补助冷却中，还需约 {remain} 分钟。")

    repaid = _credit(acc, int(cfg.relief_amount))
    acc["last_relief"] = int(now)
    return {
        "amount": int(cfg.relief_amount),
        "total_before": total,
        "cash": acc["cash"],
        "repaid": repaid,
        "debt": int(acc.get("debt", 0)),
    }


def rank(
    state: dict[str, Any], cfg: Config, limit: int | None = None
) -> list[dict[str, Any]]:
    rows = []
    for user, acc in (state.get("accounts") or {}).items():
        assets = account_total(state, acc)
        cash = int(acc.get("cash", 0))
        frozen = int(acc.get("frozen", 0))
        debt = int(acc.get("debt", 0))
        rows.append(
            {
                "user": user,
                "name": acc.get("name") or user,
                "cash": cash,
                "market_value": assets - cash - frozen,
                "debt": debt,
                "total": assets - debt,
            }
        )
    rows.sort(key=lambda row: (-row["total"], row["user"]))
    size = max(1, int(limit if limit else cfg.rank_size))
    return rows[:size]


def pnl_of(state: dict[str, Any], acc: dict[str, Any]) -> tuple[int, int]:
    """返回 (已实现盈亏, 浮动盈亏)，只统计买卖带来的盈亏。

    签到、补助、give、抢劫、db 这些资金变动不计入，否则盈亏榜会变成「谁签到多」。
    """
    realized = int(acc.get("realized", 0))
    floating = 0
    for code, pos in (acc.get("positions") or {}).items():
        shares = int(pos.get("shares", 0))
        if shares <= 0:
            continue
        stock = state["stocks"].get(code)
        if stock is None:
            continue
        floating += (
            value_cents(stock, int(stock["price"]), shares)
            - int(pos.get("debt", 0))
            - int(pos.get("cost", 0))
        )
    return realized, floating


def profit_rank(
    state: dict[str, Any], cfg: Config, limit: int | None = None
) -> dict[str, list[dict[str, Any]]]:
    rows = []
    for user, acc in (state.get("accounts") or {}).items():
        realized, floating = pnl_of(state, acc)
        total = realized + floating
        if total == 0:
            continue
        rows.append(
            {
                "user": user,
                "name": acc.get("name") or user,
                "realized": realized,
                "floating": floating,
                "total": total,
            }
        )
    size = max(1, int(limit if limit else cfg.rank_size))
    gainers = sorted((row for row in rows if row["total"] > 0), key=lambda row: -row["total"])
    losers = sorted((row for row in rows if row["total"] < 0), key=lambda row: row["total"])
    return {"gainers": gainers[:size], "losers": losers[:size]}


def _parse_amount(text: Any) -> int:
    """把以「元」为单位的输入换算成分。"""
    value = str(text).strip().replace(",", "")
    try:
        amount = Decimal(value)
    except Exception:
        raise MarketError("金额格式不正确，请输入以元为单位的数字，例如 1000、9.50。")
    if amount <= 0:
        raise MarketError("金额必须大于 0。")
    return int((amount * 100).to_integral_value(rounding=ROUND_CEILING))


def gamble(state: dict[str, Any], user: str, amount_text: Any, cfg: Config) -> dict[str, Any]:
    if not cfg.gamble_enabled:
        raise MarketError("本群没有开启赌博玩法。")
    acc = require_account(state, user)
    bet = _parse_amount(amount_text)
    cash = int(acc.get("cash", 0))
    if bet > cash:
        raise MarketError(f"可用资金不足：当前可用 {fmt_money(cash)} 元。")

    if bet == cash:
        # 全仓押注按特殊规则结算：80% 翻倍，20% 归零
        payout = bet * 2 if _sysrand.randint(1, 5) != 1 else 0
    else:
        payout = _sysrand.randint(0, bet * 2)

    acc["cash"] = cash - bet + payout
    log = state.setdefault("gambles", [])
    log.append({"ts": int(time.time()), "user": user, "bet": bet, "payout": payout})
    if len(log) > 100:
        del log[: len(log) - 100]
    return {"bet": bet, "payout": payout, "net": payout - bet, "cash": acc["cash"]}


def rob(
    state: dict[str, Any],
    user: str,
    target: Any,
    cfg: Config,
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    target = str(target or "").strip()
    if not target:
        raise MarketError("请 @ 一个目标，或直接写对方 QQ 号。")
    if target == str(user):
        raise MarketError("不能抢劫自己。")

    acc = require_account(state, user)
    target_acc = state.get("accounts", {}).get(target)
    if target_acc is None:
        raise MarketError("对方还没有开户，没东西可抢。")
    target_cash = int(target_acc.get("cash", 0))
    if target_cash < 1000:
        raise MarketError("目标的可用资金不足 10 元，抢劫不划算。")

    cooldown = int(cfg.rob_cooldown_minutes) * 60
    last = float(acc.get("last_rob") or 0)
    if cooldown and last and now - last < cooldown:
        remain = max(1, int((cooldown - (now - last)) // 60) + 1)
        raise MarketError(f"抢劫冷却中，还需约 {remain} 分钟。")

    # 以双方「可用资金」为基准（元）比较：抢劫只抢现金，持仓市值不参与概率计算。
    # 除数 500：每差 500 元加减 1 个百分点，约 ±1.75 万元才触顶/触底，避免经常贴边。
    my_cash = int(acc.get("cash", 0))
    chance = float(cfg.rob_base_chance) + ((my_cash - target_cash) / 100.0) / 500.0
    chance = max(25.0, min(75.0, chance))
    success = _sysrand.randint(1, 100) <= chance

    low, high = sorted((float(cfg.rob_min_pct), float(cfg.rob_max_pct)))
    pct = _sysrand.uniform(low, high)
    acc["last_rob"] = int(now)

    if success:
        amount = min(target_cash, max(1, int(target_cash * pct / 100.0)))
        target_acc["cash"] = target_cash - amount
        repaid = _credit(acc, amount)
        return {
            "success": True,
            "chance": chance,
            "pct": pct,
            "amount": amount,
            "cash": acc["cash"],
            "repaid": repaid,
            "debt": int(acc.get("debt", 0)),
            "target_cash": target_acc["cash"],
        }

    my_cash = int(acc.get("cash", 0))
    if my_cash <= 0:
        return {
            "success": False,
            "chance": chance,
            "pct": pct,
            "amount": 0,
            "cash": my_cash,
            "target_cash": target_cash,
        }
    amount = min(my_cash, max(1, int(my_cash * pct / 100.0)))
    acc["cash"] = my_cash - amount
    target_acc["cash"] = target_cash + amount
    return {
        "success": False,
        "chance": chance,
        "pct": pct,
        "amount": amount,
        "cash": acc["cash"],
        "target_cash": target_acc["cash"],
    }


# --------------------------------------------------------------------------- #
# 玩法：转账 / 管理员发钱 / 清零
# --------------------------------------------------------------------------- #


def _require_target(
    state: dict[str, Any], user: str, target: Any, allow_self: bool = False
) -> tuple[str, dict[str, Any]]:
    target = str(target or "").strip()
    if not target:
        raise MarketError("请 @ 一个目标，或直接写对方 QQ 号。")
    if not allow_self and target == str(user):
        raise MarketError("不能给自己转账。")
    acc = state.get("accounts", {}).get(target)
    if acc is None:
        raise MarketError("对方还没有开户，让对方先发「/股市 开户」。")
    return target, acc


def transfer(
    state: dict[str, Any], user: str, target: Any, amount_text: Any, cfg: Config
) -> dict[str, Any]:
    target, target_acc = _require_target(state, user, target)
    acc = require_account(state, user)
    amount = _parse_amount(amount_text)
    cash = int(acc.get("cash", 0))
    if amount > cash:
        raise MarketError(f"可用资金不足：当前可用 {fmt_money(cash)} 元。")

    acc["cash"] = cash - amount
    target_repaid = _credit(target_acc, amount)
    return {
        "amount": amount,
        "cash": acc["cash"],
        "target": target,
        "target_name": target_acc.get("name") or target,
        "target_cash": target_acc["cash"],
        "target_repaid": target_repaid,
    }


def give(
    state: dict[str, Any], user: str, target: Any, amount_text: Any, cfg: Config
) -> dict[str, Any]:
    """管理员凭空发钱（发起人账户不变，与积分加分的语义一致）。

    钱是凭空产生的，不是在账户间移动，所以给自己发钱也允许。
    """
    target, target_acc = _require_target(state, user, target, allow_self=True)
    amount = _parse_amount(amount_text)
    target_repaid = _credit(target_acc, amount)
    return {
        "amount": amount,
        "target": target,
        "target_name": target_acc.get("name") or target,
        "target_cash": target_acc["cash"],
        "target_repaid": target_repaid,
    }


def clear_cash(state: dict[str, Any], target: Any) -> dict[str, Any]:
    """管理员把目标可用资金清零，同时撤销其挂单；持仓保持不变。"""
    target = str(target or "").strip()
    if not target:
        raise MarketError("请 @ 一个目标，或直接写对方 QQ 号。")
    acc = state.get("accounts", {}).get(target)
    if acc is None:
        raise MarketError("对方还没有开户。")
    before = int(acc.get("cash", 0))

    acc["cash"] = 0
    acc["frozen"] = 0
    for oid, order in list(state.get("orders", {}).items()):
        if order.get("user") != target:
            continue
        if order.get("side") == "sell":
            pos = position_of(acc, order["code"])
            if pos:
                pos["frozen"] = max(0, int(pos["frozen"]) - int(order["shares"]))
        state["orders"].pop(oid, None)
    return {"before": before, "target": target, "name": acc.get("name") or target}


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #


def quote_page(
    state: dict[str, Any], page: int, cfg: Config, kind: str | None = None
) -> tuple[list[dict[str, Any]], int, int]:
    codes = sorted(
        code
        for code, stock in state["stocks"].items()
        if kind is None or (stock.get("kind") or "stock") == kind
    )
    size = max(1, cfg.page_size)
    pages = max(1, math.ceil(len(codes) / size)) if codes else 1
    page = max(1, min(pages, page))
    chunk = codes[(page - 1) * size : page * size]
    rows = []
    for code in chunk:
        stock = state["stocks"][code]
        rows.append(
            {
                "code": code,
                "name": stock.get("name") or "",
                "price": int(stock["price"]),
                "price_text": fmt_price(stock, int(stock["price"])),
                "pct": stock_pct(stock),
                "delisted": is_delisted(stock),
                "kind": stock.get("kind") or "stock",
                "currency": currency_of(stock),
                "digits": price_digits(stock),
                "price_scale": price_scale(stock),
                "open": stock.get("open"),
                "high": stock.get("high"),
                "low": stock.get("low"),
            }
        )
    return rows, page, pages


def account_summary(state: dict[str, Any], user: str) -> dict[str, Any]:
    acc = require_account(state, user)
    market_value = 0
    cost_total = 0
    rows = []
    for code, pos in sorted(acc.get("positions", {}).items()):
        shares = int(pos.get("shares", 0))
        if shares <= 0:
            continue
        stock = state["stocks"].get(code)
        price = int(stock["price"]) if stock else 0
        value = value_cents(stock, price, shares) if stock else 0
        market_value += value
        cost_total += int(pos.get("cost", 0))
        debt = int(pos.get("debt", 0))
        rows.append(
            {
                "code": code,
                "name": (stock.get("name") or "") if stock else "",
                "label": display_label(stock) if stock else code,
                "shares": shares,
                "frozen": int(pos.get("frozen", 0)),
                "cost": int(pos.get("cost", 0)),
                "debt": debt,
                "price": price,
                "price_text": fmt_price(stock, price) if stock else "0.00",
                "currency": currency_of(stock) if stock else "CNY",
                "kind": (stock or {}).get("kind") or "stock",
                "unit": unit_label(stock),
                "value": value,
                "floating": value - debt - int(pos.get("cost", 0)),
            }
        )

    frozen = int(acc.get("frozen", 0))
    debt_total = int(acc.get("debt", 0))
    total = int(acc["cash"]) + frozen + market_value
    return {
        "cash": int(acc["cash"]),
        "frozen": frozen,
        "market_value": market_value,
        "cost": cost_total,
        "debt": debt_total,
        "floating": market_value - cost_total - sum(row["debt"] for row in rows),
        "realized": int(acc.get("realized", 0)),
        "total": total,
        "net": total - debt_total,
        "positions": rows,
    }


def rename_stock(state: dict[str, Any], key: str, name: str) -> dict[str, Any]:
    stock = find_stock(state, key)
    name = (name or "").strip()
    if not name:
        raise MarketError("请提供名称，例如「/股市 改名 ST001 星辰科技」。")
    if len(name) > 12:
        raise MarketError("名称最多 12 个字。")
    stock["name"] = name
    return stock
