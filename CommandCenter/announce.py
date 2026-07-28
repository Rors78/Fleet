#!/usr/bin/env python3
"""One-time beta access announcement to Fleet Pulse and Fleet Intelligence.

Usage:
  python announce.py           # dry run — prints formatted messages, does not send
  python announce.py --send    # actually send to Telegram

Reads Telegram credentials from signal_config.json. No fleet imports required.
Stdlib only.
"""
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Force UTF-8 for stdout so emoji and Unicode separators print on Windows cp1252 console
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CONFIG_PATH = "D:/CommandCenter/signal_config.json"

PULSE_MESSAGE = """📣 Beta Access — Free

Fleet Pulse is free during the beta testing period. No subscription, no payment, no paywall. You're getting the full signal feed while the fleet proves itself.

Thanks for testing with us.

─────────────────────────
Fleet Pulse · {timestamp}"""

INTEL_MESSAGE = """📣 Beta Access — Free

Fleet Intelligence is free during the beta testing period. You're seeing the full detail feed: trade narrators, whale context, regime shifts, and fleet health updates — all at zero cost.

This isn't a free trial. It's a measurement period. We're running the fleet honestly and proving the signal before we ask anyone to pay for it. Feedback is the only price — if you see something that works, or something that doesn't, tell us.

Thanks for being part of this.

─────────────────────────
Fleet Intelligence · {timestamp}"""


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    token = cfg.get("telegram_bot_token", "").strip()
    free_chat = cfg.get("telegram_free_chat_id", "").strip()
    paid_chat = cfg.get("telegram_paid_chat_id", "").strip()
    if not token or not free_chat or not paid_chat:
        raise RuntimeError(
            f"Missing credentials in {CONFIG_PATH}: "
            f"token={'ok' if token else 'MISSING'}, "
            f"free={'ok' if free_chat else 'MISSING'}, "
            f"paid={'ok' if paid_chat else 'MISSING'}"
        )
    return token, free_chat, paid_chat


def send_telegram(token, chat_id, text):
    """Send a plain text message to a Telegram chat. Returns (ok, response_or_error)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return data.get("ok", False), data
    except Exception as e:
        return False, str(e)


def main():
    send_mode = "--send" in sys.argv

    try:
        token, free_chat, paid_chat = load_config()
    except Exception as e:
        print(f"[announce] config error: {e}")
        return 1

    ts = datetime.now(timezone.utc).strftime("%H:%M UTC · %d %b %Y")
    pulse_text = PULSE_MESSAGE.format(timestamp=ts)
    intel_text = INTEL_MESSAGE.format(timestamp=ts)

    print("=" * 68)
    print("FLEET PULSE (free channel) — chat_id =", free_chat)
    print("=" * 68)
    print(pulse_text)
    print()
    print("=" * 68)
    print("FLEET INTELLIGENCE (paid channel) — chat_id =", paid_chat)
    print("=" * 68)
    print(intel_text)
    print()
    print("=" * 68)

    if not send_mode:
        print("DRY RUN — no messages sent.")
        print("To actually broadcast, run: python announce.py --send")
        return 0

    print("SENDING to both channels...")
    print()

    ok_pulse, resp_pulse = send_telegram(token, free_chat, pulse_text)
    print(f"Fleet Pulse:        {'✓ sent' if ok_pulse else '✗ FAILED'}")
    if not ok_pulse:
        print(f"  error: {resp_pulse}")

    ok_intel, resp_intel = send_telegram(token, paid_chat, intel_text)
    print(f"Fleet Intelligence: {'✓ sent' if ok_intel else '✗ FAILED'}")
    if not ok_intel:
        print(f"  error: {resp_intel}")

    print()
    if ok_pulse and ok_intel:
        print("Both channels delivered. Done.")
        return 0
    else:
        print("One or more sends failed. See errors above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
