"""引擎单测：直接运行 `python tests/test_engine.py`，无需 pytest。"""

from __future__ import annotations

import sys
import time
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


def test_name_store_shared_across_groups() -> None:
    import asyncio
    import tempfile

    from market.storage import NameStore

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
