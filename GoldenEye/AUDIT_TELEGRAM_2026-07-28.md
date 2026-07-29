# GoldenEye Telegram / Subscriber System Audit — 2026-07-28

Scope: everything between a trade event and a subscriber's phone — card triggers,
render correctness, send/delivery, subscriber management, registration API,
failure modes. Method: full source read of `card_renderer.py` (~1300 lines) +
every card/send/subscriber path in `goldeneye.py`, **plus** live verification:
all 9 card variants rendered through the real renderer and visually inspected,
Telegram API probed with real credentials, subscriber API attacked on port 18096.

Verdict at time of audit: the pipeline that runs (OPEN/CLOSE cards to one
channel) was solid — atomic writes, worker-thread isolation, 429 handling,
placeholder token hygiene. But two of four promised card types never fire, the
operator alarm channel was dead, and a dozen subscriber-visible content bugs
undermined the "built honest" brand promise.

Status legend: **[FIXED]** applied + verified this session · **[OPEN]** needs a
product decision — listed under Recommendations.

---

## A. Documented features that never fire

| # | Finding | Where | Status |
|---|---------|-------|--------|
| A1 | **WHALE ALERT card never sent.** `render_whale_alert` + `copy_values_whale` + full magnitude-scale visual exist; zero call sites. Whale score is computed on every entry eval (`_ws`) — the data is there. | card_renderer.py:1034; no caller in goldeneye.py | OPEN |
| A2 | **END OF DAY card never sent.** Both variants (trades + flat) fully built, including the personal note — the product's soul — and `/api/trades` even exists "for EOD card" (goldeneye.py:6610). No scheduler calls it. | card_renderer.py:1084; no caller | OPEN |

## B. Subscriber-visible content bugs

| # | Finding | Where | Status |
|---|---------|-------|--------|
| B1 | **Dead operator alarm channel.** `send_alert()` (health alerts, WR-deviation alerts, shutdown notice) reads `TELEGRAM_CHAT_ID` env = the closed fleet_intelligence chat. Every alert died silently with a WARN. | goldeneye.py:3854-3866; launch.bat | **FIXED** — launch.bat now points at the live channel; dead `TELEGRAM_CHAT_ID_2` removed |
| B2 | **Negative trade nets rendered positive.** EOD best/worst: `'-'` sign dropped (`else ''` + `abs()`) → a $0.69 loss printed "Net: $0.69". Same bug in the record-bar net line. Verified visually. | card_renderer.py:780, :477 | **FIXED** both |
| B3 | **WIN header + LOSS voice on one card.** Send path picked voice by `net >= 0`; renderer sets WIN/LOSS by `r_mult > 0 or tp_hit > 0`. Fee-eaten small winners got a WIN header, red numbers, and a mourning voice line. Verified visually. | goldeneye.py:4140 vs card_renderer.py:943 | **FIXED** — send path now uses the renderer's definition |
| B4 | **"SESSION RECORD" showed lifetime stats** (close card feeds `_live_metrics` lifetime wins/losses); EOD fed *today's* net into a hardcoded "Net all time" label. | card_renderer.py:431/479; goldeneye.py:4148 | **FIXED** — parameterized: close = TRACK RECORD / Net all time, EOD = TODAY'S RECORD / Net today |
| B5 | **Raw internal exit codes on cards.** `EXH`, `FDEC`, `AI`, `TIME`, `RECON_SL` printed verbatim as the exit reason. | goldeneye.py:4145; stream :5870 | **FIXED** — mapped to plain English (Momentum exhausted, Signal decay, AI risk exit, Time stop, …) |
| B6 | **TP1 voice claimed an open position on a closed trade** ("Letting the rest ride." on the final close card). | goldeneye.py:_VOICE_TP1 | **FIXED** — reworded past-tense, voice tone kept |
| B7 | **Stale "7 gates" claims** in `_VOICE_OPEN`, dashboard.py's copy, and the EOD flat card. Real chain: 16 gates (`_FUNNEL_GATE_ORDER`). | goldeneye.py:3908; dashboard.py:3558; card_renderer.py:1176 | **FIXED** — all say 16; `GATE_COUNT` constant added beside `SIGNAL_COUNT` |
| B8 | **Gate grid silently truncated.** `gates[:12]` dropped 4 of 16 gates and rendered a false "12 / 12" header. Verified visually. | card_renderer.py:923 | **FIXED** — cap raised to 18 (6 rows); renders "16 / 16" with all gates |
| B9 | Duplicate "Exit" row labels (price + reason) on close card. | card_renderer.py:1016 | **FIXED** — second row is "Reason" |
| B10 | Whale header diamond `◆` renders as a tofu box (Segoe UI Bold lacks the glyph). Verified visually. | card_renderer.py:1042 | **FIXED** — glyph dropped |
| B11 | **Footer collision**: right-side text overlapped the INTELLIGENCE wordmark on every card, and led with "Fleet Intelligence" — the closed chat's name. Verified visually. | card_renderer.py:720-759 | **FIXED** — channel prefix removed; footer now "27 signals · 16 gates · Kraken", clear of the wordmark |

## C. Reliability & security

| # | Finding | Where | Status |
|---|---------|-------|--------|
| C1 | **Subscriber API had zero auth.** `verify_api_key` existed since day one with no callers; any local process could silently `/unsubscribe` the only channel or rewrite `/preferences`. Malformed JSON killed the handler thread with no response. (Loopback-bound, so local-only exposure.) | goldeneye.py:4299, 4476-4519 | **FIXED** — api_key required on unsubscribe + preferences (verified live: 403/403/400/200), 400 on bad JSON, missing-field validation on register |
| C2 | **Transient network errors lost cards permanently** — only 429s were retried; a timeout/DNS blip dropped the card with a WARN. | card_renderer.py send loops | **FIXED** — one 2s-backoff retry on generic errors, both sendPhoto and sendMessage (worker thread only) |
| C3 | **No plain-text OPEN fallback.** If `card_renderer` failed to import, close paths fell back to `send_alert` but entries notified nobody. | goldeneye.py:4012 | **FIXED** — OPEN fallback mirrors the close paths |
| C4 | Latent crash: `_draw_sparkline` early-return returned an int where callers unpack `(y, draw)` — would crash the EOD card if ever called with <2 points. | card_renderer.py:530 | **FIXED** — tuple shape preserved |
| C5 | Variable shadowing: sendPhoto's 429 handler reassigned the multipart `body` to the error dict. Harmless today (request object holds the bytes) — a landmine tomorrow. | card_renderer.py:1381 | **FIXED** — renamed `err_body` |
| C6 | Live-only precision: exit-size reconcile (`:5827`) can shrink `filled_size` for P/L while the card recomputes fees/gross from the stale `pos['filled']` (`:4127`). Small distortion, live mode only. | goldeneye.py | OPEN (minor) |
| C7 | Scale note: PNG re-uploaded per subscriber (no `file_id` reuse). Irrelevant at 1 channel; matters at N. | card_renderer.py:1339 | OPEN (not needed yet) |

## D. What was verified healthy

- Card render + Telegram HTTP fully isolated on a dedicated worker thread; trade
  path only enqueues (`put_nowait`, bounded queue 50) — a Telegram outage can
  never stall SL/TP monitoring.
- `pnl` fed to close cards is net of both fee legs at every exit path (stream,
  reconciler SL/TP, force-reconcile) — card gross/fees math is consistent.
- Reconciliation closes send cards (subscribers who saw OPEN always get CLOSE).
- Token hygiene: `${TELEGRAM_BOT_TOKEN}` placeholders never resolved to disk;
  raw/resolved subscriber dicts strictly separated; corrupt-file guard blocks
  saves that would wipe last-good state.
- `_iter_telegram_targets` re-reads subscribers.json per send — config changes
  apply without restart. Locks never held across HTTP.
- 429 handling honors `retry_after` (capped 60s). Card worker heartbeats into
  `/health` thread monitoring.
- All 9 card variants render cleanly, including sub-cent prices (10-decimal
  adaptive formatting), 0-conviction, empty signals, 120-trade record bars,
  single-trade EOD sparkline, flat-day EOD.

## E. Recommendations (product decisions, not bugs)

1. **Wire the END OF DAY card** (A2) — daily accountability is the strongest
   subscriber-trust artifact this product has, and it's already built. Needs a
   ~30-line daily scheduler thread assembling from `_live_metrics['trade_log']`.
2. **Wire the WHALE ALERT card** (A1) — trigger on whale_score threshold
   crossing (e.g. ≥70) with a cooldown, from data already computed per cycle.
3. **TP1/TP2 hit notifications** — targets published on the OPEN card get
   tagged silently (goldeneye.py:5620-5643). A short text ping (not a full
   card) when a published target fills is what a subscriber trading alongside
   actually needs.
4. **Target-move notifications** — the dynamic TP ratchet (:5585-5618) can move
   published TP2/TP3 without a word. Either notify on ratchet or state on the
   OPEN card that upper targets are dynamic.
5. Startup channel self-check — one `getChat` per configured chat at boot,
   loud ERROR on failure (the dead fleet_intelligence chat sat unnoticed
   because every failure was a quiet WARN).

---
*All fixes verified by: py_compile on 3 files, 10/10 render tests re-passed,
visual inspection of fixed cards, live API attack tests on 18096 (403/403/400/
200), bot restarted healthy (21/21 threads) with fixes loaded.*
