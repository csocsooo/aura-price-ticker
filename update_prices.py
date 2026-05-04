"""
AURA DEX Telegram pinned price ticker (v2)
------------------------------------------
Fetches BTC / ETH / SOL / TRX / JUP / RAY / ORCA prices, 24h and 7d
percent changes from CoinGecko and edits a pinned Telegram channel
message via the Bot API.

Required env vars:
    BOT_TOKEN   Telegram bot token from @BotFather
    CHAT_ID     Channel ID or @username (e.g. @auradexecosystem)

Optional env vars:
    MESSAGE_ID    ID of the message to edit. If missing or 0, the
                  script SENDS + PINS a new message and prints its ID.
                  Take that ID and put it in MESSAGE_ID for future runs.
    AURA_DEX_URL  Link shown at the bottom. Default: https://auradex.io
"""

import os
import sys
import time
import json
import urllib.request
import urllib.parse
import urllib.error

# CoinGecko IDs in the display order we want.
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

COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd"
    "&ids={ids}"
    "&price_change_percentage=24h,7d"
)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def http_get_json(url: str, timeout: int = 15) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "aura-price-bot/2.0"})
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


def fetch_market_data() -> list:
    ids = ",".join(c[0] for c in COINS)
    url = COINGECKO_URL.format(ids=urllib.parse.quote(ids, safe=","))
    last_err = None
    for attempt in range(3):
        try:
            data = http_get_json(url)
            if not isinstance(data, list) or not data:
                raise RuntimeError(f"Unexpected response: {data}")
            # Build dict by id, then re-sort to our display order
            by_id = {c["id"]: c for c in data}
            ordered = []
            for cg_id, sym in COINS:
                if cg_id not in by_id:
                    raise RuntimeError(f"CoinGecko response missing id: {cg_id}")
                ordered.append((sym, by_id[cg_id]))
            return ordered
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"CoinGecko fetch failed after retries: {last_err}")


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


def build_message(market_data: list, aura_url: str) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    # Monospace table inside <pre> for clean column alignment.
    rows = []
    for sym, c in market_data:
        emoji = direction_emoji(c.get("price_change_percentage_24h"))
        price = fmt_price(c.get("current_price"))
        pct24 = fmt_pct(c.get("price_change_percentage_24h"))
        pct7d = fmt_pct(c.get("price_change_percentage_7d_in_currency"))
        # Pad symbol to 5 chars, price to 11, percentages to 8 each
        rows.append(f"{emoji} {sym:<5} {price:<11} 24h {pct24:>8}   7d {pct7d:>8}")

    table = "\n".join(rows)

    return (
        "<b>Live Crypto Prices</b>\n"
        "\n"
        f"<pre>{table}</pre>\n"
        "\n"
        f"<i>Updated: {ts} · auto-refresh 15 min</i>\n"
        "\n"
        f'Trade on <a href="{aura_url}">AURA DEX</a>'
    )


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
    text = build_message(market_data, aura_url)
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
