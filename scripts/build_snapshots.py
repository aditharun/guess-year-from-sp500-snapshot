#!/usr/bin/env python3
"""Build the static snapshot files for The Tape.

No API key. Downloads, then caches under data/.cache/ (gitignored):

- S&P 500 membership history from the public fja05680/sp500 dataset
- Unadjusted daily closes from CNBC's public chart endpoint
- Shares outstanding from SEC companyfacts-style concept filings
- A current quote, used only when SEC shares disagree with the live market cap

Each month from January 1996 through the latest complete price month contributes
one real session. Prices are unadjusted closes. Market caps are reconstructed
so the tape can be sorted largest to smallest; they are not shown in the game.
Before SEC share counts exist (roughly 2009), the sort uses today's market cap
scaled by the split-adjusted price, and delisted tickers are often missing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / ".cache"
SNAP_DIR = ROOT / "data" / "snapshots"

CONSTITUENTS_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv"
)
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
SEC_UA = "Sp500Guesser/1.0 (educational static game; contact: research@example.com)"

# Old symbols whose price history now lives under the successor ticker.
PRICE_SYMBOL = {
    "FB": "META",
    "ANTM": "ELV",
}

# Extra CIKs when the current ticker map points at a successor with a short history.
EXTRA_CIKS = {
    "XOM": [34088],
    "GOOGL": [1288776],
    "GOOG": [1288776],
    "FB": [1326801],
    "META": [1326801],
}

SHARE_CONCEPTS = (
    ("us-gaap", "CommonStockSharesOutstanding"),
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
)
ALLOWED_FORMS = {
    "10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A",
}
COMMON_SPLITS = {2, 3, 4, 5, 7, 8, 10, 15, 20, 25, 50}
MAX_SHARE_AGE_DAYS = 550
START = date(1996, 1, 1)
PRICE_FROM = "1996-01-01"
PRICE_FROM_STAMP = "19960101000000"

# Date bounds are exclusive on the right. Only renames that are easy to misread.
RENAMES = (
    ("GOOGL", "0000-01-01", "2014-04-03", "Google"),
    ("GOOGL", "2014-04-03", "2015-10-02", "Google Class A"),
    ("GOOGL", "2015-10-02", "9999-01-01", "Alphabet Class A"),
    ("GOOG", "0000-01-01", "2015-10-02", "Google Class C"),
    ("GOOG", "2015-10-02", "9999-01-01", "Alphabet Class C"),
    ("FB", "0000-01-01", "2021-10-28", "Facebook"),
    ("FB", "2021-10-28", "9999-01-01", "Meta Platforms"),
    ("META", "0000-01-01", "2021-10-28", "Facebook"),
    ("META", "2021-10-28", "9999-01-01", "Meta Platforms"),
    ("ANTM", "0000-01-01", "9999-01-01", "Anthem"),
    ("ELV", "0000-01-01", "9999-01-01", "Elevance Health"),
    ("XOM", "0000-01-01", "9999-01-01", "Exxon Mobil"),
    ("GE", "0000-01-01", "2024-04-02", "GE"),
    ("IBM", "0000-01-01", "9999-01-01", "IBM"),
    ("AMZN", "0000-01-01", "9999-01-01", "Amazon"),
    ("BKNG", "0000-01-01", "2018-02-21", "Priceline"),
    ("BKNG", "2018-02-21", "9999-01-01", "Booking"),
)

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")
SUFFIXES = (
    " incorporated",
    " corporation",
    " & co.",
    " & co",
    " corp.",
    " corp",
    " inc.",
    " inc",
    " ltd.",
    " ltd",
    " plc",
    " l.p.",
    " n.v.",
    " s.a.",
    " limited",
    " co.",
    " co",
    " lp",
)


class NotFound(Exception):
    pass


class Limiter:
    def __init__(self, per_sec: float):
        self.gap = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.gap
        if delay:
            time.sleep(delay)


def http_get(url: str, headers: dict, timeout: int = 60, attempts: int = 5) -> bytes:
    delay = 1.5
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 404:
                raise NotFound(url) from exc
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError(f"failed {url}") from last_error


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def cache_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def parse_cnbc_magnitude(raw) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "").replace("$", "")
    if not text or text.upper() in {"N/A", "NULL", "NONE", "-"}:
        return None
    mult = 1.0
    suffix = text[-1].upper()
    if suffix in {"M", "B", "T"} and len(text) > 1:
        mult = {"M": 1e6, "B": 1e9, "T": 1e12}[suffix]
        text = text[:-1]
    try:
        value = float(text) * mult
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def match_ratio(ratio: float) -> int | None:
    """Nearest share-count factor if an overnight move looks like a split, not a crash."""
    if ratio < 1.85 or ratio > 80:
        return None
    candidates = []
    for n in range(2, 81):
        err = abs(ratio - n) / n
        limit = 0.045 if n <= 3 else 0.075
        if err <= limit:
            candidates.append((err, n))
    if not candidates:
        return None
    best_err = min(err for err, _n in candidates)
    close = [(err, n) for err, n in candidates if err <= best_err + 0.015]
    close.sort(key=lambda item: (0 if item[1] in COMMON_SPLITS else 1, item[0]))
    return close[0][1]


def price_divisors(bars: dict[str, float]) -> dict[str, float]:
    """Cumulative price divisor for splits strictly after each date."""
    dates = sorted(bars)
    event = [1.0] * len(dates)
    for i in range(1, len(dates)):
        prev = bars[dates[i - 1]]
        cur = bars[dates[i]]
        if prev <= 0 or cur <= 0:
            continue
        # Penny-stock glitches and reused tickers are not splits we can trust.
        if min(prev, cur) < 2 or max(prev, cur) < 8:
            continue
        nxt = bars[dates[i + 1]] if i + 1 < len(dates) else None
        if nxt is not None:
            if abs(nxt - prev) < abs(nxt - cur):
                continue
            if abs(nxt - cur) / cur > 0.35:
                continue
        # Median is only a fallback when the overnight gap is already split-sized.
        # Using it on an ordinary day will see the previous split inside the window
        # and count that split a second time.
        overnight = max(prev, cur) / min(prev, cur)
        pre = [bars[dates[j]] for j in range(max(0, i - 3), i)]
        post = [bars[dates[j]] for j in range(i, min(len(dates), i + 3))]
        pre_med = statistics.median(pre) if pre else prev
        post_med = statistics.median(post) if post else cur
        if prev > cur:
            factor = match_ratio(prev / cur)
            if factor is None and overnight >= 3 and post_med:
                factor = match_ratio(pre_med / post_med)
            if factor:
                event[i] = float(factor)
        else:
            factor = match_ratio(cur / prev)
            if factor is None and overnight >= 3 and pre_med:
                factor = match_ratio(post_med / pre_med)
            if factor:
                event[i] = 1.0 / float(factor)
    suffix = [1.0] * (len(dates) + 1)
    for i in range(len(dates) - 1, -1, -1):
        suffix[i] = event[i] * suffix[i + 1]
    return {dates[i]: suffix[i + 1] for i in range(len(dates))}


def clean_name(name: str | None) -> str | None:
    if not name:
        return None
    text = " ".join(str(name).replace("\u00a0", " ").replace(",", " ").split())
    changed = True
    while changed and text:
        changed = False
        low = text.lower()
        for suffix in SUFFIXES:
            if low.endswith(suffix):
                text = text[: len(text) - len(suffix)].rstrip(" ,.")
                changed = True
                break
    if text.lower().startswith("the "):
        text = text[4:].strip()
    if text.lower().endswith(" company") and len(text.split()) > 2:
        text = text[: -len(" company")].strip()
    words = []
    for word in text.split(" "):
        if word.isupper() and word.isalpha() and len(word) > 4:
            words.append(word.title())
        else:
            words.append(word)
    text = " ".join(words).strip(" ,.")
    return text or None


def display_name(ticker: str, day: str, quote_name: str | None) -> str:
    for symbol, start, end, label in RENAMES:
        if ticker == symbol and start <= day < end:
            return label
    cleaned = clean_name(quote_name)
    return cleaned or ticker


def era_ceiling(day: date) -> float:
    # Before 2008 the only reconstructed caps above this line were the reused
    # ticker C (today's Citigroup) sitting near $1T. Real leaders were lower.
    if day < date(2008, 1, 1):
        return 650e9
    if day < date(2012, 1, 1):
        return 1.2e12
    if day < date(2018, 8, 1):
        return 1.8e12
    if day < date(2020, 8, 1):
        return 2.8e12
    if day < date(2024, 1, 1):
        return 4.5e12
    return 8.5e12


def download_constituents() -> list[tuple[str, list[str]]]:
    path = CACHE / "constituents.csv"
    if not path.exists():
        print("downloading constituents…", flush=True)
        raw = http_get(CONSTITUENTS_URL, {"User-Agent": BROWSER_UA}, timeout=120)
        atomic_write(path, raw.decode("utf-8", "replace"))
    rows: list[tuple[str, list[str]]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            tickers = []
            for part in (record.get("tickers") or "").split(","):
                ticker = part.strip().upper()
                if TICKER_RE.match(ticker):
                    tickers.append(ticker)
            if record.get("date") and tickers:
                rows.append((record["date"], tickers))
    rows.sort(key=lambda item: item[0])
    if not rows:
        raise SystemExit("constituent file was empty")
    print(f"constituent changes {rows[0][0]} → {rows[-1][0]} ({len(rows)} rows)", flush=True)
    return rows


def constituents_on(history: list[tuple[str, list[str]]], day: str) -> list[str]:
    chosen: list[str] | None = None
    for stamp, tickers in history:
        if stamp <= day:
            chosen = tickers
        else:
            break
    return list(chosen or [])


def download_sec_tickers() -> dict[str, int]:
    path = CACHE / "sec_tickers.json"
    if not path.exists():
        print("downloading SEC ticker map…", flush=True)
        raw = http_get(SEC_TICKERS_URL, {"User-Agent": SEC_UA, "Accept": "application/json"})
        atomic_write(path, raw.decode("utf-8"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.values() if isinstance(payload, dict) else payload
    mapped: dict[str, int] = {}
    for record in records:
        ticker = str(record.get("ticker") or "").upper()
        if ticker:
            mapped[ticker] = int(record["cik_str"])
    print(f"SEC tickers {len(mapped)}", flush=True)
    return mapped


def candidate_ciks(ticker: str, sec_map: dict[str, int]) -> list[int]:
    found: list[int] = []

    def add(value) -> None:
        if value is None:
            return
        cik = int(value)
        if cik not in found:
            found.append(cik)

    for symbol in (ticker, ticker.replace(".", "-"), PRICE_SYMBOL.get(ticker, "")):
        if not symbol:
            continue
        add(sec_map.get(symbol))
        add(sec_map.get(symbol.replace(".", "-")))
        for extra in EXTRA_CIKS.get(symbol, ()):
            add(extra)
    for extra in EXTRA_CIKS.get(ticker, ()):
        add(extra)
    return found


def company_key(ticker: str, sec_map: dict[str, int]) -> str:
    for symbol in (ticker, ticker.replace(".", "-"), PRICE_SYMBOL.get(ticker, "")):
        if symbol and symbol.replace(".", "-") in sec_map:
            return f"cik:{sec_map[symbol.replace('.', '-')]}"
        if symbol and symbol in sec_map:
            return f"cik:{sec_map[symbol]}"
    alias = PRICE_SYMBOL.get(ticker)
    if alias:
        return f"sym:{alias}"
    return f"sym:{ticker}"


def price_cache_path(symbol: str) -> Path:
    return CACHE / "prices" / f"{symbol.replace('/', '_')}.json"


def price_end_stamp() -> str:
    return date.today().strftime("%Y%m%d") + "000000"


def parse_price_bars(payload: dict) -> tuple[dict[str, float], str]:
    bars_out = (payload.get("barData") or {}).get("priceBars") or []
    message = (payload.get("statusMessage") or "").lower()
    bars: dict[str, float] = {}
    for bar in bars_out:
        stamp = str(bar.get("tradeTime") or "")[:8]
        if len(stamp) != 8:
            continue
        try:
            close = float(bar.get("close"))
        except (TypeError, ValueError):
            continue
        if close > 0:
            bars[f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"] = round(close, 4)
    return bars, message


def fetch_bar_window(symbol: str, start_stamp: str, end_stamp: str) -> tuple[str, dict[str, float]]:
    url = (
        "https://ts-api.cnbc.com/harmony/app/bars/"
        f"{urllib.parse.quote(symbol, safe='.')}/1D/"
        f"{start_stamp}/{end_stamp}/unadjusted/EST5EDT.json"
    )
    try:
        raw = http_get(
            url,
            {"User-Agent": BROWSER_UA, "Accept": "application/json", "Referer": "https://www.cnbc.com/"},
        )
        payload = json.loads(raw)
    except NotFound:
        return "missing", {}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as exc:
        print(f"price {symbol} failed: {exc}", flush=True)
        return "error", {}
    bars, message = parse_price_bars(payload)
    if bars:
        return "ok", bars
    if "not found" in message or "security master" in message:
        return "missing", {}
    return "empty", {}


def store_prices(cache: Path, data: dict) -> dict:
    atomic_write(cache, json.dumps(data, separators=(",", ":")))
    return data


def fetch_prices(symbol: str, limiter: Limiter | None = None) -> dict:
    """Daily closes from 1996 on. An older cache that starts in 2011 is backfilled, not discarded."""
    cache = price_cache_path(symbol)
    cached = cache_json(cache)
    if cached and cached.get("from") == PRICE_FROM:
        return cached

    def online(start_stamp: str, end_stamp: str) -> tuple[str, dict[str, float]]:
        if limiter is not None:
            limiter.wait()
        return fetch_bar_window(symbol, start_stamp, end_stamp)

    if cached and cached.get("missing"):
        return store_prices(cache, {"missing": True, "from": PRICE_FROM, "bars": {}})

    bars: dict[str, float] = {}
    if cached:
        bars = dict(cached.get("bars") or {})

    if bars:
        first = min(bars)
        if first > PRICE_FROM:
            end_day = date.fromisoformat(first) - timedelta(days=1)
            status, older = online(PRICE_FROM_STAMP, end_day.strftime("%Y%m%d") + "000000")
            if status == "error":
                print(f"price {symbol} early window failed; keeping cached bars", flush=True)
                return {"missing": False, "bars": bars}
            bars.update(older)
    else:
        status, fetched = online(PRICE_FROM_STAMP, price_end_stamp())
        if status == "error":
            return {"missing": True, "bars": {}}
        if status == "missing":
            return store_prices(cache, {"missing": True, "from": PRICE_FROM, "bars": {}})
        bars = fetched

    if not bars:
        return store_prices(cache, {"missing": True, "from": PRICE_FROM, "bars": {}})
    return store_prices(cache, {"missing": False, "from": PRICE_FROM, "bars": bars})


def series_from_facts(facts: list[dict]) -> list[list]:
    def collect(require_form: bool) -> dict[str, tuple[str, float]]:
        best: dict[str, tuple[str, float]] = {}
        for fact in facts:
            form = fact.get("form") or ""
            if require_form and form not in ALLOWED_FORMS:
                continue
            end = fact.get("end")
            filed = fact.get("filed") or "9999-99-99"
            val = fact.get("val")
            if not end or not isinstance(val, (int, float)) or val <= 0:
                continue
            prev = best.get(end)
            if prev is None or filed < prev[0]:
                best[end] = (filed, float(val))
        return best

    chosen = collect(True) or collect(False)
    return [[end, val] for end, (_filed, val) in sorted(chosen.items())]


def fetch_shares(cik: int) -> dict:
    cache = CACHE / "shares" / f"{cik}.json"
    cached = cache_json(cache)
    if cached is not None:
        return cached
    best_name = None
    best_series: list[list] = []
    for taxonomy, concept in SHARE_CONCEPTS:
        url = (
            "https://data.sec.gov/api/xbrl/companyconcept/"
            f"CIK{cik:010d}/{taxonomy}/{concept}.json"
        )
        try:
            raw = http_get(url, {"User-Agent": SEC_UA, "Accept": "application/json"}, timeout=60)
        except NotFound:
            continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as exc:
            print(f"shares {cik} failed: {exc}", flush=True)
            return {"name": None, "series": []}
        payload = json.loads(raw)
        facts = (payload.get("units") or {}).get("shares") or []
        series = series_from_facts(facts)
        if len(series) > len(best_series):
            best_series = series
            best_name = payload.get("entityName")
        if len(best_series) >= 12:
            break
    data = {"name": best_name, "series": best_series}
    atomic_write(cache, json.dumps(data, separators=(",", ":")))
    return data


def shares_on(series: list[list], day: str) -> float | None:
    best_end = None
    best_val = None
    for end, val in series:
        if end <= day:
            best_end = end
            best_val = float(val)
        else:
            break
    if best_end is None or best_val is None:
        return None
    age = (date.fromisoformat(day) - date.fromisoformat(best_end)).days
    if age > MAX_SHARE_AGE_DAYS:
        return None
    return best_val


def fetch_quotes(symbols: list[str]) -> dict[str, dict]:
    cache_path = CACHE / "quotes.json"
    cached = cache_json(cache_path) or {}
    needed = [symbol for symbol in symbols if symbol not in cached]
    print(f"quotes cached {len(cached)} needed {len(needed)}", flush=True)
    for start in range(0, len(needed), 40):
        batch = needed[start:start + 40]
        joined = "|".join(urllib.parse.quote(symbol, safe=".") for symbol in batch)
        url = (
            "https://quote.cnbc.com/quote-html-webservice/quote.htm"
            f"?symbols={joined}&requestMethod=itv&noform=1&partnerId=2&fund=1&output=json"
        )
        try:
            raw = http_get(url, {"User-Agent": BROWSER_UA, "Accept": "application/json"})
            payload = json.loads(raw)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, NotFound) as exc:
            print(f"quote batch failed: {exc}", flush=True)
            continue
        quotes = (payload.get("ITVQuoteResult") or {}).get("ITVQuote") or []
        if isinstance(quotes, dict):
            quotes = [quotes]
        by_symbol = {}
        for quote in quotes:
            symbol = str(quote.get("symbol") or "").upper()
            if symbol:
                by_symbol[symbol] = quote
        for symbol in batch:
            quote = (
                by_symbol.get(symbol)
                or by_symbol.get(symbol.replace(".", "-"))
                or by_symbol.get(symbol.replace("-", "."))
            )
            if not quote:
                cached.setdefault(symbol, {"name": None, "last": None, "mcap": None})
                continue
            cached[symbol] = {
                "name": quote.get("name") or quote.get("altName"),
                "last": parse_cnbc_magnitude(quote.get("last")),
                "mcap": parse_cnbc_magnitude(quote.get("mktcapView")),
            }
        atomic_write(cache_path, json.dumps(cached, separators=(",", ":")))
        print(f"quotes {min(start + 40, len(needed))}/{len(needed)}", flush=True)
    return cached


def choose_mcap(sec_mcap: float | None, fallback: float | None) -> tuple[float | None, str]:
    # SEC share counts are sometimes restated for later splits. The fallback
    # scales today's company market cap by the split-adjusted price, so a wide
    # gap means the share count is on a different basis. Prefer SEC only when
    # the two stories agree; buybacks keep a true pair inside this band.
    if sec_mcap and fallback:
        ratio = sec_mcap / fallback
        if 0.5 <= ratio <= 2:
            return sec_mcap, "sec"
        return fallback, "fallback"
    if sec_mcap:
        return sec_mcap, "sec"
    if fallback:
        return fallback, "fallback"
    return None, ""


def fallback_mcap(day: str, hist_price: float, bars: dict[str, float], divisors: dict[str, float], quote: dict | None) -> float | None:
    if not quote:
        return None
    current = quote.get("last")
    mcap = quote.get("mcap")
    if not current or not mcap:
        return None
    divisor = divisors.get(day, 1.0) or 1.0
    adjusted = hist_price / divisor
    return mcap * (adjusted / current)


def pick_sessions(aapl_bars: dict[str, float]) -> list[date]:
    last = max(date.fromisoformat(stamp) for stamp in aapl_bars)
    end_cap = min(last, date(last.year, last.month, 1))
    # Include the last month only when it has a session on or before `last`.
    by_month: dict[tuple[int, int], list[date]] = defaultdict(list)
    for stamp in aapl_bars:
        current = date.fromisoformat(stamp)
        if current < START or current > last:
            continue
        if (current.year, current.month) > (end_cap.year, end_cap.month) and current.month != last.month:
            continue
        by_month[(current.year, current.month)].append(current)
    # Drop a trailing partial month only if it is the in-progress month after October logic:
    # keep every month that has at least one bar, through the month of `last` if last is not
    # within the first 3 calendar days (a stub month is a poor "session").
    sessions = []
    for key in sorted(by_month):
        days = sorted(by_month[key])
        if key == (last.year, last.month) and last.day < 10:
            continue
        digest = hashlib.sha256(f"{key[0]:04d}-{key[1]:02d}".encode()).digest()
        index = int.from_bytes(digest[:4], "big") % len(days)
        sessions.append(days[index])
    return sessions


def run_pool(items: list, worker, workers: int, label: str) -> dict:
    results = {}
    done = 0
    total = len(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            results[item] = future.result()
            done += 1
            if done % 50 == 0 or done == total:
                print(f"{label} {done}/{total}", flush=True)
    return results


def top_line(rows: list[dict], n: int = 8) -> str:
    ordered = sorted(rows, key=lambda row: (-row["mcap"], row["name"]))
    parts = []
    for row in ordered[:n]:
        parts.append(f"{row['name']} ${row['price']:.2f} {row['mcap']/1e9:.0f}B")
    return "; ".join(parts)


def validate(snapshots: list[dict]) -> None:
    if len(snapshots) < 340:
        raise SystemExit(f"only {len(snapshots)} sessions, expected a month from 1996 through the latest year")
    months = Counter(item["date"][5:7] for item in snapshots)
    missing = [f"{i:02d}" for i in range(1, 13) if months[f"{i:02d}"] < 8]
    if missing:
        raise SystemExit(f"months too thin: {missing} counts={dict(months)}")
    counts = [len(item["rows"]) for item in snapshots]
    print(
        f"sessions {len(snapshots)} names min/median/max "
        f"{min(counts)}/{sorted(counts)[len(counts)//2]}/{max(counts)}",
        flush=True,
    )
    by_year: dict[str, list[int]] = defaultdict(list)
    for item in snapshots:
        by_year[item["date"][:4]].append(len(item["rows"]))
    thin_years = []
    for year in sorted(by_year):
        vals = sorted(by_year[year])
        median = vals[len(vals) // 2]
        print(f"coverage {year} sessions={len(vals)} names {vals[0]}/{median}/{vals[-1]}", flush=True)
        if vals[0] < 180 or len(vals) < 6:
            thin_years.append(year)
    if thin_years:
        print(f"thin years: {', '.join(thin_years)}", flush=True)
    if min(counts) < 100:
        raise SystemExit("a session has fewer than 100 priced names")

    def nearest(target: date) -> dict:
        return min(snapshots, key=lambda item: abs((date.fromisoformat(item["date"]) - target).days))

    checks = (
        (date(2016, 6, 15), "Apple", 80, 140, 350e9, 950e9, 3),
        (date(2012, 6, 15), "Exxon Mobil", 50, 120, 250e9, 700e9, 8),
    )
    for target, name, pmin, pmax, cmin, cmax, rank_max in checks:
        snap = nearest(target)
        ordered = sorted(snap["rows"], key=lambda row: (-row["mcap"], row["name"]))
        match = next((row for row in ordered if row["name"] == name), None)
        print(f"check {target} via {snap['date']}: {top_line(snap['rows'])}", flush=True)
        if match is None:
            raise SystemExit(f"{name} missing near {target}")
        rank = ordered.index(match) + 1
        if not (pmin <= match["price"] <= pmax and cmin <= match["mcap"] <= cmax and rank <= rank_max):
            raise SystemExit(
                f"{name} on {snap['date']} looks wrong: price {match['price']} "
                f"mcap {match['mcap']:.3g} rank {rank} source {match['source']}"
            )
    early = nearest(date(1998, 6, 15))
    early_top = [row["name"] for row in sorted(early["rows"], key=lambda row: -row["mcap"])[:25]]
    print(f"check 1998 via {early['date']}: {top_line(early['rows'])}", flush=True)
    needles = ("Microsoft", "GE", "Exxon", "Intel", "Coca", "Walmart", "IBM", "Merck")
    if not any(any(needle in name for needle in needles) for name in early_top):
        raise SystemExit(f"1998 leaders look wrong: {early_top[:8]}")

    recent = nearest(date(2024, 11, 15))
    recent_top = [row["name"] for row in sorted(recent["rows"], key=lambda row: -row["mcap"])[:3]]
    print(f"check 2024 via {recent['date']}: {top_line(recent['rows'])}", flush=True)
    if not ({"Nvidia", "Apple", "Microsoft"} & set(recent_top)):
        raise SystemExit(f"2024 leaders look wrong: {recent_top}")

    old = nearest(date(2012, 6, 15))
    for row in old["rows"]:
        if row["mcap"] > 450e9 and row["name"] not in {"Apple", "Exxon Mobil"}:
            raise SystemExit(f"2012 cap looks inflated: {row['name']} {row['mcap']/1e9:.0f}B")
    mid = nearest(date(2016, 6, 15))
    for row in mid["rows"]:
        if row["mcap"] > 900e9:
            raise SystemExit(f"2016 cap looks inflated: {row['name']} {row['mcap']/1e9:.0f}B")
        if row["name"].startswith("Booking") and row["mcap"] > 180e9:
            raise SystemExit(f"Booking 2016 cap {row['mcap']/1e9:.0f}B")
        if row["name"].startswith("Chipotle") and row["mcap"] > 40e9:
            raise SystemExit(f"Chipotle 2016 cap {row['mcap']/1e9:.0f}B")


def self_test() -> None:
    assert parse_cnbc_magnitude("4,914,444.17M") == 4_914_444.17 * 1e6
    assert abs(parse_cnbc_magnitude("14,594.18M") - 1.459418e10) < 1
    assert clean_name("Apple Inc.") == "Apple"
    assert clean_name("BERKSHIRE HATHAWAY INC") == "Berkshire Hathaway"
    assert clean_name("NVIDIA Corporation") == "Nvidia"
    assert clean_name("JPMorgan Chase & Co.") == "JPMorgan Chase"
    assert clean_name("The Procter & Gamble Company") == "Procter & Gamble"
    assert clean_name("The Home Depot, Inc.") == "Home Depot"
    assert clean_name("Boeing Co") == "Boeing"
    assert display_name("GE", "2016-06-03", "GE Aerospace") == "GE"
    assert display_name("BKNG", "2012-06-01", "Booking Holdings Inc.") == "Priceline"
    assert display_name("AMZN", "2016-06-03", "Amazon.com Inc") == "Amazon"
    assert display_name("FB", "2018-03-01", "Meta Platforms, Inc.") == "Facebook"
    assert display_name("META", "2023-01-01", "Meta Platforms, Inc.") == "Meta Platforms"
    assert display_name("GOOGL", "2012-05-01", "Alphabet Class A") == "Google"
    bars = {
        "2020-08-27": 500.0,
        "2020-08-28": 502.0,
        "2020-08-31": 126.0,
        "2020-09-01": 127.0,
    }
    divisors = price_divisors(bars)
    assert abs(divisors["2020-08-28"] - 4) < 1e-9, divisors
    assert abs(divisors["2020-08-31"] - 1) < 1e-9, divisors
    reverse = {
        "2021-07-30": 13.0,
        "2021-08-02": 104.0,
        "2021-08-03": 103.0,
    }
    rev = price_divisors(reverse)
    assert abs(rev["2021-07-30"] - (1 / 8)) < 1e-9, rev
    assert match_ratio(20.52) == 20
    assert match_ratio(7.16) == 7
    assert match_ratio(49.85) == 50
    assert match_ratio(23.81) == 24
    assert match_ratio(2.30) is None
    tesla = {
        "2020-08-26": 2153.17,
        "2020-08-27": 2238.75,
        "2020-08-28": 2213.40,
        "2020-08-31": 498.32,
        "2020-09-01": 475.05,
        "2020-09-02": 447.37,
    }
    tesla_div = price_divisors(tesla)
    assert abs(tesla_div["2020-08-28"] - 5) < 1e-9, tesla_div
    assert abs(tesla_div["2020-08-31"] - 1) < 1e-9, tesla_div
    series = [["2016-03-31", 100.0], ["2016-06-30", 110.0], ["2017-06-30", 90.0]]
    assert shares_on(series, "2016-08-01") == 110.0
    assert shares_on(series, "2016-01-01") is None
    print("self-test ok")


def build(workers: int) -> None:
    history = download_constituents()
    sec_map = download_sec_tickers()
    cnbc_limit = Limiter(8)
    sec_limit = Limiter(6)
    aapl = fetch_prices("AAPL", cnbc_limit)
    if not aapl.get("bars"):
        raise SystemExit("could not load AAPL prices; CNBC may be blocking this machine")
    sessions = pick_sessions(aapl["bars"])
    print(f"sessions {len(sessions)} {sessions[0]} → {sessions[-1]}", flush=True)

    members = {day: constituents_on(history, day.isoformat()) for day in sessions}
    tickers = sorted({ticker for names in members.values() for ticker in names})
    price_symbols = sorted({PRICE_SYMBOL.get(ticker, ticker) for ticker in tickers})
    print(f"tickers {len(tickers)} price symbols {len(price_symbols)}", flush=True)

    def price_worker(symbol: str) -> dict:
        return fetch_prices(symbol, cnbc_limit)

    prices = run_pool(price_symbols, price_worker, workers, "prices")
    priced = sum(1 for item in prices.values() if item.get("bars"))
    print(f"price series with bars {priced}/{len(price_symbols)}", flush=True)

    ciks = sorted({cik for ticker in tickers for cik in candidate_ciks(ticker, sec_map)})

    def share_worker(cik: int) -> dict:
        if (CACHE / "shares" / f"{cik}.json").exists():
            return fetch_shares(cik)
        sec_limit.wait()
        return fetch_shares(cik)

    shares = run_pool(ciks, share_worker, min(workers, 4), "shares")
    quotes = fetch_quotes(price_symbols)
    divisors = {
        symbol: price_divisors(item["bars"])
        for symbol, item in prices.items()
        if item.get("bars")
    }

    snapshots = []
    source_counts = Counter()
    era_sources = Counter()
    drop_counts = Counter()
    for day in sessions:
        iso = day.isoformat()
        draft = []
        for ticker in members[day]:
            symbol = PRICE_SYMBOL.get(ticker, ticker)
            series = (prices.get(symbol) or {}).get("bars") or {}
            price = series.get(iso)
            if not price:
                drop_counts[ticker] += 1
                continue
            ciks_for = candidate_ciks(ticker, sec_map)
            sec_shares = None
            for cik in ciks_for:
                sec_shares = shares_on((shares.get(cik) or {}).get("series") or [], iso)
                if sec_shares:
                    break
            sec_mcap = price * sec_shares if sec_shares else None
            quote = quotes.get(symbol) or {}
            alt = fallback_mcap(iso, price, series, divisors.get(symbol) or {}, quote)
            mcap, source = choose_mcap(sec_mcap, alt)
            if not mcap:
                drop_counts[ticker] += 1
                continue
            draft.append({
                "ticker": ticker,
                "name": display_name(ticker, iso, quote.get("name")),
                "price": price,
                "mcap": mcap,
                "source": source,
                "key": company_key(ticker, sec_map),
            })
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in draft:
            grouped[row["key"]].append(row)
        for group in grouped.values():
            if len(group) < 2:
                continue
            caps = [row["mcap"] for row in group]
            lo, hi = min(caps), max(caps)
            if lo > 0 and hi / lo < 3:
                for row in group:
                    row["mcap"] /= len(group)
        kept = []
        ceiling = era_ceiling(day)
        floor = 1.5e8 if day < date(2004, 1, 1) else 4e8
        for row in draft:
            if row["mcap"] < floor or row["mcap"] > ceiling:
                drop_counts[row["ticker"]] += 1
                continue
            kept.append(row)
        names = Counter(row["name"] for row in kept)
        for row in kept:
            if names[row["name"]] > 1:
                row["name"] = f"{row['name']} ({row['ticker']})"
        kept.sort(key=lambda row: (-row["mcap"], row["name"]))
        era = "pre-2011" if iso < "2011-11-01" else "2011-on"
        for row in kept:
            source_counts[row["source"]] += 1
            era_sources[f"{era} {row['source']}"] += 1
        snapshots.append({"date": iso, "rows": kept})

    print(f"mcap sources {dict(source_counts)}", flush=True)
    print(f"mcap by era {dict(era_sources)}", flush=True)
    print("most often dropped:", ", ".join(f"{name}×{n}" for name, n in drop_counts.most_common(18)), flush=True)
    validate(snapshots)

    if SNAP_DIR.exists():
        for old in SNAP_DIR.glob("*.json"):
            old.unlink()
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    for snap in snapshots:
        payload = {
            "d": snap["date"],
            "rows": [
                [row["name"], round(row["price"], 2), int(round(row["mcap"] / 1e6))]
                for row in snap["rows"]
            ],
        }
        atomic_write(
            SNAP_DIR / f"{snap['date']}.json",
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        )
    manifest = {
        "game": "the-tape",
        "from": snapshots[0]["date"],
        "to": snapshots[-1]["date"],
        "snapshots": [snap["date"] for snap in snapshots],
        "built": date.today().isoformat(),
    }
    atomic_write(ROOT / "data" / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    total_bytes = sum(path.stat().st_size for path in SNAP_DIR.glob("*.json"))
    print(f"wrote {len(snapshots)} snapshots, {total_bytes/1e6:.1f} MB", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build static S&P 500 sessions for The Tape")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    build(max(1, args.workers))


if __name__ == "__main__":
    main()
