"""初始上市公司。

名称暂时留空，发送 `/股市 改名 <代码> <名称>`（股市管理员）即可补齐，
也可以直接改这里的 name 字段后重载插件（只对新开户的群生效）。
价格单位为「分」。
"""

from __future__ import annotations

SEED_STOCKS: list[dict] = [
    {"code": "ST001", "name": "", "price": 1000},
    {"code": "ST002", "name": "", "price": 1800},
    {"code": "ST003", "name": "", "price": 2500},
    {"code": "ST004", "name": "", "price": 3600},
    {"code": "ST005", "name": "", "price": 800},
    {"code": "ST006", "name": "", "price": 2200},
    {"code": "ST007", "name": "", "price": 4800},
    {"code": "ST008", "name": "", "price": 1600},
    {"code": "ST009", "name": "", "price": 3200},
    {"code": "ST010", "name": "", "price": 1200},
]
