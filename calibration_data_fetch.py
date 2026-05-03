#!/usr/bin/env python3
"""
calibration_data_fetch.py — Retrospective calibration data retrieval.

Fetches resolved btc-updown-5m market history from Polymarket (Gamma series
API) and BTCUSDT aggTrades from data.binance.vision.

Three independent phases; run together with "all" or individually:

  markets   Enumerate resolved btc-updown-5m events from the Gamma series
            API (series_id=10684, slug=btc-up-or-down-5m).
            Writes: {out}/markets.jsonl

  prices    Best-effort 1-minute YES/NO token price series from CLOB
            prices-history.  NOTE: Polymarket does not serve historical price
            data for resolved markets.  This phase succeeds only for markets
            that are still active or very recently resolved; all others return
            an empty series.  For full-resolution calibration, BBO data must
            be collected live via the market WS.
            Writes: {out}/prices/YES_{condition_id}.csv  (if data available)
                    {out}/prices/NO_{condition_id}.csv   (if data available)

  binance   Daily BTCUSDT aggTrades archives from data.binance.vision.
            Writes: {out}/binance/BTCUSDT-aggTrades-YYYY-MM-DD.csv.gz
                    (header row added; original ZIP extracted, gzip-recompressed)

  all       Run markets → prices → binance in sequence.

Usage:
  python calibration_data_fetch.py markets --out data/ --days 90
  python calibration_data_fetch.py prices  --out data/
  python calibration_data_fetch.py binance --out data/ --start 2025-11-21 --end 2026-05-03
  python calibration_data_fetch.py all     --out data/ --days 90

Output columns
--------------
markets.jsonl fields:
  event_slug, condition_id, question, yes_token_id, no_token_id,
  start_ts, end_ts, outcome (1=YES/Up won, 0=NO/Down won, null=unknown),
  resolved, fee_schedule (raw dict from Gamma API)

binance aggTrades CSV columns (8 per row):
  agg_trade_id, price, quantity, first_trade_id, last_trade_id,
  transact_time_us, is_buyer_maker, best_price_match

  transact_time_us: microseconds since Unix epoch (NOT milliseconds).
  is_buyer_maker: True  = sell-initiated (aggressor is seller, bid hit).
                  False = buy-initiated (aggressor is buyer, ask lifted).
  OFI convention: buy_qty = quantity where is_buyer_maker == False.

Feature approximations (live bot uses BBO; archive provides aggTrades):
  move      <- price_end - price_start over aggTrades window (proxy: last-trade-VWAP)
  ofi_sum   <- sum(buy_qty - sell_qty) over aggTrades window (proxy via is_buyer_maker)
  imbalance <- (buy_qty - sell_qty) / total_qty over window  (proxy)
  spread    <- not available from archive; set to NaN in pipeline

Fee note:
  Live markets carry feeSchedule.rate = 0.072 (7.2% applied to payout,
  takers only, 20% rebate to makers).  Verify current schedule at
  https://docs.polymarket.com/trading/fees before each calibration run.

Add the output directory to .gitignore — 90 days of Binance data is ~10 GB.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import io
import json
import logging
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp

# ── constants ──────────────────────────────────────────────────────────────────

_GAMMA_BASE  = "https://gamma-api.polymarket.com"
_CLOB_BASE   = "https://clob.polymarket.com"
_BN_BASE     = "https://data.binance.vision/data/spot/daily/aggTrades"
_BN_SYMBOL   = "BTCUSDT"

# Gamma series for btc-updown-5m (series_id discovered via live slug probe)
_SERIES_ID   = "10684"
_SERIES_SLUG = "btc-up-or-down-5m"

# Binance aggTrades CSV columns.  Eight fields per row; the archive has no
# header.  transact_time_us is microseconds (NOT milliseconds); confirmed
# empirically — divide by 1_000_000 to get Unix seconds.
_BN_COLS = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time_us",
    "is_buyer_maker",
    "best_price_match",
)

_SERIES_PAGE          = 200   # events per Gamma API page
# Empirical anchors (confirmed via binary search on May 3, 2026):
#   series offset=0       -> Dec 18, 2025 (first market in API)
#   series offset=25,000  -> Mar 31, 2026
#   series ~34,750 total  -> May 3, 2026 21:10 UTC (includes future slots)
# Rate: (34750 / 136.6 days) = ~255 markets/day
_SERIES_API_START     = date(2025, 12, 18)  # first item in series API
_MARKETS_PER_DAY      = 255                 # empirical; ~21 h/day × 12/h
_PRICES_CONCURRENCY   = 20    # concurrent CLOB prices-history requests
_BN_CONCURRENCY       = 3     # concurrent Binance archive downloads

log = logging.getLogger("calibration_fetch")


# ── small utilities ────────────────────────────────────────────────────────────

def _parse_json_field(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            pass
    return []


def _settlement_outcome(outcome_prices: list) -> int | None:
    """1 = YES/Up won, 0 = NO/Down won, None = unresolved or ambiguous."""
    if len(outcome_prices) < 2:
        return None
    try:
        yes = float(outcome_prices[0])
        no  = float(outcome_prices[1])
    except (TypeError, ValueError):
        return None
    if yes > 0.9:
        return 1
    if no > 0.9:
        return 0
    return None


def _iso_to_ts(s: str | None) -> float | None:
    if not s:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _date_to_offset(target: date) -> int:
    """Estimate the Gamma series offset for `target` date.

    Uses the empirical anchor _SERIES_API_START with a 14-day safety margin
    so we start paging slightly before the desired date window.
    """
    days = max(0, (target - _SERIES_API_START).days - 14)
    return int(days * _MARKETS_PER_DAY)


# ── HTTP helpers ───────────────────────────────────────────────────────────────

async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    params: dict[str, str] | None = None,
    *,
    retries: int = 3,
) -> Any:
    for attempt in range(retries):
        try:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status == 429:
                    wait = 2.0 ** attempt
                    log.warning("Rate-limited on %s; sleeping %.1fs", url, wait)
                    await asyncio.sleep(wait)
                    continue
                if resp.status >= 500:
                    if attempt < retries - 1:
                        await asyncio.sleep(0.5 * 2 ** attempt)
                        continue
                    log.warning("Server error %d on %s", resp.status, url)
                    return None
                resp.raise_for_status()
                text = await resp.text(encoding="utf-8")
                return json.loads(text)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt == retries - 1:
                log.warning("GET %s failed after %d retries: %s", url, retries, exc)
                return None
            await asyncio.sleep(0.5 * 2 ** attempt)
    return None


async def _get_bytes(
    session: aiohttp.ClientSession,
    url: str,
    *,
    retries: int = 3,
) -> bytes | None:
    for attempt in range(retries):
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status == 429:
                    await asyncio.sleep(2.0 ** attempt * 2)
                    continue
                resp.raise_for_status()
                return await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt == retries - 1:
                log.warning("GET %s failed: %s", url, exc)
                return None
            await asyncio.sleep(2.0 ** attempt)
    return None


# ── phase 1: market enumeration ────────────────────────────────────────────────

def _extract_market_record(ev: dict) -> dict | None:
    """Parse one Gamma API event object into a market record, or None if unusable."""
    markets = ev.get("markets")
    if not isinstance(markets, list) or not markets:
        return None

    # Each btc-updown event contains exactly one market
    m = markets[0]

    token_ids     = _parse_json_field(m.get("clobTokenIds", "[]"))
    outcome_prices = _parse_json_field(m.get("outcomePrices", "[]"))
    if len(token_ids) < 2:
        return None

    # Use eventStartTime (the actual trading window start) when present
    start_ts = _iso_to_ts(ev.get("startTime") or m.get("startDate") or ev.get("startDate"))
    end_ts   = _iso_to_ts(m.get("endDate") or ev.get("endDate"))

    return {
        "event_slug":    ev.get("slug", ""),
        "condition_id":  m.get("conditionId", ""),
        "question":      m.get("question", ""),
        "yes_token_id":  str(token_ids[0]),
        "no_token_id":   str(token_ids[1]),
        "start_ts":      start_ts,
        "end_ts":        end_ts,
        "outcome":       _settlement_outcome(outcome_prices),
        "resolved":      bool(m.get("closed", False)),
        "fee_schedule":  m.get("feeSchedule"),
    }


async def fetch_markets(out_dir: Path, *, days: int = 90) -> list[dict]:
    """
    Enumerate resolved btc-updown-5m markets from Gamma series API.

    Strategy:
    - Use series_id=10684 (btc-up-or-down-5m) without any status filter.
    - The series returns records in ascending endDate order (oldest first).
    - Estimate the starting offset from the requested date range.
    - Paginate forward until end_ts exceeds the target end date.
    - Filter for settled outcomes (clear winner in outcomePrices).

    Writes {out_dir}/markets.jsonl and returns the list.
    """
    now_utc = datetime.now(tz=timezone.utc)
    end_cutoff   = now_utc.timestamp() - 300   # exclude currently active markets
    start_cutoff = (now_utc - timedelta(days=days)).timestamp()

    # Estimate starting offset to avoid paginating from the beginning of the series
    start_date  = (now_utc - timedelta(days=days)).date()
    start_offset = _date_to_offset(start_date)
    log.info(
        "markets: requesting %d days back; estimated start offset=%d",
        days,
        start_offset,
    )

    out_file = out_dir / "markets.jsonl"
    markets: list[dict] = []

    async with aiohttp.ClientSession() as session:
        offset    = start_offset
        exhausted = False

        while not exhausted:
            data = await _get_json(
                session,
                f"{_GAMMA_BASE}/events",
                params={
                    "series_id": _SERIES_ID,
                    "limit":     str(_SERIES_PAGE),
                    "offset":    str(offset),
                },
            )
            if data is None:
                break
            events: list[dict] = data if isinstance(data, list) else []
            if not events:
                break

            for ev in events:
                slug = ev.get("slug", "")
                if not slug.startswith("btc-updown-5m-"):
                    continue

                end_ts = _iso_to_ts(ev.get("endDate"))
                if end_ts is None:
                    continue
                if end_ts < start_cutoff:
                    continue   # too old, skip but keep paging (offset may overshoot)
                if end_ts > end_cutoff:
                    exhausted = True   # hit the active frontier
                    break

                record = _extract_market_record(ev)
                if record is None:
                    continue
                if record["outcome"] is None:
                    continue   # skip unresolved / no clear winner
                markets.append(record)

            offset += _SERIES_PAGE
            log.info(
                "markets: offset=%d -> %d resolved records so far",
                offset - _SERIES_PAGE,
                len(markets),
            )
            await asyncio.sleep(0.15)   # polite rate-limit

    out_dir.mkdir(parents=True, exist_ok=True)
    with out_file.open("w", encoding="utf-8") as fh:
        for m in markets:
            fh.write(json.dumps(m, ensure_ascii=False) + "\n")

    yes_count = sum(1 for m in markets if m["outcome"] == 1)
    no_count  = sum(1 for m in markets if m["outcome"] == 0)
    log.info(
        "markets phase complete: %d records (YES=%d NO=%d) -> %s",
        len(markets),
        yes_count,
        no_count,
        out_file,
    )
    return markets


# ── phase 2: CLOB price history (best-effort) ──────────────────────────────────

async def _fetch_price_series(
    session: aiohttp.ClientSession,
    token_id: str,
    start_ts: float,
    end_ts: float,
) -> list[dict[str, Any]]:
    data = await _get_json(
        session,
        f"{_CLOB_BASE}/prices-history",
        params={
            "market":   token_id,
            "interval": "1m",
            "fidelity": "1",
            "startTs":  str(int(start_ts)),
            "endTs":    str(int(end_ts)),
        },
    )
    if not isinstance(data, dict):
        return []
    return [
        {"t": int(h["t"]), "p": float(h["p"])}
        for h in (data.get("history") or [])
        if "t" in h and "p" in h
    ]


async def fetch_prices(markets: list[dict], out_dir: Path) -> None:
    """
    Attempt to fetch 1-min price history for YES/NO tokens from CLOB
    prices-history.

    KNOWN LIMITATION: Polymarket does not serve historical BBO data for
    resolved markets.  The prices-history endpoint is live-only.  Expect
    0 data points for all historical markets; only currently active or
    very recently resolved markets return data.  This phase is included
    for completeness and future proofing; the Binance aggTrades phase
    is the primary retrospective data source.
    """
    prices_dir = out_dir / "prices"
    prices_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(_PRICES_CONCURRENCY)

    async def _one(session: aiohttp.ClientSession, mkt: dict, side: str) -> bool:
        token_id = mkt["yes_token_id"] if side == "YES" else mkt["no_token_id"]
        cid      = mkt["condition_id"]
        if not cid or not token_id:
            return False

        out_file = prices_dir / f"{side}_{cid}.csv"
        if out_file.exists():
            return False

        start = mkt.get("start_ts") or 0.0
        end   = mkt.get("end_ts")   or 0.0
        if not start or not end:
            return False

        async with sem:
            history = await _fetch_price_series(session, token_id, start - 60, end + 60)

        if not history:
            return False

        with out_file.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["t", "p"])
            w.writeheader()
            w.writerows(history)
        return True

    async with aiohttp.ClientSession() as session:
        tasks = [
            _one(session, mkt, side)
            for mkt in markets
            for side in ("YES", "NO")
        ]
        total = len(tasks)
        done  = 0
        hits  = 0
        for coro in asyncio.as_completed(tasks):
            result = await coro
            if result:
                hits += 1
            done += 1
            if done % 200 == 0 or done == total:
                log.info("prices: %d / %d token series processed", done, total)

    if hits == 0:
        log.warning(
            "prices phase: 0 / %d token series returned data. "
            "This is expected — Polymarket prices-history is live-only. "
            "BBO history for resolved markets requires live WS collection "
            "or L2-authenticated CLOB /data/trades.",
            total,
        )
    else:
        log.info("prices phase complete: %d / %d series had data", hits, total)


# ── phase 3: Binance aggTrades archive ────────────────────────────────────────

def _date_range(start: date, end: date) -> list[date]:
    dates, cur = [], start
    while cur <= end:
        dates.append(cur)
        cur += timedelta(days=1)
    return dates


async def _one_day(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    d: date,
    out_dir: Path,
) -> None:
    date_str = d.strftime("%Y-%m-%d")
    out_file = out_dir / f"{_BN_SYMBOL}-aggTrades-{date_str}.csv.gz"
    if out_file.exists():
        log.debug("binance: skip %s (already exists)", out_file.name)
        return

    zip_url = f"{_BN_BASE}/{_BN_SYMBOL}/{_BN_SYMBOL}-aggTrades-{date_str}.zip"
    async with sem:
        content = await _get_bytes(session, zip_url)

    if content is None:
        log.warning("binance: no archive for %s (404 or error)", date_str)
        return

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                log.error("binance: ZIP for %s has no CSV", date_str)
                return
            raw_csv = zf.read(csv_names[0])
    except zipfile.BadZipFile as exc:
        log.error("binance: corrupt ZIP for %s: %s", date_str, exc)
        return

    # Prepend header row (archive CSVs have no header)
    header = (",".join(_BN_COLS) + "\n").encode()
    with gzip.open(out_file, "wb", compresslevel=6) as gz:
        gz.write(header)
        gz.write(raw_csv)

    log.info("binance: saved %s (%.1f MB raw)", out_file.name, len(raw_csv) / 1e6)


async def fetch_binance(out_dir: Path, *, start: date, end: date) -> None:
    """
    Download daily BTCUSDT aggTrades ZIP archives from data.binance.vision,
    extract the CSV, prepend a header row, and save as gzip CSV.

    Column transact_time_us is in microseconds since Unix epoch.
    Divide by 1_000_000 to get seconds; divide by 1_000 to get milliseconds.

    is_buyer_maker = True  -> sell-initiated trade (taker hit the bid).
                   = False -> buy-initiated trade (taker lifted the ask).

    OFI from aggTrades:
      buy_qty  = sum(quantity where is_buyer_maker == False)
      sell_qty = sum(quantity where is_buyer_maker == True)
      ofi      = buy_qty - sell_qty
    """
    binance_dir = out_dir / "binance"
    binance_dir.mkdir(parents=True, exist_ok=True)

    days = _date_range(start, end)
    sem  = asyncio.Semaphore(_BN_CONCURRENCY)
    log.info("binance: fetching %d day(s) %s to %s", len(days), start, end)

    async with aiohttp.ClientSession() as session:
        tasks = [_one_day(session, sem, d, binance_dir) for d in days]
        total = len(tasks)
        done  = 0
        for coro in asyncio.as_completed(tasks):
            await coro
            done += 1
            if done % 10 == 0 or done == total:
                log.info("binance: %d / %d days processed", done, total)

    log.info("binance phase complete")


# ── CLI ────────────────────────────────────────────────────────────────────────

def _infer_binance_range(markets: list[dict]) -> tuple[date, date]:
    stamps = (
        [m["start_ts"] for m in markets if m.get("start_ts")] +
        [m["end_ts"]   for m in markets if m.get("end_ts")]
    )
    if not stamps:
        today = date.today()
        return today - timedelta(days=90), today
    lo = datetime.fromtimestamp(min(stamps), tz=timezone.utc).date()
    hi = datetime.fromtimestamp(max(stamps), tz=timezone.utc).date()
    return lo, hi


def _load_markets(path: Path) -> list[dict]:
    markets: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                markets.append(json.loads(line))
    return markets


async def _main(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # markets phase
    if args.command in ("markets", "all"):
        markets = await fetch_markets(out, days=args.days)
    else:
        manifest = out / "markets.jsonl"
        if not manifest.exists():
            log.error("markets.jsonl not found at %s — run the 'markets' phase first.", manifest)
            sys.exit(1)
        markets = _load_markets(manifest)
        log.info("Loaded %d markets from %s", len(markets), manifest)

    # prices phase (best-effort)
    if args.command in ("prices", "all"):
        await fetch_prices(markets, out)

    # binance phase
    if args.command in ("binance", "all"):
        if args.start and args.end:
            bn_start = date.fromisoformat(args.start)
            bn_end   = date.fromisoformat(args.end)
        else:
            bn_start, bn_end = _infer_binance_range(markets)
            log.info("Binance date range inferred: %s to %s", bn_start, bn_end)
        await fetch_binance(out, start=bn_start, end=bn_end)


def main() -> None:
    p = argparse.ArgumentParser(
        prog="calibration_data_fetch.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    for name in ("markets", "prices", "binance", "all"):
        sp = sub.add_parser(name)
        sp.add_argument(
            "--out", default="calibration_data",
            help="Output directory (default: calibration_data/)",
        )
        sp.add_argument(
            "--days", type=int, default=90,
            help="Days of market history to fetch (default: 90). Used by 'markets' and 'all'.",
        )
        sp.add_argument(
            "--start",
            help="Binance start date YYYY-MM-DD (inferred from markets.jsonl if omitted).",
        )
        sp.add_argument(
            "--end",
            help="Binance end date YYYY-MM-DD (inferred from markets.jsonl if omitted).",
        )
        sp.add_argument("-v", "--verbose", action="store_true")

    asyncio.run(_main(p.parse_args()))


if __name__ == "__main__":
    main()
