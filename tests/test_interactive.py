"""消息交互测试：按钮规格、多行面板、10 字截断、5 个上限、指令链编码。

直接运行 `python tests/test_interactive.py`，不需要 AstrBot：

- 有 AstrBot 组件时验证真实 `ActionRow`；
- 没有时注入最小替身，仍然验证规格与截断逻辑（这部分是平台硬限制，必须锁死）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market import interactive  # noqa: E402


class _Style:
    DEFAULT = "default"
    PRIMARY = "primary"
    SUCCESS = "success"
    DANGER = "danger"


class _Callback:
    type = "callback"

    def __init__(self, data=None) -> None:
        self.data = data


class _Button:
    Style = _Style
    CallbackAction = _Callback

    def __init__(self, id, label, action=None, style=None) -> None:
        self.id = id
        self.label = label
        self.action = action or _Callback()
        self.style = style


class _Row:
    def __init__(self, buttons=None, fallback_text=None) -> None:
        self.buttons = list(buttons or [])
        self.fallback_text = fallback_text


def _with_fakes(callable_):
    """临时注入替身组件，保证纯离线环境也能测 button_row / button_rows。"""
    original = (interactive.ActionRow, interactive.Button, interactive.HAS_COMPONENTS)
    interactive.ActionRow = _Row
    interactive.Button = _Button
    interactive.HAS_COMPONENTS = True
    try:
        return callable_()
    finally:
        interactive.ActionRow, interactive.Button, interactive.HAS_COMPONENTS = original


def test_clip_truncates_to_ten_chars() -> None:
    assert interactive.clip("行情") == "行情"
    assert len(interactive.clip("一二三四五六七八九十十一")) == 10
    assert interactive.clip("   ") == ""
    assert interactive.clip("一二三四五六七八九十十一") == "一二三四五六七八九…"


def test_cmd_link_urlencodes_text_and_show() -> None:
    link = interactive.cmd_link("/股市 行情 1", "行情")
    assert link.startswith("<qqbot-cmd-input text=\"")
    assert "%2F" in link, link
    assert "%20" in link, link
    assert 'show="%E8%A1%8C%E6%83%85"' in link
    assert link.endswith(" />")


def test_cmd_link_reference_flag() -> None:
    link = interactive.cmd_link("股市 持仓", reference=True)
    assert 'reference="true"' in link
    assert "show=" not in link


def test_with_links_keeps_body_when_empty() -> None:
    assert interactive.with_links("正文", []) == "正文"
    joined = interactive.with_links("正文", interactive.HELP_LINKS)
    assert joined.startswith("正文\n")
    assert joined.count("<qqbot-cmd-input") == len(interactive.HELP_LINKS)


def test_button_row_caps_at_five() -> None:
    specs = [{"id": f"b{i}", "label": f"按钮{i}"} for i in range(7)]

    row = _with_fakes(lambda: interactive.button_row(specs, "fallback"))
    assert row is not None
    assert len(row.buttons) == interactive.MAX_BUTTONS_PER_ROW
    assert row.fallback_text == "fallback"


def test_button_rows_multi_line() -> None:
    groups = [interactive.TRADE_ROWS[0], interactive.TRADE_ROWS[1]]

    rows = _with_fakes(lambda: interactive.button_rows(groups, "交易"))
    assert len(rows) == 2
    assert all(len(row.buttons) <= 5 for row in rows)
    assert [b.id for b in rows[0].buttons][:2] == ["stock:buy", "stock:sell"]


def test_button_row_empty_or_invalid_returns_none() -> None:
    original = (interactive.ActionRow, interactive.Button, interactive.HAS_COMPONENTS)
    interactive.ActionRow = _Row
    interactive.Button = _Button
    interactive.HAS_COMPONENTS = True
    try:
        assert interactive.button_row([], "x") is None
        assert interactive.button_row([{"id": "", "label": "空"}], "x") is None
        assert interactive.button_row([{"id": "a", "label": ""}], "x") is None
    finally:
        interactive.ActionRow, interactive.Button, interactive.HAS_COMPONENTS = original


def test_button_row_carries_callback_data() -> None:
    specs = [{"id": "stock:chart", "label": "走势", "data": {"code": "SH600519"}, "style": "primary"}]

    row = _with_fakes(lambda: interactive.button_row(specs))
    button = row.buttons[0]
    assert button.id == "stock:chart"
    assert button.action.data == {"code": "SH600519"}
    assert button.style == "primary"


def test_all_button_groups_are_platform_safe() -> None:
    groups = [
        list(interactive.NAV_BUTTONS),
        *[list(row) for row in interactive.TRADE_ROWS],
        *[list(row) for row in interactive.PLAY_ROWS],
        list(interactive.STOCK_BUTTONS),
        list(interactive.CONFIRM_BUTTONS),
    ]
    for group in groups:
        ids = [spec["id"] for spec in group]
        assert len(ids) == len(set(ids)), f"同一行按钮 id 必须唯一：{ids}"
        assert len(group) <= interactive.MAX_BUTTONS_PER_ROW
        for spec in group:
            assert spec["label"]
            assert len(spec["label"]) <= interactive.LABEL_LIMIT, spec
            assert spec["id"].startswith("stock:")


def test_nav_covers_main_entries() -> None:
    ids = {spec["id"] for spec in interactive.NAV_BUTTONS}
    assert {"stock:quote", "stock:fx", "stock:position", "stock:trade", "stock:play"} <= ids
    labels = [spec["label"] for spec in interactive.NAV_BUTTONS]
    assert labels == ["行情", "汇市", "持仓", "交易", "玩法"]


def test_holding_rows_build_sell_buttons() -> None:
    positions = [{"code": f"SH60000{i}", "shares": 100 + i} for i in range(5)]
    spec_rows = interactive.holding_rows(positions)
    assert len(spec_rows) == 2
    assert [s["label"] for s in spec_rows[0]] == ["卖①", "卖②", "卖③", "卖④", "卖⑤"]
    assert spec_rows[0][2]["data"] == {"code": "SH600002", "shares": 102}
    assert [s["id"] for s in spec_rows[1]] == [s["id"] for s in interactive.NAV_BUTTONS]
    rendered = _with_fakes(lambda: interactive.button_rows(spec_rows, "快捷操作"))
    assert len(rendered) == 2
    assert rendered[0].buttons[0].action.data == {"code": "SH600000", "shares": 100}


def test_holding_rows_without_positions() -> None:
    spec_rows = interactive.holding_rows([])
    assert len(spec_rows) == 1
    assert [s["label"] for s in spec_rows[0]] == ["行情", "汇市", "持仓", "交易", "玩法"]


def test_stock_links_reference_the_code() -> None:
    links = interactive.stock_links("FX_JPYCNY", "0.041805")
    assert links[0][0] == "/股市 买入 FX_JPYCNY 100"
    assert "/股市 买单 FX_JPYCNY 100 0.041805" in [text for text, _ in links]
    assert interactive.stock_links("") == ()


def test_links_point_to_real_commands() -> None:
    for text, show in (
        *interactive.BUY_LINKS,
        *interactive.SELL_LINKS,
        *interactive.LIMIT_LINKS,
        *interactive.PLAY_LINKS,
        *interactive.HELP_LINKS,
    ):
        assert text.startswith("/股市 "), text
        assert show, "指令链必须有展示文字"


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