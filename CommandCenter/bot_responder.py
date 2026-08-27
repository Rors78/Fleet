"""
Bot Responder — Auto-reply handler for @OracleQNTMCoreBot DMs.
================================================================

Long-polls Telegram getUpdates for private messages and replies with
info about Fleet Pulse (free) and Fleet Intelligence (paid).

Usage:
    python bot_responder.py

Reads config from signal_config.json. Stdlib + urllib only.
"""

import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "signal_config.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "bot_responder.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("bot_responder")

# ── Config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

# ── Telegram API helpers ──────────────────────────────────────────────────────
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def tg_request(token: str, method: str, payload: dict = None) -> dict:
    """Fire a Telegram Bot API request. Returns the parsed JSON response."""
    url = TELEGRAM_API.format(token=token, method=method)
    if payload:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log.error("Telegram %s HTTP %d: %s", method, e.code, body)
        return {"ok": False, "description": body}
    except Exception as e:
        log.error("Telegram %s error: %s", method, e)
        return {"ok": False, "description": str(e)}


# ── Reply messages ────────────────────────────────────────────────────────────

WELCOME_MSG = """\
Hey! Welcome to the Fleet. Let me break down what we've got for you.

<b>Fleet Pulse</b> (FREE)
Our public channel with real-time fleet signals:
  - Trade opens/closes from the bot fleet
  - Regime changes and market shifts
  - Whale alerts and emergency events
  - Delayed by a few hours vs the premium feed

Join here: @goldeneye_fleet_pulse

<b>Fleet Intelligence</b> (PREMIUM)
The full-fat feed. Everything, in real time:
  - Instant signals (no delay)
  - AI trade journals and post-mortems
  - Conviction scores and risk context
  - Fleet ensemble decisions (the "why" behind every move)
  - Regime-conditional expectancy data
  - Portfolio exposure breakdowns

This is for people who want to see exactly what a 17-bot fleet sees, as it sees it.

<b>Want Intelligence access?</b>
DM me and I'll get you sorted. No payment system yet — we're keeping it personal for now.
"""

GENERIC_MSG = """\
Hey! Looks like you sent me something — appreciate the interest.

If you're looking for fleet signals, here's the quick version:

<b>Free:</b> @goldeneye_fleet_pulse — delayed signals, regime changes, whale alerts.

<b>Premium:</b> Fleet Intelligence — real-time everything. DM me for access.

Hit /start if you want the full rundown.
"""


def get_reply(text: str) -> str:
    """Pick the right reply based on the incoming message."""
    if not text:
        return GENERIC_MSG
    cmd = text.strip().lower().split()[0]
    if cmd in ("/start", "/help", "/info"):
        return WELCOME_MSG
    return GENERIC_MSG


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(token: str):
    """Long-poll getUpdates and reply to private messages."""
    log.info("Bot responder starting. Verifying token...")

    me = tg_request(token, "getMe")
    if me.get("ok"):
        bot_info = me["result"]
        log.info("Authenticated as @%s (id=%s)",
                 bot_info.get("username", "?"), bot_info.get("id", "?"))
    else:
        log.error("getMe failed: %s — check your token", me.get("description"))
        sys.exit(1)

    offset = 0  # tracks last processed update_id
    backoff = 1
    log.info("Entering long-poll loop (timeout=30s)...")

    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset

            result = tg_request(token, "getUpdates", params)

            if not result.get("ok"):
                log.warning("getUpdates failed: %s", result.get("description"))
                time.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)
                continue

            backoff = 1  # reset on success
            updates = result.get("result", [])

            for update in updates:
                update_id = update["update_id"]
                offset = update_id + 1  # always advance past this update

                msg = update.get("message")
                if not msg:
                    continue

                chat = msg.get("chat", {})
                chat_type = chat.get("type", "")

                # Only reply to private (DM) messages — but LOG group ids
                # first: a chat id silently discarded here cost us Jeremy's
                # briefing destination on 2026-07-30 (group messages were
                # consumed and lost with no trace).
                if chat_type != "private":
                    log.info("Non-private message ignored: type=%s chat_id=%s title=%r",
                             chat_type, chat.get("id"), chat.get("title", ""))
                    continue

                chat_id = chat["id"]
                text = msg.get("text", "")
                user = msg.get("from", {})
                username = user.get("username", user.get("first_name", "unknown"))

                log.info("DM from @%s (chat_id=%s): %s",
                         username, chat_id, text[:80] if text else "(no text)")

                reply = get_reply(text)
                send_result = tg_request(token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": reply,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                })

                if send_result.get("ok"):
                    log.info("Replied to @%s", username)
                else:
                    log.error("Failed to reply to @%s: %s",
                              username, send_result.get("description"))

        except KeyboardInterrupt:
            log.info("Shutting down bot responder.")
            break
        except Exception as e:
            log.error("Poll loop error: %s", e, exc_info=True)
            time.sleep(min(backoff, 30))
            backoff = min(backoff * 2, 30)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    cfg = load_config()
    token = cfg.get("telegram_bot_token")
    if not token:
        log.error("No telegram_bot_token in %s", CONFIG_PATH)
        sys.exit(1)
    run(token)


if __name__ == "__main__":
    main()
