"""真实行情源：腾讯股票接口 + 新浪外汇接口。

两者都是公开、免 key 的行情接口，返回 GBK 文本：

- 腾讯：`https://qt.gtimg.cn/q=sh600519,hk00700,usAAPL`
  `v_sh600519="1~贵州茅台~600519~1439.00~1485.75~..."`，`~` 分隔。
- 新浪外汇：`https://hq.sinajs.cn/list=fx_susdcny`（需要 Referer）
  `var hq_str_fx_susdcny="时间,买价,卖价,昨收,量,今开,最高,最低,最新,名称,..."`。

设计原则：网络失败绝不影响交易指令——`QuoteFeed` 保留上一帧并在内存/磁盘各留一份，
拿不到新数据就沿用旧价，只在状态里标注 stale。
"""

from __future__ import annotations

import asyncio
import calendar
import json
import os
import re
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

try:  # 插件运行在 AstrBot 里；脱离 AstrBot 跑单元测试时退回标准库日志
    from astrbot.api import logger
except Exception:  # pragma: no cover - 仅测试环境
    import logging

    logger = logging.getLogger("astrbot_plugin_stock_market")

from .instruments import Instrument

TENCENT_URL = "https://qt.gtimg.cn/q="
SINA_URL = "https://hq.sinajs.cn/list="
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
SINA_REFERER = "https://finance.sina.com.cn/"

_TENCENT_LINE = re.compile(r'v_([A-Za-z0-9_]+)="([^"]*)"')
_DATE_PATTERN = re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})(?:[ T](\d{2}):(\d{2}):(\d{2}))?|(\d{14})")


@dataclass(frozen=True)
class Quote:
    code: str
    symbol: str
    name: str
    last: float
    prev_close: float
    open: float
    high: float
    low: float
    currency: str
    ts: float


def _to_float(text: str, default: float = 0.0) -> float:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return value


def _parse_ts(text: str, fallback: float | None = None) -> float:
    """解析行情里的本地时间（UTC+8），失败时返回当前时间。"""
    match = _DATE_PATTERN.search(text or "")
    if not match:
        return fallback if fallback is not None else time.time()
    if match.group(7):
        raw = match.group(7)
        try:
            return calendar.timegm(time.strptime(raw, "%Y%m%d%H%M%S")) - 8 * 3600
        except ValueError:
            return fallback if fallback is not None else time.time()
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    hour = int(match.group(4) or 15)
    minute = int(match.group(5) or 0)
    second = int(match.group(6) or 0)
    try:
        return calendar.timegm((year, month, day, hour, minute, second, 0, 1, 0)) - 8 * 3600
    except (ValueError, OverflowError):
        return fallback if fallback is not None else time.time()


def _http_get(url: str, referer: str | None = None, timeout: float = 6.0) -> str:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return raw.decode("gbk", errors="replace")


def parse_tencent(text: str, instruments: dict[str, Instrument]) -> dict[str, Quote]:
    """解析腾讯股票行情；`instruments` 用 symbol -> Instrument 索引。"""
    quotes: dict[str, Quote] = {}
    for symbol, payload in _TENCENT_LINE.findall(text or ""):
        inst = instruments.get(symbol.lower())
        if inst is None or not payload:
            continue
        fields = payload.split("~")
        if len(fields) < 34:
            continue
        last = _to_float(fields[3])
        prev = _to_float(fields[4])
        if last <= 0 or prev <= 0:
            continue
        ts = _parse_ts(fields[30])
        quotes[inst.code] = Quote(
            code=inst.code,
            symbol=inst.symbol,
            name=(fields[1] or inst.name).strip() or inst.name,
            last=last,
            prev_close=prev,
            open=_to_float(fields[5]) or last,
            high=_to_float(fields[33]) or last,
            low=_to_float(fields[34] if len(fields) > 34 else "") or last,
            currency=inst.currency,
            ts=ts,
        )
    return quotes


def parse_sina_fx(text: str, instruments: dict[str, Instrument]) -> dict[str, Quote]:
    """解析新浪外汇行情；字段顺序：时间,买价,卖价,昨收,量,今开,最高,最低,最新,名称,..."""
    quotes: dict[str, Quote] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("var hq_str_"):
            continue
        try:
            key, _, payload = line.partition("=")
            symbol = key[len("var hq_str_") :].strip()
            payload = payload.strip().strip(";").strip('"')
        except ValueError:
            continue
        inst = instruments.get(symbol.lower())
        if inst is None or not payload:
            continue
        fields = payload.split(",")
        if len(fields) < 9:
            continue
        prev = _to_float(fields[3])
        last = _to_float(fields[8])
        if last <= 0 or prev <= 0:
            continue
        quotes[inst.code] = Quote(
            code=inst.code,
            symbol=inst.symbol,
            name=(fields[9] if len(fields) > 9 else "").strip() or inst.name,
            last=last,
            prev_close=prev,
            open=_to_float(fields[5]) or last,
            high=_to_float(fields[6]) or last,
            low=_to_float(fields[7]) or last,
            currency="CNY",
            ts=_parse_ts(fields[0], fallback=time.time()),
        )
    return quotes


def fetch_quotes(
    instruments: Iterable[Instrument], timeout: float = 6.0
) -> tuple[dict[str, Quote], list[str]]:
    """同步抓取一批品种的真实报价，返回 (报价, 错误列表)。"""
    items = list(instruments)
    by_symbol = {inst.symbol.lower(): inst for inst in items}
    tencent = [inst for inst in items if inst.provider == "tencent"]
    sina = [inst for inst in items if inst.provider == "sina_fx"]

    quotes: dict[str, Quote] = {}
    errors: list[str] = []

    if tencent:
        url = TENCENT_URL + ",".join(inst.symbol for inst in tencent)
        try:
            quotes.update(parse_tencent(_http_get(url, timeout=timeout), by_symbol))
        except Exception as exc:  # 网络问题一律降级，不抛给调用方
            errors.append(f"腾讯行情失败：{exc}")

    if sina:
        url = SINA_URL + ",".join(inst.symbol for inst in sina)
        try:
            quotes.update(parse_sina_fx(_http_get(url, SINA_REFERER, timeout=timeout), by_symbol))
        except Exception as exc:
            errors.append(f"新浪外汇失败：{exc}")

    return quotes, errors


class QuoteFeed:
    """带 TTL 内存缓存 + 磁盘缓存的行情快照器。"""

    def __init__(self, cache_path: Path | None = None, timeout: float = 6.0) -> None:
        self.cache_path = Path(cache_path) if cache_path else None
        self.timeout = timeout
        self._mem: dict[str, Quote] = {}
        self._mem_at = 0.0
        self._lock = asyncio.Lock()
        self.status = "init"
        self.last_error = ""
        if self.cache_path is not None:
            self._mem, self._mem_at = self._load_disk()

    # -- 缓存 ------------------------------------------------------------- #

    def _load_disk(self) -> tuple[dict[str, Quote], float]:
        """读盘：返回 (报价, 落盘时间)。落盘时间用真实时间，避免重启后把旧帧当新鲜数据。"""
        if self.cache_path is None or not self.cache_path.exists():
            return {}, 0.0
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, 0.0
        quotes: dict[str, Quote] = {}
        for code, row in (raw.get("quotes") or {}).items():
            try:
                quotes[code] = Quote(
                    code=str(row["code"]),
                    symbol=str(row["symbol"]),
                    name=str(row["name"]),
                    last=float(row["last"]),
                    prev_close=float(row["prev_close"]),
                    open=float(row.get("open") or row["last"]),
                    high=float(row.get("high") or row["last"]),
                    low=float(row.get("low") or row["last"]),
                    currency=str(row.get("currency") or "CNY"),
                    ts=float(row.get("ts") or 0.0),
                )
            except (KeyError, TypeError, ValueError):
                continue
        return quotes, float(raw.get("saved_at") or 0.0)

    def _save_disk(self, quotes: dict[str, Quote]) -> None:
        if self.cache_path is None or not quotes:
            return
        path = self.cache_path
        payload = {
            "saved_at": int(time.time()),
            "quotes": {code: asdict(quote) for code, quote in quotes.items()},
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"))
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("[stock] 行情缓存写入失败：%s", exc)

    # -- 对外接口 --------------------------------------------------------- #

    def cached(self) -> dict[str, Quote]:
        return dict(self._mem)

    async def snapshot(
        self, instruments: Iterable[Instrument], ttl: float = 60.0
    ) -> dict[str, Quote]:
        """取一批品种的最新报价；TTL 内直接返回内存缓存。"""
        items = list(instruments)
        if not items:
            return {}
        wanted = {inst.code for inst in items}
        now = time.time()
        if self._mem and wanted <= set(self._mem) and now - self._mem_at <= ttl:
            self.status = "cache"
            return dict(self._mem)

        async with self._lock:
            now = time.time()
            if self._mem and wanted <= set(self._mem) and now - self._mem_at <= ttl:
                self.status = "cache"
                return dict(self._mem)

            quotes, errors = await asyncio.to_thread(fetch_quotes, items, self.timeout)
            if quotes:
                # 只覆盖本次拿到的品种，其余保留旧帧
                self._mem.update(quotes)
                self._mem_at = time.time()
                self._save_disk(self._mem)
                self.status = "ok" if not errors else "partial"
                self.last_error = "; ".join(errors)
                if errors:
                    logger.warning("[stock] 行情部分失败，沿用旧价：%s", self.last_error)
                return dict(self._mem)

            self.status = "stale" if self._mem else "fail"
            self.last_error = "; ".join(errors) or "行情接口无返回"
            logger.warning("[stock] 行情抓取失败：%s", self.last_error)
            return dict(self._mem)

    def quote_time(self) -> float:
        return self._mem_at