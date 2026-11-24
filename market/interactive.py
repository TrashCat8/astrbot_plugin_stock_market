"""消息交互：按钮导航、快捷指令链、二层操作面板。

对应 QQ 机器人 API v2「消息交互」：

- **按钮**：消息体携带 `keyboard`，点击后平台推 `INTERACTION_CREATE`，AstrBot 规范化成
  `ButtonInteraction`，插件用 `filter.button_action` 接收。支持多行（≤5 行 × 5 个）。
- **文字指令链**：Markdown 正文里的 `<qqbot-cmd-input text="..." show="..." />`，
  点一下把整条指令填进输入框，用户改数量再发；需要参数的交易指令靠它落地。

按钮不做「直接下单」：数量、价格都要用户确认，避免误点买错量。唯一带二次确认的
按钮是持仓页的「卖①…卖⑥」。
"""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import quote

try:  # AstrBot >= 4.29.4 才有多平台按钮组件
    from astrbot.api.message_components import ActionRow, Button, Plain

    HAS_COMPONENTS = True
except Exception:  # pragma: no cover - 老版本 AstrBot
    ActionRow = Button = Plain = None  # type: ignore[assignment]
    HAS_COMPONENTS = False

LABEL_LIMIT = 10
MAX_BUTTONS_PER_ROW = 5
MAX_ROWS = 5

# 数字圈号，用于「卖①…卖⑥」
CIRCLED = "①②③④⑤⑥⑦⑧⑨"


def available() -> bool:
    return HAS_COMPONENTS


def clip(text: str, limit: int = LABEL_LIMIT) -> str:
    """按钮 label 最多 10 个字符，超出用省略号，避免整条消息被平台拒绝。"""
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)] + "…"


def _style(name: str) -> Any:
    if not HAS_COMPONENTS:
        return None
    return getattr(Button.Style, (name or "default").upper(), Button.Style.DEFAULT)


def button_row(specs: Iterable[dict[str, Any]], fallback_text: str = "") -> Any | None:
    """一行按钮 → ActionRow；组件不可用或规格为空时返回 None。"""
    if not HAS_COMPONENTS:
        return None
    items = [spec for spec in specs if spec.get("id") and spec.get("label")]
    if not items:
        return None
    items = items[:MAX_BUTTONS_PER_ROW]
    buttons = []
    for spec in items:
        data = spec.get("data")
        action = (
            Button.CallbackAction(data=data)
            if data is not None
            else Button.CallbackAction()
        )
        buttons.append(
            Button(
                id=str(spec["id"]),
                label=clip(str(spec["label"])),
                action=action,
                style=_style(str(spec.get("style") or "default")),
            )
        )
    fallback = (fallback_text or "").strip() or " / ".join(
        button.label for button in buttons
    )
    return ActionRow(buttons=buttons, fallback_text=fallback)


def button_rows(
    groups: Iterable[Iterable[dict[str, Any]]], fallback_text: str = "快捷操作"
) -> list[Any]:
    """多行按钮 → ActionRow 列表（≤5 行，每行 ≤5 个）。"""
    rows: list[Any] = []
    for group in list(groups)[:MAX_ROWS]:
        row = button_row(group, fallback_text)
        if row is not None:
            rows.append(row)
    return rows


def cmd_link(text: str, show: str | None = None, reference: bool = False) -> str:
    """文字指令链：点击后把 `/股市 ...` 填进输入框。"""
    command = "/" + str(text or "").strip().lstrip("/")
    parts = [f'<qqbot-cmd-input text="{quote(command, safe="")}"']
    if show:
        parts.append(f' show="{quote(str(show), safe="")}"')
    if reference:
        parts.append(' reference="true"')
    return "".join(parts) + " />"


def with_links(body: str, commands: Iterable[tuple[str, str]]) -> str:
    """在正文末尾追加一行文字指令链（正文需按 Markdown 发送才会渲染）。"""
    links = [cmd_link(text, show) for text, show in commands]
    if not links:
        return body
    return f"{body}\n{' · '.join(links)}"


# --------------------------------------------------------------------------- #
# 按钮组：(id, label, data, style)
# --------------------------------------------------------------------------- #

# 一级导航：所有卡片底部都是这一行
NAV_BUTTONS: tuple[dict[str, Any], ...] = (
    {"id": "stock:quote", "label": "行情", "style": "primary"},
    {"id": "stock:fx", "label": "汇市"},
    {"id": "stock:position", "label": "持仓"},
    {"id": "stock:trade", "label": "交易", "style": "success"},
    {"id": "stock:play", "label": "玩法"},
)

# 二级：交易面板（2 行）
TRADE_ROWS: tuple[tuple[dict[str, Any], ...], ...] = (
    (
        {"id": "stock:buy", "label": "买入", "style": "primary"},
        {"id": "stock:sell", "label": "卖出", "style": "danger"},
        {"id": "stock:limitbuy", "label": "买单"},
        {"id": "stock:limitsell", "label": "卖单"},
        {"id": "stock:margin", "label": "融资"},
    ),
    (
        {"id": "stock:orders", "label": "挂单"},
        {"id": "stock:trades", "label": "成交"},
        {"id": "stock:leverage", "label": "杠杆"},
        {"id": "stock:repay", "label": "还债"},
        {"id": "stock:home", "label": "返回"},
    ),
)

# 二级：玩法面板（2 行）
PLAY_ROWS: tuple[tuple[dict[str, Any], ...], ...] = (
    (
        {"id": "stock:checkin", "label": "签到", "style": "success"},
        {"id": "stock:relief", "label": "补助"},
        {"id": "stock:rank", "label": "排行"},
        {"id": "stock:profit", "label": "盈亏"},
        {"id": "stock:news", "label": "异动"},
    ),
    (
        {"id": "stock:dice", "label": "db"},
        {"id": "stock:rob", "label": "抢劫"},
        {"id": "stock:transfer", "label": "转账"},
        {"id": "stock:help", "label": "帮助"},
        {"id": "stock:home", "label": "返回"},
    ),
)

# 单个品种的操作面板
STOCK_BUTTONS: tuple[dict[str, Any], ...] = (
    {"id": "stock:buy", "label": "买入", "style": "primary"},
    {"id": "stock:sell", "label": "卖出", "style": "danger"},
    {"id": "stock:limitbuy", "label": "挂买单"},
    {"id": "stock:chart", "label": "走势"},
    {"id": "stock:home", "label": "返回"},
)

# 卖出确认
CONFIRM_BUTTONS: tuple[dict[str, Any], ...] = (
    {"id": "stock:confirm_sell", "label": "确认卖出", "style": "danger"},
    {"id": "stock:cancel", "label": "取消"},
)


def holding_rows(positions: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """持仓页按钮：卖①…卖⑤ + 一行导航（持仓每页 5 行，正好两行按钮）。"""
    sells = []
    for index, pos in enumerate(list(positions)[:5]):
        sells.append(
            {
                "id": f"stock:ask_sell:{index}",
                "label": f"卖{CIRCLED[index]}",
                "data": {"code": pos.get("code"), "shares": int(pos.get("shares", 0))},
                "style": "danger",
            }
        )
    rows = [sells] if sells else []
    rows.append(list(NAV_BUTTONS))
    return rows


# 常用快捷指令链（show 是点击后看到的文字）
BUY_LINKS = (
    ("/股市 买入 SH600519 100", "买茅台"),
    ("/股市 买入 SZ000001 1000", "买平安"),
    ("/股市 买入 FX_USDCNY 1000", "买美元"),
)
SELL_LINKS = (
    ("/股市 卖出 SH600519 100", "卖茅台"),
    ("/股市 卖出 FX_USDCNY 1000", "卖美元"),
    ("/股市 持仓", "看持仓"),
)
LIMIT_LINKS = (
    ("/股市 买单 SH600519 100 1430.00", "挂买茅台"),
    ("/股市 卖单 SH600519 100 1500.00", "挂卖茅台"),
    ("/股市 挂单", "看挂单"),
)
PLAY_LINKS = (
    ("/股市 db 1000", "db 1000"),
    ("/股市 抢劫 ", "抢劫"),
    ("/股市 transfer ", "转账"),
    ("/股市 补助", "补助"),
)
HELP_LINKS = (
    ("/股市 帮助", "帮助"),
    ("/股市 行情 1", "行情"),
    ("/股市 汇市", "汇市"),
)


def stock_links(code: str, price_text: str = "") -> tuple[tuple[str, str], ...]:
    """某个品种的快捷指令链。"""
    base = (code or "").strip()
    if not base:
        return ()
    price = price_text or "现价"
    return (
        (f"/股市 买入 {base} 100", "买"),
        (f"/股市 卖出 {base} 100", "卖"),
        (f"/股市 买单 {base} 100 {price}", "挂买"),
        (f"/股市 行情 {base}", "详情"),
    )