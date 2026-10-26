"""插件配置到引擎配置的映射。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Config:
    """引擎运行参数。金额单位统一为「分」。"""

    initial_capital: int = 10_000_000  # 100,000.00 元
    round_seconds: int = 300
    max_change_pct: float = 10.0
    min_price: int = 10  # 0.10 元：整数分精度下再低就会锁死在原地
    fee_rate: float = 0.001
    order_ttl_hours: int = 24
    max_catchup: int = 500
    news_probability: float = 0.35
    page_size: int = 10
    render_cards: bool = True
    allowed_groups: list[str] = field(default_factory=list)
    # 玩法：签到 / 破产补助 / 排行
    daily_bonus: int = 200_000  # 2000.00 元
    streak_bonus_pct: float = 10.0
    max_streak_bonus_pct: float = 50.0
    relief_threshold: int = 500_000  # 5000.00 元
    relief_amount: int = 1_000_000  # 10000.00 元
    relief_cooldown_hours: int = 24
    rank_size: int = 10
    # 玩法：抢劫
    rob_cooldown_minutes: int = 10
    rob_base_chance: float = 40.0
    rob_min_pct: float = 5.0
    rob_max_pct: float = 20.0
    gamble_enabled: bool = True
    # 玩法：杠杆（倍数开仓，借钱部分计入账户欠款）
    margin_enabled: bool = True
    max_leverage: float = 2.0
    margin_daily_rate: float = 0.0005  # 0.05% / 天
    margin_call_ratio: float = 0.25  # 权益 / 市值 低于此比例即强平
    # 两套独立白名单：改名 / give+clear，均与 AstrBot 管理员完全独立
    rename_admins: list[str] = field(default_factory=list)
    market_admins: list[str] = field(default_factory=list)

    @property
    def order_ttl_rounds(self) -> int:
        return max(1, int(self.order_ttl_hours * 3600 // self.round_seconds))

    @classmethod
    def from_plugin_config(cls, cfg: Any) -> "Config":
        """从 AstrBot 的插件配置构造，脏数据一律回落到默认值。"""

        def num(key: str, default, cast=float):
            try:
                value = cfg.get(key, default)
                if value is None:
                    return default
                return cast(value)
            except (TypeError, ValueError):
                return default

        groups = cfg.get("allowed_groups") or []
        if not isinstance(groups, (list, tuple)):
            groups = []

        # 默认空名单：改名 / give / clear 都需在插件配置里手动填写，代码里不留任何 QQ 号
        admins = cfg.get("market_admins") or []
        if not isinstance(admins, (list, tuple)):
            admins = []
        rename_admins = cfg.get("rename_admins") or []
        if not isinstance(rename_admins, (list, tuple)):
            rename_admins = []

        return cls(
            initial_capital=max(0, int(round(num("initial_capital", 100000.0, float) * 100))),
            round_seconds=max(30, num("round_seconds", 300, int)),
            max_change_pct=min(50.0, max(1.0, num("max_change_pct", 10.0, float))),
            min_price=min(10_000, max(1, int(round(num("min_price", 0.10, float) * 100)))),
            fee_rate=min(0.1, max(0.0, num("fee_rate", 0.001, float))),
            order_ttl_hours=max(1, num("order_ttl_hours", 24, int)),
            max_catchup=max(1, num("max_catchup_rounds", 500, int)),
            news_probability=min(1.0, max(0.0, num("news_probability", 0.35, float))),
            page_size=max(1, min(50, num("page_size", 10, int))),
            render_cards=bool(cfg.get("render_cards", True)),
            allowed_groups=[str(g) for g in groups],
            daily_bonus=max(0, int(round(num("daily_bonus", 2000.0, float) * 100))),
            streak_bonus_pct=min(100.0, max(0.0, num("streak_bonus_pct", 10.0, float))),
            max_streak_bonus_pct=min(200.0, max(0.0, num("max_streak_bonus_pct", 50.0, float))),
            relief_threshold=max(0, int(round(num("relief_threshold", 5000.0, float) * 100))),
            relief_amount=max(0, int(round(num("relief_amount", 10000.0, float) * 100))),
            relief_cooldown_hours=max(1, num("relief_cooldown_hours", 24, int)),
            rank_size=max(1, min(50, num("rank_size", 10, int))),
            rob_cooldown_minutes=max(0, num("rob_cooldown_minutes", 10, int)),
            rob_base_chance=min(100.0, max(0.0, num("rob_base_chance", 40.0, float))),
            rob_min_pct=min(100.0, max(0.1, num("rob_min_pct", 5.0, float))),
            rob_max_pct=min(100.0, max(0.1, num("rob_max_pct", 20.0, float))),
            gamble_enabled=bool(cfg.get("gamble_enabled", True)),
            margin_enabled=bool(cfg.get("margin_enabled", True)),
            max_leverage=min(10.0, max(1.0, num("max_leverage", 2.0, float))),
            margin_daily_rate=min(0.01, max(0.0, num("margin_daily_rate", 0.0005, float))),
            margin_call_ratio=min(0.9, max(0.05, num("margin_call_ratio", 0.25, float))),
            rename_admins=[str(a).strip() for a in rename_admins if str(a).strip()],
            market_admins=[str(a).strip() for a in admins if str(a).strip()],
        )
