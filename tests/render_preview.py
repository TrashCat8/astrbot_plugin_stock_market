"""本地生成四张卡片预览图，确认排版后删除即可。

用法：python tests/render_preview.py [输出目录]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market import engine, render  # noqa: E402
from market.config import Config  # noqa: E402


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("preview")
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(news_probability=1.0)
    now = time.time()
    state = engine.new_state("group_preview", now, cfg)
    engine.rename_stock(state, "ST001", "星辰科技")
    engine.rename_stock(state, "ST002", "鸡窝教育")
    engine.rename_stock(state, "ST003", "韵律之夕")
    engine.rename_stock(state, "ST004", "鸽游网络")
    engine.rename_stock(state, "ST005", "广州射线")
    engine.rename_stock(state, "ST007", "百合控股")
    engine.ensure_account(state, "u1", cfg)
    engine.buy(state, "u1", "ST001", "50", cfg)
    engine.advance(state, now + 300 * 6, cfg)

    rows, page, pages = engine.quote_page(state, 1, cfg)
    updated = engine.fmt_time(time.time())
    render.render_quote(
        rows,
        page,
        pages,
        int(state["round"]),
        updated,
        refresh_seconds=cfg.round_seconds,
        fee_rate=cfg.fee_rate,
        out=out_dir / "quote.png",
    )
    summary = engine.account_summary(state, "u1")
    render.render_position(summary, int(state["round"]), updated, out=out_dir / "position.png")
    render.render_chart(state["stocks"]["ST001"], out=out_dir / "chart.png")
    render.render_help(
        {
            "capital": engine.fmt_money(cfg.initial_capital),
            "round": str(cfg.round_seconds),
            "max_pct": f"{cfg.max_change_pct:g}",
            "floor": engine.fmt_money(cfg.min_price),
            "fee": f"{cfg.fee_rate * 100:.3f}",
            "ttl": str(cfg.order_ttl_hours),
            "daily": engine.fmt_money(cfg.daily_bonus),
            "relief": engine.fmt_money(cfg.relief_threshold),
        },
        out=out_dir / "help.png",
    )
    print(out_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
