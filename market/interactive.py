"""消息交互：给卡片结果挂可点击按钮（keyboard）与文字指令链。

对应 QQ 机器人 API v2「消息交互」：

- **按钮消息**：消息体携带 `keyboard`，用户点击后平台推 `INTERACTION_CREATE`，
  AstrBot 把它规范化成 `ButtonInteraction` 组件，插件用 `filter.button_action`
  接收。发送侧统一用 `ActionRow` + `Button`，由适配器翻译成各平台格式。
- **文字指令链**：在 Markdown 正文里嵌 `<qqbot-cmd-input text="..." show="..." />`，
  用户点击后指令落进输入框（群聊、文字子频道都支持）。`text` / `show` 需要 URL 编码。

按钮有平台硬限制：最多 5 行、每行 5 个、`label` ≤ 10 个字符、回调数据 ≤ 1024 字节；
本模块统一做截断，超限时宁可不挂，也不会让整条消息发送失败。
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


def button_row(
    specs: Iterable[dict[str, Any]], fallback_text: str = ""
) -> Any | None:
    """一组按钮 → ActionRow；组件不可用或规格为空时返回 None。

    spec 字段：`id`（回调路由键）、`label`（≤10 字）、`data`（JSON 上下文，可空）、
    `style`（primary / success / danger / default）。
    """
    if not HAS_COMPONENTS:
        return None
    items = [spec for spec in specs if spec.get("id") and spec.get("label")]
    if not items:
        return None
    # 平台每行最多 5 个，超出的丢掉而不是抛错
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


# 各卡片的按钮组：(action_id, label, data, style)
QUOTE_BUTTONS: tuple[dict[str, Any], ...] = (
    {"id": "stock:quote", "label": "行情", "data": None, "style": "primary"},
    {"id": "stock:fx", "label": "汇市", "data": None, "style": "default"},
    {"id": "stock:position", "label": "持仓", "data": None, "style": "default"},
    {"id": "stock:checkin", "label": "签到", "data": None, "style": "success"},
    {"id": "stock:rank", "label": "排行", "data": None, "style": "default"},
)
POSITION_BUTTONS: tuple[dict[str, Any], ...] = (
    {"id": "stock:quote", "label": "行情", "data": None, "style": "primary"},
    {"id": "stock:fx", "label": "汇市", "data": None, "style": "default"},
    {"id": "stock:profit", "label": "盈亏", "data": None, "style": "default"},
    {"id": "stock:relief", "label": "补助", "data": None, "style": "success"},
    {"id": "stock:help", "label": "帮助", "data": None, "style": "default"},
)
HELP_BUTTONS: tuple[dict[str, Any], ...] = (
    {"id": "stock:quote", "label": "行情", "data": None, "style": "primary"},
    {"id": "stock:fx", "label": "汇市", "data": None, "style": "default"},
    {"id": "stock:position", "label": "持仓", "data": None, "style": "default"},
    {"id": "stock:checkin", "label": "签到", "data": None, "style": "success"},
    {"id": "stock:news", "label": "异动", "data": None, "style": "default"},
)

# 文字指令链：卡片正文末尾的可点指令
QUOTE_LINKS: tuple[tuple[str, str], ...] = (
    ("/股市 行情 1", "行情"),
    ("/股市 汇市", "汇市"),
    ("/股市 持仓", "持仓"),
)
POSITION_LINKS: tuple[tuple[str, str], ...] = (
    ("/股市 行情 1", "行情"),
    ("/股市 盈亏", "盈亏"),
    ("/股市 签到", "签到"),
)
HELP_LINKS: tuple[tuple[str, str], ...] = (
    ("/股市 行情 1", "行情"),
    ("/股市 汇市", "汇市"),
    ("/股市 帮助", "帮助"),
)