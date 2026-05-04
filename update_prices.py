"""
AURA DEX Telegram pinned price ticker (v3)
------------------------------------------
Fetches market data and edits a pinned Telegram channel message.

v3 features:
    - 8 coins: BTC ETH SOL TRX XAUT JUP RAY ORCA
    - 24h and 7d percent change with green/red emoji
    - 7-day sparkline mini-chart per coin (Unicode block chars)
    - Global market line: total market cap, BTC dominance, Fear & Greed
    - Top mover / worst performer (24h) callouts
    - Auto pin + edit; safe re-runs

Required env vars:
    BOT_TOKEN   Telegram bot token from @BotFather
    CHAT_ID     Channel ID or @username (e.g. @auradexecosystem)

Optional env vars:
    MESSAGE_ID    ID of the message to edit. If missing or 0, the
                  script SENDS + PINS a new message and prints its ID.
    AURA_DEX_URL  Link in the footer. Default: https://auradex.io
"""

import os
import sys
import time
import json
import urllib.request
import urllib.parse
import urllib.error

# (CoinGecko-id, display-symbol)
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

SPARK_CHARS = "▁▂▃▄▅▆▇█"
SPARK_WIDTH = 10  # chars per coin row


# ---------- HTTP ----------

def http_get_json(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": "aura-price-bot/3.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def telegram_call(token: str, method: str, payload: dict) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram {method} failed: HTTP {e.code} {body}")


def fetch_with_retry(url: str, attempts: int = 3):
    last_err = None
    for i in range(attempts):
        try:
            return http_get_json(url)
        except Exception as e:
            last_err = e
            time.sleep(2 ** i)
    raise RuntimeError(f"Fetch failed: {url} -> {last_err}")


# ---------- Data fetchers ----------

def fetch_market_data() -> list:
    ids = ",".join(c[0] for c in COINS)
    url = CG_MARKETS_URL.format(ids=urllib.parse.quote(ids, safe=","))
    data = fetch_with_retry(url)
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
    """Returns {'mcap_usd', 'mcap_change_24h_pct', 'btc_dominance'} or None on failure."""
    try:
        d = fetch_with_retry(CG_GLOBAL_URL)["data"]
        return {
            "mcap_usd": float(d["total_market_cap"]["usd"]),
            "mcap_change_24h_pct": float(d.get("market_cap_change_percentage_24h_usd", 0.0)),
            "btc_dominance": float(d["market_cap_percentage"]["btc"]),
        }
    except Exception as e:
        print(f"WARN: global fetch failed: {e}", file=sys.stderr)
        return None


def fetch_fng() -> dict:
    """Returns {'value', 'classification'} or None on failure."""
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

def build_message(market_data: list, global_data, fng_data, aura_url: str) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    # Top header line: market cap + BTC.D + sentiment
    header_bits = []
    if global_data:
        header_bits.append(
            f"Market {fmt_mcap(global_data['mcap_usd'])} "
            f"({global_data['mcap_change_24h_pct']:+.2f}%)"
        )
        header_bits.append(f"BTC.D {global_data['btc_dominance']:.1f}%")
    if fng_data:
        header_bits.append(
            f"Sentiment {fng_data['value']} "
            f"{fng_data['classification']} {fng_emoji(fng_data['classification'])}"
        )
    header_line = " · ".join(header_bits)

    # Coin table rows with sparklines
    rows = []
    for sym, c in market_data:
        emoji = direction_emoji(c.get("price_change_percentage_24h"))
        price = fmt_price(c.get("current_price"))
        pct24 = fmt_pct(c.get("price_change_percentage_24h"))
        pct7d = fmt_pct(c.get("price_change_percentage_7d_in_currency"))
        sp = make_sparkline(c.get("sparkline_in_7d", {}).get("price", []))
        # symbol pad 5, price pad 11, sparkline 10
        rows.append(f"{emoji} {sym:<5} {price:<11} {sp}  {pct24:>8} / {pct7d:>8}")
    table = "\n".join(rows)

    # Top mover / worst performer (24h)
    movers = [
        (sym, c.get("price_change_percentage_24h") or 0.0)
        for sym, c in market_data
    ]
    movers_sorted = sorted(movers, key=lambda x: x[1])
    worst_sym, worst_pct = movers_sorted[0]
    best_sym, best_pct = movers_sorted[-1]

    movers_lines = (
        f"🚀 Best 24h: <b>{best_sym}</b> {best_pct:+.2f}%\n"
        f"💀 Worst 24h: <b>{worst_sym}</b> {worst_pct:+.2f}%"
    )

    parts = ["<b>Live Crypto Prices</b>", ""]
    if header_line:
        parts.append(f"<i>{header_line}</i>")
        parts.append("")
    parts.append(f"<pre>{table}</pre>")
    parts.append("")
    parts.append(movers_lines)
    parts.append("")
    parts.append("<i>Format: 24h % / 7d % · 🟢 up · 🔴 down · ▁▂▃▄▅▆▇█ = 7d trend</i>")
    parts.append(f"<i>Updated: {ts} · auto-refresh 15 min</i>")
    parts.append("")
    parts.append(f'Trade on <a href="{aura_url}">AURA DEX</a>')

    return "\n".join(parts)


# ---------- Telegram I/O ----------

def send_and_pin(token: str, chat_id: str, text: str) -> int:
    print("MESSAGE_ID not set -> sending a new message and pinning it.")
    res = telegram_call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    if not res.get("ok"):
        raise RuntimeError(f"sendMessage failed: {res}")
    msg_id = res["result"]["message_id"]
    print(f"Sent message_id = {msg_id}")

    pin = telegram_call(token, "pinChatMessage", {
        "chat_id": chat_id,
        "message_id": msg_id,
        "disable_notification": "true",
    })
    if not pin.get("ok"):
        print(f"WARNING: pinChatMessage failed: {pin}")
    else:
        print("Pinned successfully.")

    print("\n=========================================")
    print(f"  COPY THIS -> MESSAGE_ID = {msg_id}")
    print("  Set it as a GitHub Actions secret named MESSAGE_ID.")
    print("=========================================\n")
    return msg_id


def edit_message(token: str, chat_id: str, message_id: int, text: str) -> None:
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
            print("No change in content. Skipping.")
            return
        raise RuntimeError(f"editMessageText failed: {res}")
    print(f"Edited message {message_id}.")


# ---------- main ----------

def main() -> int:
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
    message_id_raw = os.environ.get("MESSAGE_ID", "").strip()
    aura_url = os.environ.get("AURA_DEX_URL", "https://auradex.io").strip()

    if not token:
        print("ERROR: BOT_TOKEN env var missing.", file=sys.stderr)
        return 2
    if not chat_id:
        print("ERROR: CHAT_ID env var missing.", file=sys.stderr)
        return 2

    market_data = fetch_market_data()
    global_data = fetch_global()
    fng_data = fetch_fng()

    text = build_message(market_data, global_data, fng_data, aura_url)
    print("Built message:\n" + text + "\n")

    try:
        message_id = int(message_id_raw) if message_id_raw else 0
    except ValueError:
        message_id = 0

    if message_id <= 0:
        send_and_pin(token, chat_id, text)
        return 0

    edit_message(token, chat_id, message_id, text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
