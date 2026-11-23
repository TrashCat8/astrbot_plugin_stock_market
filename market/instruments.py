"""交易品种表：真实行情代码 ↔ 群内交易代码。

两种精度：

- 股票（A 股 / 港股 / 美股）：报价精确到分，`price_scale = 100`，
  内部价格整数直接就是「分」。
- 外汇：报价精确到百万分之一元，`price_scale = 1_000_000`，
  这样 0.041805（日元）这种小额报价也不会被整数化吃掉。

金额换算统一走 `price * units * 100 // price_scale`，得到「分」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCALE_STOCK = 100
SCALE_FX = 1_000_000

FX_NAMES = {
    "USD": "美元",
    "EUR": "欧元",
    "JPY": "日元",
    "HKD": "港元",
    "GBP": "英镑",
    "AUD": "澳元",
    "CAD": "加元",
    "CHF": "瑞郎",
    "SGD": "新加坡元",
    "NZD": "新西兰元",
}


@dataclass(frozen=True)
class Instrument:
    """一个可交易品种。"""

    code: str  # 群内交易代码，例如 SH600519 / FX_USDCNY
    name: str  # 展示名称
    kind: str  # stock / fx
    provider: str  # tencent / sina_fx
    symbol: str  # 外部行情代码，例如 sh600519 / fx_susdcny
    sector: str = ""
    price_scale: int = SCALE_STOCK
    price_digits: int = 2
    currency: str = "CNY"  # 报价币种
    ref_price: float = 0.0  # 参考价：首帧行情拿不到时的兜底


# 默认股票池：A 股 + 港股 + 美股，都是腾讯行情接口稳定覆盖的标的。
DEFAULT_STOCKS: tuple[Instrument, ...] = (
    Instrument("SH600519", "贵州茅台", "stock", "tencent", "sh600519", "白酒", currency="CNY", ref_price=1440.0),
    Instrument("SZ000001", "平安银行", "stock", "tencent", "sz000001", "金融", currency="CNY", ref_price=11.5),
    Instrument("SH601318", "中国平安", "stock", "tencent", "sh601318", "金融", currency="CNY", ref_price=55.0),
    Instrument("SZ300750", "宁德时代", "stock", "tencent", "sz300750", "新能源", currency="CNY", ref_price=380.0),
    Instrument("SH600036", "招商银行", "stock", "tencent", "sh600036", "金融", currency="CNY", ref_price=40.0),
    Instrument("SZ002594", "比亚迪", "stock", "tencent", "sz002594", "新能源", currency="CNY", ref_price=300.0),
    Instrument("HK00700", "腾讯控股", "stock", "tencent", "hk00700", "互联网", currency="HKD", ref_price=344.0),
    Instrument("HK09988", "阿里巴巴", "stock", "tencent", "hk09988", "互联网", currency="HKD", ref_price=120.0),
    Instrument("US_AAPL", "苹果", "stock", "tencent", "usAAPL", "科技", currency="USD", ref_price=348.0),
    Instrument("US_NVDA", "英伟达", "stock", "tencent", "usNVDA", "科技", currency="USD", ref_price=180.0),
    Instrument("US_TSLA", "特斯拉", "stock", "tencent", "usTSLA", "科技", currency="USD", ref_price=400.0),
)

DEFAULT_FX_CURRENCIES: tuple[str, ...] = ("USD", "EUR", "JPY", "HKD", "GBP", "AUD")

_FX_REF = {
    "USD": 6.66,
    "EUR": 7.27,
    "JPY": 0.0418,
    "HKD": 0.855,
    "GBP": 8.9,
    "AUD": 4.3,
    "CAD": 4.8,
    "CHF": 8.4,
    "SGD": 5.0,
    "NZD": 3.9,
}


def fx_instrument(currency: str) -> Instrument:
    cur = currency.strip().upper()
    if cur not in FX_NAMES:
        raise ValueError(f"不支持的外汇品种：{currency}")
    return Instrument(
        code=f"FX_{cur}CNY",
        name=f"{FX_NAMES[cur]}/人民币",
        kind="fx",
        provider="sina_fx",
        symbol=f"fx_s{cur.lower()}cny",
        sector="外汇",
        price_scale=SCALE_FX,
        price_digits=4 if _FX_REF.get(cur, 1.0) >= 1 else 6,
        currency="CNY",
        ref_price=_FX_REF.get(cur, 1.0),
    )


def stock_instrument(symbol: str, name: str = "", sector: str = "") -> Instrument:
    """把 sh600519 / hk00700 / usAAPL 这类外部代码转成内部品种。"""
    raw = (symbol or "").strip()
    low = raw.lower()
    if not low:
        raise ValueError("股票代码不能为空")
    if low.startswith("sh"):
        code = low.upper()
    elif low.startswith("sz"):
        code = low.upper()
    elif low.startswith("hk"):
        code = "HK" + low[2:].upper()
    elif low.startswith("us"):
        code = "US_" + low[2:].upper()
    else:
        raise ValueError(f"无法识别的行情代码：{raw}（支持 sh/sz/hk/us 前缀）")
    return Instrument(
        code=code,
        name=name or raw.upper(),
        kind="stock",
        provider="tencent",
        symbol=low,
        sector=sector or "其他",
        currency="CNY" if low[:2] in ("sh", "sz") else ("HKD" if low.startswith("hk") else "USD"),
        ref_price=0.0,
    )


def default_instruments(fx_currencies: tuple[str, ...] = DEFAULT_FX_CURRENCIES) -> tuple[Instrument, ...]:
    return DEFAULT_STOCKS + tuple(fx_instrument(cur) for cur in fx_currencies)


def build_instruments(
    symbols: Any = None,
    fx_pairs: Any = None,
    *,
    fx_enabled: bool = True,
) -> tuple[Instrument, ...]:
    """按配置拼装品种表。

    `symbols` 留空表示用默认股票池；非空则完全替换（格式 `sh600519` 或
    `sh600519:贵州茅台`）。`fx_pairs` 留空表示用默认 6 个货币对，填 `none`
    表示不要外汇。
    """
    items: list[Instrument] = []
    if isinstance(symbols, (list, tuple)) and symbols:
        for item in symbols:
            text = str(item).strip()
            if not text:
                continue
            symbol, _, name = text.partition(":")
            try:
                items.append(stock_instrument(symbol, name.strip()))
            except ValueError:
                continue
    else:
        items.extend(DEFAULT_STOCKS)

    if fx_enabled:
        pairs: list[str] = []
        if isinstance(fx_pairs, (list, tuple)) and fx_pairs:
            pairs = [str(p).strip().upper().replace("CNY", "").replace("/", "") for p in fx_pairs if str(p).strip()]
        else:
            pairs = list(DEFAULT_FX_CURRENCIES)
        for cur in pairs:
            try:
                items.append(fx_instrument(cur))
            except ValueError:
                continue

    # 去重，保持配置顺序
    seen: set[str] = set()
    unique: list[Instrument] = []
    for inst in items:
        if inst.code in seen:
            continue
        seen.add(inst.code)
        unique.append(inst)
    return tuple(unique)


def instrument_map(instruments: tuple[Instrument, ...]) -> dict[str, Instrument]:
    return {inst.code: inst for inst in instruments}