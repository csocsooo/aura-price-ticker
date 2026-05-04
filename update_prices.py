"""
AURA DEX Telegram pinned price ticker
-------------------------------------
Fetches BTC / ETH / SOL prices from CoinGecko and edits a pinned
Telegram channel message via the Bot API.

Required env vars:
    BOT_TOKEN   Telegram bot token from @BotFather
    CHAT_ID     Channel ID or @username (e.g. @auradexecosystem)

Optional env vars:
    MESSAGE_ID  ID of the message to edit. If missing or 0, the script
                will SEND + PIN a new message and print its ID. Take
                that ID and put it in MESSAGE_ID for future runs.
    AURA_DEX_URL  Link shown at the bottom of the message.
                  Default: https://auradex.io
"""

import os
import sys
import time
import json
import urllib.request
import urllib.parse
import urllib.error

COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=bitcoin,ethereum,solana&vs_currencies=usd"
)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

COINS = [
    ("Bitcoin",  "BTC", "bitcoin"),
    ("Ethereum", "ETH", "ethereum"),
    ("Solana",   "SOL", "solana"),
]


def http_get_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "aura-price-bot/1.0"})
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


def fetch_prices() -> dict:
    last_err = None
    for attempt in range(3):
        try:
            data = http_get_json(COINGECKO_URL)
            return {
                "BTC": data["bitcoin"]["usd"],
                "ETH": data["ethereum"]["usd"],
                "SOL": data["solana"]["usd"],
            }
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"CoinGecko fetch failed after retries: {last_err}")


def fmt_price(value: float) -> str:
    if value >= 1000:
        return f"${value:,.0f}"
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:.4f}"


def build_message(prices: dict, aura_url: str) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = [
        "<b>Live Crypto Prices</b>",
        "",
        f"BTC   {fmt_price(prices['BTC'])}",
        f"ETH   {fmt_price(prices['ETH'])}",
        f"SOL   {fmt_price(prices['SOL'])}",
        "",
        f"<i>Updated: {ts}</i>",
        "",
        f'Trade on <a href="{aura_url}">AURA DEX</a>',
    ]
    return "\n".join(lines)


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
        # Telegram returns a benign error if content didn't change. Tolerate it.
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
        print("ERROR: CHAT_ID env var missing (e.g. @auradexecosystem).", file=sys.stderr)
        return 2

    prices = fetch_prices()
    text = build_message(prices, aura_url)
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
