"""
AURA DEX Telegram pinned price ticker (v4 — multi-channel, EN + RO)
-------------------------------------------------------------------
Fetches market data and edits a pinned Telegram message in one or
more channels in different languages, in a single run.

Required env vars:
    BOT_TOKEN     Telegram bot token from @BotFather (one bot, both channels)

Channel 1 (English) — required:
    CHAT_ID       e.g. @auradexecosystem
    MESSAGE_ID    Set after the first init run.

Channel 2 (Romanian) — optional:
    CHAT_ID_RO    e.g. -1003185057272 (numeric for private channels)
    MESSAGE_ID_RO Set after the first init run for the RO channel.

Optional:
    AURA_DEX_URL  Footer link. Default: https://auradex.io
"""

import os
import sys
import time
import json
import http.client
import urllib.request
import urllib.parse
import urllib.error

COINS = [
    ("bitcoin",                  "BTC"),
    ("ethereum",                 "ETH"),
    ("solana",                   "SOL"),
    ("tron",                     "TRX"),
    ("tether-gold",              "XAUT"),
    ("jupiter-exchange-solana",  "JUP"),
    ("raydium",                  "RAY"),
    ("orca",                     "ORCA"),
]

CG_MARKETS_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd"
    "&ids={ids}"
    "&price_change_percentage=24h,7d"
    "&sparkline=true"
)
CG_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
FNG_URL = "https://api.alternative.me/fng/"

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# ---------- Watchdog ----------
#
# A transient failure exits 0, so a 15-minute blip does not email the owner.
# The price of that quiet is silence: if Telegram or CoinGecko stays down,
# every run would keep exiting 0 and nobody would ever learn that the pinned
# message had stopped moving. So count the runs in a row that failed to
# update it, and once the ticker has been silent for MAX_SILENT_RUNS, fail
# loudly. The counter lives in a file the workflow round-trips through the
# Actions cache; a missing or unreadable file just means "healthy".
HEALTH_FILE = ".ticker-health"
MAX_SILENT_RUNS = 4   # 4 consecutive misses = one hour on the 15-min cron

SPARK_CHARS = "▁▂▃▄▅▆▇█"
SPARK_WIDTH = 10

# ---------- i18n ----------

LABELS = {
    "en": {
        "title": "Live Crypto Prices",
        "market": "Market",
        "btc_d": "BTC.D",
        "sentiment": "Sentiment",
        "best_24h": "Best 24h",
        "worst_24h": "Worst 24h",
        "format_note": "Format: 24h % / 7d % · 🟢 up · 🔴 down · ▁▂▃▄▅▆▇█ = 7d trend",
        "updated": "Updated",
        "auto_refresh": "auto-refresh 15 min",
        "trade_on": "Trade on",
    },
    "ro": {
        "title": "Prețuri Crypto Live",
        "market": "Piață",
        "btc_d": "BTC.D",
        "sentiment": "Sentiment",
        "best_24h": "Top 24h",
        "worst_24h": "Flop 24h",
        "format_note": "Format: 24h % / 7d % · 🟢 creștere · 🔴 scădere · ▁▂▃▄▅▆▇█ = trend 7 zile",
        "updated": "Actualizat",
        "auto_refresh": "actualizare automată 15 min",
        "trade_on": "Tranzacționează pe",
    },
}

FNG_TRANS = {
    "en": {
        "extreme fear":  "Extreme Fear",
        "fear":          "Fear",
        "neutral":       "Neutral",
        "greed":         "Greed",
        "extreme greed": "Extreme Greed",
    },
    "ro": {
        "extreme fear":  "Frică Extremă",
        "fear":          "Frică",
        "neutral":       "Neutru",
        "greed":         "Lăcomie",
        "extreme greed": "Lăcomie Extremă",
    },
}


# ---------- Errors ----------

class TransientError(Exception):
    """A self-healing failure (network blip, rate limit, upstream 5xx, or a
    temporary bad response). The next scheduled run 15 minutes later re-posts
    the same pinned message, so a TransientError must NOT fail the workflow —
    otherwise GitHub emails the owner about noise that already fixed itself.
    Reserve a non-zero exit (and thus an email) for permanent/config errors."""


# ---------- HTTP ----------

def http_get_json(url: str, timeout: int = 15, extra_headers: dict = None):
    headers = {"User-Agent": "aura-price-bot/4.0"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def coingecko_headers() -> dict:
    """Returns CoinGecko Demo API key header if env var is set."""
    key = os.environ.get("COINGECKO_API_KEY", "").strip()
    if key:
        return {"x-cg-demo-api-key": key}
    return {}


def _retry_after(body: str, default: int = 5) -> int:
    """Telegram 429 responses carry {"parameters": {"retry_after": N}}."""
    try:
        n = int(json.loads(body).get("parameters", {}).get("retry_after", default))
        return max(1, min(n, 60))  # clamp so we never blow the 5-min job budget
    except Exception:
        return default


def telegram_call(token: str, method: str, payload: dict, attempts: int = 4) -> dict:
    """Call the Telegram Bot API with retry on transient failures.

    Retries network errors, HTTP 429 (rate limit, honouring retry_after) and
    HTTP 5xx (Telegram-side hiccups) with backoff. Genuine application errors
    (other 4xx: "message is not modified", "chat not found", ...) are returned
    to the caller as the JSON envelope (ok=false) exactly as before, so their
    permanent-vs-benign handling is unchanged. If the transient retries are
    exhausted, raises TransientError so the run self-heals without emailing."""
    url = TELEGRAM_API.format(token=token, method=method)
    data = urllib.parse.urlencode(payload).encode("utf-8")
    last_err = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                # Reading the error body can itself time out. Losing the body
                # must not turn a classified HTTP error into an unclassified
                # crash, so fall back to an empty body.
                body = ""
            if e.code == 429 or e.code >= 500:
                # Transient: rate limited or upstream error. Back off + retry.
                last_err = f"HTTP {e.code} {body[:200]}"
                wait = _retry_after(body) if e.code == 429 else (2 ** i)
                print(f"WARN: Telegram {method} HTTP {e.code} on attempt "
                      f"{i+1}/{attempts}; sleeping {wait}s", file=sys.stderr)
                if i < attempts - 1:
                    time.sleep(wait)
                continue
            # Other 4xx = application error with a JSON envelope
            # (ok=false, description). Return it so callers can inspect the
            # description ("message is not modified" is a benign no-op; things
            # like "chat not found" are permanent and should surface).
            try:
                return json.loads(body)
            except ValueError:
                raise RuntimeError(f"Telegram {method} failed: HTTP {e.code} {body}")
        except (OSError, http.client.HTTPException, ValueError) as e:
            # Transport-level blip: connect/DNS failure (URLError), read
            # timeout, connection reset, TLS hiccup, or a truncated body that
            # will not parse as JSON. All self-heal on the next 15-min cron.
            #
            # Catching urllib.error.URLError alone was NOT enough: a timeout
            # while reading the response body raises a bare TimeoutError
            # ("The read operation timed out"), which is an OSError but not a
            # URLError, so it escaped this handler, skipped the retries, and
            # was counted as a permanent error in main() -> exit 1 -> failure
            # email. That is exactly what happened on run 11463 (2026-08-19).
            last_err = f"{type(e).__name__}: {e}"
            print(f"WARN: Telegram {method} network error on attempt "
                  f"{i+1}/{attempts}: {last_err}; retrying", file=sys.stderr)
            if i < attempts - 1:
                time.sleep(2 ** i)
    raise TransientError(
        f"Telegram {method} failed after {attempts} attempts: {last_err}")


def fetch_with_retry(url: str, attempts: int = 4, extra_headers: dict = None):
    last_err = None
    for i in range(attempts):
        try:
            return http_get_json(url, extra_headers=extra_headers)
        except urllib.error.HTTPError as e:
            last_err = e
            # 429 = rate limited. Wait longer.
            wait = 30 if e.code == 429 else (2 ** i)
            print(f"WARN: HTTP {e.code} on attempt {i+1}; sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
        except Exception as e:
            last_err = e
            time.sleep(2 ** i)
    raise TransientError(f"Fetch failed after {attempts} attempts: {url} -> {last_err}")


# ---------- Data fetchers ----------

def fetch_market_data() -> list:
    ids = ",".join(c[0] for c in COINS)
    url = CG_MARKETS_URL.format(ids=urllib.parse.quote(ids, safe=","))
    data = fetch_with_retry(url, extra_headers=coingecko_headers())
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"Unexpected markets response: {data}")
    by_id = {c["id"]: c for c in data}
    ordered = []
    for cg_id, sym in COINS:
        if cg_id not in by_id:
            raise RuntimeError(f"Markets response missing id: {cg_id}")
        ordered.append((sym, by_id[cg_id]))
    return ordered


def fetch_global() -> dict:
    try:
        d = fetch_with_retry(CG_GLOBAL_URL, extra_headers=coingecko_headers())["data"]
        return {
            "mcap_usd": float(d["total_market_cap"]["usd"]),
            "mcap_change_24h_pct": float(d.get("market_cap_change_percentage_24h_usd", 0.0)),
            "btc_dominance": float(d["market_cap_percentage"]["btc"]),
        }
    except Exception as e:
        print(f"WARN: global fetch failed: {e}", file=sys.stderr)
        return None


def fetch_fng() -> dict:
    try:
        d = fetch_with_retry(FNG_URL)
        item = d["data"][0]
        return {
            "value": int(item["value"]),
            "classification": item["value_classification"],
        }
    except Exception as e:
        print(f"WARN: F&G fetch failed: {e}", file=sys.stderr)
        return None


# ---------- Formatters ----------

def fmt_price(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if v >= 1000:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:,.2f}"
    if v >= 0.01:
        return f"${v:.4f}"
    return f"${v:.6f}"


def fmt_pct(value) -> str:
    if value is None:
        return "  n/a "
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "  n/a "
    return f"{v:+.2f}%"


def fmt_mcap(v: float) -> str:
    if v >= 1e12:
        return f"${v/1e12:.2f}T"
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.2f}M"
    return f"${v:,.0f}"


def direction_emoji(value) -> str:
    if value is None:
        return "⚪"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "⚪"
    if v > 0:
        return "🟢"
    if v < 0:
        return "🔴"
    return "⚪"


def fng_emoji(classification: str) -> str:
    c = (classification or "").lower()
    if "extreme fear" in c:
        return "🔴"
    if "fear" in c:
        return "🟠"
    if "neutral" in c:
        return "🟡"
    if "extreme greed" in c:
        return "🟢"
    if "greed" in c:
        return "🟢"
    return "⚪"


def translate_fng(classification: str, lang: str) -> str:
    key = (classification or "").strip().lower()
    return FNG_TRANS.get(lang, FNG_TRANS["en"]).get(key, classification or "")


def make_sparkline(prices, width: int = SPARK_WIDTH) -> str:
    if not prices or len(prices) < 2:
        return " " * width
    if width <= 1:
        return SPARK_CHARS[3]
    step = (len(prices) - 1) / (width - 1)
    sampled = [prices[round(i * step)] for i in range(width)]
    lo = min(sampled)
    hi = max(sampled)
    if hi - lo < 1e-9:
        return SPARK_CHARS[3] * width
    n = len(SPARK_CHARS) - 1
    return "".join(
        SPARK_CHARS[round((p - lo) / (hi - lo) * n)]
        for p in sampled
    )


# ---------- Message builder ----------

def build_message(market_data, global_data, fng_data, aura_url: str, lang: str = "en") -> str:
    L = LABELS.get(lang, LABELS["en"])
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    header_bits = []
    if global_data:
        header_bits.append(
            f"{L['market']} {fmt_mcap(global_data['mcap_usd'])} "
            f"({global_data['mcap_change_24h_pct']:+.2f}%)"
        )
        header_bits.append(f"{L['btc_d']} {global_data['btc_dominance']:.1f}%")
    if fng_data:
        cls_translated = translate_fng(fng_data["classification"], lang)
        header_bits.append(
            f"{L['sentiment']} {fng_data['value']} "
            f"{cls_translated} {fng_emoji(fng_data['classification'])}"
        )
    header_line = " · ".join(header_bits)

    rows = []
    for sym, c in market_data:
        emoji = direction_emoji(c.get("price_change_percentage_24h"))
        price = fmt_price(c.get("current_price"))
        pct24 = fmt_pct(c.get("price_change_percentage_24h"))
        pct7d = fmt_pct(c.get("price_change_percentage_7d_in_currency"))
        sp = make_sparkline(c.get("sparkline_in_7d", {}).get("price", []))
        rows.append(f"{emoji} {sym:<5} {price:<11} {sp}  {pct24:>8} / {pct7d:>8}")
    table = "\n".join(rows)

    movers = [
        (sym, c.get("price_change_percentage_24h") or 0.0)
        for sym, c in market_data
    ]
    movers_sorted = sorted(movers, key=lambda x: x[1])
    worst_sym, worst_pct = movers_sorted[0]
    best_sym, best_pct = movers_sorted[-1]

    movers_lines = (
        f"🚀 {L['best_24h']}: <b>{best_sym}</b> {best_pct:+.2f}%\n"
        f"💀 {L['worst_24h']}: <b>{worst_sym}</b> {worst_pct:+.2f}%"
    )

    parts = [f"<b>{L['title']}</b>", ""]
    if header_line:
        parts.append(f"<i>{header_line}</i>")
        parts.append("")
    parts.append(f"<pre>{table}</pre>")
    parts.append("")
    parts.append(movers_lines)
    parts.append("")
    parts.append(f"<i>{L['format_note']}</i>")
    parts.append(f"<i>{L['updated']}: {ts} · {L['auto_refresh']}</i>")
    parts.append("")
    parts.append(f'{L["trade_on"]} <a href="{aura_url}">AURA DEX</a>')

    return "\n".join(parts)


# ---------- Telegram I/O ----------

def send_and_pin(token: str, chat_id: str, text: str, label: str = "") -> int:
    print(f"[{label}] No MESSAGE_ID -> sending + pinning new message in {chat_id}")
    res = telegram_call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    if not res.get("ok"):
        raise RuntimeError(f"sendMessage failed: {res}")
    msg_id = res["result"]["message_id"]
    print(f"[{label}] Sent message_id = {msg_id}")

    pin = telegram_call(token, "pinChatMessage", {
        "chat_id": chat_id,
        "message_id": msg_id,
        "disable_notification": "true",
    })
    if not pin.get("ok"):
        print(f"[{label}] WARNING: pinChatMessage failed: {pin}")
    else:
        print(f"[{label}] Pinned successfully.")

    print("\n=========================================")
    print(f"  COPY THIS -> {label}MESSAGE_ID = {msg_id}")
    print("=========================================\n")
    return msg_id


def edit_message(token: str, chat_id: str, message_id: int, text: str, label: str = "") -> None:
    res = telegram_call(token, "editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    if not res.get("ok"):
        desc = str(res.get("description", "")).lower()
        if "message is not modified" in desc:
            print(f"[{label}] No change. Skipping.")
            return
        raise RuntimeError(f"editMessageText failed: {res}")
    print(f"[{label}] Edited message {message_id} in {chat_id}.")


# ---------- main ----------

def parse_int(s: str) -> int:
    try:
        return int(s) if s else 0
    except ValueError:
        return 0


def read_silent_runs() -> int:
    """How many runs in a row have failed to update the pinned message."""
    try:
        with open(HEALTH_FILE, "r", encoding="utf-8") as fh:
            return max(0, int(fh.read().strip() or 0))
    except (OSError, ValueError):
        # No cache entry yet, or a corrupted one. Assume healthy: a broken
        # watchdog must never be the reason a run fails.
        return 0


def write_silent_runs(n: int) -> None:
    """Persist the counter and tell the workflow the cache is worth saving.

    The workflow only re-saves when health_changed is set, otherwise a green
    run every 15 minutes would leave ~96 cache entries a day behind."""
    try:
        with open(HEALTH_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(n))
    except OSError as e:
        print(f"WARN: could not write {HEALTH_FILE}: {e}", file=sys.stderr)
        return
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return   # running locally, not under Actions
    try:
        with open(out, "a", encoding="utf-8") as fh:
            print("health_changed=true", file=fh)
    except OSError as e:
        print(f"WARN: could not write GITHUB_OUTPUT: {e}", file=sys.stderr)


def record_silent_run(previous: int, reason: str) -> int:
    """The pinned message did not get updated this run. Returns the exit code.

    0 while the outage is still plausibly a blip, 1 once it has lasted
    MAX_SILENT_RUNS in a row, which is the point where staying quiet is worse
    than an email. The counter resets on escalation, so a long outage emails
    roughly once an hour instead of every 15 minutes."""
    n = previous + 1
    if n < MAX_SILENT_RUNS:
        print(f"::warning::Ticker silent {n}/{MAX_SILENT_RUNS} runs in a row "
              f"({reason}). Not failing the workflow yet.", file=sys.stderr)
        write_silent_runs(n)
        return 0
    print(f"::error::Ticker has not updated for {n} runs in a row, about "
          f"{n * 15} minutes. This is no longer a blip. Last reason: {reason}",
          file=sys.stderr)
    write_silent_runs(0)
    return 1


def main() -> int:
    token = os.environ.get("BOT_TOKEN", "").strip()
    aura_url = os.environ.get("AURA_DEX_URL", "https://auradex.io").strip()

    if not token:
        print("ERROR: BOT_TOKEN env var missing.", file=sys.stderr)
        return 2

    # Build list of channels from env. EN required, RO optional.
    channels = []
    en_chat = os.environ.get("CHAT_ID", "").strip()
    if en_chat:
        channels.append({
            "label": "EN",
            "chat_id": en_chat,
            "message_id": parse_int(os.environ.get("MESSAGE_ID", "").strip()),
            "lang": "en",
        })
    ro_chat = os.environ.get("CHAT_ID_RO", "").strip()
    if ro_chat:
        channels.append({
            "label": "RO",
            "chat_id": ro_chat,
            "message_id": parse_int(os.environ.get("MESSAGE_ID_RO", "").strip()),
            "lang": "ro",
        })

    if not channels:
        print("ERROR: No channels configured. Set CHAT_ID and/or CHAT_ID_RO.", file=sys.stderr)
        return 2

    silent_runs = read_silent_runs()

    # Market data is required to build the message. If it can't be fetched
    # (rate limit, upstream outage, malformed payload) there is nothing to
    # post — skip this run and let the next 15-min cron retry. Exit 0 so no
    # failure email is sent for a blip that fixes itself, but the watchdog
    # still counts it: no market data means the ticker went stale too.
    try:
        market_data = fetch_market_data()
    except Exception as e:
        print(f"::warning::Market data unavailable, skipping this run "
              f"(self-heals next cron): {e}", file=sys.stderr)
        return record_silent_run(silent_runs, f"market data unavailable: {e}")

    global_data = fetch_global()   # soft-fails to None
    fng_data = fetch_fng()         # soft-fails to None

    transient_failures = 0
    permanent_failures = 0
    for ch in channels:
        try:
            text = build_message(market_data, global_data, fng_data, aura_url, lang=ch["lang"])
            print(f"\n[{ch['label']}] Built message:\n{text}\n")
            if ch["message_id"] <= 0:
                send_and_pin(token, ch["chat_id"], text, label=f"{ch['label']}_")
            else:
                edit_message(token, ch["chat_id"], ch["message_id"], text, label=ch["label"])
        except TransientError as e:
            # Network/rate-limit/5xx after retries. Self-heals next run.
            print(f"::warning::[{ch['label']}] transient error, next cron will "
                  f"retry: {e}", file=sys.stderr)
            transient_failures += 1
        except Exception as e:
            # Permanent/config error (bad token, chat not found, message
            # deleted, no edit rights, ...). This will NOT fix itself, so
            # surface it: non-zero exit -> the owner gets a failure email.
            print(f"::error::[{ch['label']}] permanent error: {e}", file=sys.stderr)
            permanent_failures += 1

    # A permanent error already emails on its own, so the watchdog has
    # nothing to add and its counter goes back to zero.
    if permanent_failures:
        if silent_runs:
            write_silent_runs(0)
        return 1

    if transient_failures:
        print("::warning::One or more channels failed transiently; the next "
              "scheduled run will retry. Not failing the workflow.", file=sys.stderr)
        return record_silent_run(
            silent_runs,
            f"{transient_failures} of {len(channels)} channel(s) failed transiently")

    # Every channel is current again.
    if silent_runs:
        print(f"Ticker recovered after {silent_runs} silent run(s).")
        write_silent_runs(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
