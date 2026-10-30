"""引擎单测：直接运行 `python tests/test_engine.py`，无需 pytest。"""

from __future__ import annotations

import math
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market import engine  # noqa: E402
from market.config import Config  # noqa: E402

CFG = Config(
    initial_capital=10_000_000,
    round_seconds=300,
    max_change_pct=10.0,
    fee_rate=0.001,
    order_ttl_hours=24,
    max_catchup=500,
    news_probability=0.0,
    page_size=10,
)


def fresh(now: float | None = None) -> dict:
    return engine.new_state("group_1", now or time.time(), CFG)


def test_admin_lists_default_empty() -> None:
    """代码里不得留任何 QQ 号：未配置时两张名单都为空，改名 / give / clear 谁都用不了。"""
    assert Config().market_admins == []
    assert Config().rename_admins == []
    assert Config.from_plugin_config({}).market_admins == []
    assert Config.from_plugin_config({}).rename_admins == []
    assert Config.from_plugin_config({"market_admins": None}).market_admins == []
    assert Config.from_plugin_config({"rename_admins": None}).rename_admins == []
    assert Config.from_plugin_config({"market_admins": [" 10001 ", ""]}).market_admins == ["10001"]
    assert Config.from_plugin_config({"rename_admins": [" 10002 ", ""]}).rename_admins == ["10002"]


def test_fee_rounds_up() -> None:
    assert engine.calc_fee(50_000, CFG) == 50, engine.calc_fee(50_000, CFG)
    assert engine.calc_fee(1, CFG) == 1
    assert engine.calc_fee(0, CFG) == 0


def test_buy_matches_screenshot() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    trade = engine.buy(state, "u1", "ST001", "50", CFG)
    assert trade["price"] == 1000
    assert trade["fee"] == 50
    assert trade["cash"] == 9_949_950, trade["cash"]
    acc = state["accounts"]["u1"]
    pos = acc["positions"]["ST001"]
    assert pos["shares"] == 50
    assert pos["cost"] == 50_050

    summary = engine.account_summary(state, "u1")
    assert summary["market_value"] == 50_000
    assert summary["total"] == 9_949_950 + 50_000
    assert summary["floating"] == -50


def test_cannot_overspend() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    try:
        engine.buy(state, "u1", "ST001", "100000", CFG)
    except engine.MarketError:
        pass
    else:
        raise AssertionError("超额买入应当报错")
    assert state["accounts"]["u1"]["cash"] == 10_000_000


def test_cannot_short() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    try:
        engine.sell(state, "u1", "ST001", "1", CFG)
    except engine.MarketError:
        pass
    else:
        raise AssertionError("无持仓卖出应当报错")


def test_advance_is_deterministic() -> None:
    now = 1_700_000_000.0
    a = fresh(now)
    b = fresh(now)
    engine.advance(a, now + 300 * 8, CFG)
    engine.advance(b, now + 300 * 8, CFG)
    assert a["round"] == 8
    for code in a["stocks"]:
        assert a["stocks"][code]["price"] == b["stocks"][code]["price"], code


def test_round_change_capped() -> None:
    now = 1_700_000_000.0
    state = fresh(now)
    prices = {code: stock["price"] for code, stock in state["stocks"].items()}
    for step in range(1, 40):
        engine.advance(state, now + 300 * step, CFG)
        for code, stock in state["stocks"].items():
            prev = prices[code]
            change = abs(stock["price"] - prev) / prev
            assert change <= 0.1001, f"{code} 单轮涨幅 {change:.4f} 超限"
            assert stock["price"] >= 1
            prices[code] = stock["price"]


def test_catchup_limit() -> None:
    cfg = Config(round_seconds=300, max_catchup=10, news_probability=0.0)
    now = 1_700_000_000.0
    state = engine.new_state("group_1", now, cfg)
    advanced = engine.advance(state, now + 300 * 1000, cfg)
    assert advanced == 10, advanced
    assert state["round"] == 1000


def test_limit_buy_freezes_and_executes() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    state["stocks"]["ST001"]["price"] = 1000

    result = engine.place_order(state, "u1", "buy", "ST001", "100", "9.50", CFG)
    assert result["executed_at"] is None
    order = result["order"]
    frozen = order["frozen"]
    assert frozen == 100 * 950 + 95, frozen
    acc = state["accounts"]["u1"]
    assert acc["frozen"] == frozen
    assert acc["cash"] == 10_000_000 - frozen

    # 跌到限价以下的一轮：按市价成交，冻结差额退回
    state["stocks"]["ST001"]["price"] = 940
    engine._process_orders(state, 1, CFG)
    assert not state["orders"]
    pos = acc["positions"]["ST001"]
    assert pos["shares"] == 100
    assert pos["cost"] == 100 * 940 + 94
    assert acc["frozen"] == 0
    assert acc["cash"] == 10_000_000 - (100 * 940 + 94)


def test_limit_sell_freezes_shares() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    engine.buy(state, "u1", "ST001", "100", CFG)
    acc = state["accounts"]["u1"]

    result = engine.place_order(state, "u1", "sell", "ST001", "100", "11.00", CFG)
    assert result["executed_at"] is None
    pos = acc["positions"]["ST001"]
    assert pos["frozen"] == 100
    try:
        engine.sell(state, "u1", "ST001", "1", CFG)
    except engine.MarketError:
        pass
    else:
        raise AssertionError("已被挂单冻结的股份不应可卖")

    state["stocks"]["ST001"]["price"] = 1100
    engine._process_orders(state, 1, CFG)
    assert pos["shares"] == 0
    assert pos["frozen"] == 0
    assert acc["cash"] > 9_949_950


def test_cancel_refunds() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    before = state["accounts"]["u1"]["cash"]
    result = engine.place_order(state, "u1", "buy", "ST001", "10", "5.00", CFG)
    order_id = result["order"]["id"]
    engine.cancel_order(state, "u1", order_id)
    acc = state["accounts"]["u1"]
    assert acc["cash"] == before
    assert acc["frozen"] == 0
    assert not state["orders"]


def test_orders_expire() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    before = state["accounts"]["u1"]["cash"]
    result = engine.place_order(state, "u1", "buy", "ST001", "10", "5.00", CFG)
    expire = result["order"]["expire_round"]
    engine._process_orders(state, expire + 1, CFG)
    acc = state["accounts"]["u1"]
    assert acc["cash"] == before
    assert acc["frozen"] == 0
    assert not state["orders"]


def test_sell_realized_pnl() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    engine.buy(state, "u1", "ST001", "50", CFG)
    state["stocks"]["ST001"]["price"] = 1200
    trade = engine.sell(state, "u1", "ST001", "50", CFG)
    # 成本 50000+50=50050，卖出 60000-60=59940，盈亏 9890
    assert trade["proceeds"] == 59_940, trade["proceeds"]
    assert trade["realized"] == 9_890, trade["realized"]
    acc = state["accounts"]["u1"]
    assert acc["cash"] == 10_000_000 - 50_050 + 59_940
    assert acc["positions"]["ST001"]["shares"] == 0


def test_leverage_buy_uses_margin() -> None:
    """2 倍开仓只冻结一半自有资金，其余记入账户和持仓欠款。"""
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    trade = engine.leverage_buy(state, "u1", "ST001", "100", "2", CFG)
    assert trade["price"] == 1000
    assert trade["margin"] == 50_000, trade
    assert trade["borrowed"] == 50_000, trade
    assert trade["fee"] == 100, trade
    assert trade["cash"] == 10_000_000 - 50_100, trade
    assert trade["debt"] == 50_000, trade

    acc = state["accounts"]["u1"]
    pos = acc["positions"]["ST001"]
    assert pos["shares"] == 100
    assert pos["cost"] == 50_100
    assert pos["debt"] == 50_000

    summary = engine.account_summary(state, "u1")
    assert summary["market_value"] == 100_000
    assert summary["total"] == 10_049_900
    assert summary["debt"] == 50_000
    assert summary["net"] == 9_999_900
    assert summary["floating"] == -100


def test_leverage_guards() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    for bad in ("1", "2.5", "abc"):
        try:
            engine.leverage_buy(state, "u1", "ST001", "10", bad, CFG)
        except engine.MarketError:
            pass
        else:
            raise AssertionError(f"杠杆 {bad} 应当报错")
    engine.leverage_buy(state, "u1", "ST001", "10", "2", CFG)
    try:
        engine.leverage_buy(state, "u1", "ST001", "10", "2", CFG)
    except engine.MarketError:
        pass
    else:
        raise AssertionError("有欠款时不应允许继续融资")


def test_leverage_interest_daily() -> None:
    """欠款按自然日计息，利息并入欠款。"""
    cfg = Config(round_seconds=10**9, margin_daily_rate=0.0005)
    now = time.time()
    state = engine.new_state("group_1", now, cfg)
    engine.ensure_account(state, "u1", cfg)
    engine.leverage_buy(state, "u1", "ST001", "100", "2", cfg)
    engine.advance(state, now + 2 * 86400, cfg)
    assert state["accounts"]["u1"]["debt"] == 50_050, state["accounts"]["u1"]["debt"]


def test_liquidation_repays_debt() -> None:
    now = time.time()
    state = fresh(now)
    engine.ensure_account(state, "u1", CFG)
    engine.leverage_buy(state, "u1", "ST001", "100", "2", CFG)
    state["stocks"]["ST001"]["price"] = 600  # 市值 60,000，权益 10,000，权益比 16.7%
    engine.advance(state, now, CFG)

    acc = state["accounts"]["u1"]
    assert acc["positions"]["ST001"]["shares"] == 0
    assert acc["debt"] == 0
    # 60,000 − 60 手续费 = 59,940，先还 50,000 欠款
    assert acc["cash"] == 9_949_900 + 9_940, acc["cash"]
    assert acc["realized"] == 59_940 - 50_000 - 50_100
    assert any("强平" in note for note in acc["notices"])


def test_liquidation_keeps_shortfall_then_income_repays() -> None:
    now = time.time()
    state = fresh(now)
    engine.ensure_account(state, "u1", CFG)
    engine.leverage_buy(state, "u1", "ST001", "100", "2", CFG)
    state["stocks"]["ST001"]["price"] = 400  # 市值 40,000，权益为负
    engine.advance(state, now, CFG)

    acc = state["accounts"]["u1"]
    # 40,000 − 40 手续费 = 39,960，不够还 50,000，差额保留为欠款
    assert acc["debt"] == 10_040, acc["debt"]
    assert acc["cash"] == 9_949_900, acc["cash"]

    info = engine.checkin(state, "u1", CFG, now)
    assert info["repaid"] == 10_040, info
    assert acc["debt"] == 0
    assert acc["cash"] == 9_949_900 + 200_000 - 10_040


def test_leverage_sell_repays_proportional_debt() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    engine.leverage_buy(state, "u1", "ST001", "100", "2", CFG)
    state["stocks"]["ST001"]["price"] = 1200
    trade = engine.sell(state, "u1", "ST001", "50", CFG)
    # 卖出 60,000 − 60 = 59,940，按股数偿还一半欠款 25,000
    assert trade["repaid"] == 25_000, trade
    assert trade["debt"] == 25_000, trade
    assert trade["realized"] == 59_940 - 25_000 - 25_050
    acc = state["accounts"]["u1"]
    assert acc["cash"] == 9_949_900 + 59_940 - 25_000
    assert acc["positions"]["ST001"]["debt"] == 25_000
    assert acc["positions"]["ST001"]["cost"] == 25_050


def test_repay_debt_command() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    engine.leverage_buy(state, "u1", "ST001", "10", "2", CFG)
    info = engine.repay_debt(state, "u1", "1000", CFG)
    assert info["amount"] == 5_000, info  # 欠款只有 5,000 分，多还的部分不扣
    assert info["debt"] == 0
    assert state["accounts"]["u1"]["cash"] == 10_000_000 - 5_010 - 5_000
    try:
        engine.repay_debt(state, "u1", "", CFG)
    except engine.MarketError:
        pass
    else:
        raise AssertionError("没有欠款时还债应当报错")


def test_rank_uses_net_assets() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    engine.leverage_buy(state, "u1", "ST001", "100", "2", CFG)
    rows = engine.rank(state, CFG, 10)
    assert rows[0]["debt"] == 50_000
    assert rows[0]["total"] == 9_999_900, rows[0]


def test_news_deterministic() -> None:
    cfg = Config(news_probability=1.0)
    now = 1_700_000_000.0
    a = engine.new_state("group_1", now, cfg)
    b = engine.new_state("group_1", now, cfg)
    engine.advance(a, now + 300 * 20, cfg)
    engine.advance(b, now + 300 * 20, cfg)
    assert [n["text"] for n in a["news"]] == [n["text"] for n in b["news"]]
    assert a["news"] and "【" in a["news"][0]["text"]


def test_quote_pagination() -> None:
    state = fresh()
    rows, page, pages = engine.quote_page(state, 1, CFG)
    assert pages == 1 and page == 1 and len(rows) == 10
    cfg = Config(page_size=4)
    rows, page, pages = engine.quote_page(state, 2, cfg)
    assert pages == 3 and page == 2 and len(rows) == 4


def test_rename() -> None:
    state = fresh()
    stock = engine.rename_stock(state, "ST001", "星辰科技")
    assert stock["name"] == "星辰科技"
    assert engine.find_stock(state, "星辰科技")["code"] == "ST001"


def test_price_walks_without_long_term_drift() -> None:
    """乘数改用 exp(δ) 后对数价格无漂移，长时间跑下来中位数不会整体沉底。"""
    cfg = Config(news_probability=0.0)
    prices: list[int] = []
    for g in range(12):
        state = engine.new_state(f"drift_{g}", 0.0, cfg)
        for round_no in range(1, 601):
            engine._apply_round(state, round_no, cfg)
        prices.extend(int(stock["price"]) for stock in state["stocks"].values())
    prices.sort()
    seeds = sorted(int(item["price"]) for item in engine.SEED_STOCKS)
    mid_end = prices[len(prices) // 2]
    mid_start = seeds[len(seeds) // 2]
    assert 0.6 * mid_start <= mid_end <= 1.6 * mid_start, (mid_start, mid_end)


def test_price_floor_not_stuck() -> None:
    """低于最低价的老数据会被拉回地板，且在地板上仍能继续涨，不会变成新的固定点。"""
    cfg = Config(news_probability=0.0)
    assert cfg.min_price == 10
    state = engine.new_state("floor", 0.0, cfg)
    stock = state["stocks"]["ST003"]
    stock["price"] = 9  # 旧版本里卡死在 0.09 元的数据
    seen = set()
    for round_no in range(1, 201):
        engine._apply_round(state, round_no, cfg)
        price = int(stock["price"])
        assert price >= cfg.min_price, (round_no, price)
        seen.add(price)
    assert max(seen) > cfg.min_price, seen
    assert len(seen) > 3, seen


def test_name_store_shared_across_groups() -> None:
    import asyncio
    import tempfile

    try:
        # storage 按上架规范从 astrbot.api 取 logger，纯离线环境没有 astrbot 就跳过；
        # 该行为在容器冒烟测试（tests/smoke_handlers.py）里仍会被覆盖。
        from market.storage import NameStore
    except ImportError as exc:
        print(f"SKIP  test_name_store_shared_across_groups（需要 AstrBot 环境：{exc}）")
        return

    base = Path(tempfile.mkdtemp(prefix="stock_names_"))
    store = NameStore(base)
    group_a = engine.new_state("group_a", time.time(), CFG)
    group_b = engine.new_state("group_b", time.time(), CFG)

    store.apply(group_a)
    store.apply(group_b)
    assert group_a["stocks"]["ST001"]["name"] == ""

    asyncio.run(store.set_name("ST001", "星辰科技"))
    store.apply(group_a)
    store.apply(group_b)
    assert group_a["stocks"]["ST001"]["name"] == "星辰科技"
    assert group_b["stocks"]["ST001"]["name"] == "星辰科技"

    # 新实例（模拟插件重启）也要能读到同一份名称表
    NameStore(base).apply(group_b)
    assert group_b["stocks"]["ST001"]["name"] == "星辰科技"
    # 其他字段仍然各群独立
    assert group_a["stocks"]["ST001"]["price"] == group_b["stocks"]["ST001"]["price"] == 1000


def test_checkin_once_per_day() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    now = 1_700_000_000.0
    info = engine.checkin(state, "u1", CFG, now)
    assert info["amount"] == CFG.daily_bonus
    assert info["streak"] == 1
    assert state["accounts"]["u1"]["cash"] == 10_000_000 + CFG.daily_bonus
    try:
        engine.checkin(state, "u1", CFG, now + 60)
    except engine.MarketError:
        pass
    else:
        raise AssertionError("同一天不应能重复签到")


def test_checkin_streak_bonus_capped() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    now = 1_700_000_000.0
    info = {}
    for day in range(8):
        info = engine.checkin(state, "u1", CFG, now + day * 86400)
    assert info["streak"] == 8, info
    # 10% × 7 天 = 70%，应被 max_streak_bonus_pct(50%) 截断
    assert info["bonus_pct"] == 50.0, info
    assert info["amount"] == CFG.daily_bonus + CFG.daily_bonus // 2, info

    # 断签一天后从 1 重新计算
    later = engine.checkin(state, "u1", CFG, now + 10 * 86400)
    assert later["streak"] == 1 and later["bonus_pct"] == 0.0, later


def test_relief_threshold_and_cooldown() -> None:
    state = fresh()
    acc = engine.ensure_account(state, "u1", CFG)
    now = 1_700_000_000.0
    try:
        engine.relief(state, "u1", CFG, now)
    except engine.MarketError:
        pass
    else:
        raise AssertionError("资产充足时不应发补助")

    acc["cash"] = 100_000
    info = engine.relief(state, "u1", CFG, now)
    assert info["amount"] == CFG.relief_amount
    assert acc["cash"] == 100_000 + CFG.relief_amount

    acc["cash"] = 0
    try:
        engine.relief(state, "u1", CFG, now + 3600)
    except engine.MarketError as exc:
        assert "冷却" in str(exc), exc
    else:
        raise AssertionError("冷却期内不应再发补助")


def test_rank_orders_by_total() -> None:
    state = fresh()
    a = engine.ensure_account(state, "a", CFG)
    b = engine.ensure_account(state, "b", CFG)
    a["name"] = "甲"
    b["name"] = "乙"
    a["cash"] = 5_000_000
    b["cash"] = 8_000_000
    engine.buy(state, "b", "ST001", 1000, CFG)
    rows = engine.rank(state, CFG, 10)
    assert [row["user"] for row in rows] == ["b", "a"], rows
    assert rows[0]["name"] == "乙"
    assert rows[0]["total"] == 7_999_000, rows[0]
    assert engine.rank(state, CFG, 1) == rows[:1]


def test_profit_rank_splits_gainers_and_losers() -> None:
    state = fresh()
    engine.ensure_account(state, "a", CFG)
    engine.ensure_account(state, "b", CFG)
    engine.ensure_account(state, "c", CFG)
    engine.buy(state, "a", "ST001", 100, CFG)
    engine.buy(state, "b", "ST001", 100, CFG)

    state["stocks"]["ST001"]["price"] = 900
    engine.sell(state, "b", "ST001", 100, CFG)  # 亏损卖出
    state["stocks"]["ST001"]["price"] = 1200     # a 继续持有，浮盈
    data = engine.profit_rank(state, CFG, 10)

    assert [row["user"] for row in data["gainers"]] == ["a"], data
    assert [row["user"] for row in data["losers"]] == ["b"], data
    # a：100 股 × (1200-1000) 减去买入手续费 100 分
    assert data["gainers"][0]["total"] == 19_900, data["gainers"][0]
    assert data["gainers"][0]["floating"] == 19_900
    # b：成本 100,100，卖出 90,000-90=89,910
    assert data["losers"][0]["total"] == -10_190, data["losers"][0]

    # 签到 / 赌博带来的资金变化不计入盈亏，0 盈亏的人不进榜
    engine.checkin(state, "c", CFG, 1_700_000_000.0)
    engine.gamble(state, "c", "10", CFG)
    data = engine.profit_rank(state, CFG, 10)
    assert all(row["user"] != "c" for row in data["gainers"] + data["losers"]), data


def test_gamble_all_in_rule() -> None:
    state = fresh()
    acc = engine.ensure_account(state, "u1", CFG)
    for _ in range(30):
        acc["cash"] = 100_000  # 1000 元全仓
        info = engine.gamble(state, "u1", "1000", CFG)
        assert info["payout"] in (0, 200_000), info
        assert info["cash"] in (0, 200_000), info


def test_gamble_partial_bet_range() -> None:
    state = fresh()
    acc = engine.ensure_account(state, "u1", CFG)
    acc["cash"] = 5_000_000
    for _ in range(50):
        before = acc["cash"]
        info = engine.gamble(state, "u1", "100", CFG)
        assert 0 <= info["payout"] <= 20_000, info
        assert info["cash"] == before - 10_000 + info["payout"], info


def test_gamble_rejects_overspend() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    try:
        engine.gamble(state, "u1", "9999999", CFG)
    except engine.MarketError:
        pass
    else:
        raise AssertionError("下注超过可用资金应当报错")


def test_rob_guards() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    engine.ensure_account(state, "u2", CFG)
    now = 1_700_000_000.0
    try:
        engine.rob(state, "u1", "u1", CFG, now)
    except engine.MarketError as exc:
        assert "自己" in str(exc), exc
    else:
        raise AssertionError("不能抢自己")

    try:
        engine.rob(state, "u1", "nobody", CFG, now)
    except engine.MarketError as exc:
        assert "开户" in str(exc), exc
    else:
        raise AssertionError("未开户目标应报错")

    state["accounts"]["u2"]["cash"] = 500  # 5 元
    try:
        engine.rob(state, "u1", "u2", CFG, now)
    except engine.MarketError as exc:
        assert "10 元" in str(exc), exc
    else:
        raise AssertionError("目标资金不足 10 元应拒绝")


def test_rob_chance_uses_available_cash() -> None:
    """成功率按双方「可用资金」差计算（每 500 元 1 个百分点），持仓市值不参与。"""
    state = fresh()
    a = engine.ensure_account(state, "u1", CFG)
    b = engine.ensure_account(state, "u2", CFG)
    a["cash"], b["cash"] = 1_000_000, 1_500_000  # 1 万 / 1.5 万元
    # u1 持仓市值 1 万元：按总资产算是 2 万 > 1.5 万（旧口径会顶到 75%），
    # 按可用资金算则是 1 万 < 1.5 万，应为 40 - 10 = 30%
    a["positions"]["ST001"] = {"shares": 1000, "cost": 1_000_000}
    now = 1_700_000_000.0
    a["last_rob"] = 0
    info = engine.rob(state, "u1", "u2", CFG, now)
    expected = max(25.0, min(75.0, CFG.rob_base_chance + ((1_000_000 - 1_500_000) / 100.0) / 500.0))
    assert info["chance"] == expected, info
    assert info["chance"] == 30.0, info


def test_rob_amount_range_and_cooldown() -> None:
    state = fresh()
    a = engine.ensure_account(state, "u1", CFG)
    b = engine.ensure_account(state, "u2", CFG)
    b["cash"] = 10_000_000
    a["cash"] = 1_000_000
    now = 1_700_000_000.0
    for step in range(60):
        a["last_rob"] = 0
        before_a, before_b = a["cash"], b["cash"]
        info = engine.rob(state, "u1", "u2", CFG, now + step * 3600)
        base = before_b if info["success"] else before_a
        moved = abs(info["cash"] - before_a)
        assert moved == info["amount"], info
        assert 4.9 <= moved / base * 100 <= 20.1, info
        # 冷却：紧接着再抢一次应被拒绝
        try:
            engine.rob(state, "u1", "u2", CFG, now + step * 3600 + 1)
        except engine.MarketError as exc:
            assert "冷却" in str(exc), exc
        else:
            raise AssertionError("冷却期内不应允许再次抢劫")


def test_transfer_moves_cash() -> None:
    state = fresh()
    a = engine.ensure_account(state, "u1", CFG)
    b = engine.ensure_account(state, "u2", CFG)
    info = engine.transfer(state, "u1", "u2", "1000", CFG)
    assert info["amount"] == 100_000
    assert a["cash"] == 10_000_000 - 100_000
    assert b["cash"] == 10_000_000 + 100_000
    assert info["cash"] == a["cash"] and info["target_cash"] == b["cash"]


def test_transfer_guards() -> None:
    state = fresh()
    engine.ensure_account(state, "u1", CFG)
    for target, keyword in (("u1", "自己"), ("nobody", "开户")):
        try:
            engine.transfer(state, "u1", target, "10", CFG)
        except engine.MarketError as exc:
            assert keyword in str(exc), exc
        else:
            raise AssertionError(f"目标 {target} 应当被拒绝")
    try:
        engine.transfer(state, "u1", "u2", "10", CFG)
    except engine.MarketError as exc:
        assert "开户" in str(exc), exc
    else:
        raise AssertionError("未开户目标应当被拒绝")

    engine.ensure_account(state, "u2", CFG)
    try:
        engine.transfer(state, "u1", "u2", "99999999", CFG)
    except engine.MarketError as exc:
        assert "不足" in str(exc), exc
    else:
        raise AssertionError("超额转账应当报错")
    try:
        engine.transfer(state, "u1", "u2", "-5", CFG)
    except engine.MarketError:
        pass
    else:
        raise AssertionError("负数金额应当报错")


def test_give_does_not_charge_sender() -> None:
    state = fresh()
    a = engine.ensure_account(state, "u1", CFG)
    b = engine.ensure_account(state, "u2", CFG)
    info = engine.give(state, "u1", "u2", "5000", CFG)
    assert info["amount"] == 500_000
    assert a["cash"] == CFG.initial_capital
    assert b["cash"] == CFG.initial_capital + 500_000


def test_give_allows_self() -> None:
    state = fresh()
    a = engine.ensure_account(state, "u1", CFG)
    info = engine.give(state, "u1", "u1", "1000", CFG)
    assert info["amount"] == 100_000
    assert a["cash"] == CFG.initial_capital + 100_000


def test_clear_cash_keeps_positions() -> None:
    state = fresh()
    acc = engine.ensure_account(state, "u1", CFG)
    engine.buy(state, "u1", "ST001", 50, CFG)
    engine.place_order(state, "u1", "sell", "ST001", 20, "999.00", CFG)
    before = acc["cash"]
    info = engine.clear_cash(state, "u1")
    assert info["before"] == before
    assert acc["cash"] == 0 and acc["frozen"] == 0
    assert acc["positions"]["ST001"]["shares"] == 50
    assert acc["positions"]["ST001"]["frozen"] == 0
    assert not state["orders"]


def test_delist_config_mapping() -> None:
    """退市参数有默认值，脏数据回落；金额从元换算成「分」。"""
    cfg = Config.from_plugin_config({})
    assert cfg.delist_enabled is True
    assert cfg.delist_price == 50 and cfg.delist_rounds == 72 and cfg.relist_price == 100
    cfg = Config.from_plugin_config(
        {"delist_enabled": False, "delist_price": 0.8, "delist_rounds": 12, "relist_price": "2.5"}
    )
    assert cfg.delist_enabled is False
    assert cfg.delist_price == 80 and cfg.delist_rounds == 12 and cfg.relist_price == 250
    cfg = Config.from_plugin_config(
        {"delist_price": "脏数据", "delist_rounds": None, "relist_price": "x"}
    )
    assert cfg.delist_price == 50 and cfg.delist_rounds == 72 and cfg.relist_price == 100


def test_delist_triggers_and_force_closes() -> None:
    """连续 N 轮低于退市线即退市：所有持仓按最后价强制平仓，行情停牌。"""
    cfg = replace(CFG, delist_rounds=2, delist_price=50)
    now = 1_000_000.0
    state = engine.new_state("group_1", now, cfg)
    acc = engine.ensure_account(state, "u1", cfg)
    engine.buy(state, "u1", "ST001", 10, cfg)
    stock = state["stocks"]["ST001"]
    stock["price"] = 40
    stock["prev"] = 40

    engine.advance(state, now + cfg.round_seconds, cfg)
    assert not engine.is_delisted(stock), "只跌了 1 轮，不应退市"
    engine.advance(state, now + cfg.round_seconds * 2, cfg)

    assert engine.is_delisted(stock), "连续 2 轮低于 0.50 元应退市"
    pos = acc["positions"]["ST001"]
    assert pos["shares"] == 0 and pos["frozen"] == 0 and pos["cost"] == 0
    assert acc["cash"] > cfg.initial_capital - 10 * 1000, acc["cash"]
    assert any("退市" in item["text"] for item in state["news"])
    rows, _, _ = engine.quote_page(state, 1, cfg)
    row = next(r for r in rows if r["code"] == "ST001")
    assert row["delisted"] is True
    # 停牌后价格不再变动
    price = int(stock["price"])
    engine.advance(state, now + cfg.round_seconds * 20, cfg)
    assert int(stock["price"]) == price


def test_delist_cancels_orders_and_refunds() -> None:
    """退市时撤掉该股全部挂单，冻结股份/资金一并退回。"""
    cfg = replace(CFG, delist_rounds=1, delist_price=50)
    now = 1_000_000.0
    state = engine.new_state("group_1", now, cfg)
    acc = engine.ensure_account(state, "u1", cfg)
    engine.buy(state, "u1", "ST001", 100, cfg)
    engine.place_order(state, "u1", "sell", "ST001", 60, "20.00", cfg)
    engine.place_order(state, "u1", "buy", "ST002", 10, "9.00", cfg)
    assert len(state["orders"]) == 2, state["orders"]
    frozen_before = int(acc["frozen"])
    assert frozen_before > 0

    stock = state["stocks"]["ST001"]
    stock["price"] = 30
    stock["prev"] = 30
    engine.advance(state, now + cfg.round_seconds, cfg)

    assert engine.is_delisted(stock)
    # 只有该股的挂单被撤，别的股票挂单不受影响
    left = list(state["orders"].values())
    assert len(left) == 1 and left[0]["code"] == "ST002", left
    pos = acc["positions"]["ST001"]
    assert pos["shares"] == 0 and pos["frozen"] == 0
    # 冻结股份随强平一起卖出，冻结资金（ST002 买单）继续冻结
    assert acc["frozen"] == frozen_before
    # 100 股按退市时的最后价全部卖出，扣手续费后到账
    price = int(stock["price"])
    assert price < cfg.delist_price, price
    proceeds = price * 100 - max(1, math.ceil(price * 100 * cfg.fee_rate))
    assert acc["cash"] == cfg.initial_capital - 100_100 - 9_009 + proceeds, acc["cash"]


def test_delist_blocks_trading_until_relist() -> None:
    """退市股票禁止买卖/杠杆/挂单，管理员复牌后恢复。"""
    cfg = replace(CFG, delist_rounds=1, delist_price=50)
    now = 1_000_000.0
    state = engine.new_state("group_1", now, cfg)
    engine.ensure_account(state, "u1", cfg)
    stock = state["stocks"]["ST001"]
    stock["price"] = 30
    stock["prev"] = 30
    engine.advance(state, now + cfg.round_seconds, cfg)
    assert engine.is_delisted(stock)

    for call in (
        lambda: engine.buy(state, "u1", "ST001", 10, cfg),
        lambda: engine.sell(state, "u1", "ST001", 10, cfg),
        lambda: engine.leverage_buy(state, "u1", "ST001", 10, "2", cfg),
        lambda: engine.place_order(state, "u1", "buy", "ST001", 10, "5.00", cfg),
    ):
        try:
            call()
        except engine.MarketError as exc:
            assert "退市" in str(exc), exc
        else:
            raise AssertionError("退市股票不应允许交易")

    info = engine.relist(state, "ST001", "", cfg)
    assert info["price"] == cfg.relist_price == 100
    assert not engine.is_delisted(stock)
    assert engine.stock_pct(stock) == 0
    engine.buy(state, "u1", "ST001", 10, cfg)
    assert state["accounts"]["u1"]["positions"]["ST001"]["shares"] == 10
    assert any("复牌" in item["text"] for item in state["news"])


def test_relist_guards() -> None:
    """未退市的股票不能复牌；复牌价必须高于退市线；显式价格生效。"""
    cfg = replace(CFG, delist_rounds=1, delist_price=50, relist_price=100)
    now = 1_000_000.0
    state = engine.new_state("group_1", now, cfg)
    try:
        engine.relist(state, "ST001", "", cfg)
    except engine.MarketError as exc:
        assert "没有退市" in str(exc), exc
    else:
        raise AssertionError("没退市不该允许复牌")

    stock = state["stocks"]["ST001"]
    stock["delisted"] = True
    for bad in ("0.50", "0.30"):
        try:
            engine.relist(state, "ST001", bad, cfg)
        except engine.MarketError as exc:
            assert "退市线" in str(exc), exc
        else:
            raise AssertionError(f"复牌价 {bad} 不该被接受")

    info = engine.relist(state, "ST001", "3.00", cfg)
    assert info["price"] == 300
    assert int(stock["price"]) == 300 and int(stock["prev"]) == 300
    assert not engine.is_delisted(stock)


def test_delist_disabled_keeps_price() -> None:
    """关闭退市机制后，低价股也只会贴着最低价走，不会被退市。"""
    cfg = replace(CFG, delist_enabled=False, delist_rounds=1, delist_price=5000)
    now = 1_000_000.0
    state = engine.new_state("group_1", now, cfg)
    stock = state["stocks"]["ST001"]
    stock["price"] = 30
    stock["prev"] = 30
    engine.advance(state, now + cfg.round_seconds * 5, cfg)
    assert not engine.is_delisted(stock)
    assert int(stock["price"]) >= cfg.min_price, stock["price"]
    assert engine.quote_page(state, 1, cfg)[0][0]["delisted"] is False


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
