"""初始上市公司。

名称暂时留空，发送 `/股市 改名 <代码> <名称>`（股市管理员）即可补齐，
也可以直接改这里的 name 字段后重载插件（只对新开户的群生效）。
价格单位为「分」。

`sector` 用于新闻的同板块传导：同一板块的其他股票会跟着一起吃涨/吃跌。
"""

from __future__ import annotations

SEED_STOCKS: list[dict] = [
    {"code": "ST001", "name": "", "price": 1000, "sector": "科技"},
    {"code": "ST002", "name": "", "price": 1800, "sector": "消费"},
    {"code": "ST003", "name": "", "price": 2500, "sector": "网络"},
    {"code": "ST004", "name": "", "price": 3600, "sector": "科技"},
    {"code": "ST005", "name": "", "price": 800, "sector": "消费"},
    {"code": "ST006", "name": "", "price": 2200, "sector": "网络"},
    {"code": "ST007", "name": "", "price": 4800, "sector": "科技"},
    {"code": "ST008", "name": "", "price": 1600, "sector": "网络"},
    {"code": "ST009", "name": "", "price": 3200, "sector": "网络"},
    {"code": "ST010", "name": "", "price": 1200, "sector": "消费"},
]

SECTOR_OF: dict[str, str] = {item["code"]: item["sector"] for item in SEED_STOCKS}


def sector_of(code: str) -> str:
    """股票所属板块；未知代码返回空字符串（老账本也不会出错）。"""
    return SECTOR_OF.get((code or "").upper(), "")


def codes_in_sector(sector: str) -> list[str]:
    return [code for code, name in SECTOR_OF.items() if name == sector]
