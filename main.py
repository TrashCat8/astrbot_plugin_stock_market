"""群内股市插件：开户、真实行情、买卖、限价单、外汇、走势。

指令统一挂在 `/股市` 下，未命中的子指令一律回落到帮助，
不会影响其它插件的 `/` 指令。
"""

from __future__ import annotations

import math
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter

try:  # 主动播报要用 MessageChain；老版本没有时退回随指令回复
    from astrbot.api.event import MessageChain
except Exception:  # pragma: no cover
    MessageChain = None
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .market import engine, interactive, render, textfmt
from .market.config import Config
from .market.engine import MarketError
from .market.feeds import QuoteFeed
from .market.storage import GroupStore, NameStore

PLUGIN_NAME = "astrbot_plugin_stock_market"

# 消息按钮要求 AstrBot >= 4.29.4；老版本自动退回纯文字
HAS_BUTTON_ACTION = hasattr(filter, "button_action")
HAS_LLM_TOOL = hasattr(filter, "llm_tool")

ALIASES = {
    "开户": "open",
    "行情": "quote",
    "汇市": "fx",
    "外汇": "fx",
    "fx": "fx",
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
    "复牌": "relist",
    "重新上市": "relist",
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
    "买入": "用法：/股市 买入 SH600519 100（外汇同理：/股市 买入 FX_USDCNY 1000）",
    "卖出": "用法：/股市 卖出 SH600519 100",
    "融资买入": "用法：/股市 融资买入 SH600519 100 2（2 倍杠杆）",
    "杠杆": "用法：/股市 杠杆（查看欠款与强平价）",
    "还债": "用法：/股市 还债 [金额]，不填金额表示全部还清",
    "买单": "用法：/股市 买单 SH600519 100 1430.00",
    "卖单": "用法：/股市 卖单 SH600519 100 1500.00",
    "撤单": "用法：/股市 撤单 1",
    "走势": "用法：/股市 走势 SH600519",
    "改名": "用法：/股市 改名 SH600519 茅台（需股市管理员）",
    "复牌": "用法：/股市 复牌 ST003 [价格]，不填价格按默认复牌价（需股市管理员）",
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
        cfg = Config.from_plugin_config(config)
        self.feed = QuoteFeed(self.store.cache_file("quotes.json"), timeout=cfg.quote_timeout)
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
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        for comp in components:
            qq = getattr(comp, "qq", None)
            if qq is None:
                continue
            qq = str(qq)
            # 跳过 @全体成员 和 @机器人自己（否则先 @bot 再 @对方时会选错目标）
            if not qq.isdigit() or qq == self_id:
                continue
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
        """「@某人 金额」或「QQ号 金额」两种写法都支持。

        AstrBot 会把 @ 一起渲染进 message_str（形如「@昵称(QQ号)」，昵称里还可能有空格），
        所以用 @ 判断目标时，金额一律取最后一段，避免把 @ 文本当成金额。
        """
        target, name = self._at_from_chain(event)
        if target:
            return target, name, (rest[-1] if rest else "")
        tokens = [token for token in rest if token and not token.startswith("@")]
        if len(tokens) >= 2:
            return tokens[0].lstrip("@"), "", tokens[1]
        return "", "", ""

    @asynccontextmanager
    async def _session(
        self, namespace: str, cfg: Config, create: bool = False
    ) -> AsyncIterator[dict[str, Any] | None]:
        """加载账本、同步行情，退出时原子落盘。同时刻同群指令串行执行。"""
        async with self.store.lock(namespace):
            state = self.store.load(namespace)
            if state is None and create:
                state = engine.new_state(namespace, time.time(), cfg)
            if state is not None:
                quotes = None
                if cfg.data_source == "real" and cfg.instruments:
                    # 行情抓取失败时 feeds 会退回上一帧，交易不受影响
                    quotes = await self.feed.snapshot(cfg.instruments, cfg.refresh_seconds)
                engine.ensure_instruments(state, cfg)
                # 股票名称是全局的，每次进群先套用最新的名称表
                self.names.apply(state)
                engine.advance(state, time.time(), cfg, quotes=quotes)
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
    # 消息交互：按钮（keyboard）与文字指令链
    # ------------------------------------------------------------------ #

    @staticmethod
    def _umo(event: AstrMessageEvent) -> str:
        value = getattr(event, "unified_msg_origin", None)
        if value:
            return str(value)
        getter = getattr(event, "get_unified_msg_origin", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception:
                pass
        return str(getattr(event, "session_id", "") or "")

    @staticmethod
    def _platform_label(event: AstrMessageEvent) -> str:
        parts: list[str] = []
        for name in ("get_platform_name", "get_platform_id"):
            fn = getattr(event, name, None)
            if callable(fn):
                try:
                    parts.append(str(fn() or ""))
                except Exception:
                    continue
        return " ".join(parts).lower()

    def _is_qq_official(self, event: AstrMessageEvent) -> bool:
        """文字指令链只在 QQ 官方（走 Markdown）可用，其它平台会显示成乱标签。"""
        label = self._platform_label(event)
        return "official" in label and ("qq" in label or "q.qq" in label)

    @staticmethod
    def _is_button_event(event: AstrMessageEvent) -> bool:
        fn = getattr(event, "is_button_interaction", None)
        try:
            return bool(fn()) if callable(fn) else False
        except Exception:
            return False

    @staticmethod
    def _plain_text_of(result) -> str | None:
        """纯文字结果返回正文，图片/ActionRow 等复合结果返回 None。"""
        chain = getattr(result, "chain", None)
        if not chain:
            return None
        if not all(type(comp).__name__ in ("Plain", "Text") for comp in chain):
            return None
        fn = getattr(result, "get_plain_text", None)
        if callable(fn):
            try:
                return str(fn() or "")
            except Exception:
                pass
        return "\n".join(str(getattr(comp, "text", "") or "") for comp in chain)

    def _interactive(
        self,
        event: AstrMessageEvent,
        cfg: Config,
        result,
        rows=(),
        links=(),
    ):
        """给指令结果挂按钮行/文字链。

        按钮点击本身产生的新消息不再挂按钮（面板走 `_panel`），避免「点一次长一条」；
        QQ 官方对交互事件的回复只接受文字，因此按钮事件下的卡片命令统一走文字分支。
        """
        if result is None or self._is_button_event(event):
            return result
        if not interactive.available():
            return result

        action_rows = (
            interactive.button_rows(rows, "快捷操作")
            if cfg.interactive_buttons and rows
            else []
        )
        want_links = bool(cfg.text_chain and links and self._is_qq_official(event))
        if not action_rows and not want_links:
            return result

        items = list(result) if isinstance(result, (list, tuple)) else [result]
        head, last = items[:-1], items[-1]

        plain = self._plain_text_of(last)
        if plain is not None:
            if len(plain) > 1500:
                # 平台文本长度有限，超长正文先保消息本身，按钮/链一律不加
                return result
            text = interactive.with_links(plain, links) if want_links else plain
            chain = [interactive.Plain(text), *action_rows]
            return [*head, event.chain_result(chain)]

        # 图片等非文字结果：按钮单独发一条，避免平台拒绝「媒体 + keyboard」
        if not action_rows:
            return result
        return [*items, event.chain_result([interactive.Plain("快捷操作"), *action_rows])]

    def _panel(self, event: AstrMessageEvent, cfg: Config, text: str, rows, links=()):
        """下钻面板：按钮事件里也要带按钮，所以不走 `_interactive` 的抑制逻辑。"""
        body = (
            interactive.with_links(text, links)
            if cfg.text_chain and links and self._is_qq_official(event)
            else text
        )
        chain: list[Any] = [interactive.Plain(body)]
        if cfg.interactive_buttons and rows:
            chain.extend(interactive.button_rows(rows, "快捷操作"))
        return event.chain_result(chain)

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

        # 本条指令可能把某人净值打到 ≤0：群内播报一次
        for line in await self._notify_broke(event, cfg, namespace):
            yield event.plain_result(line)

    # ------------------------------------------------------------------ #
    # 各子指令
    # ------------------------------------------------------------------ #

    async def _notify_broke(self, event, cfg: Config, namespace: str) -> list[str]:
        """检测净资产归零的人，返回需要随指令回复的兜底文案（主动发送成功则为空）。"""
        if not cfg.broke_broadcast:
            return []
        async with self.store.lock(namespace):
            state = self.store.load(namespace)
            if state is None:
                return []
            alerts = engine.detect_broke(state)
            if not alerts:
                return []
            self.store.save(namespace, state)

        texts = [
            f"💥 {alert['name']} 空中飞人了！净资产 {engine.fmt_money(alert['net'])} 元"
            for alert in alerts
        ]
        umo = self._umo(event)
        if (
            self.context is not None
            and umo
            and MessageChain is not None
            and interactive.Plain is not None
        ):
            try:
                for text in texts:
                    await self.context.send_message(
                        umo, MessageChain([interactive.Plain(text)])
                    )
                return []
            except Exception:
                logger.warning("[stock] 破产播报主动发送失败，改为随指令回复")
        return texts

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

    async def _cmd_quote(self, event, namespace, cfg, rest, kind: str | None = "stock"):
        user = self._user(event)
        text_only = self._is_button_event(event)
        arg = rest[0] if rest else ""
        if arg.lower() in ("fx", "外汇", "汇市"):
            kind = "fx"
            arg = rest[1] if len(rest) > 1 else ""
        elif arg.lower() in ("all", "全部"):
            kind = None
            arg = rest[1] if len(rest) > 1 else ""
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            engine.require_account(state, user)

            if arg and not arg.isdigit():
                stock = engine.find_stock(state, arg)
                detail = self._detail_text(stock, cfg, int(state["round"]))
                return self._interactive(
                    event,
                    cfg,
                    event.plain_result(detail),
                    () if text_only else [interactive.stock_trade_row(stock["code"])],
                    interactive.stock_links(
                        stock["code"], engine.fmt_price(stock, int(stock["price"]))
                    ),
                )

            page_no = int(arg) if arg.isdigit() else 1
            rows, page, pages = engine.quote_page(state, page_no, cfg, kind=kind)
            if not rows:
                return self._interactive(
                    event,
                    cfg,
                    event.plain_result(
                        "本群还没有配置外汇品种，请在插件配置里开启「外汇交易」。"
                        if kind == "fx"
                        else "本群还没有配置任何品种。"
                    ),
                    (),
                    (),
                )
            round_no = int(state["round"])
            updated = engine.fmt_time(time.time())
            title = "本群汇市" if kind == "fx" else "本群行情"
            refresh = cfg.refresh_seconds if cfg.data_source == "real" else cfg.round_seconds
            text = textfmt.quote_text(
                rows, page, pages, round_no, refresh, title=title
            )
            out = self.store.cache_file(
                f"quote_{namespace}_{kind or 'all'}_{page}_{round_no}.png"
            )

        if text_only or not self._card_ok(cfg):
            return self._interactive(
                event,
                cfg,
                event.plain_result(text),
                () if text_only else [interactive.NAV_BUTTONS],
                interactive.HELP_LINKS,
            )
        try:
            render.render_quote(
                rows,
                page,
                pages,
                round_no,
                updated,
                refresh_seconds=refresh,
                fee_rate=cfg.fee_rate,
                title=title,
                live=cfg.data_source == "real",
                out=out,
            )
        except Exception:
            logger.exception("[stock] 行情卡渲染失败，回退文字")
            return self._interactive(
                event,
                cfg,
                event.plain_result(text),
                [interactive.NAV_BUTTONS],
                interactive.HELP_LINKS,
            )
        return self._interactive(
            event,
            cfg,
            event.image_result(str(out)),
            [interactive.NAV_BUTTONS],
            interactive.HELP_LINKS,
        )

    async def _cmd_fx(self, event, namespace, cfg, rest):
        return await self._cmd_quote(event, namespace, cfg, rest, kind="fx")

    async def _cmd_position(self, event, namespace, cfg, rest):
        user = self._user(event)
        text_only = self._is_button_event(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            summary = engine.account_summary(state, user)
            notes = engine.pop_notices(engine.require_account(state, user))
            round_no = int(state["round"])
            updated = engine.fmt_time(time.time())

            # 持仓可能很多（17 个品种全买），按 page_size 分页，避免一张卡长到看不清；
            # 持仓行高比行情行高，单页再压到最多 6 行
            size = max(1, min(5, cfg.page_size))
            all_rows = summary["positions"]
            total = len(all_rows)
            pages = max(1, math.ceil(total / size))
            page = int(rest[0]) if rest and rest[0].isdigit() else 1
            page = max(1, min(pages, page))
            view = dict(summary)
            view["positions"] = all_rows[(page - 1) * size : page * size]

            text = textfmt.position_text(view, round_no, page, pages, total)
            out = self.store.cache_file(
                f"position_{namespace}_{page}_{round_no}.png"
            )

        if text_only or not self._card_ok(cfg):
            return self._interactive(
                event,
                cfg,
                self._with_notes(event, notes, event.plain_result(text)),
                () if text_only else interactive.holding_rows(view["positions"]),
                interactive.SELL_LINKS,
            )
        try:
            render.render_position(
                view,
                round_no,
                updated,
                out=out,
                page=page,
                pages=pages,
                total_positions=total,
            )
        except Exception:
            logger.exception("[stock] 持仓卡渲染失败，回退文字")
            return self._interactive(
                event,
                cfg,
                self._with_notes(event, notes, event.plain_result(text)),
                interactive.holding_rows(view["positions"]),
                interactive.SELL_LINKS,
            )
        return self._interactive(
            event,
            cfg,
            self._with_notes(event, notes, event.image_result(str(out))),
            interactive.holding_rows(view["positions"]),
            interactive.SELL_LINKS,
        )

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
            f"买入成功：{trade['label']} × {trade['shares']} {trade['unit']}",
            f"成交价 {trade['price_text']} 元/{trade['unit']}，"
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
            f"卖出成功：{trade['label']} × {trade['shares']} {trade['unit']}",
            f"成交价 {trade['price_text']} 元/{trade['unit']}，"
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
            f"融资买入成功：{trade['label']} × {trade['shares']} {trade['unit']}"
            f"（{trade['leverage']:g} 倍）",
            f"成交价 {trade['price_text']} 元/{trade['unit']}，"
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
            lines.append("没有杠杆持仓。用法：/股市 融资买入 SH600519 100 2")
        for row in info["positions"]:
            lines.append(
                f"{row['label']}：{row['shares']} {row['unit']}，"
                f"欠款 {engine.fmt_money(row['debt'])} 元"
            )
            lines.append(
                f"  现价 {row['price_text']} · 市值 {engine.fmt_money(row['value'])} 元 · "
                f"权益比 {row['ratio']:.1f}% · "
                f"强平价 {row['liq_price_text']} 元"
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
        unit = engine.unit_label(stock)
        if executed_at:
            return event.plain_result(
                f"{label}单已立即成交：{engine.display_label(stock)} × {order['shares']} {unit}\n"
                f"成交价 {engine.fmt_price(stock, executed_at)} 元/{unit}"
                f"（优于限价 {engine.fmt_price(stock, int(order['limit']))} 元）"
            )

        frozen = (
            f"冻结 {engine.fmt_money(order['frozen'])} 元"
            if side == "buy"
            else f"冻结 {order['shares']} {unit}"
        )
        return event.plain_result(
            f"已挂{label}单 #{order['id']}：{engine.display_label(stock)} "
            f"{order['shares']} {unit}，限价 {engine.fmt_price(stock, int(order['limit']))} 元\n"
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
            lines = ["【我的挂单】"]
            for order in orders:
                stock = state["stocks"].get(order["code"]) or {"code": order["code"]}
                side_text = "买入" if order["side"] == "buy" else "卖出"
                remain = max(0, int(order["expire_round"]) - round_no)
                hours = remain * cfg.round_seconds / 3600
                remain_text = f"{hours:.1f} 小时" if hours >= 1 else f"{max(1, int(hours * 60))} 分钟"
                lines.append(
                    f"#{order['id']} {side_text} {order['code']} {order['shares']} "
                    f"{engine.unit_label(stock)} @ "
                    f"{engine.fmt_price(stock, int(order['limit']))} 元（还剩约 {remain_text}）"
                )

        if not orders:
            return event.plain_result("当前没有挂单。")
        lines.append(f"共 {len(orders)} 笔 · 发「/股市 撤单 编号」可撤销")
        return event.plain_result("\n".join(lines))

    async def _cmd_cancel(self, event, namespace, cfg, rest):
        if not rest:
            raise MarketError(USAGE["撤单"])
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            order = engine.cancel_order(state, user, rest[0])
            stock = state["stocks"].get(order["code"]) or {"code": order["code"]}
            unit = engine.unit_label(stock)
        return event.plain_result(
            f"已撤销挂单 #{order['id']}：{order['code']} {order['shares']} {unit}，"
            f"冻结的{'资金' if order['side'] == 'buy' else '持仓'}已退回。"
        )

    async def _cmd_trades(self, event, namespace, cfg, rest):
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            trades = [t for t in state.get("trades", []) if t["user"] == user][-10:]
            lines = ["【我的最近成交】"]
            kinds = {
                "market": "市价",
                "limit": "限价",
                "margin": "融资",
                "liquidate": "强平",
                "delist": "清盘",
            }
            for trade in reversed(trades):
                stock = state["stocks"].get(trade["code"]) or {"code": trade["code"]}
                side_text = "买入" if trade["side"] == "buy" else "卖出"
                kind_text = kinds.get(trade["kind"], "限价")
                lines.append(
                    f"{engine.fmt_time(trade['ts'])} {side_text}{kind_text} {trade['code']} "
                    f"{trade['shares']} {engine.unit_label(stock)} @ "
                    f"{engine.fmt_price(stock, int(trade['price']))} 元 "
                    f"费 {engine.fmt_money(trade['fee'])} 元"
                )

        if not trades:
            return event.plain_result("还没有成交记录。")
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
        text_only = self._is_button_event(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            stock = engine.find_stock(state, rest[0])
            snapshot = {
                "code": stock["code"],
                "name": stock.get("name") or "",
                "kind": stock.get("kind") or "stock",
                "price": int(stock["price"]),
                "price_scale": engine.price_scale(stock),
                "price_digits": engine.price_digits(stock),
                "prev": int(stock.get("prev") or stock["price"]),
                "quote_ts": stock.get("quote_ts"),
                "history": [list(item) for item in stock.get("history", [])],
            }
            text = textfmt.chart_text(snapshot)
            out = self.store.cache_file(
                f"chart_{namespace}_{snapshot['code']}_{int(state['round'])}.png"
            )

        if text_only or not self._card_ok(cfg):
            return self._interactive(event, cfg, event.plain_result(text), (), ())
        try:
            render.render_chart(snapshot, out=out)
        except Exception:
            logger.exception("[stock] 走势卡渲染失败，回退文字")
            return self._interactive(event, cfg, event.plain_result(text), (), ())
        return self._interactive(event, cfg, event.image_result(str(out)), (), ())

    async def _cmd_relist(self, event, namespace, cfg, rest):
        self._require_market_admin(event, cfg, "复牌")
        if not rest:
            raise MarketError(USAGE["复牌"])
        user = self._user(event)
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            self._mark_name(event, state, user)
            info = engine.relist(state, rest[0], rest[1] if len(rest) > 1 else "", cfg)
            label = info["label"]
            price_text = engine.fmt_price(info["stock"], int(info["price"]))

        return event.plain_result(
            f"已复牌：{label}\n"
            f"起售价 {price_text} 元"
            f"（退市线 {engine.fmt_money(cfg.delist_price)} 元），行情从下一轮继续。"
        )

    async def _cmd_rename(self, event, namespace, cfg, rest):
        self._require_rename_admin(event, cfg)
        if len(rest) < 2:
            raise MarketError(USAGE["改名"])
        code = rest[0].upper()
        name = " ".join(rest[1:]).strip()
        if not name:
            raise MarketError("请提供名称，例如「/股市 改名 SH600519 茅台」。")
        if len(name) > 12:
            raise MarketError("名称最多 12 个字。")

        async with self._session(namespace, cfg) as state:
            if state is not None:
                engine.find_stock(state, code)  # 校验代码是否存在
                state["stocks"][code]["name"] = name
            elif not engine.is_known_code(code, cfg):
                raise MarketError(f"没有找到品种「{rest[0]}」。")
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
        text_only = self._is_button_event(event)
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
        if text_only or not self._card_ok(cfg):
            return self._interactive(event, cfg, event.plain_result(text), (), ())
        try:
            render.render_rank(rows, round_no, updated, out=out)
        except Exception:
            logger.exception("[stock] 排行卡渲染失败，回退文字")
            return self._interactive(event, cfg, event.plain_result(text), (), ())
        return self._interactive(event, cfg, event.image_result(str(out)), (), ())

    async def _cmd_profit(self, event, namespace, cfg, rest):
        limit = cfg.rank_size
        text_only = self._is_button_event(event)
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
        if text_only or not self._card_ok(cfg):
            return self._interactive(event, cfg, event.plain_result(text), (), ())
        try:
            render.render_profit(data, round_no, updated, out=out)
        except Exception:
            logger.exception("[stock] 盈亏卡渲染失败，回退文字")
            return self._interactive(event, cfg, event.plain_result(text), (), ())
        return self._interactive(event, cfg, event.image_result(str(out)), (), ())

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
        text_only = self._is_button_event(event)
        if text_only or not self._card_ok(cfg):
            return self._interactive(
                event,
                cfg,
                event.plain_result(textfmt.help_text(cfg)),
                () if text_only else [interactive.NAV_BUTTONS],
                interactive.HELP_LINKS,
            )
        out = self.store.cache_file("help.png")
        values = {
            "capital": engine.fmt_money(cfg.initial_capital),
            "round": str(cfg.round_seconds),
            "max_pct": f"{cfg.max_change_pct:g}",
            "floor": engine.fmt_money(cfg.min_price),
            "source_rule": textfmt.source_rule_text(cfg),
            "news_rule": textfmt.news_rule_text(cfg),
            "delist_rule": textfmt.delist_rule_text(cfg),
            "fee": f"{cfg.fee_rate * 100:.3f}",
            "ttl": str(cfg.order_ttl_hours),
            "daily": engine.fmt_money(cfg.daily_bonus),
            "relief": engine.fmt_money(cfg.relief_threshold),
        }
        try:
            render.render_help(values, out=out)
        except Exception:
            logger.exception("[stock] 帮助卡渲染失败，回退文字")
            return self._interactive(
                event,
                cfg,
                event.plain_result(textfmt.help_text(cfg)),
                [interactive.NAV_BUTTONS],
                interactive.HELP_LINKS,
            )
        return self._interactive(
            event,
            cfg,
            event.image_result(str(out)),
            [interactive.NAV_BUTTONS],
            interactive.HELP_LINKS,
        )

    # ------------------------------------------------------------------ #
    # 按钮回调：点击卡片下方的按钮，等价于发一条对应指令
    # 需要 AstrBot >= 4.29.4（filter.button_action + 内置按钮组件）
    # ------------------------------------------------------------------ #

    if HAS_BUTTON_ACTION and interactive.available():

        @filter.button_action()
        async def on_stock_button(self, event: AstrMessageEvent):
            """所有 stock:* 按钮的统一入口：按 action_id 分发，非本插件的按钮直接忽略。"""
            async for result in self._route_button(event):
                yield result

    async def _route_button(self, event: AstrMessageEvent):
        getter = getattr(event, "get_button_interaction", None)
        interaction = getter() if callable(getter) else None
        if interaction is None:
            return
        action = str(getattr(interaction, "action_id", "") or "")
        if not action.startswith("stock:"):
            return
        data = getattr(interaction, "data", None)
        data = data if isinstance(data, dict) else {}
        cfg = self._cfg()
        if not self._allowed(event, cfg):
            return
        act = action.split(":", 1)[1]
        namespace = self._namespace(event)
        user = self._user(event)

        # 直接对应指令的按钮
        direct = {
            "quote": "行情",
            "fx": "汇市",
            "position": "持仓",
            "checkin": "签到",
            "rank": "排行",
            "profit": "盈亏",
            "relief": "补助",
            "news": "新闻",
            "help": "帮助",
            "orders": "挂单",
            "trades": "成交",
            "leverage": "杠杆",
        }
        if act in direct:
            async for result in self.market(event, direct[act]):
                yield result
            return

        if act == "home":
            yield self._panel(
                event,
                cfg,
                "【快捷面板】点按钮进入对应功能；需要填数量的操作，点下面的指令链改参数。",
                [interactive.NAV_BUTTONS],
            )
            return

        if act == "trade":
            yield self._panel(
                event,
                cfg,
                "【交易】按钮只负责翻页，不直接下单。\n"
                "点下面指令链 → 把数量和价格改成你的 → 发送。",
                [interactive.TRADE_ROWS[0], interactive.TRADE_ROWS[1]],
                interactive.BUY_LINKS,
            )
            return

        if act == "play":
            yield self._panel(
                event,
                cfg,
                "【玩法】签到 / 补助 / 排行 / 盈亏点按钮直接执行；\n"
                "db、抢劫、转账要填金额或对象，点指令链补上。",
                [interactive.PLAY_ROWS[0], interactive.PLAY_ROWS[1]],
                interactive.PLAY_LINKS,
            )
            return

        if act in ("buy", "sell"):
            async for result in self._pick_panel(event, cfg, namespace, user, act):
                yield result
            return

        if act.startswith(("qbuy", "qsell")):
            async for result in self._ask_order(event, cfg, namespace, user, act, data):
                yield result
            return

        if act == "confirm_order":
            async for result in self._do_order(event, namespace, cfg, user, data):
                yield result
            return

        if act == "pick":
            async for result in self._stock_panel(
                event, cfg, namespace, user, str(data.get("code") or "")
            ):
                yield result
            return

        if act in ("limitbuy", "limitsell"):
            yield self._panel(
                event,
                cfg,
                "【限价单】点指令链后把价格改成你的目标价；达到限价按当时更优价成交。",
                [interactive.NAV_BUTTONS],
                interactive.LIMIT_LINKS,
            )
            return

        if act in ("margin", "repay", "dice", "rob", "transfer"):
            text = {
                "margin": "【融资】用法：/股市 融资买入 代码 数量 倍数，例如 SH600519 100 2",
                "repay": "【还债】点指令链，或直接发送 /股市 还债。",
                "dice": "【db】点指令链把 1000 改成你的下注金额。",
                "rob": "【抢劫】点指令链，在空格后 @ 对方或填 QQ 号。",
                "transfer": "【转账】点指令链，补上 @对方 和金额。",
            }[act]
            links = interactive.PLAY_LINKS
            if act == "margin":
                links = (("/股市 融资买入 SH600519 100 2", "融资买茅台"),)
            yield self._panel(event, cfg, text, [interactive.NAV_BUTTONS], links)
            return

        if act == "chart":
            code = str(data.get("code") or "")
            if code:
                async for result in self.market(event, f"走势 {code}"):
                    yield result
            else:
                yield self._panel(
                    event,
                    cfg,
                    "先查详情再点走势，或发送 /股市 走势 代码。",
                    [interactive.NAV_BUTTONS],
                )
            return

        if act.startswith("ask_sell"):
            async for result in self._ask_sell(event, cfg, namespace, user):
                yield result
            return

        if act == "confirm_sell":
            async for result in self._do_sell(event, cfg, namespace, user, data):
                yield result
            return

        if act == "cancel":
            yield event.plain_result("已取消。")
            return

    async def _pick_panel(self, event, cfg, namespace, user, side: str):
        """选品种：持仓优先，再补行情里的品种。"""
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            acc = engine.require_account(state, user)
            held = [
                code
                for code, pos in (acc.get("positions") or {}).items()
                if int(pos.get("shares", 0)) > 0
            ]
            codes = held[:5]
            for code in sorted(state["stocks"]):
                if len(codes) >= 10:
                    break
                if code not in codes:
                    codes.append(code)
            names = {c: engine.display_name(s) for c, s in state["stocks"].items()}

        rows = interactive.pick_rows(codes, names, side)
        rows.append([{"id": "stock:home", "label": "返回"}])
        title = "买入" if side == "buy" else "卖出"
        yield self._panel(
            event,
            cfg,
            f"【{title}】点品种 → 选数量 → 确认成交。",
            rows,
        )

    async def _stock_panel(self, event, cfg, namespace, user, code: str):
        """单个品种的现价 + 快捷下单按钮。"""
        if not code:
            yield event.plain_result("这个按钮没带品种信息，请从行情页重新点一次。")
            return
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            stock = engine.find_stock(state, code)
            unit = engine.unit_label(stock)
            price_text = engine.fmt_price(stock, int(stock["price"]))
            pct = engine.stock_pct(stock)
            label = engine.display_label(stock)
        yield self._panel(
            event,
            cfg,
            f"{label} · 现价 {price_text} 元/{unit} {engine.fmt_pct(pct)}\n"
            "点数量按钮 → 确认后成交。",
            [interactive.stock_trade_row(code)],
        )

    async def _ask_order(self, event, cfg, namespace, user, act: str, data: dict):
        """数量按钮 → 确认面板（显示预计金额，资金/持仓不足直接拦下）。"""
        side = "buy" if act.startswith("qbuy") else "sell"
        code = str(data.get("code") or "")
        try:
            shares = int(data.get("shares") or 0)
        except (TypeError, ValueError):
            shares = 0
        if not code:
            yield event.plain_result("这个按钮没带品种信息，请重新点一次。")
            return

        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            stock = engine.find_stock(state, code)
            acc = engine.require_account(state, user)
            unit = engine.unit_label(stock)
            price = int(stock["price"])
            price_text = engine.fmt_price(stock, price)
            label = engine.display_label(stock)
            if side == "sell" and shares <= 0:
                pos = engine.position_of(acc, stock["code"])
                shares = int(pos["shares"]) if pos else 0
            amount = engine.value_cents(stock, price, shares) if shares > 0 else 0
            fee = engine.calc_fee(amount, cfg)
            cash = int(acc.get("cash", 0))
            held = 0
            pos = engine.position_of(acc, stock["code"])
            if pos:
                held = int(pos["shares"]) - int(pos.get("frozen", 0))

        if shares <= 0:
            yield event.plain_result("这个持仓已经不在了。")
            return
        if side == "sell" and shares > held:
            yield event.plain_result(f"可卖数量不足：当前可卖 {held} {unit}。")
            return
        if side == "buy" and cash < amount + fee:
            yield event.plain_result(
                f"可用资金不足：需要 {engine.fmt_money(amount + fee)} 元，"
                f"当前可用 {engine.fmt_money(cash)} 元。"
            )
            return

        if side == "buy":
            cost_text = f"预计支出 {engine.fmt_money(amount + fee)} 元（含手续费 {engine.fmt_money(fee)} 元）"
        else:
            cost_text = f"预计到账 {engine.fmt_money(amount - fee)} 元（扣手续费 {engine.fmt_money(fee)} 元）"

        rows = [
            [
                {
                    "id": "stock:confirm_order",
                    "label": "确认成交",
                    "style": "danger" if side == "sell" else "primary",
                    "data": {"code": code, "side": side, "shares": shares},
                },
                {"id": "stock:cancel", "label": "取消"},
            ]
        ]
        yield self._panel(
            event,
            cfg,
            f"【确认{'买入' if side == 'buy' else '卖出'}】{label} {shares} {unit}\n"
            f"现价 {price_text} 元 · {cost_text}",
            rows,
        )

    async def _do_order(self, event, namespace, cfg, user, data: dict):
        """确认后真正下单：复用买入/卖出指令的结算与文案。"""
        code = str(data.get("code") or "")
        side = str(data.get("side") or "buy")
        try:
            shares = int(data.get("shares") or 0)
        except (TypeError, ValueError):
            shares = 0
        if not code or shares <= 0:
            yield event.plain_result("订单参数不完整，请重新点一次。")
            return
        try:
            if side == "sell":
                result = await self._cmd_sell(event, namespace, cfg, [code, str(shares)])
            else:
                result = await self._cmd_buy(event, namespace, cfg, [code, str(shares)])
        except MarketError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        if result is not None:
            yield result

    async def _position_links(self, event, namespace, cfg, user):
        """把用户当前持仓做成卖出指令链（最多 5 条）。"""
        links: list[tuple[str, str]] = []
        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            acc = engine.require_account(state, user)
            for code, pos in (acc.get("positions") or {}).items():
                shares = int(pos.get("shares", 0))
                if shares <= 0:
                    continue
                stock = state["stocks"].get(code)
                name = engine.display_name(stock) if stock else code
                links.append((f"/股市 卖出 {code} {shares}", f"卖{name[:4]}"))
        return tuple(links[:5]) or interactive.SELL_LINKS

    async def _ask_sell(self, event, cfg, namespace, user):
        """持仓行的「卖①」按钮：先确认再成交。"""
        getter = getattr(event, "get_button_interaction", None)
        interaction = getter() if callable(getter) else None
        data = getattr(interaction, "data", None) if interaction else None
        code = str((data or {}).get("code") or "") if isinstance(data, dict) else ""
        if not code:
            yield event.plain_result("这个按钮没有带上持仓信息，请从持仓页重新点一次。")
            return

        async with self._session(namespace, cfg) as state:
            state = self._require(state)
            stock = engine.find_stock(state, code)
            acc = engine.require_account(state, user)
            pos = engine.position_of(acc, stock["code"])
            shares = int(pos["shares"]) if pos else 0
            label = engine.display_label(stock)
            unit = engine.unit_label(stock)
            price_text = engine.fmt_price(stock, int(stock["price"]))

        if shares <= 0:
            yield event.plain_result("这个持仓已经不在了。")
            return

        rows = [
            [
                {
                    "id": "stock:confirm_sell",
                    "label": "确认卖出",
                    "style": "danger",
                    "data": {"code": code, "shares": shares},
                },
                {"id": "stock:cancel", "label": "取消"},
            ]
        ]
        yield self._panel(
            event,
            cfg,
            f"【确认卖出】{label} {shares} {unit}，按现价 {price_text} 元成交？",
            rows,
        )

    async def _do_sell(self, event, cfg, namespace, user, data):
        """二次确认后的市价卖出。"""
        code = str(data.get("code") or "")
        try:
            shares = int(data.get("shares") or 0)
        except (TypeError, ValueError):
            shares = 0
        if not code or shares <= 0:
            yield event.plain_result("卖出参数不完整，请从持仓页重新点一次。")
            return

        try:
            async with self._session(namespace, cfg) as state:
                state = self._require(state)
                self._mark_name(event, state, user)
                trade = engine.sell(state, user, code, str(shares), cfg)
                notes = engine.pop_notices(engine.require_account(state, user))
        except MarketError as exc:
            yield event.plain_result(f"❌ {exc}")
            return

        lines = [
            *notes,
            f"卖出成功：{trade['label']} × {trade['shares']} {trade['unit']}",
            f"成交价 {trade['price_text']} 元/{trade['unit']}，"
            f"手续费 {engine.fmt_money(trade['fee'])} 元",
            f"到账 {engine.fmt_money(trade['proceeds'])} 元，"
            f"本次盈亏 {engine.fmt_money(trade['realized'])} 元",
        ]
        if trade.get("repaid"):
            lines.append(f"偿还欠款 {engine.fmt_money(trade['repaid'])} 元")
        lines.append(f"可用资金: {engine.fmt_money(trade['cash'])} 元")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #

    @staticmethod
    def _detail_text(stock: dict[str, Any], cfg: Config, round_no: int) -> str:
        pct = engine.stock_pct(stock)
        history = [int(p) for _, p in stock.get("history", [])] or [int(stock["price"])]
        unit = engine.unit_label(stock)
        prev = int(stock.get("prev") or stock["price"])
        # 真实行情有当日高低就用它做区间，模拟模式退回历史点
        low = int(stock.get("low") or min(history))
        high = int(stock.get("high") or max(history))
        lines = [
            engine.display_label(stock),
            f"现价 {engine.fmt_price(stock, int(stock['price']))} 元/{unit} {engine.fmt_pct(pct)}",
            f"昨收 {engine.fmt_price(stock, prev)} 元 · "
            f"区间 {engine.fmt_price(stock, low)} ~ {engine.fmt_price(stock, high)} 元",
        ]
        if stock.get("open"):
            lines.append(
                f"今日开 {engine.fmt_price(stock, int(stock['open']))} · "
                f"高 {engine.fmt_price(stock, int(stock.get('high') or stock['price']))} · "
                f"低 {engine.fmt_price(stock, int(stock.get('low') or stock['price']))}"
            )
        if engine.is_delisted(stock):
            left = engine.rounds_until_relist(stock, round_no, cfg)
            tail = "\n状态 已退市（停牌），持仓已按退市价强制平仓"
            if left > 0:
                tail += (
                    f"；{left} 轮后自动复牌（起售价 "
                    f"{engine.fmt_price(stock, engine.auto_relist_price(stock, cfg))} 元）"
                )
            lines.append(tail + f"。管理员也可用「/股市 复牌 {stock['code']}」提前恢复。")
        return "\n".join(lines)

# ------------------------------------------------------------------ #
    # LLM 工具：让群友用自然语言玩股市
    # 需要 AstrBot >= 4.9.2（filter.llm_tool）；工具返回纯文本，由模型转述
    # ------------------------------------------------------------------ #

    if HAS_LLM_TOOL:

        @filter.llm_tool(name="stock_market")
        async def llm_market(
            self,
            event: AstrMessageEvent,
            market: str = "stock",
            keyword: str = "",
            page: int = 1,
        ):
            '''查看群内股市行情，返回现价与当日涨跌幅。用户问"茅台多少钱""汇率多少""看看行情"时使用。

            Args:
                market(string): stock=股票，fx=外汇，all=全部
                keyword(string): 品种代码或名称，例如 SH600519、FX_USDCNY、茅台；留空返回行情列表
                page(number): 列表页码，从 1 开始
            '''
            cfg = self._cfg()
            namespace = self._namespace(event)
            if not self._allowed(event, cfg):
                return "本群未启用股市插件。"
            try:
                async with self._session(namespace, cfg) as state:
                    state = self._require(state)
                    engine.require_account(state, self._user(event))
                    if keyword.strip():
                        stock = engine.find_stock(state, keyword.strip())
                        return self._detail_text(stock, cfg, int(state["round"]))
                    kind = {"fx": "fx", "all": None}.get(market.strip().lower(), "stock")
                    try:
                        page_no = max(1, int(page))
                    except (TypeError, ValueError):
                        page_no = 1
                    rows, cur, pages = engine.quote_page(state, page_no, cfg, kind=kind)
                    if not rows:
                        return "没有可显示的品种。"
                    title = {"fx": "外汇", "stock": "股票"}.get(kind or "", "全部品种")
                    lines = [f"{title}行情 第 {cur}/{pages} 页（金额单位：元）"]
                    for row in rows:
                        unit = "单位" if row.get("kind") == "fx" else "股"
                        lines.append(
                            f"{row['code']} {row.get('name') or ''} "
                            f"现价 {row['price_text']} 元/{unit}，当日 {row['pct']:+.2f}%"
                        )
                    return "\n".join(lines)
            except MarketError as exc:
                return f"查询失败：{exc}"

        @filter.llm_tool(name="stock_account")
        async def llm_account(self, event: AstrMessageEvent, view: str = "overview"):
            '''查看当前用户在群内股市的账户：持仓、盈亏、杠杆、挂单、成交。用户问"我赚了多少""我持仓什么"时使用。

            Args:
                view(string): overview=账户概览与持仓，leverage=杠杆欠款与强平价，orders=挂单，trades=最近成交
            '''
            cfg = self._cfg()
            namespace = self._namespace(event)
            user = self._user(event)
            if not self._allowed(event, cfg):
                return "本群未启用股市插件。"
            view_key = (view or "overview").strip().lower()
            try:
                async with self._session(namespace, cfg) as state:
                    state = self._require(state)
                    acc = engine.require_account(state, user)
                    self._mark_name(event, state, user)
                    if view_key == "leverage":
                        info = engine.leverage_info(state, user, cfg)
                        lines = [
                            f"可用 {engine.fmt_money(info['cash'])} 元，总资产 "
                            f"{engine.fmt_money(info['total'])} 元，欠款 "
                            f"{engine.fmt_money(info['debt'])} 元，净资产 "
                            f"{engine.fmt_money(info['net'])} 元",
                            f"最高 {info['max_leverage']:g} 倍，日息 "
                            f"{info['daily_rate'] * 100:.3f}%，权益/市值低于 "
                            f"{info['call_ratio'] * 100:.0f}% 自动强平",
                        ]
                        for row in info["positions"]:
                            lines.append(
                                f"{row['label']} {row['shares']} {row['unit']}，"
                                f"现价 {row['price_text']}，市值 "
                                f"{engine.fmt_money(row['value'])} 元，权益比 "
                                f"{row['ratio']:.1f}%，强平价 {row['liq_price_text']}"
                            )
                        if not info["positions"]:
                            lines.append("当前没有杠杆持仓。")
                        return "\n".join(lines)

                    if view_key == "orders":
                        orders = sorted(
                            (o for o in state.get("orders", {}).values() if o["user"] == user),
                            key=lambda o: int(o["id"]),
                        )
                        if not orders:
                            return "当前没有挂单。"
                        lines = [f"共 {len(orders)} 笔挂单："]
                        for order in orders:
                            stock = state["stocks"].get(order["code"]) or {"code": order["code"]}
                            side = "买入" if order["side"] == "buy" else "卖出"
                            lines.append(
                                f"#{order['id']} {side} {order['code']} "
                                f"{order['shares']} {engine.unit_label(stock)}，限价 "
                                f"{engine.fmt_price(stock, int(order['limit']))} 元"
                            )
                        return "\n".join(lines)

                    if view_key == "trades":
                        trades = [t for t in state.get("trades", []) if t["user"] == user][-8:]
                        if not trades:
                            return "还没有成交记录。"
                        lines = ["最近成交："]
                        for trade in reversed(trades):
                            stock = state["stocks"].get(trade["code"]) or {"code": trade["code"]}
                            side = "买入" if trade["side"] == "buy" else "卖出"
                            lines.append(
                                f"{engine.fmt_time(trade['ts'])} {side} {trade['code']} "
                                f"{trade['shares']} {engine.unit_label(stock)} @ "
                                f"{engine.fmt_price(stock, int(trade['price']))} 元"
                            )
                        return "\n".join(lines)

                    summary = engine.account_summary(state, user)
                    lines = [
                        f"可用 {engine.fmt_money(summary['cash'])} 元，冻结 "
                        f"{engine.fmt_money(summary['frozen'])} 元，持仓市值 "
                        f"{engine.fmt_money(summary['market_value'])} 元",
                        f"总资产 {engine.fmt_money(summary['total'])} 元，杠杆欠款 "
                        f"{engine.fmt_money(summary['debt'])} 元，净资产 "
                        f"{engine.fmt_money(summary['net'])} 元",
                        f"已实现盈亏 {engine.fmt_money(summary['realized'])} 元，"
                        f"浮动盈亏 {engine.fmt_money(summary['floating'])} 元",
                    ]
                    if not summary["positions"]:
                        lines.append("当前空仓。")
                    for pos in summary["positions"]:
                        lines.append(
                            f"{pos.get('label') or pos['code']} {pos['shares']} "
                            f"{pos.get('unit') or '股'}，现价 {pos.get('price_text')}，"
                            f"市值 {engine.fmt_money(pos['value'])} 元，浮动 "
                            f"{engine.fmt_money(pos['floating'])} 元"
                        )
                    return "\n".join(lines)
            except MarketError as exc:
                return f"查询失败：{exc}"

        @filter.llm_tool(name="stock_trade")
        async def llm_trade(
            self,
            event: AstrMessageEvent,
            action: str = "buy",
            code: str = "",
            shares: int = 0,
            price: float = 0.0,
            leverage: float = 2.0,
        ):
            '''在群内股市下单：市价买卖、限价挂单、融资买入。用户说"买100股茅台""挂个1400的买单"时使用。

            Args:
                action(string): buy=市价买入，sell=市价卖出，limitbuy=限价买入，limitsell=限价卖出，margin=融资买入
                code(string): 品种代码，例如 SH600519、FX_USDCNY
                shares(number): 数量（股票按股，外汇按单位）
                price(number): 限价单价格，仅 limitbuy/limitsell 需要
                leverage(number): 融资倍数，仅 margin 需要
            '''
            cfg = self._cfg()
            namespace = self._namespace(event)
            user = self._user(event)
            if not self._allowed(event, cfg):
                return "本群未启用股市插件。"
            act = (action or "buy").strip().lower()
            if act not in ("buy", "sell", "limitbuy", "limitsell", "margin"):
                return "action 只能是 buy / sell / limitbuy / limitsell / margin。"
            if not str(code).strip():
                return "缺少品种代码，例如 SH600519、FX_USDCNY。"
            try:
                share_text = str(int(shares))
            except (TypeError, ValueError):
                return "shares 必须是正整数。"
            try:
                async with self._session(namespace, cfg) as state:
                    state = self._require(state)
                    engine.require_account(state, user)
                    self._mark_name(event, state, user)
                    if act in ("buy", "sell"):
                        trade = (
                            engine.buy(state, user, code, share_text, cfg)
                            if act == "buy"
                            else engine.sell(state, user, code, share_text, cfg)
                        )
                        side = "买入" if act == "buy" else "卖出"
                        lines = [
                            f"{side}成功：{trade['label']} × {trade['shares']} {trade['unit']}，"
                            f"成交价 {trade['price_text']} 元/{trade['unit']}"
                        ]
                        if act == "buy":
                            lines.append(
                                f"手续费 {engine.fmt_money(trade['fee'])} 元，"
                                f"可用资金 {engine.fmt_money(trade['cash'])} 元"
                            )
                        else:
                            lines.append(
                                f"手续费 {engine.fmt_money(trade['fee'])} 元，到账 "
                                f"{engine.fmt_money(trade['proceeds'])} 元，本次盈亏 "
                                f"{engine.fmt_money(trade['realized'])} 元，可用资金 "
                                f"{engine.fmt_money(trade['cash'])} 元"
                            )
                        return "\n".join(lines)

                    if act in ("limitbuy", "limitsell"):
                        if price <= 0:
                            return "限价单需要传 price。"
                        result = engine.place_order(
                            state,
                            user,
                            "buy" if act == "limitbuy" else "sell",
                            code,
                            share_text,
                            price,
                            cfg,
                        )
                        stock = result["stock"]
                        order = result["order"]
                        if result["executed_at"]:
                            return (
                                f"限价单已立即成交：{engine.display_label(stock)} "
                                f"{order['shares']} {engine.unit_label(stock)}，成交价 "
                                f"{engine.fmt_price(stock, int(result['executed_at']))} 元"
                            )
                        return (
                            f"已挂单 #{order['id']}：{engine.display_label(stock)} "
                            f"{order['shares']} {engine.unit_label(stock)}，限价 "
                            f"{engine.fmt_price(stock, int(order['limit']))} 元，"
                            f"{cfg.order_ttl_hours} 小时未成交自动撤销"
                        )

                    trade = engine.leverage_buy(state, user, code, share_text, leverage, cfg)
                    return (
                        f"融资买入成功：{trade['label']} × {trade['shares']} "
                        f"{trade['unit']}（{trade['leverage']:g} 倍），成交价 "
                        f"{trade['price_text']} 元，保证金 "
                        f"{engine.fmt_money(trade['margin'])} 元，系统垫付 "
                        f"{engine.fmt_money(trade['borrowed'])} 元，当前欠款 "
                        f"{engine.fmt_money(trade['debt'])} 元"
                    )
            except MarketError as exc:
                return f"下单失败：{exc}"

        @filter.llm_tool(name="stock_repay")
        async def llm_repay(self, event: AstrMessageEvent, amount: float = 0.0):
            '''偿还融资欠款。用户说"还债""把欠的钱还上"时使用。

            Args:
                amount(number): 还款金额（元），传 0 表示能还多少还多少
            '''
            cfg = self._cfg()
            namespace = self._namespace(event)
            user = self._user(event)
            if not self._allowed(event, cfg):
                return "本群未启用股市插件。"
            try:
                async with self._session(namespace, cfg) as state:
                    state = self._require(state)
                    self._mark_name(event, state, user)
                    text = "" if amount <= 0 else str(amount)
                    info = engine.repay_debt(state, user, text, cfg)
                    return (
                        f"已还债 {engine.fmt_money(info['amount'])} 元，剩余欠款 "
                        f"{engine.fmt_money(info['debt'])} 元，可用资金 "
                        f"{engine.fmt_money(info['cash'])} 元"
                    )
            except MarketError as exc:
                return f"还债失败：{exc}"

        @filter.llm_tool(name="stock_play")
        async def llm_play(
            self,
            event: AstrMessageEvent,
            action: str = "checkin",
            amount: float = 0.0,
            target: str = "",
        ):
            '''群内股市的玩法：签到、补助、排行、盈亏、异动新闻、赌博、抢劫、转账。

            Args:
                action(string): checkin=签到，relief=领补助，rank=资产排行，profit=盈亏榜，news=异动新闻，gamble=赌博，rob=抢别人现金，transfer=转账给别人
                amount(number): gamble 的下注金额、transfer 的转账金额（元）
                target(string): rob/transfer 的目标 QQ 号
            '''
            cfg = self._cfg()
            namespace = self._namespace(event)
            user = self._user(event)
            if not self._allowed(event, cfg):
                return "本群未启用股市插件。"
            act = (action or "checkin").strip().lower()
            try:
                async with self._session(namespace, cfg) as state:
                    state = self._require(state)
                    engine.require_account(state, user)
                    self._mark_name(event, state, user)
                    if act == "checkin":
                        info = engine.checkin(state, user, cfg)
                        return (
                            f"签到成功，获得 {engine.fmt_money(info['amount'])} 元，"
                            f"连续 {info['streak']} 天，可用资金 "
                            f"{engine.fmt_money(info['cash'])} 元"
                        )
                    if act == "relief":
                        info = engine.relief(state, user, cfg)
                        return (
                            f"补助到账 {engine.fmt_money(info['amount'])} 元，"
                            f"可用资金 {engine.fmt_money(info['cash'])} 元"
                        )
                    if act == "rank":
                        rows = engine.rank(state, cfg, cfg.rank_size)
                        lines = [f"资产排行（共 {len(rows)} 人）："]
                        for index, row in enumerate(rows):
                            lines.append(
                                f"{index + 1}. {row['name']} 净资产 "
                                f"{engine.fmt_money(row['total'])} 元"
                            )
                        return "\n".join(lines)
                    if act == "profit":
                        data = engine.profit_rank(state, cfg, cfg.rank_size)
                        lines = ["赚钱榜："]
                        lines += [
                            f"{i + 1}. {row['name']} {engine.fmt_money(row['total'])} 元"
                            for i, row in enumerate(data["gainers"])
                        ] or ["暂无"]
                        lines.append("亏钱榜：")
                        lines += [
                            f"{i + 1}. {row['name']} {engine.fmt_money(row['total'])} 元"
                            for i, row in enumerate(data["losers"])
                        ] or ["暂无"]
                        return "\n".join(lines)
                    if act == "news":
                        news = state.get("news", [])[-5:]
                        if not news:
                            return "暂无市场异动。"
                        return "\n".join(
                            f"{engine.fmt_time(item['ts'])} {item['text']}" for item in reversed(news)
                        )
                    if act == "gamble":
                        info = engine.gamble(state, user, str(amount), cfg)
                        return (
                            f"下注 {engine.fmt_money(info['bet'])} 元，开奖 "
                            f"{engine.fmt_money(info['payout'])} 元，净变化 "
                            f"{engine.fmt_money(info['net'])} 元，可用资金 "
                            f"{engine.fmt_money(info['cash'])} 元"
                        )
                    if act == "rob":
                        if not target.strip():
                            return "抢劫需要 target（对方 QQ 号）。"
                        info = engine.rob(state, user, target.strip(), cfg)
                        if info["success"]:
                            return (
                                f"抢到 {engine.fmt_money(info['amount'])} 元，"
                                f"成功率 {info['chance']:.0f}%，可用资金 "
                                f"{engine.fmt_money(info['cash'])} 元"
                            )
                        return (
                            f"抢劫失败，被反抢 {engine.fmt_money(info['amount'])} 元，"
                            f"可用资金 {engine.fmt_money(info['cash'])} 元"
                        )
                    if act == "transfer":
                        if not target.strip():
                            return "转账需要 target（对方 QQ 号）。"
                        if amount <= 0:
                            return "转账需要 amount（金额，元）。"
                        info = engine.transfer(state, user, target.strip(), str(amount), cfg)
                        return (
                            f"已转 {engine.fmt_money(info['amount'])} 元给 "
                            f"{info['target_name']}，你的可用资金 "
                            f"{engine.fmt_money(info['cash'])} 元"
                        )
                    return "action 不在支持范围。"
            except MarketError as exc:
                return f"操作失败：{exc}"

        @filter.llm_tool(name="stock_chart")
        async def llm_chart(self, event: AstrMessageEvent, code: str = ""):
            '''查看某个品种的走势摘要（区间高低、涨跌幅、数据点）。

            Args:
                code(string): 品种代码，例如 SH600519、FX_USDCNY
            '''
            cfg = self._cfg()
            namespace = self._namespace(event)
            if not self._allowed(event, cfg):
                return "本群未启用股市插件。"
            if not str(code).strip():
                return "需要传 code，例如 SH600519。"
            try:
                async with self._session(namespace, cfg) as state:
                    state = self._require(state)
                    engine.require_account(state, self._user(event))
                    stock = engine.find_stock(state, str(code).strip())
                    snapshot = {
                        "code": stock["code"],
                        "name": stock.get("name") or "",
                        "kind": stock.get("kind") or "stock",
                        "price": int(stock["price"]),
                        "price_scale": engine.price_scale(stock),
                        "price_digits": engine.price_digits(stock),
                        "prev": int(stock.get("prev") or stock["price"]),
                        "history": [list(item) for item in stock.get("history", [])],
                    }
                    return textfmt.chart_text(snapshot)
            except MarketError as exc:
                return f"查询失败：{exc}"

        @filter.llm_tool(name="stock_help")
        async def llm_help(self, event: AstrMessageEvent):
            '''列出群内股市玩法的简要说明（指令、规则、手续费、杠杆）。用户问"怎么玩""有哪些功能"时使用。'''
            cfg = self._cfg()
            return (
                "群内股市（虚拟资金，真实行情）：\n"
                "/股市 开户 · 行情 [页码] · 汇市 · 持仓 [页码]\n"
                "/股市 买入 代码 数量 · 卖出 代码 数量 · 走势 代码\n"
                "/股市 融资买入 代码 数量 倍数 · 杠杆 · 还债\n"
                "/股市 买单/卖单 代码 数量 价格 · 挂单 · 撤单 编号 · 成交\n"
                "/股市 新闻 · 排行 · 盈亏 · 签到 · 补助\n"
                "/股市 db 金额 · 抢劫 @对方 · transfer @对方 金额\n"
                f"手续费 {cfg.fee_rate * 100:.3f}%，杠杆最高 {cfg.max_leverage:g} 倍，"
                f"欠款日息 {cfg.margin_daily_rate * 100:.3f}%，"
                f"权益/市值低于 {cfg.margin_call_ratio * 100:.0f}% 自动强平。"
                "真实行情模式下不设退市；净值清零会在群里播报。"
            )
