"""真实行情与外汇测试：解析器、精度换算、FX 交易闭环、品种切换清算。

直接运行 `python tests/test_feeds.py`，不需要 AstrBot 与网络（解析用固定报文）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market import engine, feeds  # noqa: E402
from market.config import Config  # noqa: E402
from market.instruments import (  # noqa: E402
    Instrument,
    build_instruments,
    default_instruments,
    fx_instrument,
)

TENCENT_SAMPLE = (
    'v_sh600519="1~贵州茅台~600519~1439.00~1485.75~1475.35~56976~26410~30566~1439.00~3~'
    '1438.87~4~1438.71~1~1438.50~1~1438.26~6~1439.05~1~1439.20~6~1439.40~2~1439.44~10~'
    '1439.47~1~~20261123161441~-46.75~-3.15~1478.00~1428.00~1439.00/56976/8223838967~'
    "56976~822384~0.46~21.89~~1478.00~1428.00~3.37~17988.67~17988.67~6.63~1634.33~1337.18~1.33~"
    '-5~1443.39~20.93~21.85~~~-0.06~822383.8967~446.0900~31~   A~GP-A~6.66~-1.02~3.62~30.29~'
    '26.74~1539.98~1151.01~-0.41~6.59~13.06~1250081601~1250081601~-14.29~0.84~1250081601~~~1.72~'
    '-0.12~~CNY~0~___D__F__N~1438.16~6~";\n'
)
FX_SAMPLE = (
    'var hq_str_fx_susdcny="00:09:01,6.6655,6.6660,6.6657,162.0,6.6744,6.6782,6.6620,6.6660,'
    '美元人民币,0.0045,0.0003,0.0162,";\n'
    'var hq_str_fx_sjpycny="00:09:55,0.0418051462,0.0418051462,0.0421169672,3.27,0.0421169672,'
    '0.0421302752,0.0418025249,0.0418051462,日元人民币,-0.7404,-0.0003,0.0003,";\n'
)


def fx_only(cur: str = "USD") -> Config:
    return Config(data_source="real", instruments=(fx_instrument(cur),))


def test_parse_tencent_fields() -> None:
    stock = Instrument("SH600519", "贵州茅台", "stock", "tencent", "sh600519", "白酒")
    quotes = feeds.parse_tencent(TENCENT_SAMPLE, {"sh600519": stock})
    quote = quotes["SH600519"]
    assert quote.last == 1439.0, quote.last
    assert quote.prev_close == 1485.75, quote.prev_close
    assert quote.open == 1475.35, quote.open
    assert quote.high == 1478.0, quote.high
    assert quote.low == 1428.0, quote.low
    assert quote.name == "贵州茅台", quote.name
    assert quote.ts > 0


def test_parse_sina_fx_fields() -> None:
    usd = fx_instrument("USD")
    jpy = fx_instrument("JPY")
    quotes = feeds.parse_sina_fx(
        FX_SAMPLE, {usd.symbol: usd, jpy.symbol: jpy}
    )
    assert quotes["FX_USDCNY"].last == 6.666, quotes["FX_USDCNY"].last
    assert quotes["FX_USDCNY"].prev_close == 6.6657
    assert quotes["FX_JPYCNY"].last == 0.0418051462
    assert quotes["FX_JPYCNY"].prev_close == 0.0421169672


def test_parse_skips_garbage() -> None:
    stock = Instrument("SH600519", "贵州茅台", "stock", "tencent", "sh600519")
    assert feeds.parse_tencent("v_sh600519=\"\"", {"sh600519": stock}) == {}
    assert feeds.parse_tencent("not a quote", {"sh600519": stock}) == {}
    empty = fx_instrument("EUR")
    assert feeds.parse_sina_fx("", {"fx_seurcny": empty}) == {}


def test_price_scale_roundtrip() -> None:
    fx = fx_instrument("USD")
    stock = engine.new_state("g", 0, Config(instruments=(fx,)))["stocks"]["FX_USDCNY"]
    assert engine.price_scale(stock) == 1_000_000
    assert engine.price_digits(stock) == 4
    # 6.6660 美元/人民币：1 万美元 = 66,660.00 元
    assert engine.value_cents(stock, 6_666_000, 10_000) == 6_666_000
    assert engine.fmt_price(stock, 6_666_000) == "6.6660"


def test_jpy_precision_not_lost() -> None:
    jpy = fx_instrument("JPY")
    state = engine.new_state("g", 0, Config(instruments=(jpy,)))
    stock = state["stocks"]["FX_JPYCNY"]
    assert engine.price_digits(stock) == 6
    # 0.041805 元/日元：1 万日元 = 418.05 元 = 41,805 分
    assert engine.value_cents(stock, 41_805, 10_000) == 41_805
    assert engine.fmt_price(stock, 41_805) == "0.041805"


def test_fx_buy_sell_closed_loop() -> None:
    cfg = fx_only("USD")
    state = engine.new_state("g", time.time(), cfg)
    engine.ensure_account(state, "u1", cfg)
    cash0 = state["accounts"]["u1"]["cash"]

    trade = engine.buy(state, "u1", "FX_USDCNY", "1000", cfg)
    assert trade["price"] == 6_660_000, trade["price"]
    assert trade["total"] == 666_000 + 666, trade["total"]  # 6,660 元 + 0.1%
    assert trade["cash"] == cash0 - trade["total"]

    pos = state["accounts"]["u1"]["positions"]["FX_USDCNY"]
    assert pos["shares"] == 1000
    assert pos["cost"] == trade["total"]

    summary = engine.account_summary(state, "u1")
    assert summary["market_value"] == 666_000, summary["market_value"]

    sold = engine.sell(state, "u1", "FX_USDCNY", "1000", cfg)
    assert sold["proceeds"] == 666_000 - 666, sold["proceeds"]
    assert sold["realized"] == -2 * 666, sold["realized"]
    assert state["accounts"]["u1"]["positions"].get("FX_USDCNY", {}).get("shares", 0) == 0


def test_fx_micro_trade_rounds_fee_up() -> None:
    """最小单位成交（1 日元 ≈ 4 分）：手续费向上取整到 1 分，总额闭合。"""
    cfg = fx_only("JPY")
    state = engine.new_state("g", 0, cfg)
    engine.ensure_account(state, "u1", cfg)
    cash0 = state["accounts"]["u1"]["cash"]
    trade = engine.buy(state, "u1", "FX_JPYCNY", "1", cfg)
    assert trade["total"] == 4 + 1, trade["total"]
    assert trade["cash"] == cash0 - 5


def test_fx_limit_order_freeze() -> None:
    cfg = fx_only("USD")
    state = engine.new_state("g", 0, cfg)
    engine.ensure_account(state, "u1", cfg)
    # 限价 6.5000 低于现价 6.6600：买单挂起，冻结 = 6500 * 1000 / 10000 元 + 手续费
    result = engine.place_order(state, "u1", "buy", "FX_USDCNY", "1000", "6.5000", cfg)
    order = result["order"]
    assert result["executed_at"] is None
    assert order["limit"] == 6_500_000
    assert order["frozen"] == 650_000 + 650, order["frozen"]
    assert state["accounts"]["u1"]["frozen"] == order["frozen"]


def test_fx_limit_order_executes() -> None:
    cfg = fx_only("USD")
    state = engine.new_state("g", 0, cfg)
    engine.ensure_account(state, "u1", cfg)
    result = engine.place_order(state, "u1", "buy", "FX_USDCNY", "100", "6.7000", cfg)
    assert result["executed_at"] == 6_660_000, result["executed_at"]
    assert state["accounts"]["u1"]["positions"]["FX_USDCNY"]["shares"] == 100


def test_apply_quotes_updates_state() -> None:
    stock = Instrument("SH600519", "贵州茅台", "stock", "tencent", "sh600519", "白酒")
    cfg = Config(data_source="real", instruments=(stock,))
    state = engine.new_state("g", 0, cfg)
    quotes = {
        "SH600519": feeds.Quote(
            code="SH600519",
            symbol="sh600519",
            name="贵州茅台",
            last=1439.0,
            prev_close=1485.75,
            open=1475.35,
            high=1478.0,
            low=1428.0,
            currency="CNY",
            ts=1_700_000_000.0,
        )
    }
    engine.advance(state, 600, cfg, quotes=quotes)
    row = state["stocks"]["SH600519"]
    assert row["price"] == 143_900
    assert row["prev"] == 148_575
    assert round(engine.stock_pct(row), 2) == -3.15
    assert row["open"] == 147_535 and row["high"] == 147_800 and row["low"] == 142_800
    assert row["history"][-1][1] == 143_900


def test_advance_real_never_walks() -> None:
    stock = Instrument("SH600519", "贵州茅台", "stock", "tencent", "sh600519")
    cfg = Config(data_source="real", instruments=(stock,))
    state = engine.new_state("g", 0, cfg)
    start = state["stocks"]["SH600519"]["price"]
    engine.advance(state, 300 * 50, cfg, quotes=None)
    assert state["stocks"]["SH600519"]["price"] == start
    assert state["round"] == 50


def test_migrate_legacy_ledger_liquidates() -> None:
    """老模拟账本切到真实行情：旧持仓按最后价结算成现金，资产不凭空蒸发。"""
    legacy_cfg = Config()  # 模拟模式，ST001 等
    state = engine.new_state("g", 0, legacy_cfg)
    engine.ensure_account(state, "u1", legacy_cfg)
    engine.buy(state, "u1", "ST001", "50", legacy_cfg)
    before = engine.account_total(state, state["accounts"]["u1"])

    real_cfg = Config(data_source="real", instruments=(fx_instrument("USD"),))
    closed = engine.ensure_instruments(state, real_cfg)
    assert closed == 1, closed
    acc = state["accounts"]["u1"]
    assert "ST001" not in acc["positions"]
    assert "FX_USDCNY" in state["stocks"]
    after = engine.account_total(state, acc)
    # 平仓没有手续费，只有买时的 50 分手续费损失
    assert after == before, (before, after)


def test_build_instruments_from_config() -> None:
    items = build_instruments(["sh600519:茅台", "usAAPL"], ["USD"], fx_enabled=True)
    codes = [inst.code for inst in items]
    assert codes == ["SH600519", "US_AAPL", "FX_USDCNY"], codes
    assert items[0].name == "茅台"
    assert items[1].currency == "USD"

    no_fx = build_instruments(["hk00700"], None, fx_enabled=False)
    assert [inst.code for inst in no_fx] == ["HK00700"]

    default = default_instruments()
    assert any(inst.kind == "fx" for inst in default)
    assert len({inst.code for inst in default}) == len(default)


def test_instrument_rejects_bad_prefix() -> None:
    try:
        build_instruments(["btc123"], None, fx_enabled=False)
    except ValueError:
        pass
    # build_instruments 内部吞掉非法项，结果为空
    assert build_instruments(["btc123"], None, fx_enabled=False) == ()


def test_fetch_degrades_without_network() -> None:
    """接口不可达时返回空报价 + 错误描述，不抛异常。"""
    stock = Instrument("SH600519", "贵州茅台", "stock", "tencent", "sh600519")
    original = feeds.TENCENT_URL
    feeds.TENCENT_URL = "https://127.0.0.1:9/q="
    try:
        quotes, errors = feeds.fetch_quotes([stock], timeout=1.0)
    finally:
        feeds.TENCENT_URL = original
    assert quotes == {}, quotes
    assert errors, "网络失败必须返回错误描述"


def test_feed_keeps_last_frame_on_failure() -> None:
    """已有帧时抓取失败：沿用旧价，状态标 stale。"""
    import asyncio

    stock = Instrument("SH600519", "贵州茅台", "stock", "tencent", "sh600519")
    feed = feeds.QuoteFeed(None, timeout=1.0)
    stale = feeds.Quote(
        code="SH600519", symbol="sh600519", name="贵州茅台", last=1439.0,
        prev_close=1485.75, open=1475.35, high=1478.0, low=1428.0,
        currency="CNY", ts=1_700_000_000.0,
    )
    feed._mem = {"SH600519": stale}
    feed._mem_at = 0.0
    original = feeds.TENCENT_URL
    feeds.TENCENT_URL = "https://127.0.0.1:9/q="
    try:
        quotes = asyncio.run(feed.snapshot([stock], ttl=0))
    finally:
        feeds.TENCENT_URL = original
    assert quotes["SH600519"].last == 1439.0
    assert feed.status == "stale", feed.status


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