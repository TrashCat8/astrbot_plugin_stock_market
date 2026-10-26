"""群内模拟股市插件：开户、行情、买卖、限价单、新闻、走势。

指令统一挂在 `/股市` 下，未命中的子指令一律回落到帮助，
不会影响其它插件的 `/` 指令。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .market import engine, render, textfmt
from .market.config import Config
from .market.engine import MarketError
from .market.storage import GroupStore, NameStore

PLUGIN_NAME = "astrbot_plugin_stock_market"

ALIASES = {
    "开户": "open",
    "行情": "quote",
    "持仓": "position",
    "买入": "buy",
    "卖出": "sell",
    "融资买入": "margin_buy",
    "杠杆买入": "margin_buy",
    "杠杆": "leverage",
    "负债": "leverage",
    "还债": "repay",
    "还款": "repay",
    "买单": "limit_buy",
    "卖单": "limit_sell",
    "挂单": "orders",
    "撤单": "cancel",
    "成交": "trades",
    "新闻": "news",
    "走势": "chart",
    "帮助": "help",
    "改名": "rename",
    "签到": "checkin",
    "排行": "rank",
    "盈亏": "profit",
    "赚钱榜": "profit",
    "亏钱榜": "profit",
    "补助": "relief",
    "db": "gamble",
    "赌博": "gamble",
    "抢劫": "rob",
    "transfer": "transfer",
    "give": "give",
    "clear": "clear",
}

NEED_ACCOUNT = "你还没有开户，请先发送「/股市 开户」。"
USAGE = {
    "买入": "用法：/股市 买入 ST001 100",
    "卖出": "用法：/股市 卖出 ST001 100",
    "融资买入": "用法：/股市 融资买入 ST001 100 2（2 倍杠杆）",
    "杠杆": "用法：/股市 杠杆（查看欠款与强平价）",
    "还债": "用法：/股市 还债 [金额]，不填金额表示全部还清",
    "买单": "用法：/股市 买单 ST001 100 9.50",
    "卖单": "用法：/股市 卖单 ST001 100 12.00",
    "撤单": "用法：/股市 撤单 1",
    "走势": "用法：/股市 走势 ST001",
    "改名": "用法：/股市 改名 ST001 星辰科技（需股市管理员）",
    "db": "用法：/股市 db 1000（下注 1000 元）",
    "抢劫": "用法：/股市 抢劫 @对方，或 /股市 抢劫 QQ号",
    "transfer": "用法：/股市 transfer @对方 1000，或 /股市 transfer QQ号 1000",
    "give": "用法：/股市 give @对方 1000（管理员）",
    "clear": "用法：/股市 clear @对方（管理员）",
}


class StockMarketPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        base = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self.store = GroupStore(base)
        self.names = NameStore(base)
        self._calls = 0
        asset_dir = Path(__file__).resolve().parent / "assets"
        try:
            asset_dir.mkdir(exist_ok=True)
        except OSError:
            asset_dir = None
        render.set_asset_dir(asset_dir)

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #

    def _cfg(self) -> Config:
        return Config.from_plugin_config(self.config)

    def _namespace(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if group_id:
            return f"group_{group_id}"
        return f"private_{event.get_sender_id()}"

    def _user(self, event: AstrMessageEvent) -> str:
        return event.get_sender_id() or "unknown"

    def _allowed(self, event: AstrMessageEvent, cfg: Config) -> bool:
        if not event.get_group_id():
            return True
        if not cfg.allowed_groups:
            return True
        return str(event.get_group_id()) in cfg.allowed_groups

    def _is_market_admin(self, event: AstrMessageEvent, cfg: Config) -> bool:
        """give / clear 白名单：只看插件配置，与 AstrBot 管理员/群管理身份无关。"""
        return str(self._user(event)) in set(cfg.market_admins)

    def _require_market_admin(self, event: AstrMessageEvent, cfg: Config, action: str) -> None:
        if self._is_market_admin(event, cfg):
            return
        if not cfg.market_admins:
            raise MarketError(
                f"没有配置股市管理员，无法{action}。请在插件配置的「股市管理员 QQ 名单」里填写。"
            )
        raise MarketError(f"你不是股市管理员，无法{action}。")

    def _is_rename_admin(self, event: AstrMessageEvent, cfg: Config) -> bool:
        """改名白名单：与 give/clear 名单、AstrBot 管理员完全独立。"""
        return str(self._user(event)) in set(cfg.rename_admins)

    def _require_rename_admin(self, event: AstrMessageEvent, cfg: Config) -> None:
        if self._is_rename_admin(event, cfg):
            return
        if not cfg.rename_admins:
            raise MarketError(
                "没有配置改名管理员，无法改名。请在插件配置的「改名管理员 QQ 名单」里填写。"
            )
        raise MarketError("你不是改名管理员，无法改名。")

    @staticmethod
    def _sender_name(event: AstrMessageEvent) -> str:
        try:
            return event.get_sender_name() or ""
        except Exception:
            return ""

    def _mark_name(self, event: AstrMessageEvent, state: dict[str, Any], user: str | None = None) -> None:
        engine.touch_name(state, user or self._user(event), self._sender_name(event))

    @staticmethod
    def _at_from_chain(event: AstrMessageEvent) -> tuple[str, str]:
        """只从消息链的 @ 组件取目标，避免把「金额」误当成 QQ 号。"""
        try:
            components = event.get_messages()
        except Exception:
            components = []
        for comp in components:
            qq = getattr(comp, "qq", None)
            if qq is None:
                continue
            qq = str(qq)
            if qq.isdigit():
                return qq, str(getattr(comp, "name", "") or "")
        return "", ""

    def _at_target(self, event: AstrMessageEvent, rest: list[str]) -> tuple[str, str]:
        """先看 @ 组件，取不到再退回参数里的第一个数字（抢劫用）。"""
        target, name = self._at_from_chain(event)
        if target:
            return target, name
        for token in rest:
            token = token.lstrip("@")
            if token.isdigit():
                return token, ""
        return "", ""

    def _target_and_amount(self, event: AstrMessageEvent, rest: list[str]) -> tuple[str, str, str]:
        """「@某人 金额」或「QQ号 金额」两种写法都支持。"""
        target, name = self._at_from_chain(event)
        if target:
            return target, name, (rest[0] if rest else "")
        if len(rest) >= 2:
            return rest[0].lstrip("@"), "", rest[1]
        return "", "", ""

    @asynccontextmanager
    async def _session(
        self, namespace: str, cfg: Config, create: bool = False
    ) -> AsyncIterator[dict[str, Any] | None]:
        """加载账本、补算行情，退出时原子落盘。同时刻同群指令串行执行。"""
        async with self.store.lock(namespace):
            state = self.store.load(namespace)
            if state is None and create:
                state = engine.new_state(namespace, time.time(), cfg)
            if state is not None:
                # 股票名称是全局的，每次进群先套用最新的名称表
                self.names.apply(state)
                engine.advance(state, time.time(), cfg)
            try:
                yield state
            finally:
                if state is not None:
                    self.store.save(namespace, state)

    @staticmethod
    def _require(state: dict[str, Any] | None) -> dict[str, Any]:
        if state is None:
            raise MarketError(NEED_ACCOUNT)
        return state

    def _card_ok(self, cfg: Config) -> bool:
        return cfg.render_cards

    def _with_notes(
        self, event: AstrMessageEvent, notes: list[str], result
    ) -> list[Any] | Any:
        """把强平/计息通知放在正式结果前面一起发出。"""
        if not notes:
            return result
        return [*(event.plain_result(note) for note in notes), result]

    def _image_or_text(self, event: AstrMessageEvent, path: Path, text: str):
        try:
            return event.image_result(str(path))
        except Exception:
            logger.exception("[stock] 图片渲染失败，回退文字")
            return event.plain_result(text)

    # ------------------------------------------------------------------ #
    # 指令入口
    # ------------------------------------------------------------------ #

    @filter.command("股市")
    async def market(self, event: AstrMessageEvent, args: GreedyStr):
        cfg = self._cfg()
        if not self._allowed(event, cfg):
            return

        self._calls += 1
        if self._calls % 50 == 0:
            self.store.cleanup_cache()

        tokens = str(args).split()
        action = ALIASES.get(tokens[0]) if tokens else None
        rest = tokens[1:]
        namespace = self._namespace(event)

        try:
            if action is None:
                result = await self._cmd_help(event, namespace, cfg, rest)
            else:
                handler = getattr(self, f"_cmd_{action}")
                result = await handler(event, namespace, cfg, rest)
        except MarketError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        except Exception:
            logger.exception("[stock] 指令执行失败：%s", tokens)
            yield event.plain_result("股市服务出错了，请稍后再试。")
            return

        if result is None:
            return
        results = result if isinstance(result, (list, tuple)) else [result]
        for item in results:
            if item is not None:
                yield item

    # ------------------------------------------------------------------ #
    # 各子指令
    # ------------------------------------------------------------------ #

    async def _cmd_open(self, event, namespace, cfg, rest):
        user = self._user(event)
        async with self._session(namespace, cfg, create=True) as state:
            self._require(state)
            if user in state["accounts"]:
                raise MarketError("你已经开过户了，发送「/股市 持仓」查看账户。")
            account = engine.ensure_account(state, user, cfg)
            self._mark_name(event, state, user)
            cash = account["cash"]
        return event.plain_result(
            "开户成功！\n"
            f"本群可用资金: {engine.fmt_money(cash)} 元\n"
            "发送「/股市 行情」开始交易。"
        )

    async def _cmd_quote(self, event, namespace, cfg, rest):
        user = self._user(event)
        arg = rest[0] if rest else ""
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            engine.require_account(state, user)

            if arg and not arg.isdigit():
                stock = engine.find_stock(state, arg)
                return event.plain_result(self._detail_text(stock))

            page_no = int(arg) if arg.isdigit() else 1
            rows, page, pages = engine.quote_page(state, page_no, cfg)
            round_no = int(state["round"])
            updated = engine.fmt_time(time.time())
            text = textfmt.quote_text(rows, page, pages, round_no, cfg.round_seconds)
            out = self.store.cache_file(
                f"quote_{namespace}_{page}_{round_no}.png"
            )

        if not self._card_ok(cfg):
            return event.plain_result(text)
        try:
            render.render_quote(
                rows,
                page,
                pages,
                round_no,
                updated,
                refresh_seconds=cfg.round_seconds,
                fee_rate=cfg.fee_rate,
                out=out,
            )
        except Exception:
            logger.exception("[stock] 行情卡渲染失败，回退文字")
            return event.plain_result(text)
        return event.image_result(str(out))

    async def _cmd_position(self, event, namespace, cfg, rest):
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            summary = engine.account_summary(state, user)
            notes = engine.pop_notices(engine.require_account(state, user))
            round_no = int(state["round"])
            updated = engine.fmt_time(time.time())
            text = textfmt.position_text(summary, round_no)
            out = self.store.cache_file(f"position_{namespace}_{round_no}.png")

        if not self._card_ok(cfg):
            return self._with_notes(event, notes, event.plain_result(text))
        try:
            render.render_position(summary, round_no, updated, out=out)
        except Exception:
            logger.exception("[stock] 持仓卡渲染失败，回退文字")
            return self._with_notes(event, notes, event.plain_result(text))
        return self._with_notes(event, notes, event.image_result(str(out)))

    async def _cmd_buy(self, event, namespace, cfg, rest):
        if len(rest) < 2:
            raise MarketError(USAGE["买入"])
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            trade = engine.buy(state, user, rest[0], rest[1], cfg)
            notes = engine.pop_notices(engine.require_account(state, user))
        lines = [
            *notes,
            f"买入成功：{trade['label']} × {trade['shares']} 股",
            f"成交价 {engine.fmt_money(trade['price'], comma=False)} 元，"
            f"手续费 {engine.fmt_money(trade['fee'])} 元",
            f"可用资金: {engine.fmt_money(trade['cash'])} 元",
        ]
        return event.plain_result("\n".join(lines))

    async def _cmd_sell(self, event, namespace, cfg, rest):
        if len(rest) < 2:
            raise MarketError(USAGE["卖出"])
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            trade = engine.sell(state, user, rest[0], rest[1], cfg)
            notes = engine.pop_notices(engine.require_account(state, user))
        lines = [
            *notes,
            f"卖出成功：{trade['label']} × {trade['shares']} 股",
            f"成交价 {engine.fmt_money(trade['price'], comma=False)} 元，"
            f"手续费 {engine.fmt_money(trade['fee'])} 元",
            f"到账 {engine.fmt_money(trade['proceeds'])} 元，"
            f"本次盈亏 {engine.fmt_money(trade['realized'])} 元",
        ]
        if trade.get("repaid"):
            lines.append(f"偿还欠款 {engine.fmt_money(trade['repaid'])} 元")
        if trade.get("debt"):
            lines.append(f"剩余欠款 {engine.fmt_money(trade['debt'])} 元")
        lines.append(f"可用资金: {engine.fmt_money(trade['cash'])} 元")
        return event.plain_result("\n".join(lines))

    async def _cmd_margin_buy(self, event, namespace, cfg, rest):
        if len(rest) < 3:
            raise MarketError(USAGE["融资买入"])
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            trade = engine.leverage_buy(state, user, rest[0], rest[1], rest[2], cfg)
            notes = engine.pop_notices(engine.require_account(state, user))
        lines = [
            *notes,
            f"融资买入成功：{trade['label']} × {trade['shares']} 股"
            f"（{trade['leverage']:g} 倍）",
            f"成交价 {engine.fmt_money(trade['price'], comma=False)} 元，"
            f"手续费 {engine.fmt_money(trade['fee'])} 元",
            f"自有保证金 {engine.fmt_money(trade['margin'])} 元，"
            f"系统垫付 {engine.fmt_money(trade['borrowed'])} 元",
            f"当前欠款 {engine.fmt_money(trade['debt'])} 元，"
            f"可用资金 {engine.fmt_money(trade['cash'])} 元",
            "发送「/股市 杠杆」查看强平价；欠款按天计息，还清前不能再融资。",
        ]
        return event.plain_result("\n".join(lines))

    async def _cmd_leverage(self, event, namespace, cfg, rest):
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            info = engine.leverage_info(state, user, cfg)
            notes = engine.pop_notices(engine.require_account(state, user))

        lines = [
            *notes,
            "【杠杆账户】",
            f"可用 {engine.fmt_money(info['cash'])} 元 · "
            f"总资产 {engine.fmt_money(info['total'])} 元 · "
            f"欠款 {engine.fmt_money(info['debt'])} 元",
            f"净资产 {engine.fmt_money(info['net'])} 元 · "
            f"最高 {info['max_leverage']:g} 倍 · "
            f"日息 {info['daily_rate'] * 100:.3f}%",
        ]
        if not info["positions"]:
            lines.append("没有杠杆持仓。用法：/股市 融资买入 ST001 100 2")
        for row in info["positions"]:
            lines.append(
                f"{row['label']}：{row['shares']} 股，欠款 {engine.fmt_money(row['debt'])} 元"
            )
            lines.append(
                f"  市值 {engine.fmt_money(row['value'])} 元 · "
                f"权益比 {row['ratio']:.1f}% · "
                f"强平价 {engine.fmt_money(row['liq_price'], comma=False)} 元"
            )
        lines.append(
            f"权益/市值低于 {info['call_ratio'] * 100:.0f}% 时自动强平；"
            "强平后仍欠的钱会保留，后续收入优先还债。"
        )
        return event.plain_result("\n".join(lines))

    async def _cmd_repay(self, event, namespace, cfg, rest):
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            info = engine.repay_debt(state, user, rest[0] if rest else "", cfg)
            notes = engine.pop_notices(engine.require_account(state, user))
        lines = [
            *notes,
            f"已还债 {engine.fmt_money(info['amount'])} 元",
            f"剩余欠款 {engine.fmt_money(info['debt'])} 元，"
            f"可用资金 {engine.fmt_money(info['cash'])} 元",
        ]
        return event.plain_result("\n".join(lines))

    async def _cmd_limit_buy(self, event, namespace, cfg, rest):
        return await self._place_limit(event, namespace, cfg, rest, "buy")

    async def _cmd_limit_sell(self, event, namespace, cfg, rest):
        return await self._place_limit(event, namespace, cfg, rest, "sell")

    async def _place_limit(self, event, namespace, cfg, rest, side: str):
        key = "买单" if side == "buy" else "卖单"
        if len(rest) < 3:
            raise MarketError(USAGE[key])
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            result = engine.place_order(state, user, side, rest[0], rest[1], rest[2], cfg)
            order = dict(result["order"])
            stock = result["stock"]
            executed_at = result["executed_at"]

        label = "买入" if side == "buy" else "卖出"
        if executed_at:
            return event.plain_result(
                f"{label}单已立即成交：{engine.display_label(stock)} × {order['shares']} 股\n"
                f"成交价 {engine.fmt_money(executed_at, comma=False)} 元"
                f"（优于限价 {engine.fmt_money(order['limit'], comma=False)} 元）"
            )

        frozen = (
            f"冻结 {engine.fmt_money(order['frozen'])} 元"
            if side == "buy"
            else f"冻结 {order['shares']} 股"
        )
        return event.plain_result(
            f"已挂{label}单 #{order['id']}：{engine.display_label(stock)} "
            f"{order['shares']} 股，限价 {engine.fmt_money(order['limit'], comma=False)} 元\n"
            f"{frozen}，{cfg.order_ttl_hours} 小时未成交自动撤销。"
        )

    async def _cmd_orders(self, event, namespace, cfg, rest):
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            orders = sorted(
                (o for o in state.get("orders", {}).values() if o["user"] == user),
                key=lambda o: int(o["id"]),
            )
            round_no = int(state["round"])

        if not orders:
            return event.plain_result("当前没有挂单。")
        lines = ["【我的挂单】"]
        for order in orders:
            side = "买入" if order["side"] == "buy" else "卖出"
            remain = max(0, int(order["expire_round"]) - round_no)
            lines.append(
                f"#{order['id']} {side} {order['code']} {order['shares']} 股 "
                f"@ {engine.fmt_money(order['limit'], comma=False)} 元（还剩 {remain} 轮）"
            )
        lines.append(f"共 {len(orders)} 笔 · 发「/股市 撤单 编号」可撤销")
        return event.plain_result("\n".join(lines))

    async def _cmd_cancel(self, event, namespace, cfg, rest):
        if not rest:
            raise MarketError(USAGE["撤单"])
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            order = engine.cancel_order(state, user, rest[0])
        return event.plain_result(
            f"已撤销挂单 #{order['id']}：{order['code']} {order['shares']} 股，"
            f"冻结的{'资金' if order['side'] == 'buy' else '股份'}已退回。"
        )

    async def _cmd_trades(self, event, namespace, cfg, rest):
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            trades = [t for t in state.get("trades", []) if t["user"] == user][-10:]

        if not trades:
            return event.plain_result("还没有成交记录。")
        lines = ["【我的最近成交】"]
        for trade in reversed(trades):
            side = "买入" if trade["side"] == "buy" else "卖出"
            kind = "市价" if trade["kind"] == "market" else "限价"
            lines.append(
                f"{engine.fmt_time(trade['ts'])} {side}{kind} {trade['code']} "
                f"{trade['shares']} 股 @ {engine.fmt_money(trade['price'], comma=False)} 元 "
                f"费 {engine.fmt_money(trade['fee'])} 元"
            )
        return event.plain_result("\n".join(lines))

    async def _cmd_news(self, event, namespace, cfg, rest):
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            news = state.get("news", [])[-5:]

        if not news:
            return event.plain_result("【最近市场新闻 · UTC+8】\n暂无新闻。")
        lines = ["【最近市场新闻 · UTC+8】"]
        for item in reversed(news):
            lines.append(f"{engine.fmt_time(item['ts'])} {item['text']}")
        lines.append("新闻会在当期盘面中体现，可发「/股市 走势 代码」查看。")
        return event.plain_result("\n".join(lines))

    async def _cmd_chart(self, event, namespace, cfg, rest):
        if not rest:
            raise MarketError(USAGE["走势"])
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            stock = engine.find_stock(state, rest[0])
            snapshot = {
                "code": stock["code"],
                "name": stock.get("name") or "",
                "price": int(stock["price"]),
                "history": [list(item) for item in stock.get("history", [])],
            }
            text = textfmt.chart_text(snapshot)
            out = self.store.cache_file(
                f"chart_{namespace}_{snapshot['code']}_{int(state['round'])}.png"
            )

        if not self._card_ok(cfg):
            return event.plain_result(text)
        try:
            render.render_chart(snapshot, out=out)
        except Exception:
            logger.exception("[stock] 走势卡渲染失败，回退文字")
            return event.plain_result(text)
        return event.image_result(str(out))

    async def _cmd_rename(self, event, namespace, cfg, rest):
        self._require_rename_admin(event, cfg)
        if len(rest) < 2:
            raise MarketError(USAGE["改名"])
        code = rest[0].upper()
        name = " ".join(rest[1:]).strip()
        if not name:
            raise MarketError("请提供名称，例如「/股市 改名 ST001 星辰科技」。")
        if len(name) > 12:
            raise MarketError("名称最多 12 个字。")

        async with self._session(namespace, cfg) as state:
            if state is not None:
                engine.find_stock(state, code)  # 校验代码是否存在
                state["stocks"][code]["name"] = name
            elif not engine.is_known_code(code):
                raise MarketError(f"没有找到股票「{rest[0]}」。")
            # 名称写入全局表，所有群下一次指令自动同步
            await self.names.set_name(code, name)

        return event.plain_result(f"已将 {code} 改名为「{name}」（所有群同步生效）。")

    async def _cmd_checkin(self, event, namespace, cfg, rest):
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            info = engine.checkin(state, user, cfg)
            notes = engine.pop_notices(engine.require_account(state, user))

        lines = [*notes, f"签到成功！获得 {engine.fmt_money(info['amount'])} 元"]
        if info["streak"] > 1:
            lines.append(
                f"连续签到 {info['streak']} 天，奖励加成 +{info['bonus_pct']:.0f}%"
            )
        if info.get("repaid"):
            lines.append(f"其中 {engine.fmt_money(info['repaid'])} 元自动还债")
        if info.get("debt"):
            lines.append(f"剩余欠款 {engine.fmt_money(info['debt'])} 元")
        lines.append(f"可用资金: {engine.fmt_money(info['cash'])} 元")
        return event.plain_result("\n".join(lines))

    async def _cmd_relief(self, event, namespace, cfg, rest):
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            info = engine.relief(state, user, cfg)
            notes = engine.pop_notices(engine.require_account(state, user))

        lines = [
            *notes,
            f"补助到账：{engine.fmt_money(info['amount'])} 元",
            f"补助前净资产 {engine.fmt_money(info['total_before'])} 元"
            f"（低于 {engine.fmt_money(cfg.relief_threshold)} 元可领）",
        ]
        if info.get("repaid"):
            lines.append(f"其中 {engine.fmt_money(info['repaid'])} 元自动还债")
        if info.get("debt"):
            lines.append(f"剩余欠款 {engine.fmt_money(info['debt'])} 元")
        lines.append(
            f"下次可领需等 {cfg.relief_cooldown_hours} 小时，"
            f"可用资金: {engine.fmt_money(info['cash'])} 元"
        )
        return event.plain_result("\n".join(lines))

    async def _cmd_rank(self, event, namespace, cfg, rest):
        limit = cfg.rank_size
        if rest and rest[0].isdigit():
            limit = max(1, min(50, int(rest[0])))
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state)
            rows = engine.rank(state, cfg, limit)
            round_no = int(state["round"])
            updated = engine.fmt_time(time.time())
            text = textfmt.rank_text(rows, round_no)
            out = self.store.cache_file(f"rank_{namespace}_{round_no}_{limit}.png")

        if not rows:
            return event.plain_result("本群还没有人开户，发「/股市 开户」开始。")
        if not self._card_ok(cfg):
            return event.plain_result(text)
        try:
            render.render_rank(rows, round_no, updated, out=out)
        except Exception:
            logger.exception("[stock] 排行卡渲染失败，回退文字")
            return event.plain_result(text)
        return event.image_result(str(out))

    async def _cmd_profit(self, event, namespace, cfg, rest):
        limit = cfg.rank_size
        if rest and rest[0].isdigit():
            limit = max(1, min(50, int(rest[0])))
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state)
            data = engine.profit_rank(state, cfg, limit)
            round_no = int(state["round"])
            updated = engine.fmt_time(time.time())
            text = textfmt.profit_text(data, round_no)
            out = self.store.cache_file(f"profit_{namespace}_{round_no}_{limit}.png")

        if not data["gainers"] and not data["losers"]:
            return event.plain_result("本群还没有人产生盈亏，先做几笔买卖吧。")
        if not self._card_ok(cfg):
            return event.plain_result(text)
        try:
            render.render_profit(data, round_no, updated, out=out)
        except Exception:
            logger.exception("[stock] 盈亏卡渲染失败，回退文字")
            return event.plain_result(text)
        return event.image_result(str(out))

    async def _cmd_gamble(self, event, namespace, cfg, rest):
        if not rest:
            raise MarketError(USAGE["db"])
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            info = engine.gamble(state, user, rest[0], cfg)

        net = int(info["net"])
        if net > 0:
            head = "🎉 手气不错！"
        elif net == 0:
            head = "😐 刚好回本。"
        else:
            head = "💸 这回亏了。"
        return event.plain_result(
            f"{head}\n"
            f"下注 {engine.fmt_money(info['bet'])} 元，"
            f"开奖 {engine.fmt_money(info['payout'])} 元\n"
            f"净变化 {engine.fmt_money(net)} 元\n"
            f"可用资金: {engine.fmt_money(info['cash'])} 元"
        )

    async def _cmd_rob(self, event, namespace, cfg, rest):
        user = self._user(event)
        target, target_name = self._at_target(event, rest)
        if not target:
            raise MarketError(USAGE["抢劫"])
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            if target_name:
                engine.touch_name(state, target, target_name)
            info = engine.rob(state, user, target, cfg)
            notes = engine.pop_notices(engine.require_account(state, user))

        who = target_name or target
        if info["success"]:
            lines = [
                *notes,
                f"抢劫成功！从 {who} 抢到 {engine.fmt_money(info['amount'])} 元",
                f"成功率 {info['chance']:.0f}%，实际抢走 {info['pct']:.1f}%",
            ]
            if info.get("repaid"):
                lines.append(f"其中 {engine.fmt_money(info['repaid'])} 元自动还债")
            if info.get("debt"):
                lines.append(f"剩余欠款 {engine.fmt_money(info['debt'])} 元")
            lines.extend(
                [
                    f"你的可用资金: {engine.fmt_money(info['cash'])} 元",
                    f"（{cfg.rob_cooldown_minutes} 分钟冷却中）",
                ]
            )
            return event.plain_result("\n".join(lines))
        if int(info["amount"]) <= 0:
            return event.plain_result(
                f"抢劫失败！不过你也没钱可被反抢…（成功率 {info['chance']:.0f}%）\n"
                f"（{cfg.rob_cooldown_minutes} 分钟冷却中）"
            )
        return event.plain_result(
            f"抢劫失败！反被 {who} 抢走 {engine.fmt_money(info['amount'])} 元\n"
            f"成功率 {info['chance']:.0f}%\n"
            f"你的可用资金: {engine.fmt_money(info['cash'])} 元\n"
            f"（{cfg.rob_cooldown_minutes} 分钟冷却中）"
        )

    async def _cmd_transfer(self, event, namespace, cfg, rest):
        user = self._user(event)
        target, target_name, amount = self._target_and_amount(event, rest)
        if not target or not amount:
            raise MarketError(USAGE["transfer"])
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            if target_name:
                engine.touch_name(state, target, target_name)
            info = engine.transfer(state, user, target, amount, cfg)

        lines = [
            f"转账成功：{engine.fmt_money(info['amount'])} 元 → {info['target_name']}",
            f"你的可用资金: {engine.fmt_money(info['cash'])} 元",
            f"对方可用资金: {engine.fmt_money(info['target_cash'])} 元",
        ]
        if info.get("target_repaid"):
            lines.append(
                f"对方有欠款，其中 {engine.fmt_money(info['target_repaid'])} 元已自动还债"
            )
        return event.plain_result("\n".join(lines))

    async def _cmd_give(self, event, namespace, cfg, rest):
        self._require_market_admin(event, cfg, "发钱")
        user = self._user(event)
        target, target_name, amount = self._target_and_amount(event, rest)
        if not target or not amount:
            raise MarketError(USAGE["give"])
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            if target_name:
                engine.touch_name(state, target, target_name)
            info = engine.give(state, user, target, amount, cfg)

        lines = [
            f"已发放 {engine.fmt_money(info['amount'])} 元给 {info['target_name']}",
            f"对方可用资金: {engine.fmt_money(info['target_cash'])} 元",
        ]
        if info.get("target_repaid"):
            lines.append(
                f"对方有欠款，其中 {engine.fmt_money(info['target_repaid'])} 元已自动还债"
            )
        return event.plain_result("\n".join(lines))

    async def _cmd_clear(self, event, namespace, cfg, rest):
        self._require_market_admin(event, cfg, "清零")
        user = self._user(event)
        target, target_name = self._at_target(event, rest)
        if not target:
            raise MarketError(USAGE["clear"])
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            if target_name:
                engine.touch_name(state, target, target_name)
            info = engine.clear_cash(state, target)

        return event.plain_result(
            f"已清空 {info['name']} 的可用资金（原有 {engine.fmt_money(info['before'])} 元）\n"
            "挂单已一并撤销，持仓保持不变。"
        )

    async def _cmd_help(self, event, namespace, cfg, rest):
        if not self._card_ok(cfg):
            return event.plain_result(textfmt.help_text(cfg))
        out = self.store.cache_file("help.png")
        values = {
            "capital": engine.fmt_money(cfg.initial_capital),
            "round": str(cfg.round_seconds),
            "max_pct": f"{cfg.max_change_pct:g}",
            "floor": engine.fmt_money(cfg.min_price),
            "fee": f"{cfg.fee_rate * 100:.3f}",
            "ttl": str(cfg.order_ttl_hours),
            "daily": engine.fmt_money(cfg.daily_bonus),
            "relief": engine.fmt_money(cfg.relief_threshold),
        }
        try:
            render.render_help(values, out=out)
        except Exception:
            logger.exception("[stock] 帮助卡渲染失败，回退文字")
            return event.plain_result(textfmt.help_text(cfg))
        return event.image_result(str(out))

    # ------------------------------------------------------------------ #

    @staticmethod
    def _detail_text(stock: dict[str, Any]) -> str:
        pct = engine.stock_pct(stock)
        history = [int(p) for _, p in stock.get("history", [])] or [int(stock["price"])]
        return (
            f"{engine.display_label(stock)}\n"
            f"现价 {engine.fmt_money(stock['price'], comma=False)} 元 {engine.fmt_pct(pct)}\n"
            f"区间最高 {engine.fmt_money(max(history), comma=False)} 元 · "
            f"最低 {engine.fmt_money(min(history), comma=False)} 元"
        )
