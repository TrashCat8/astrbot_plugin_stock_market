"""容器内冒烟测试：用假事件把全部指令跑一遍，验证文案、卡片与落盘。

需要 AstrBot 环境（在容器里运行）：

    ASTRBOT_ROOT=/tmp/astrbot_smoke python \\
        /AstrBot/data/plugins/astrbot_plugin_stock_market/tests/smoke_handlers.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# 每次都用全新的临时根目录，避免上一次运行的账本影响结果、也避免写进线上数据
os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="astrbot_smoke_")

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR.parent))

from astrbot_plugin_stock_market.main import StockMarketPlugin  # noqa: E402


class FakeEvent:
    def __init__(self, gid: str = "123456", uid: str = "10001", admin: bool = False) -> None:
        self.gid = gid
        self.uid = uid
        self.admin = admin

    def get_group_id(self) -> str:
        return self.gid

    def get_sender_id(self) -> str:
        return self.uid

    def is_admin(self) -> bool:
        return self.admin

    def get_sender_name(self) -> str:
        return f"用户{self.uid}"

    def get_messages(self) -> list:
        return []

    def plain_result(self, text: str) -> dict:
        return {"type": "text", "text": text}

    def image_result(self, path: str) -> dict:
        file = Path(path)
        return {"type": "image", "path": str(file), "size": file.stat().st_size}


async def main() -> int:
    # 两套白名单故意给不同的人：10001 管 give/clear，10002 管改名
    plugin = StockMarketPlugin(  # type: ignore[arg-type]
        None, {"market_admins": ["10001"], "rename_admins": ["10002"]}
    )
    cfg = plugin._cfg()
    namespace = "group_123456"
    event = FakeEvent(admin=True)
    failures = []

    async def call(name: str, *rest: str):
        return await getattr(plugin, f"_cmd_{name}")(event, namespace, cfg, list(rest))

    def check(name: str, result: dict, expect_type: str = "text") -> None:
        ok = result.get("type") == expect_type
        if expect_type == "image":
            ok = ok and result.get("size", 0) > 1000
        detail = result.get("text", "").splitlines()[0] if expect_type == "text" else f"{result.get('size')} bytes"
        print(("OK   " if ok else "FAIL ") + f"{name}: {detail}")
        if not ok:
            failures.append(name)

    check("开户", await call("open"))
    buy = await call("buy", "ST001", "50")
    check("买入", buy)
    if "99,499.50" not in buy["text"]:
        failures.append("买入金额")
        print("FAIL 买入金额与截图不一致:", buy["text"])
    check("行情", await call("quote"), "image")
    check("持仓", await call("position"), "image")
    check("限价买单", await call("limit_buy", "ST002", "10", "5.00"))
    check("挂单", await call("orders"))
    check("新闻", await call("news"))
    check("走势", await call("chart", "ST001"), "image")
    check("帮助", await call("help"), "image")
    check("改名", await plugin._cmd_rename(FakeEvent(uid="10002"), namespace, cfg, ["ST001", "星辰科技"]))
    check("卖出", await call("sell", "ST001", "20"))
    check("成交", await call("trades"))
    check("撤单", await call("cancel", "1"))
    check("签到", await call("checkin"))
    check("排行", await call("rank"), "image")
    check("盈亏", await call("profit"), "image")
    check("db", await call("gamble", "100"))
    check("融资买入", await call("margin_buy", "ST003", "10", "2"))
    check("杠杆查询", await call("leverage"))
    check("还债", await call("repay"))

    # 补助：资产充足时应当被拒绝
    try:
        await call("relief")
    except Exception as exc:  # noqa: BLE001
        print(f"OK   补助拒绝: {exc}")
    else:
        failures.append("补助")
        print("FAIL 补助在资产充足时不应发放")

    # 抢劫需要目标已开户
    check("目标开户", await plugin._cmd_open(FakeEvent(uid="10003"), namespace, cfg, []))
    check("抢劫", await call("rob", "10003"))
    check("转账", await call("transfer", "10003", "100"))
    check("give", await call("give", "10003", "50"))
    check("give自己", await call("give", "10001", "10"))
    check("清零", await call("clear", "10003"))

    # 股市管理员名单独立于 AstrBot：10003 即使是 AstrBot 管理员也应被拒绝
    try:
        await plugin._cmd_give(FakeEvent(uid="10003", admin=True), namespace, cfg, ["10001", "10"])
    except Exception as exc:  # noqa: BLE001
        print(f"OK   give权限拒绝: {exc}")
    else:
        failures.append("give权限")
        print("FAIL AstrBot 管理员不应自动获得股市管理员权限")

    # 改名只看改名白名单：AstrBot 管理员不行，give/clear 白名单成员也不行
    for uid, admin, label in (("10003", True, "AstrBot 管理员"), ("10001", False, "give 管理员")):
        try:
            await plugin._cmd_rename(FakeEvent(uid=uid, admin=admin), namespace, cfg, ["ST002", "测试名"])
        except Exception as exc:  # noqa: BLE001
            print(f"OK   改名权限拒绝({label}): {exc}")
        else:
            failures.append(f"改名权限{uid}")
            print(f"FAIL {label}不应自动获得改名权限")

    # 反向：改名白名单成员不能 give / clear
    try:
        await plugin._cmd_give(FakeEvent(uid="10002"), namespace, cfg, ["10001", "10"])
    except Exception as exc:  # noqa: BLE001
        print(f"OK   give权限拒绝(改名管理员): {exc}")
    else:
        failures.append("give权限2")
        print("FAIL 改名管理员不应自动获得 give 权限")

    # 别名解析：走真实入口，确认「/股市 transfer ...」能分发到 transfer
    async for res in plugin.market(event, "transfer 10003 1"):
        check("指令分发 transfer", res)

    data_file = (
        Path(os.environ["ASTRBOT_ROOT"])
        / "data"
        / "plugin_data"
        / "astrbot_plugin_stock_market"
        / "groups"
        / f"{namespace}.json"
    )
    print(f"{'OK   ' if data_file.exists() else 'FAIL '}账本落盘: {data_file} ({data_file.stat().st_size if data_file.exists() else 0} bytes)")
    if not data_file.exists():
        failures.append("账本落盘")

    # 第二个群：股票名称全局共享，账本仍然独立
    event_b = FakeEvent(gid="654321", uid="10002", admin=True)
    check("B群开户", await plugin._cmd_open(event_b, "group_654321", cfg, []))
    check("B群行情", await plugin._cmd_quote(event_b, "group_654321", cfg, []), "image")
    b_file = data_file.with_name("group_654321.json")
    b_state = json.loads(b_file.read_text(encoding="utf-8"))
    b_name = b_state["stocks"]["ST001"]["name"]
    b_cash = b_state["accounts"]["10002"]["cash"]
    names_file = data_file.parent.parent / "names.json"
    shared_ok = (
        b_name == "星辰科技"
        and b_cash == cfg.initial_capital
        and names_file.exists()
    )
    print(
        ("OK   " if shared_ok else "FAIL ")
        + f"名称共享/账本独立: B群名称={b_name or '空'} 资金={b_cash} 名称表={names_file.name}"
    )
    if not shared_ok:
        failures.append("名称共享")

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
