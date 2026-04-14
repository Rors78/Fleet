import os
import logging
import requests
from urllib.parse import quote
from flask import Flask, render_template_string, jsonify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', static_url_path='/static')

GOLDENEYE_URL = os.getenv('GOLDENEYE_URL', 'http://localhost:18095')
SUB_API_URL = os.getenv('SUB_API_URL', 'http://localhost:18096')

# =============================================================================
# Data helpers — single source of truth from bot's /analytics endpoint
# =============================================================================

def fetch_health():
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/health", timeout=5)
        return resp.json() if resp.status_code == 200 else None
    except Exception:
        return None

def fetch_telegram_messages():
    """Fetch recent Telegram messages from subscriber API."""
    try:
        resp = requests.get(f"{SUB_API_URL}/api/messages", timeout=5)
        return resp.json() if resp.status_code == 200 else []
    except Exception:
        return []

def fetch_analytics():
    """Fetch live analytics from the bot — same data the TUI reads.
    Returns data with bot_online flag to distinguish offline from zero trades."""
    # First check if bot is reachable via health endpoint
    health = fetch_health()
    bot_online = health is not None and health.get('status') != 'starting'
    
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/analytics", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            data['bot_online'] = bot_online
            return data
    except Exception:
        pass
    
    # Bot is offline — return explicit offline status, not fake zeros
    return {
        'bot_online': False,
        'error': 'Bot unreachable',
        'trades': 0, 'wins': 0, 'losses': 0, 'win_rate': 0.0,
        'avg_r': 0.0, 'max_drawdown': 0.0, 'uptime_hours': 0.0,
        'long_wins': 0, 'long_trades': 0, 'short_wins': 0, 'short_trades': 0,
        'total_signals': 0, 'total_signal_wins': 0, 'signal_win_rate': 0.0,
        'symbol_count': 0, 'per_signal': {}, 'sym_stats': [],
        'total_pnl': 0.0, 'trade_log': [],
    }

# Signal letter -> human-readable description (27 signals, v2.4)
SIG_LABELS = {
    'a': 'EMA Cross Up',       'b': 'Bullish Engulfing',
    'c': 'RSI<30 Cross',       'd': 'BB Lower Bounce',
    'e': 'EMA Pullback Buy',   'f': 'MACD Hist Rising',
    'g': 'RSI Bull Range',     'h': 'BB %B Recovery',
    'i': 'MFI Oversold Cross', 'j': 'OBV Trend Breakout',
    'k': 'BB Squeeze Up',      'l': 'Hist Bull Div',
    'm': 'MFI Bull Range',     'n': 'Volume Surge',
    'o': 'ADX Trend Str',      'p': 'Stoch Oversold',
    'q': 'Hammer Candle',      'r': 'MACD Zero Cross',
    's': 'ATR Expansion',      't': 'Chop Exit',
    'u': 'Stoch Bull Momentum','v': 'EMA200 Reclaim',
    'w': 'OBV Accumulation',   'x': 'Triple Confluence',
    'y': 'BB Mid Reclaim',     'z': 'Vol-Confirmed Trend',
    '2': 'Chop+MFI Confirm',
}

# =============================================================================
# Shared CSS / layout used by detail pages
# =============================================================================

BASE_STYLE = """
:root {
  --obsidian: #0a0a0c;
  --onyx:     #111114;
  --graphite: #1a1a1f;
  --slate:    #252530;
  --pewter:   #3a3a48;
  --ash:      #5a5a6e;
  --silver:   #8a8a9e;
  --bone:     #c8c4b8;
  --ivory:    #e8e4d8;
  --gold:     #c9a84c;
  --gold-dim: #8a7535;
  --gold-bright: #e8c55a;
  --amber:    #b8884a;
  --copper:   #9a6a3a;
  --rust:     #7a4a2a;
  --green:    #22c55e;
  --red:      #ef4444;
  --bg:       #0a0a0c;
  --bg2:      #111114;
  --bg3:      #1a1a1f;
  --border:   #252530;
  --text:     #c8c4b8;
  --muted:    #8a8a9e;
  --dim:      #5a5a6e;
  --accent:   #8a7535;
  --sym:      #c8c4b8;
  --font-display: 'Cormorant Garamond', Georgia, serif;
  --font-body: 'Outfit', system-ui, sans-serif;
  --font-mono: 'IBM Plex Mono', 'Courier New', monospace;
  --grain: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.04'/%3E%3C/svg%3E");
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--font-body); font-weight: 300; background: var(--obsidian); color: var(--bone);
       min-height: 100vh; font-size: 20px; line-height: 1.6; -webkit-font-smoothing: antialiased; }
body::after { content: ''; position: fixed; inset: 0; background: var(--grain); pointer-events: none; z-index: 50; }
::selection { background: var(--gold); color: var(--obsidian); }
a { color: var(--gold-dim); text-decoration: none; }
a:hover { color: var(--gold); }
.mono { font-family: var(--font-mono); }
.card { background: var(--onyx); border: 1px solid rgba(201,168,76,0.06); border-radius: 2px;
        padding: 20px; transition: border-color .3s; }
.card:hover { border-color: var(--gold-dim); }
.badge { display: inline-block; padding: 3px 10px; border-radius: 2px;
         font-family: var(--font-mono); font-size: .9rem; font-weight: 500; letter-spacing: .06em; }
.badge-green { background: rgba(34,197,94,.12); color: var(--green); }
.badge-red   { background: rgba(239,68,68,.12); color: var(--red); }
.badge-amber { background: rgba(184,136,74,.15); color: var(--amber); }
.badge-dim   { background: rgba(90,90,110,.15); color: var(--silver); }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
table { width: 100%; border-collapse: collapse; }
thead { position: sticky; top: 0; }
th { text-align: left; padding: 10px 14px; color: var(--ash); font-family: var(--font-mono);
     font-size: .9rem; font-weight: 400; text-transform: uppercase; letter-spacing: .12em;
     border-bottom: 1px solid rgba(201,168,76,0.06); background: var(--onyx); }
td { padding: 10px 14px; font-size: 1.12rem; }
tbody tr:hover td { background: var(--graphite); }
.container { max-width: 960px; margin: 0 auto; padding: 20px; }
.fade-in { animation: fadeIn .3s ease-in; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: translateY(0); } }
/* Utility colors */
.up { color: var(--green); } .dn { color: var(--red); }
.g { color: var(--green); } .r { color: var(--red); } .a { color: var(--amber); }
.d { color: var(--ash); } .m { color: var(--silver); } .t { color: var(--bone); }
.gld { color: var(--gold); } .sym { color: var(--bone); } .bold { font-weight: 600; }
/* Scrollbar */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: var(--obsidian); }
::-webkit-scrollbar-thumb { background: var(--pewter); border-radius: 2px; }
::-webkit-scrollbar-thumb:hover { background: var(--ash); }
* { scrollbar-width: thin; scrollbar-color: var(--pewter) var(--obsidian); }
/* Shared detail page elements */
.back-link { color: var(--silver); font-family: var(--font-mono); font-size: .9rem;
             letter-spacing: .1em; display: inline-block; margin-bottom: 12px; }
.back-link:hover { color: var(--gold); }
.sec-title { font-family: var(--font-mono); font-size: .75rem; font-weight: 400; color: var(--gold-dim);
             text-transform: uppercase; letter-spacing: .3em; margin: 24px 0 10px; }
.no-data { color: var(--ash); text-align: center; padding: 16px; font-family: var(--font-mono);
           font-size: .94rem; letter-spacing: .1em; }
.ind-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 20px; }
.ind-item { background: var(--onyx); border: 1px solid rgba(201,168,76,0.06); border-radius: 2px; padding: 10px; text-align: center; }
.ind-label { color: var(--ash); font-family: var(--font-mono); font-size: .75rem; text-transform: uppercase; letter-spacing: .12em; }
.ind-val { font-family: var(--font-mono); font-size: 1.25rem; font-weight: 500; margin-top: 2px; color: var(--ivory); }
.sum-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 20px; }
.sum-card { background: var(--onyx); border: 1px solid rgba(201,168,76,0.06); border-radius: 2px; padding: 12px; text-align: center; }
.sum-lbl { color: var(--ash); font-family: var(--font-mono); font-size: .75rem; text-transform: uppercase; letter-spacing: .12em; }
.sum-val { font-family: var(--font-mono); font-size: 1.38rem; font-weight: 500; margin-top: 4px; color: var(--ivory); }
.sig-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 20px; }
.sig-chip { padding: 4px 12px; border: 1px solid rgba(201,168,76,0.1); border-radius: 2px;
            font-family: var(--font-mono); font-size: .98rem; color: var(--gold); }
.sig-chip:hover { border-color: var(--gold-dim); background: rgba(201,168,76,.06); }
/* TP Progress bar */
.tp-track { position: relative; background: var(--obsidian); border: 1px solid var(--slate);
            border-radius: 2px; height: 48px; margin-bottom: 20px; overflow: visible; }
.tp-marker { position: absolute; top: 0; height: 100%; display: flex; align-items: center;
             justify-content: center; flex-direction: column; transform: translateX(-50%); z-index: 2; }
.tp-marker-line { width: 2px; height: 100%; position: absolute; top: 0; }
.tp-marker-label { font-family: var(--font-mono); font-size: .72rem; color: var(--ash); position: absolute; top: -16px; white-space: nowrap; letter-spacing: .08em; }
.tp-marker-val { font-family: var(--font-mono); font-size: .72rem; position: absolute; bottom: -16px; white-space: nowrap; }
.tp-fill { position: absolute; top: 0; left: 0; height: 100%; border-radius: 1px; transition: width .4s; }
.price-needle { position: absolute; top: -4px; width: 3px; height: calc(100% + 8px);
                background: var(--ivory); border-radius: 2px; transform: translateX(-50%); z-index: 5; }
.price-tag { position: absolute; top: -22px; transform: translateX(-50%); font-family: var(--font-mono);
             font-size: .8rem; font-weight: 500; padding: 1px 6px; border-radius: 2px; white-space: nowrap; z-index: 6; }
/* Levels */
.level-row { display: flex; align-items: center; gap: 12px; padding: 8px 0; border-bottom: 1px solid rgba(201,168,76,0.06); }
.level-row:last-child { border-bottom: none; }
.level-tag { min-width: 36px; font-family: var(--font-mono); font-weight: 500; font-size: .98rem; letter-spacing: .06em; }
.level-price { flex: 1; font-family: var(--font-mono); }
.level-check { font-size: 1.25rem; }
.level-note { color: var(--silver); font-family: var(--font-mono); font-size: .88rem; }
/* Factor bars */
.factor-row { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.factor-lbl { font-family: var(--font-mono); font-size: .8rem; letter-spacing: .1em;
              text-transform: uppercase; color: var(--ash); width: 90px; }
.factor-bar-bg { flex: 1; height: 4px; background: var(--slate); border-radius: 2px; overflow: hidden; }
.factor-bar-fill { height: 100%; border-radius: 2px; transition: width .4s; }
.factor-val { font-family: var(--font-mono); font-size: .9rem; color: var(--silver); width: 36px; text-align: right; }
@media (max-width: 640px) {
  .container { padding: 16px 12px; }
  td, th { padding: 8px 10px; font-size: 1.02rem; }
}
"""

FONTS = '<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Rajdhani:wght@300;400;600;700&family=JetBrains+Mono:wght@300;400;700&display=swap" rel="stylesheet">'

# =============================================================================
# AUDIT DASHBOARD — CINEMATIC COMMAND DISPLAY v3
# =============================================================================

AUDIT_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GOLDENEYE — Mission Control</title>
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;500;600;700&family=JetBrains+Mono:wght@300;400;500;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #08090d;
  --bg-deep: #05060a;
  --surface: #12141a;
  --surface-lift: #181b22;
  --text: #e8e4dd;
  --text-dim: #9a958a;
  --gold: #c9a227;
  --gold-soft: #8a7018;
  --cyan: #00e5ff;
  --cyan-soft: #0087a6;
  --red: #ff2d55;
  --red-soft: #8a0020;
  --green: #2dd4a8;
  --amber: #f59e0b;
  --dim: #3a3f4b;
  --dim-2: #262a34;
  --border: #1e222b;
  --glow-cyan: 0 0 16px rgba(0,229,255,0.35);
  --glow-gold: 0 0 12px rgba(201,162,39,0.4);
}

* { box-sizing: border-box; margin: 0; padding: 0; }

html, body {
  background: var(--bg);
  color: var(--text);
  font-family: 'Rajdhani', system-ui, sans-serif;
  font-weight: 400;
  min-height: 100vh;
  overflow: hidden;
  -webkit-font-smoothing: antialiased;
}

body::before {
  content: '';
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 1000;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.025'/%3E%3C/svg%3E");
  mix-blend-mode: overlay;
}

.mono { font-family: 'JetBrains Mono', 'Consolas', monospace; font-feature-settings: "tnum"; }
.up { color: var(--gold); }
.dn { color: var(--red); }
.cyan { color: var(--cyan); }
.dim { color: var(--text-dim); }
.green { color: var(--green); }
.amber { color: var(--amber); }
.gold { color: var(--gold); }
.hidden { display: none !important; }

/* ========== PAGE LAYOUT ========== */

.app {
  display: grid;
  grid-template-rows: 48px 1fr;
  height: 100vh;
  width: 100vw;
}

.main {
  display: grid;
  grid-template-columns: 1.1fr 1.1fr 1.1fr 1.2fr;
  grid-template-rows: minmax(0, 1.4fr) minmax(0, 1fr) 160px;
  grid-template-areas:
    "equity  equity  equity  positions"
    "aegis   signals funnel  symbols"
    "log     log     log     log";
  gap: 10px;
  padding: 10px;
  min-height: 0;
}

.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  position: relative;
  overflow: hidden;
  min-height: 0;
  min-width: 0;
}

.panel-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px 8px;
  border-bottom: 1px solid var(--border);
}

.panel-head::before {
  content: '';
  width: 3px;
  height: 13px;
  background: var(--gold);
  box-shadow: var(--glow-gold);
}

.panel-head h2 {
  font-family: 'Rajdhani', sans-serif;
  font-weight: 600;
  font-size: 12px;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--text);
}

.panel-head .badge {
  margin-left: auto;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  letter-spacing: 0.1em;
  color: var(--text-dim);
  padding: 2px 8px;
  border: 1px solid var(--dim);
}

.panel-body {
  padding: 12px 14px;
  height: calc(100% - 34px);
  min-height: 0;
  overflow: hidden;
}

.p-equity   { grid-area: equity; }
.p-pos      { grid-area: positions; }
.p-aegis    { grid-area: aegis; }
.p-signals  { grid-area: signals; }
.p-funnel   { grid-area: funnel; }
.p-symbols  { grid-area: symbols; }
.p-log      { grid-area: log; }

/* ========== TOP BAR ========== */

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 18px;
  background: var(--bg-deep);
  border-bottom: 1px solid var(--border);
  position: relative;
}

.topbar::after {
  content: '';
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--gold-soft) 20%, var(--gold-soft) 80%, transparent);
  opacity: 0.4;
}

.brand {
  font-family: 'Rajdhani', sans-serif;
  font-weight: 700;
  font-size: 20px;
  color: var(--gold);
  letter-spacing: 0.3em;
  text-shadow: 0 0 14px rgba(201,162,39,0.35);
}

.tb-right {
  display: flex;
  align-items: center;
  gap: 18px;
  font-size: 11px;
}

.tb-group {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border: 1px solid var(--border);
  background: var(--surface);
}

.tb-label {
  font-family: 'Rajdhani', sans-serif;
  font-weight: 500;
  font-size: 9px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--text-dim);
}

.tb-value {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  font-weight: 500;
  color: var(--text);
}

.tb-value.big {
  font-size: 15px;
  color: var(--gold);
  font-weight: 700;
  text-shadow: 0 0 8px rgba(201,162,39,0.3);
  transition: text-shadow 0.6s;
}

.tb-value.big.pulse {
  text-shadow: 0 0 18px rgba(201,162,39,0.8);
}

.tb-value.clock {
  color: var(--cyan);
  text-shadow: 0 0 6px rgba(0,229,255,0.25);
}

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--dim);
  display: inline-block;
}
.dot.ok    { background: var(--green); box-shadow: 0 0 6px rgba(45,212,168,0.6); }
.dot.warn  { background: var(--amber); box-shadow: 0 0 6px rgba(245,158,11,0.6); }
.dot.err   { background: var(--red);   box-shadow: 0 0 6px rgba(255,45,85,0.6); }

.thr-bar {
  display: inline-flex;
  gap: 1px;
  height: 10px;
  align-items: center;
}
.thr-seg {
  width: 2px;
  height: 100%;
  background: var(--dim);
}
.thr-seg.on { background: var(--green); box-shadow: 0 0 4px rgba(45,212,168,0.5); }
.thr-seg.off { background: var(--red-soft); }

.mode-live  { color: var(--green); }
.mode-paper { color: var(--amber); }
.mode-off   { color: var(--red); }

/* ========== EQUITY HERO ========== */

.p-equity .panel-body { padding: 0; position: relative; }
#equityCanvas {
  display: block;
  width: 100%;
  height: 100%;
}

.p-equity .panel-body::before {
  content: '';
  position: absolute;
  inset: 0;
  background: radial-gradient(ellipse at center, rgba(0,229,255,0.04), transparent 70%);
  pointer-events: none;
}

#equityTooltip {
  position: absolute;
  background: var(--bg-deep);
  border: 1px solid var(--cyan-soft);
  padding: 6px 10px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--text);
  pointer-events: none;
  z-index: 20;
  display: none;
  white-space: nowrap;
  box-shadow: 0 0 20px rgba(0,229,255,0.15);
}
#equityTooltip .tt-time { color: var(--text-dim); font-size: 10px; margin-bottom: 2px; }
#equityTooltip .tt-bal  { color: var(--gold); font-size: 13px; font-weight: 700; }
#equityTooltip .tt-trade { color: var(--cyan); font-size: 10px; margin-top: 3px; }

/* ========== POSITIONS PANEL ========== */

.p-pos .panel-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.pos-card {
  background: var(--surface-lift);
  border: 1px solid var(--border);
  padding: 10px 12px;
  border-left: 3px solid var(--cyan);
}
.pos-card.short { border-left-color: var(--amber); }
.pos-card.win { border-left-color: var(--gold); }
.pos-card.loss { border-left-color: var(--red); }

.pos-card-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 6px;
}
.pos-sym {
  font-family: 'Rajdhani', sans-serif;
  font-weight: 700;
  font-size: 18px;
  letter-spacing: 0.04em;
  color: var(--text);
}
.pos-dir {
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  letter-spacing: 0.14em;
  padding: 1px 6px;
  border: 1px solid var(--cyan);
  color: var(--cyan);
  text-transform: uppercase;
}
.pos-dir.short { color: var(--amber); border-color: var(--amber); }

.pos-age {
  margin-left: auto;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--text-dim);
}

.pos-row {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 6px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  margin-bottom: 8px;
}
.pos-row .pc-lbl {
  font-family: 'Rajdhani', sans-serif;
  font-size: 9px;
  letter-spacing: 0.12em;
  color: var(--text-dim);
  text-transform: uppercase;
}
.pos-row .pc-val {
  font-size: 12px;
  color: var(--text);
}

.pos-sltp {
  position: relative;
  height: 18px;
  background: var(--bg-deep);
  border: 1px solid var(--border);
}
.pos-sltp-fill {
  position: absolute;
  top: 0;
  left: 0;
  height: 100%;
  background: linear-gradient(90deg, rgba(255,45,85,0.22) 0%, rgba(255,45,85,0.1) 30%, rgba(45,212,168,0.1) 70%, rgba(45,212,168,0.22) 100%);
}
.pos-sltp-marker {
  position: absolute;
  top: -2px;
  bottom: -2px;
  width: 2px;
  background: var(--cyan);
  box-shadow: 0 0 6px rgba(0,229,255,0.7);
}
.pos-sltp-label {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  color: var(--text-dim);
  padding: 0 4px;
}
.pos-sltp-label.sl { left: 2px; }
.pos-sltp-label.tp { right: 2px; }

/* Empty state + nearest entry */

.pos-empty {
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  height: 100%;
  gap: 14px;
  text-align: center;
}
.pos-empty-msg {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  letter-spacing: 0.25em;
  color: var(--dim);
  text-transform: uppercase;
}
.pos-nearest {
  background: var(--bg-deep);
  border: 1px solid var(--border);
  border-left: 2px solid var(--cyan-soft);
  padding: 10px 14px;
  width: calc(100% - 24px);
  max-width: 360px;
}
.pos-nearest .pn-label {
  font-family: 'Rajdhani', sans-serif;
  font-size: 9px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--text-dim);
  margin-bottom: 4px;
}
.pos-nearest .pn-sym {
  font-family: 'Rajdhani', sans-serif;
  font-weight: 700;
  font-size: 16px;
  color: var(--cyan);
  letter-spacing: 0.04em;
}
.pos-nearest .pn-depth {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--text);
  margin-top: 3px;
}
.pos-nearest .pn-block {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--amber);
  margin-top: 2px;
}

/* ========== AEGIS + ANALYTICS ========== */

.aegis-wrap {
  display: grid;
  grid-template-rows: auto 1fr;
  gap: 10px;
  height: 100%;
}

.aegis-gauge {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding-top: 2px;
}

.aegis-canvas {
  width: 110px;
  height: 110px;
  display: block;
}

.aegis-score-val {
  position: absolute;
  top: 48px;
  left: 0;
  right: 0;
  text-align: center;
  font-family: 'JetBrains Mono', monospace;
  font-weight: 700;
  font-size: 28px;
  color: var(--gold);
  text-shadow: 0 0 10px rgba(201,162,39,0.4);
  pointer-events: none;
}

.aegis-label {
  position: absolute;
  top: 82px;
  left: 0;
  right: 0;
  text-align: center;
  font-family: 'Rajdhani', sans-serif;
  font-size: 9px;
  letter-spacing: 0.2em;
  color: var(--text-dim);
  text-transform: uppercase;
  pointer-events: none;
}

.aegis-sub {
  font-family: 'Rajdhani', sans-serif;
  font-size: 8px;
  letter-spacing: 0.2em;
  color: var(--dim);
  text-transform: uppercase;
  text-align: center;
  margin-top: -3px;
}

.aa-stats {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 14px;
  padding: 6px 4px 0;
  font-family: 'JetBrains Mono', monospace;
  align-content: start;
}
.aa-stats .stat-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  border-bottom: 1px dotted var(--dim-2);
  padding-bottom: 2px;
}
.aa-stats .stat-lbl {
  font-family: 'Rajdhani', sans-serif;
  font-size: 9px;
  letter-spacing: 0.14em;
  color: var(--text-dim);
  text-transform: uppercase;
}
.aa-stats .stat-val {
  font-size: 12px;
  color: var(--text);
  font-weight: 500;
}

.cb-line {
  grid-column: 1 / -1;
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px solid var(--border);
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
}
.cb-line .cb-lbl {
  font-family: 'Rajdhani', sans-serif;
  font-size: 9px;
  letter-spacing: 0.14em;
  color: var(--text-dim);
  text-transform: uppercase;
}
.cb-line .cb-val.clear { color: var(--green); }
.cb-line .cb-val.tripped { color: var(--red); }

/* ========== SIGNAL INTELLIGENCE ========== */

.si-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  height: 100%;
}

.si-row {
  background: var(--surface-lift);
  border: 1px solid var(--border);
  border-left: 2px solid var(--cyan-soft);
  padding: 6px 10px;
}
.si-row.hot {
  border-left-color: var(--gold);
}

.si-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 3px;
}
.si-sym {
  font-family: 'Rajdhani', sans-serif;
  font-weight: 700;
  font-size: 13px;
  color: var(--text);
  letter-spacing: 0.03em;
}
.si-dir {
  font-family: 'JetBrains Mono', monospace;
  font-size: 8px;
  letter-spacing: 0.14em;
  padding: 0 5px;
  color: var(--cyan);
  border: 1px solid var(--cyan-soft);
}
.si-dir.short { color: var(--amber); border-color: var(--amber); }
.si-meta {
  margin-left: auto;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  color: var(--text-dim);
}
.si-meta b { color: var(--text); font-weight: 500; }

.si-gates {
  display: flex;
  flex-wrap: wrap;
  gap: 3px 6px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  line-height: 1.4;
}
.si-g {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}
.si-g .mark {
  font-size: 10px;
  line-height: 1;
}
.si-g.ok { color: var(--green); }
.si-g.ok .mark { color: var(--green); }
.si-g.fail { color: var(--red); }
.si-g.fail .mark { color: var(--red); }
.si-g.dim { color: var(--dim); }

.si-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--dim);
  letter-spacing: 0.2em;
  text-transform: uppercase;
}

/* ========== GATE FUNNEL ========== */

.funnel-wrap {
  display: flex;
  flex-direction: column;
  gap: 4px;
  height: 100%;
  overflow: hidden;
}

.funnel-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--text-dim);
  margin-bottom: 2px;
}
.funnel-head b { color: var(--text); font-weight: 500; }

.funnel-row {
  display: grid;
  grid-template-columns: 68px 1fr 34px;
  align-items: center;
  gap: 6px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  line-height: 1;
  height: 14px;
}
.funnel-row.bottleneck .fn-bar {
  box-shadow: 0 0 8px rgba(255,45,85,0.4);
}
.funnel-row.bottleneck .fn-name { color: var(--red); }
.funnel-row.dim-row .fn-name { color: var(--dim); }
.funnel-row.dim-row .fn-bar { background: var(--dim-2); }

.fn-name {
  font-family: 'Rajdhani', sans-serif;
  font-size: 10px;
  letter-spacing: 0.1em;
  color: var(--text-dim);
  text-transform: uppercase;
  text-align: right;
}
.fn-bar-track {
  position: relative;
  height: 10px;
  background: var(--bg-deep);
  border: 1px solid var(--dim-2);
}
.fn-bar {
  height: 100%;
  background: linear-gradient(90deg, var(--cyan-soft), var(--cyan));
  transition: width 0.5s ease-out;
}
.funnel-row:last-child .fn-bar {
  background: linear-gradient(90deg, var(--gold-soft), var(--gold));
}
.fn-count {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--text);
  text-align: right;
}
.fn-count.dim { color: var(--dim); }

/* ========== PLACEHOLDER / UTILITY ========== */

.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--dim);
  letter-spacing: 0.2em;
  text-transform: uppercase;
}

/* ========== SYMBOL GRID ========== */

/* 2 columns x 10 rows = 20 cells. Single-line horizontal layout per cell:
   TICKER  PRICE  [BULL]  $PNL  — all on ONE line. This guarantees legibility
   at any panel width because the row has ~200px to work with instead of ~90px.
   Previous 4-column layouts clipped 3-char tickers when the panel came out
   narrower than estimated; 2-col is the conservative math-guaranteed layout. */
.sym-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  grid-auto-rows: 1fr;
  gap: 2px;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}

.sym-cell {
  background: var(--surface-lift);
  border: 1px solid var(--border);
  padding: 4px 8px;
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 8px;
  min-height: 0;
  min-width: 0;
  position: relative;
  overflow: hidden;
  transition: border-color 0.3s, box-shadow 0.3s;
}

.sym-cell.active {
  border-color: var(--cyan-soft);
  box-shadow: 0 0 6px rgba(0,229,255,0.25);
}
.sym-cell.pos-long {
  border-color: var(--gold);
  box-shadow: 0 0 8px rgba(201,162,39,0.25);
}
.sym-cell.pos-short {
  border-color: var(--amber);
  box-shadow: 0 0 8px rgba(245,158,11,0.25);
}

.sym-name {
  font-family: 'Rajdhani', sans-serif;
  font-weight: 700;
  font-size: 13px;
  letter-spacing: 0.02em;
  color: var(--text);
  line-height: 1;
  white-space: nowrap;
  flex-shrink: 0;
  /* Large enough for FARTCOIN (8 chars) at 13px Rajdhani ~= 62px */
}

.sym-price {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  font-weight: 500;
  color: var(--text);
  line-height: 1;
  white-space: nowrap;
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sym-price.up { color: var(--green); }
.sym-price.dn { color: var(--red); }

.sym-rgm {
  font-family: 'JetBrains Mono', monospace;
  font-size: 8px;
  font-weight: 600;
  letter-spacing: 0.08em;
  line-height: 1;
  padding: 2px 4px;
  border: 1px solid var(--dim);
  text-transform: uppercase;
  flex-shrink: 0;
}
.sym-rgm.bull { color: var(--green); border-color: rgba(45,212,168,0.35); }
.sym-rgm.bear { color: var(--red); border-color: rgba(255,45,85,0.35); }
.sym-rgm.range { color: var(--amber); border-color: rgba(245,158,11,0.35); }
.sym-rgm.chop { color: var(--text-dim); border-color: var(--dim); }

.sym-pnl {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  font-weight: 600;
  line-height: 1;
  flex-shrink: 0;
  white-space: nowrap;
}
.sym-pnl.up { color: var(--gold); }
.sym-pnl.dn { color: var(--red); }
.sym-pnl.zero { color: var(--dim); }

/* ========== LIVE LOG ========== */

/* Panel is 160px tall. Minus 34px head + 12px padding = ~114px body.
   At 18px per line we fit 6 lines comfortably. JS caps visible lines to 6. */

.p-log .panel-body {
  padding: 8px 14px;
}

.log-feed {
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  font-family: 'JetBrains Mono', 'Consolas', monospace;
  font-size: 11px;
  line-height: 18px;
}
.log-feed .log-line {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text-dim);
  height: 18px;
}
.log-feed .log-ts {
  color: var(--dim);
  margin-right: 6px;
}
.log-tag {
  display: inline-block;
  font-size: 9px;
  letter-spacing: 0.08em;
  padding: 0 5px;
  margin-right: 6px;
  border: 1px solid var(--dim);
  color: var(--text-dim);
  line-height: 14px;
  vertical-align: 1px;
}
.log-line.sig   .log-tag { color: var(--cyan-soft); border-color: rgba(0,135,166,0.5); }
.log-line.blk   .log-tag { color: var(--red-soft); border-color: rgba(138,0,32,0.6); }
.log-line.shadow .log-tag { color: var(--dim); }
.log-line.win   .log-tag { color: var(--gold); border-color: var(--gold-soft); }
.log-line.loss  .log-tag { color: var(--red); border-color: rgba(255,45,85,0.4); }
.log-line.open  .log-tag { color: var(--cyan); border-color: var(--cyan-soft); }
.log-line.err   .log-tag { color: var(--red); border-color: var(--red); background: rgba(255,45,85,0.1); }

.log-line.win   { color: var(--gold); }
.log-line.loss  { color: var(--red); }
.log-line.open  { color: var(--cyan); }
.log-line.err   { color: var(--red); }
.log-line.blk   { color: var(--text-dim); }
.log-line.shadow { color: var(--dim); }

.log-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--dim);
  letter-spacing: 0.2em;
  text-transform: uppercase;
}

@media (max-width: 1600px) {
  .brand { font-size: 17px; letter-spacing: 0.25em; }
  .tb-group { padding: 2px 8px; }
  .tb-value.big { font-size: 13px; }
}

</style>
</head>
<body>
<div class="app">

  <!-- ========== TOP BAR ========== -->
  <header class="topbar">
    <div class="brand">GOLDENEYE</div>
    <div class="tb-right">
      <div class="tb-group">
        <span class="dot" id="tb-kraken-dot"></span>
        <span class="tb-label">KRAKEN</span>
        <span class="tb-value" id="tb-kraken-st">—</span>
      </div>
      <div class="tb-group">
        <span class="tb-label">BALANCE</span>
        <span class="tb-value big" id="tb-balance">$0.00</span>
      </div>
      <div class="tb-group">
        <span class="tb-label">TRADE</span>
        <span class="tb-value mono" id="tb-trade-amt">$0.00</span>
      </div>
      <div class="tb-group">
        <span class="tb-label">POS</span>
        <span class="tb-value mono" id="tb-pos">0/0</span>
      </div>
      <div class="tb-group">
        <span class="tb-label">THREADS</span>
        <span class="thr-bar" id="tb-thr-bar"></span>
        <span class="tb-value mono" id="tb-thr-txt">0/0</span>
      </div>
      <div class="tb-group">
        <span class="tb-label">MODE</span>
        <span class="tb-value" id="tb-mode">—</span>
      </div>
      <div class="tb-group">
        <span class="tb-label">UP</span>
        <span class="tb-value mono" id="tb-uptime">—</span>
      </div>
      <div class="tb-group">
        <span class="tb-label">CPU</span>
        <span class="tb-value mono" id="tb-cpu">—</span>
      </div>
      <div class="tb-group">
        <span class="tb-label">MEM</span>
        <span class="tb-value mono" id="tb-mem">—</span>
      </div>
      <div class="tb-group">
        <span class="tb-label">UTC</span>
        <span class="tb-value mono clock" id="tb-clock">—</span>
      </div>
    </div>
  </header>

  <!-- ========== MAIN GRID ========== -->
  <main class="main">

    <!-- EQUITY CURVE HERO -->
    <section class="panel p-equity">
      <div class="panel-head">
        <h2>Equity Curve</h2>
        <span class="badge" id="eq-badge">— LIFETIME</span>
      </div>
      <div class="panel-body">
        <canvas id="equityCanvas"></canvas>
        <div id="equityTooltip"></div>
      </div>
    </section>

    <!-- POSITIONS -->
    <section class="panel p-pos">
      <div class="panel-head">
        <h2>Positions</h2>
        <span class="badge" id="pos-badge">0 / 20</span>
      </div>
      <div class="panel-body" id="pos-body">
        <div class="pos-empty">
          <div class="pos-empty-msg">No open positions</div>
        </div>
      </div>
    </section>

    <!-- AEGIS + ANALYTICS -->
    <section class="panel p-aegis">
      <div class="panel-head">
        <h2>Conviction · Analytics</h2>
      </div>
      <div class="panel-body">
        <div class="aegis-wrap">
          <div class="aegis-gauge">
            <canvas class="aegis-canvas" id="aegisCanvas" width="220" height="220"></canvas>
            <div class="aegis-score-val" id="aegisScore">—</div>
            <div class="aegis-label" id="aegisLabel">AWAITING</div>
            <div class="aegis-sub">Fleet Conviction</div>
          </div>
          <div class="aa-stats">
            <div class="stat-row"><span class="stat-lbl">Trades</span><span class="stat-val" id="aa-trades">—</span></div>
            <div class="stat-row"><span class="stat-lbl">WR</span><span class="stat-val" id="aa-wr">—</span></div>
            <div class="stat-row"><span class="stat-lbl">Avg R</span><span class="stat-val" id="aa-r">—</span></div>
            <div class="stat-row"><span class="stat-lbl">P/L</span><span class="stat-val" id="aa-pnl">—</span></div>
            <div class="stat-row"><span class="stat-lbl">Max DD</span><span class="stat-val" id="aa-dd">—</span></div>
            <div class="stat-row"><span class="stat-lbl">Heat</span><span class="stat-val" id="aa-heat">—</span></div>
            <div class="cb-line">
              <span class="cb-lbl">Circuit Breaker</span>
              <span class="cb-val clear" id="aa-cb">CLEAR</span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- SIGNAL INTELLIGENCE -->
    <section class="panel p-signals">
      <div class="panel-head">
        <h2>Signal Intelligence</h2>
        <span class="badge" id="si-badge">TOP 5</span>
      </div>
      <div class="panel-body">
        <div class="si-list" id="si-list">
          <div class="si-empty">Awaiting signal activity</div>
        </div>
      </div>
    </section>

    <!-- GATE FUNNEL -->
    <section class="panel p-funnel">
      <div class="panel-head">
        <h2>Gate Funnel</h2>
        <span class="badge">60s WINDOW</span>
      </div>
      <div class="panel-body">
        <div class="funnel-wrap" id="fn-wrap">
          <div class="funnel-head">
            <span>Records: <b id="fn-records">0</b></span>
            <span>Entered: <b id="fn-entered">0</b></span>
          </div>
          <div id="fn-rows"></div>
        </div>
      </div>
    </section>

    <!-- SYMBOL GRID -->
    <section class="panel p-symbols">
      <div class="panel-head">
        <h2>Symbol Grid</h2>
        <span class="badge" id="sym-badge">20</span>
      </div>
      <div class="panel-body">
        <div class="sym-grid" id="sym-grid">
          <div class="placeholder" style="grid-column:1/-1">loading</div>
        </div>
      </div>
    </section>

    <!-- LIVE LOG -->
    <section class="panel p-log">
      <div class="panel-head">
        <h2>Live Log</h2>
        <span class="badge" id="log-badge">—</span>
      </div>
      <div class="panel-body">
        <div class="log-feed" id="log-feed">
          <div class="log-empty">Awaiting events</div>
        </div>
      </div>
    </section>

  </main>
</div>

<script>
// ============================================================================
// GOLDENEYE MISSION CONTROL — Phase 2b
// ============================================================================

const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

// ---- utility formatters -----------------------------------------------------

function fmtUSD(v, digits) {
  if (v == null || isNaN(v)) return '—';
  const d = digits == null ? 2 : digits;
  return '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
}

function fmtUptime(s) {
  if (!s || s < 0) return '—';
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h >= 24) {
    const d = Math.floor(h / 24);
    return d + 'd ' + (h % 24) + 'h';
  }
  return h + 'h ' + m + 'm';
}

function fmtAge(seconds) {
  if (!seconds || seconds < 0) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h >= 1) return h + 'h ' + m + 'm';
  return m + 'm';
}

function fmtClock() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return p(d.getUTCHours()) + ':' + p(d.getUTCMinutes()) + ':' + p(d.getUTCSeconds());
}

function fmtTimeShort(ts) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return p(d.getUTCHours()) + ':' + p(d.getUTCMinutes()) + ':' + p(d.getUTCSeconds());
}

function fmtTimeLabel(ts, spanSec) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  if (spanSec < 7200) return p(d.getUTCHours()) + ':' + p(d.getUTCMinutes());
  if (spanSec < 86400) return p(d.getUTCHours()) + 'h';
  return (d.getUTCMonth() + 1) + '/' + d.getUTCDate();
}

// ---- clock tick (independent of data poll) ---------------------------------

setInterval(() => { $('tb-clock').textContent = fmtClock(); }, 1000);
$('tb-clock').textContent = fmtClock();

// ---- top bar renderer ------------------------------------------------------

let _lastBal = null;
function renderTopBar(h) {
  if (!h) {
    $('tb-kraken-dot').className = 'dot err';
    $('tb-kraken-st').textContent = 'OFFLINE';
    return;
  }
  const kOk = h.kraken_api === 'connected';
  $('tb-kraken-dot').className = 'dot ' + (kOk ? 'ok' : 'err');
  $('tb-kraken-st').textContent = kOk ? 'CONNECTED' : 'ERROR';

  const bal = h.live_balance != null ? h.live_balance : (h.paper_balance != null ? h.paper_balance : 0);
  $('tb-balance').textContent = fmtUSD(bal);
  if (_lastBal != null && Math.abs(bal - _lastBal) > 0.001) {
    const el = $('tb-balance');
    el.classList.add('pulse');
    setTimeout(() => el.classList.remove('pulse'), 700);
  }
  _lastBal = bal;

  $('tb-trade-amt').textContent = fmtUSD(h.live_trade_amt != null ? h.live_trade_amt : 0);
  $('tb-pos').textContent = (h.open_positions || 0) + '/' + (h.max_positions || 0);
  $('pos-badge').textContent = (h.open_positions || 0) + ' / ' + (h.max_positions || 0);

  const tt = h.total_threads || 0;
  const th = h.threads_healthy || 0;
  $('tb-thr-txt').textContent = th + '/' + tt;
  const bar = $('tb-thr-bar');
  bar.innerHTML = '';
  for (let i = 0; i < tt; i++) {
    const s = document.createElement('span');
    s.className = 'thr-seg ' + (i < th ? 'on' : 'off');
    bar.appendChild(s);
  }

  const mode = (h.trading_mode || '').toUpperCase() || '—';
  const modeEl = $('tb-mode');
  modeEl.textContent = mode;
  modeEl.className = 'tb-value ' + (mode === 'LIVE' ? 'mode-live' : mode === 'PAPER' ? 'mode-paper' : 'mode-off');

  $('tb-uptime').textContent = fmtUptime(h.uptime_seconds);
  $('tb-cpu').textContent = (h.cpu_percent != null ? h.cpu_percent.toFixed(0) : '—') + '%';
  $('tb-mem').textContent = (h.memory_percent != null ? h.memory_percent.toFixed(0) : '—') + '%';

  $('sym-badge').textContent = h.active_symbols || 0;
}

// ============================================================================
// EQUITY CURVE HERO (unchanged from Phase 2a)
// ============================================================================

const EquityChart = (() => {
  const canvas = $('equityCanvas');
  const ctx = canvas.getContext('2d');
  const tooltip = $('equityTooltip');

  let series = [];
  let markers = [];
  let dpr = window.devicePixelRatio || 1;
  let W = 0, H = 0;
  let plot = null;
  let animProgress = 1;
  let animStart = 0;
  let initialDrawDone = false;
  let hoverX = null;

  function resize() {
    dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    W = Math.max(1, rect.width);
    H = Math.max(1, rect.height);
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function setData(newSeries, newMarkers) {
    series = newSeries || [];
    markers = newMarkers || [];
    if (!initialDrawDone && series.length >= 2) {
      initialDrawDone = true;
      animStart = performance.now();
      animProgress = 0;
      requestAnimationFrame(animLoop);
    }
    draw();
  }

  function animLoop(t) {
    const elapsed = t - animStart;
    animProgress = Math.min(1, elapsed / 1500);
    draw();
    if (animProgress < 1) requestAnimationFrame(animLoop);
  }

  function computePlot() {
    if (series.length < 1) { plot = null; return; }
    const padL = 56, padR = 14, padT = 14, padB = 22;
    const x0 = padL, y0 = padT, x1 = W - padR, y1 = H - padB;
    const ts = series.map(s => s.ts);
    const minT = ts[0];
    const maxT = ts[ts.length - 1];
    const vals = [];
    series.forEach(s => {
      vals.push(s.bal + (s.upnl || 0));
      if (s.hwm) vals.push(s.hwm);
    });
    let minY = Math.min(...vals);
    let maxY = Math.max(...vals);
    if (minY === maxY) { minY -= 1; maxY += 1; }
    const pad = (maxY - minY) * 0.1;
    minY -= pad;
    maxY += pad;
    plot = { x0, y0, x1, y1, minT, maxT, minY, maxY };
  }

  function tsToX(ts) {
    if (!plot || plot.maxT === plot.minT) return plot.x0;
    return plot.x0 + (ts - plot.minT) / (plot.maxT - plot.minT) * (plot.x1 - plot.x0);
  }

  function valToY(v) {
    if (!plot || plot.maxY === plot.minY) return plot.y1;
    return plot.y1 - (v - plot.minY) / (plot.maxY - plot.minY) * (plot.y1 - plot.y0);
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    if (series.length < 2) {
      ctx.fillStyle = '#3a3f4b';
      ctx.font = '11px JetBrains Mono, monospace';
      ctx.textAlign = 'center';
      ctx.fillText('WAITING FOR EQUITY DATA…', W / 2, H / 2);
      return;
    }
    computePlot();
    if (!plot) return;

    ctx.strokeStyle = 'rgba(58,63,75,0.25)';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = plot.y0 + (plot.y1 - plot.y0) * (i / 4);
      ctx.beginPath();
      ctx.moveTo(plot.x0, y);
      ctx.lineTo(plot.x1, y);
      ctx.stroke();
    }

    ctx.fillStyle = '#5a5f6b';
    ctx.font = '10px JetBrains Mono, monospace';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let i = 0; i <= 4; i++) {
      const frac = i / 4;
      const y = plot.y0 + (plot.y1 - plot.y0) * frac;
      const val = plot.maxY - (plot.maxY - plot.minY) * frac;
      ctx.fillText('$' + val.toFixed(2), plot.x0 - 6, y);
    }

    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    const spanSec = plot.maxT - plot.minT;
    const xTicks = 5;
    for (let i = 0; i <= xTicks; i++) {
      const frac = i / xTicks;
      const x = plot.x0 + (plot.x1 - plot.x0) * frac;
      const ts = plot.minT + spanSec * frac;
      ctx.fillText(fmtTimeLabel(ts, spanSec), x, plot.y1 + 6);
    }

    const pts = series.map(s => ({
      x: tsToX(s.ts),
      y: valToY(s.bal + (s.upnl || 0)),
      hwmY: s.hwm ? valToY(s.hwm) : null,
    }));

    if (series.some(s => s.hwm)) {
      ctx.strokeStyle = 'rgba(201,162,39,0.35)';
      ctx.setLineDash([4, 4]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      pts.forEach((p, i) => {
        if (p.hwmY == null) return;
        if (i === 0) ctx.moveTo(p.x, p.hwmY);
        else ctx.lineTo(p.x, p.hwmY);
      });
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.fillStyle = 'rgba(255,45,85,0.06)';
      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].hwmY || pts[0].y);
      for (let i = 0; i < pts.length; i++) {
        const p = pts[i];
        ctx.lineTo(p.x, p.hwmY != null ? p.hwmY : p.y);
      }
      for (let i = pts.length - 1; i >= 0; i--) {
        ctx.lineTo(pts[i].x, pts[i].y);
      }
      ctx.closePath();
      ctx.fill();
    }

    const visCount = Math.max(2, Math.floor(pts.length * animProgress));
    const visPts = pts.slice(0, visCount);

    const grad = ctx.createLinearGradient(0, plot.y0, 0, plot.y1);
    grad.addColorStop(0, 'rgba(0,229,255,0.18)');
    grad.addColorStop(0.6, 'rgba(0,229,255,0.05)');
    grad.addColorStop(1, 'rgba(0,229,255,0.0)');
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.moveTo(visPts[0].x, plot.y1);
    visPts.forEach((p, i) => {
      if (i === 0) ctx.lineTo(p.x, p.y);
      else {
        const prev = visPts[i - 1];
        const cx = (prev.x + p.x) / 2;
        ctx.bezierCurveTo(cx, prev.y, cx, p.y, p.x, p.y);
      }
    });
    ctx.lineTo(visPts[visPts.length - 1].x, plot.y1);
    ctx.closePath();
    ctx.fill();

    ctx.save();
    ctx.shadowColor = 'rgba(0,229,255,0.55)';
    ctx.shadowBlur = 14;
    ctx.strokeStyle = 'rgba(0,229,255,0.8)';
    ctx.lineWidth = 2.2;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    visPts.forEach((p, i) => {
      if (i === 0) ctx.moveTo(p.x, p.y);
      else {
        const prev = visPts[i - 1];
        const cx = (prev.x + p.x) / 2;
        ctx.bezierCurveTo(cx, prev.y, cx, p.y, p.x, p.y);
      }
    });
    ctx.stroke();
    ctx.restore();

    ctx.strokeStyle = '#00e5ff';
    ctx.lineWidth = 1.6;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    visPts.forEach((p, i) => {
      if (i === 0) ctx.moveTo(p.x, p.y);
      else {
        const prev = visPts[i - 1];
        const cx = (prev.x + p.x) / 2;
        ctx.bezierCurveTo(cx, prev.y, cx, p.y, p.x, p.y);
      }
    });
    ctx.stroke();

    markers.forEach(m => {
      if (m.ts < plot.minT || m.ts > plot.maxT) return;
      const x = tsToX(m.ts);
      let y = null;
      for (let i = 0; i < series.length - 1; i++) {
        if (series[i].ts <= m.ts && series[i + 1].ts >= m.ts) {
          const frac = (m.ts - series[i].ts) / (series[i + 1].ts - series[i].ts || 1);
          const yA = valToY(series[i].bal + (series[i].upnl || 0));
          const yB = valToY(series[i + 1].bal + (series[i + 1].upnl || 0));
          y = yA + (yB - yA) * frac;
          break;
        }
      }
      if (y == null) y = valToY(series[series.length - 1].bal);
      const isOpen = m.type === 'open';
      const color = isOpen ? '#00e5ff' : (m.pnl != null && m.pnl >= 0 ? '#c9a227' : '#ff2d55');
      ctx.fillStyle = color;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      if (isOpen) {
        ctx.moveTo(x, y - 6);
        ctx.lineTo(x - 4, y + 2);
        ctx.lineTo(x + 4, y + 2);
      } else {
        ctx.moveTo(x, y + 6);
        ctx.lineTo(x - 4, y - 2);
        ctx.lineTo(x + 4, y - 2);
      }
      ctx.closePath();
      ctx.fill();
    });

    if (animProgress >= 1) {
      const last = pts[pts.length - 1];
      const lastSeries = series[series.length - 1];
      const pulse = (Math.sin(performance.now() / 800) + 1) / 2;
      ctx.fillStyle = 'rgba(201,162,39,' + (0.08 + pulse * 0.1) + ')';
      ctx.beginPath();
      ctx.arc(last.x, last.y, 10 + pulse * 3, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#c9a227';
      ctx.shadowColor = 'rgba(201,162,39,0.6)';
      ctx.shadowBlur = 8;
      ctx.beginPath();
      ctx.arc(last.x, last.y, 3.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
      const bal = lastSeries.bal + (lastSeries.upnl || 0);
      const label = '$' + bal.toFixed(2);
      ctx.font = 'bold 13px JetBrains Mono, monospace';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'bottom';
      ctx.fillStyle = '#c9a227';
      ctx.fillText(label, plot.x1 - 6, last.y - 10);
    }

    if (hoverX != null && plot) {
      const px = hoverX;
      let best = 0, bestDx = Infinity;
      for (let i = 0; i < series.length; i++) {
        const dx = Math.abs(pts[i].x - px);
        if (dx < bestDx) { bestDx = dx; best = i; }
      }
      const p = pts[best];
      const s = series[best];

      ctx.strokeStyle = 'rgba(0,229,255,0.35)';
      ctx.setLineDash([2, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(p.x, plot.y0);
      ctx.lineTo(p.x, plot.y1);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = '#00e5ff';
      ctx.beginPath();
      ctx.arc(p.x, p.y, 4, 0, Math.PI * 2);
      ctx.fill();

      const spanSec = plot.maxT - plot.minT;
      const tolSec = spanSec / (pts.length * 2);
      let nearMarker = null;
      for (const m of markers) {
        if (Math.abs(m.ts - s.ts) <= tolSec) { nearMarker = m; break; }
      }

      const eqBal = s.bal + (s.upnl || 0);
      let html = '<div class="tt-time">' + fmtTimeShort(s.ts) + '</div>'
               + '<div class="tt-bal">$' + eqBal.toFixed(2) + '</div>';
      if ((s.upnl || 0) !== 0) html += '<div class="dim" style="font-size:9px">bal $' + s.bal.toFixed(2) + ' · upnl ' + (s.upnl >= 0 ? '+' : '') + s.upnl.toFixed(2) + '</div>';
      if (nearMarker) {
        const mt = nearMarker.type === 'open' ? 'OPEN' : 'CLOSE';
        const detail = nearMarker.type === 'close' && nearMarker.r != null
          ? ' ' + (nearMarker.r >= 0 ? '+' : '') + nearMarker.r.toFixed(2) + 'R'
          : '';
        html += '<div class="tt-trade">▸ ' + mt + ' ' + nearMarker.sym + detail + '</div>';
      }
      tooltip.innerHTML = html;
      tooltip.style.display = 'block';
      let tx = p.x + 10;
      let ty = p.y - 30;
      const ttRect = tooltip.getBoundingClientRect();
      if (tx + ttRect.width > W) tx = p.x - ttRect.width - 10;
      if (ty < 0) ty = p.y + 10;
      tooltip.style.left = tx + 'px';
      tooltip.style.top = ty + 'px';
    } else {
      tooltip.style.display = 'none';
    }

    if (animProgress >= 1) {
      requestAnimationFrame(draw);
    }
  }

  canvas.addEventListener('mousemove', e => {
    const rect = canvas.getBoundingClientRect();
    hoverX = e.clientX - rect.left;
  });
  canvas.addEventListener('mouseleave', () => {
    hoverX = null;
    tooltip.style.display = 'none';
  });

  window.addEventListener('resize', () => { resize(); draw(); });
  resize();
  draw();

  return { setData, resize };
})();

// ============================================================================
// POSITIONS PANEL
// ============================================================================

function renderPositions(positions, decisions) {
  const body = $('pos-body');
  if (positions && positions.length) {
    // Live position cards
    const cards = positions.map(p => {
      const isShort = (p.direction || 'long').toLowerCase() === 'short';
      const pnl = Number(p.pnl || 0);
      const r = Number(p.r_mult || 0);
      const cardCls = pnl >= 0 ? 'win' : 'loss';
      const dirCls = isShort ? 'short' : '';

      // Compute SL/TP bar position
      const entry = parseFloat(String(p.entry).replace(/,/g, '')) || 0;
      const sl = parseFloat(String(p.sl).replace(/,/g, '')) || 0;
      const tp1 = p.tp1 ? parseFloat(String(p.tp1).replace(/,/g, '')) : null;
      // Current price is not on /positions payload directly; approximate from entry + pnl sign
      // For the marker position we use: distance from SL vs distance from TP1
      let markerPct = 50;
      if (sl > 0 && tp1 != null && tp1 > 0 && entry > 0) {
        // Assume current price between SL and TP1 proportional to pnl
        // pnl >= 0 -> marker between entry and TP1, pnl < 0 -> between SL and entry
        if (pnl >= 0 && tp1 !== entry) {
          const fracToTp = Math.min(1, pnl / Math.max(0.01, Math.abs(tp1 - entry) * (p.size || 1)));
          markerPct = 50 + fracToTp * 50;
        } else if (sl !== entry) {
          const fracToSl = Math.min(1, -pnl / Math.max(0.01, Math.abs(entry - sl) * (p.size || 1)));
          markerPct = 50 - fracToSl * 50;
        }
      }

      const age = p.opened_at ? fmtAge(Date.now() / 1000 - p.opened_at) : '—';
      const pnlCls = pnl >= 0 ? 'gold' : 'dn';

      return (
        '<div class="pos-card ' + cardCls + '">'
        + '<div class="pos-card-head">'
        +   '<span class="pos-sym">' + esc(p.symbol) + '</span>'
        +   '<span class="pos-dir ' + dirCls + '">' + (isShort ? 'SHORT' : 'LONG') + '</span>'
        +   '<span class="pos-age">' + age + '</span>'
        + '</div>'
        + '<div class="pos-row">'
        +   '<div><div class="pc-lbl">Entry</div><div class="pc-val">$' + esc(p.entry) + '</div></div>'
        +   '<div><div class="pc-lbl">P/L</div><div class="pc-val ' + pnlCls + '">' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '</div></div>'
        +   '<div><div class="pc-lbl">R</div><div class="pc-val ' + pnlCls + '">' + (r >= 0 ? '+' : '') + r.toFixed(2) + 'R</div></div>'
        + '</div>'
        + '<div class="pos-sltp">'
        +   '<div class="pos-sltp-fill"></div>'
        +   '<div class="pos-sltp-label sl">SL</div>'
        +   '<div class="pos-sltp-label tp">TP</div>'
        +   '<div class="pos-sltp-marker" style="left:' + markerPct.toFixed(1) + '%"></div>'
        + '</div>'
        + '</div>'
      );
    });
    body.innerHTML = cards.join('');
    return;
  }

  // Empty state: find nearest entry from decisions
  let nearest = null;
  if (Array.isArray(decisions) && decisions.length) {
    const scored = decisions.map(d => {
      const gp = d.gates_passed || [];
      const gf = d.gates_failed || [];
      // Depth = count of gates passed
      return { d, depth: gp.length, gp, gf };
    });
    scored.sort((a, b) => b.depth - a.depth || (b.d.timestamp || 0) - (a.d.timestamp || 0));
    nearest = scored[0];
  }

  let html = '<div class="pos-empty">'
           + '<div class="pos-empty-msg">No open positions</div>';
  if (nearest && nearest.depth > 0) {
    const blocker = nearest.gf && nearest.gf.length ? nearest.gf[0].split(':')[0] : 'pending';
    html += '<div class="pos-nearest">'
          + '<div class="pn-label">Nearest entry</div>'
          + '<div class="pn-sym">' + esc(nearest.d.symbol) + '</div>'
          + '<div class="pn-depth">passed ' + nearest.depth + ' gates</div>'
          + '<div class="pn-block">blocked: ' + esc(blocker) + '</div>'
          + '</div>';
  }
  html += '</div>';
  body.innerHTML = html;
}

// ============================================================================
// AEGIS / CONVICTION GAUGE
// ============================================================================

const AegisGauge = (() => {
  const canvas = $('aegisCanvas');
  const ctx = canvas.getContext('2d');
  const size = 220;
  let currentScore = 0;
  let targetScore = 0;

  function draw() {
    ctx.clearRect(0, 0, size, size);
    const cx = size / 2, cy = size / 2;
    const radius = 78;

    // Background track
    ctx.strokeStyle = 'rgba(58,63,75,0.35)';
    ctx.lineWidth = 12;
    ctx.beginPath();
    ctx.arc(cx, cy, radius, Math.PI * 0.75, Math.PI * 2.25);
    ctx.stroke();

    // Score arc
    const pct = Math.max(0, Math.min(100, currentScore)) / 100;
    const startA = Math.PI * 0.75;
    const endA = startA + Math.PI * 1.5 * pct;

    const grad = ctx.createLinearGradient(cx - radius, cy, cx + radius, cy);
    if (currentScore >= 80) {
      grad.addColorStop(0, '#8a7018');
      grad.addColorStop(1, '#c9a227');
    } else if (currentScore >= 55) {
      grad.addColorStop(0, '#0087a6');
      grad.addColorStop(1, '#00e5ff');
    } else if (currentScore >= 30) {
      grad.addColorStop(0, '#8a5018');
      grad.addColorStop(1, '#f59e0b');
    } else {
      grad.addColorStop(0, '#5a1020');
      grad.addColorStop(1, '#ff2d55');
    }
    ctx.strokeStyle = grad;
    ctx.lineWidth = 12;
    ctx.lineCap = 'round';
    ctx.shadowColor = 'rgba(201,162,39,0.3)';
    ctx.shadowBlur = 8;
    ctx.beginPath();
    ctx.arc(cx, cy, radius, startA, endA);
    ctx.stroke();
    ctx.shadowBlur = 0;
  }

  function tick() {
    const diff = targetScore - currentScore;
    if (Math.abs(diff) > 0.3) {
      currentScore += diff * 0.08;
      draw();
    } else if (currentScore !== targetScore) {
      currentScore = targetScore;
      draw();
    }
    requestAnimationFrame(tick);
  }

  function setScore(score) {
    targetScore = Math.max(0, Math.min(100, score || 0));
  }

  draw();
  tick();
  return { setScore };
})();

function renderAegis(funnel, analytics, health) {
  // Compute conviction from funnel depth: find deepest gate where majority pass
  // Score = (deepest_majority_gate_idx / gates.length) * 100
  let score = 0;
  let label = 'DORMANT';
  if (funnel && Array.isArray(funnel.gates) && funnel.gates.length && funnel.records > 0) {
    const gates = funnel.gates;
    const recs = funnel.records;
    let deepestMajority = -1;
    for (let i = 0; i < gates.length; i++) {
      const g = gates[i];
      const reached = g.reached || 0;
      const passed = g.passed || 0;
      if (reached === 0) continue; // skip unreached (e.g. ai_conf dead path)
      if (passed / reached >= 0.5) {
        deepestMajority = i;
      }
    }
    if (deepestMajority >= 0) {
      score = Math.round(((deepestMajority + 1) / gates.length) * 100);
    }
    // Boost for entered trades
    if (funnel.entered > 0) {
      score = Math.min(100, score + 10);
    }
  }

  $('aegisScore').textContent = score || '—';
  if (score >= 80) label = 'HOT';
  else if (score >= 55) label = 'ACTIVE';
  else if (score >= 30) label = 'CAUTIOUS';
  else if (score > 0) label = 'DORMANT';
  else label = 'AWAITING';
  $('aegisLabel').textContent = label;
  AegisGauge.setScore(score);

  // Analytics stats
  const a = analytics || {};
  $('aa-trades').textContent = a.trades != null ? a.trades : '—';
  $('aa-wr').textContent = a.win_rate != null ? a.win_rate.toFixed(1) + '%' : '—';
  $('aa-r').textContent = a.avg_r != null ? (a.avg_r >= 0 ? '+' : '') + a.avg_r.toFixed(2) + 'R' : '—';
  const pnl = a.total_pnl;
  const pnlEl = $('aa-pnl');
  if (pnl != null) {
    pnlEl.textContent = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
    pnlEl.className = 'stat-val ' + (pnl >= 0 ? 'gold' : 'dn');
  } else {
    pnlEl.textContent = '—';
    pnlEl.className = 'stat-val';
  }
  $('aa-dd').textContent = a.max_drawdown != null ? a.max_drawdown.toFixed(1) + '%' : '—';
  $('aa-heat').textContent = health && health.portfolio_heat != null ? '$' + health.portfolio_heat.toFixed(2) : '—';

  const cbEl = $('aa-cb');
  if (health && health.circuit_breaker_active) {
    cbEl.textContent = 'TRIPPED';
    cbEl.className = 'cb-val tripped';
  } else {
    cbEl.textContent = 'CLEAR';
    cbEl.className = 'cb-val clear';
  }
}

// ============================================================================
// SIGNAL INTELLIGENCE
// ============================================================================

// Gates we display as a progress chain (ordered top-to-bottom of chain).
// Must match the funnel order in goldeneye.py so the chain reads naturally.
const GATE_CHAIN = [
  'correlation', 'factor_floors', 'confluence', 'dir_wr', 'regime_mult',
  'whale', 'max_positions', 'drawdown', 'sentiment', 'atr_fees',
  'min_size', 'notional', 'duplicate'
];

function renderSignalIntelligence(decisions, funnel) {
  const list = $('si-list');
  const funnelRecords = funnel && funnel.records ? funnel.records : 0;

  if (!Array.isArray(decisions) || !decisions.length) {
    list.innerHTML = '<div class="si-empty">Awaiting signal activity</div>';
    return;
  }

  // Take the most recent 200 and rank top 5 by gates_passed depth, then confidence
  const recent = decisions.slice(-200).map(d => {
    const gp = d.gates_passed || [];
    const gf = d.gates_failed || [];
    const gpSet = new Set(gp.map(s => s.split(':')[0]));
    const gfSet = new Set(gf.map(s => s.split(':')[0]));
    let depth = 0;
    for (const g of GATE_CHAIN) if (gpSet.has(g)) depth++;
    return { d, depth, gpSet, gfSet, conf: d.confidence || 0 };
  }).filter(r => r.depth > 0);

  // Dedup by symbol — keep the best per symbol
  const bySym = {};
  for (const r of recent) {
    const s = r.d.symbol;
    if (!bySym[s] || r.depth > bySym[s].depth || (r.depth === bySym[s].depth && r.conf > bySym[s].conf)) {
      bySym[s] = r;
    }
  }
  const top = Object.values(bySym).sort((a, b) => b.depth - a.depth || b.conf - a.conf).slice(0, 5);

  if (!top.length) {
    // Empty state: distinguish "never reached gates" from "reached but all failed at gate 1"
    const msg = funnelRecords === 0
      ? 'Awaiting confluence (len≥2)'
      : 'All recent signals blocked at gate 1';
    list.innerHTML = '<div class="si-empty">' + msg + '</div>';
    return;
  }

  const html = top.map(r => {
    const d = r.d;
    const isHot = r.depth >= 10;
    const dirRaw = (d.signals && d.signals.length) ? 'LONG' : 'LONG'; // we only trade long
    const whale = d.whale_score != null ? d.whale_score.toFixed(1) : '—';
    const conf = (d.confidence != null ? d.confidence : 0).toFixed(3);

    const chain = GATE_CHAIN.map(g => {
      const short = g.replace('factor_floors', 'floors').replace('regime_mult', 'regime').replace('max_positions', 'maxpos').replace('atr_fees', 'atr');
      if (r.gpSet.has(g)) {
        return '<span class="si-g ok"><span class="mark">✓</span>' + short + '</span>';
      }
      if (r.gfSet.has(g)) {
        return '<span class="si-g fail"><span class="mark">✗</span>' + short + '</span>';
      }
      return '<span class="si-g dim">·' + short + '</span>';
    }).join('');

    return (
      '<div class="si-row ' + (isHot ? 'hot' : '') + '">'
      + '<div class="si-head">'
      +   '<span class="si-sym">' + esc(d.symbol) + '</span>'
      +   '<span class="si-dir">' + dirRaw + '</span>'
      +   '<span class="si-meta">conf <b>' + conf + '</b> · whale <b>' + whale + '</b> · <b>' + r.depth + '</b>/' + GATE_CHAIN.length + '</span>'
      + '</div>'
      + '<div class="si-gates">' + chain + '</div>'
      + '</div>'
    );
  }).join('');
  list.innerHTML = html;
}

// ============================================================================
// GATE FUNNEL
// ============================================================================

function renderFunnel(funnel) {
  if (!funnel) return;
  $('fn-records').textContent = funnel.records || 0;
  $('fn-entered').textContent = funnel.entered || 0;

  const gates = Array.isArray(funnel.gates) ? funnel.gates : [];
  const totalReached = gates.reduce((a, g) => a + (g.reached || 0), 0);
  const maxReached = Math.max(1, ...gates.map(g => g.reached || 0));

  // Find bottleneck: first gate (top-to-bottom) where failed > 0
  let bottleneckIdx = -1;
  for (let i = 0; i < gates.length; i++) {
    if ((gates[i].failed || 0) > 0) { bottleneckIdx = i; break; }
  }

  // Gate name shortener (consistent top-to-bottom of the funnel)
  const shortName = g => g.name.replace('factor_floors', 'FLOORS')
                               .replace('regime_mult', 'REGIME')
                               .replace('max_positions', 'MAX POS')
                               .replace('atr_fees', 'ATR FEES')
                               .replace('min_size', 'MIN SIZE')
                               .replace('dir_wr', 'DIR WR')
                               .toUpperCase();

  // Always show the primary chain (correlation through min_size) so the funnel
  // structure is visible even in quiet-market states where nothing reaches the gates.
  // Dead code paths (ai_conf in current config) and terminal race-guards (notional,
  // duplicate) are hidden unless they actually fired.
  const ALWAYS_SHOW = new Set([
    'correlation', 'factor_floors', 'confluence', 'dir_wr', 'regime_mult',
    'whale', 'max_positions', 'drawdown', 'sentiment', 'atr_fees', 'min_size',
  ]);
  const visible = gates.filter((g, i) =>
    ALWAYS_SHOW.has(g.name) || (g.reached || 0) > 0 || i === bottleneckIdx
  );

  const rowsHtml = visible.map(g => {
    const reached = g.reached || 0;
    const passed = g.passed || 0;
    const widthPct = maxReached > 0 ? (reached / maxReached) * 100 : 0;
    const isBottleneck = (g.failed || 0) > 0 && gates.indexOf(g) === bottleneckIdx;
    const isDim = reached === 0;
    const display = shortName(g);
    const countStr = passed + '';
    return (
      '<div class="funnel-row ' + (isBottleneck ? 'bottleneck' : '') + (isDim ? ' dim-row' : '') + '">'
      + '<div class="fn-name">' + display + '</div>'
      + '<div class="fn-bar-track"><div class="fn-bar" style="width:' + widthPct.toFixed(1) + '%"></div></div>'
      + '<div class="fn-count ' + (passed === 0 ? 'dim' : '') + '">' + countStr + '</div>'
      + '</div>'
    );
  }).join('');

  // ENTERED row at the bottom
  const enteredWidth = maxReached > 0 ? ((funnel.entered || 0) / maxReached) * 100 : 0;
  const enteredRow = (
    '<div class="funnel-row" style="margin-top:4px">'
    + '<div class="fn-name gold">ENTERED</div>'
    + '<div class="fn-bar-track"><div class="fn-bar" style="width:' + enteredWidth.toFixed(1) + '%;background:linear-gradient(90deg,var(--gold-soft),var(--gold))"></div></div>'
    + '<div class="fn-count ' + ((funnel.entered || 0) === 0 ? 'dim' : 'gold') + '">' + (funnel.entered || 0) + '</div>'
    + '</div>'
  );

  // If no gates have any activity, add a quiet-market notice above the bars.
  let notice = '';
  if (totalReached === 0 && (funnel.records || 0) === 0) {
    notice = '<div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;color:var(--dim);text-align:center;padding:3px 0 5px;letter-spacing:0.1em;">WAITING FOR len≥2 CONFLUENCE</div>';
  }

  $('fn-rows').innerHTML = notice + rowsHtml + enteredRow;
}

// ============================================================================
// SYMBOL GRID
// ============================================================================

// Track which symbols have had a recent signal event (cell glow trigger).
// Activity decays after 8 seconds so the glow fades if signals stop firing.
const _symActivity = {};     // { sym: lastSignalTs }
const SYM_ACTIVE_WINDOW_MS = 8000;

function markSignalActivity(symbol) {
  _symActivity[symbol] = Date.now();
}

// Format price with decimals scaled to magnitude.
// Large prices (BTC, TAO): 2 decimals.  Mid (ADA, DOT): 4 decimals.  Tiny (SHIB, FARTCOIN): 6.
function fmtSymPrice(raw) {
  const n = parseFloat(String(raw).replace(/,/g, ''));
  if (isNaN(n)) return String(raw);
  if (n >= 100) return n.toFixed(2);
  if (n >= 1) return n.toFixed(4);
  if (n >= 0.01) return n.toFixed(4);
  return n.toFixed(6);
}

function renderSymbolGrid(symbols, positions) {
  const grid = $('sym-grid');
  if (!Array.isArray(symbols) || !symbols.length) {
    grid.innerHTML = '<div class="placeholder" style="grid-column:1/-1">no symbols</div>';
    return;
  }

  // Build a quick position lookup
  const posMap = {};
  if (Array.isArray(positions)) {
    for (const p of positions) {
      posMap[p.symbol] = p;
    }
  }

  // Sort by position first, then by regime interest (bull > bear > range > chop), then alphabetic
  const regimeOrder = { bull: 0, bear: 1, range: 2, chop: 3 };
  const sorted = symbols.slice().sort((a, b) => {
    const ap = posMap[a.symbol] ? 0 : 1;
    const bp = posMap[b.symbol] ? 0 : 1;
    if (ap !== bp) return ap - bp;
    const ar = regimeOrder[a.regime] ?? 9;
    const br = regimeOrder[b.regime] ?? 9;
    if (ar !== br) return ar - br;
    return a.symbol.localeCompare(b.symbol);
  });

  const now = Date.now();
  const html = sorted.map(s => {
    const pos = posMap[s.symbol];
    const isShort = pos && (pos.direction || '').toLowerCase() === 'short';
    const active = (now - (_symActivity[s.symbol] || 0)) < SYM_ACTIVE_WINDOW_MS;

    let cls = 'sym-cell';
    if (pos) cls += isShort ? ' pos-short' : ' pos-long';
    else if (active) cls += ' active';

    const ticker = s.symbol.replace('/USD', '');
    const priceStr = fmtSymPrice(s.price);
    const rgm = (s.regime || 'chop').toLowerCase();
    const priceCls = s.up ? 'up' : 'dn';
    const pnl = pos ? (pos.pnl || 0) : (s.pnl || 0);
    const hasPnl = pos && Math.abs(pnl) > 0.001;
    const pnlStr = hasPnl ? ((pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2))
                          : '$0.00';
    const pnlCls = hasPnl ? (pnl >= 0 ? 'up' : 'dn') : 'zero';

    // Single-line horizontal: TICKER | PRICE | [BULL] | $PNL (only when position).
    // 2-column layout gives ~200px per cell — plenty of room for all four fields.
    const pnlFragment = hasPnl
      ? '<span class="sym-pnl ' + pnlCls + '">' + pnlStr + '</span>'
      : '';
    return (
      '<div class="' + cls + '">'
      +   '<span class="sym-name">' + esc(ticker) + '</span>'
      +   '<span class="sym-price ' + priceCls + '">' + esc(priceStr) + '</span>'
      +   '<span class="sym-rgm ' + rgm + '">' + rgm.toUpperCase() + '</span>'
      +   pnlFragment
      + '</div>'
    );
  }).join('');

  grid.innerHTML = html;
  $('sym-badge').textContent = symbols.length;
}

// ============================================================================
// LIVE LOG — frontend-maintained rolling buffer, dedup against last-seen line
// ============================================================================

const MAX_LOG_LINES = 6;         // visible lines cap — panel body is ~114px / 18px line = 6 rows
const _logBuffer = [];           // { ts, text, cls, tag }
let _lastLogFingerprint = '';    // used to detect what's new since last poll

function classifyLogLine(raw) {
  // raw is like "[22:21:14] NEAR/USD signals: ['2'] (len=1, pos=False, cd_ok=True)"
  let ts = '';
  let body = raw;
  const tsMatch = raw.match(/^\[(\d\d:\d\d:\d\d)\]\s*(.*)$/);
  if (tsMatch) {
    ts = tsMatch[1];
    body = tsMatch[2];
  }

  // Classify by substring. Order matters — check high-signal tags first.
  let cls = '';
  let tag = '';
  if (/\bSL HIT\b|\bSHORT entered|\bLOSS\b/i.test(body)) { cls = 'loss'; tag = 'LOSS'; }
  else if (/\bTP HIT\b|\bTP_FILL\b|\bWIN\b/i.test(body)) { cls = 'win'; tag = 'WIN'; }
  else if (/\bPAPER LONG\b|\bPAPER SHORT\b|\bLONG entered|\bOPEN\b(?! card)/i.test(body)) { cls = 'open'; tag = 'OPEN'; }
  else if (/SHADOW/.test(body)) { cls = 'shadow'; tag = 'SHDW'; }
  else if (/\bblocked\b/i.test(body)) { cls = 'blk'; tag = 'BLK'; }
  else if (/signals:\s*\[/.test(body)) { cls = 'sig'; tag = 'SIG'; }
  else if (/\berror\b|exception|traceback|unsupported/i.test(body)) { cls = 'err'; tag = 'ERR'; }

  return { ts, body, cls, tag };
}

function extractSymbolFromLogBody(body) {
  const m = body.match(/^([A-Z0-9]+\/[A-Z]+)/);
  return m ? m[1] : null;
}

function renderLogFeed(rawLines) {
  if (!Array.isArray(rawLines)) return;

  // Frontend-side rolling buffer: only append lines we haven't seen yet.
  // Bot exposes only the last 8 lines, so we dedupe by exact string and append
  // any new ones to our local buffer.
  const seen = new Set(_logBuffer.map(l => l.raw));
  for (const raw of rawLines) {
    if (seen.has(raw)) continue;
    const parsed = classifyLogLine(raw);
    _logBuffer.push({ raw, ...parsed });
    seen.add(raw);
    // Side effect: mark symbol activity for the grid cell glow
    const sym = extractSymbolFromLogBody(parsed.body);
    if (sym) markSignalActivity(sym);
  }
  // Cap the buffer
  while (_logBuffer.length > MAX_LOG_LINES * 3) {
    _logBuffer.shift();
  }
  if (_logBuffer.length > MAX_LOG_LINES) {
    // Keep last MAX_LOG_LINES for render
  }

  const visible = _logBuffer.slice(-MAX_LOG_LINES);
  const feed = $('log-feed');
  if (!visible.length) {
    feed.innerHTML = '<div class="log-empty">Awaiting events</div>';
    $('log-badge').textContent = '—';
    return;
  }

  const html = visible.map(l => {
    const cls = l.cls ? (' ' + l.cls) : '';
    const tagHtml = l.tag ? '<span class="log-tag">' + l.tag + '</span>' : '';
    return (
      '<div class="log-line' + cls + '">'
      +   '<span class="log-ts">' + esc(l.ts) + '</span>'
      +   tagHtml
      +   esc(l.body)
      + '</div>'
    );
  }).join('');
  feed.innerHTML = html;
  $('log-badge').textContent = _logBuffer.length + ' events';
}

// ============================================================================
// POLL LOOP
// ============================================================================

async function gj(url) {
  try {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

// State shared across pollers
let _latestHealth = null;
let _latestDecisions = [];
let _latestFunnel = null;
let _latestAnalytics = null;

// Shared state for cheap grid re-renders between pollMid cycles
let _latestSymbols = [];

async function pollFast() {
  // 2s: health + logs (log feed wants the snappiest cadence)
  const [h, logs] = await Promise.all([
    gj('/api/health'),
    gj('/api/logs'),
  ]);
  _latestHealth = h;
  renderTopBar(h);
  if (h && h.live_balance != null) {
    $('eq-badge').textContent = '$' + h.live_balance.toFixed(2) + ' LIFETIME';
  }
  if (Array.isArray(logs)) {
    renderLogFeed(logs);
    // Log feed calls markSignalActivity() for any new-to-us lines; re-paint
    // the symbol grid from cached data so the cell glow shows within one fast tick.
    if (_latestSymbols.length) {
      renderSymbolGrid(_latestSymbols, _latestPositions);
    }
  }
}

let _latestPositions = [];
async function pollMid() {
  // 5s: positions + decisions + funnel + symbols
  const [positions, decisions, funnel, symbols] = await Promise.all([
    gj('/api/positions'),
    gj('/api/decisions'),
    gj('/api/funnel'),
    gj('/api/symbols'),
  ]);
  _latestDecisions = Array.isArray(decisions) ? decisions : [];
  _latestFunnel = funnel;
  _latestPositions = Array.isArray(positions) ? positions : [];
  renderPositions(_latestPositions, _latestDecisions);
  renderSignalIntelligence(_latestDecisions, funnel);
  renderFunnel(funnel);
  // AEGIS depends on funnel + analytics + health
  renderAegis(funnel, _latestAnalytics, _latestHealth);
  // Symbol grid uses positions for glow + border state
  if (Array.isArray(symbols)) {
    _latestSymbols = symbols;
    renderSymbolGrid(symbols, _latestPositions);
  }
}

async function pollSlow() {
  // 15s: equity + markers + analytics
  const [eq, mk, analytics] = await Promise.all([
    gj('/api/equity'),
    gj('/api/trade_markers'),
    gj('/api/analytics'),
  ]);
  if (Array.isArray(eq)) {
    EquityChart.setData(eq, Array.isArray(mk) ? mk : []);
  }
  if (analytics && typeof analytics === 'object') {
    _latestAnalytics = analytics;
    renderAegis(_latestFunnel, _latestAnalytics, _latestHealth);
  }
}

pollFast();
pollMid();
pollSlow();
setInterval(pollFast, 2000);
setInterval(pollMid, 5000);
setInterval(pollSlow, 15000);

</script>
</body>
</html>"""



PUBLIC_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ORACLE — Quantitative Signal Intelligence</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Outfit:wght@200;300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden;background:#040608;color:#c8c8d0;font-family:'JetBrains Mono',monospace;user-select:none}

canvas.bg{position:fixed;inset:0;z-index:0}

.hud{position:fixed;inset:0;z-index:1;display:grid;grid-template-rows:52px 1fr 440px;pointer-events:none}
.hud>*{pointer-events:auto}

/* === HEADER === */
.hdr{display:flex;align-items:center;justify-content:space-between;padding:0 20px;background:rgba(4,6,8,0.88);border-bottom:1px solid rgba(212,168,67,0.10);backdrop-filter:blur(16px);z-index:10}
.logo-g{display:flex;align-items:center;gap:12px}
.eye{width:34px;height:34px;border:2px solid #d4a843;border-radius:50%;display:flex;align-items:center;justify-content:center;animation:eyeglow 6s ease-in-out infinite;flex-shrink:0}
.eye::after{content:'';width:10px;height:10px;border-radius:50%;background:#d4a843}
@keyframes eyeglow{0%,100%{box-shadow:0 0 10px rgba(212,168,67,0.15)}50%{box-shadow:0 0 28px rgba(212,168,67,0.5)}}
.bname{font-family:'Outfit',sans-serif;font-size:15px;font-weight:600;color:#d4a843;letter-spacing:5px}
.bsub{font-size:8px;color:#555568;letter-spacing:3px;margin-top:-1px}
.conn{display:flex;align-items:center;gap:6px;font-size:10px;letter-spacing:1.5px}
.conn.online{color:#00d4aa}
.conn.offline{color:#ff4757}
.cdot{width:6px;height:6px;border-radius:50%;animation:pls 2s infinite}
.cdot.online{background:#00d4aa;box-shadow:0 0 6px rgba(0,212,170,0.5)}
.cdot.offline{background:#ff4757;box-shadow:0 0 6px rgba(255,71,87,0.5)}
@keyframes pls{0%,100%{opacity:1}50%{opacity:0.25}}
.lbadge{padding:2px 8px;border:1px solid rgba(0,212,170,0.25);border-radius:2px;font-size:8px;font-weight:700;letter-spacing:2px}
.lbadge.live{color:#00d4aa;border-color:rgba(0,212,170,0.25)}
.lbadge.paper{color:#ffb347;border-color:rgba(255,179,71,0.25)}
.srow{display:flex;gap:24px}
.st{text-align:right}
.stl{font-size:6px;color:#444458;letter-spacing:1.5px;text-transform:uppercase}
.stv{font-size:12px;font-weight:500;font-family:'Outfit',sans-serif}
.g{color:#00d4aa}.r{color:#ff4757}.gd{color:#d4a843}.w{color:#e8e8ec}.am{color:#ffb347}.pp{color:#b084f4}.cy{color:#56d4e0}.dm{color:#555568}

/* === MIDDLE ORBITAL AREA === */
.mid{position:relative;overflow:hidden}
.orb-cv{position:absolute;inset:0}

.core-ring{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:300px;height:300px;border:1px solid rgba(212,168,67,0.06);border-radius:50%;display:flex;align-items:center;justify-content:center;flex-direction:column;z-index:2;pointer-events:none}
.core-bal{font-family:'Outfit',sans-serif;font-size:44px;font-weight:200;color:#e8e8ec;letter-spacing:3px}
.core-lbl{font-size:10px;color:#444458;letter-spacing:4px;margin-bottom:4px}
.core-pnl{font-size:19px;font-weight:500;margin-top:6px}
.core-sub{font-size:12px;color:#555568;margin-top:3px;letter-spacing:1px}

.gate-ring{position:absolute;top:50%;left:50%;width:360px;height:360px;transform:translate(-50%,-50%);border:1px dashed rgba(212,168,67,0.04);border-radius:50%;pointer-events:none;z-index:1}
.outer-ring{position:absolute;top:50%;left:50%;width:500px;height:500px;transform:translate(-50%,-50%);border:1px solid rgba(212,168,67,0.03);border-radius:50%;pointer-events:none;z-index:1}

/* === BOTTOM PANELS === */
.bot{display:grid;grid-template-columns:1fr 1.6fr 1.2fr;gap:1px;background:rgba(212,168,67,0.05);border-top:1px solid rgba(212,168,67,0.08)}
.pnl{background:rgba(4,6,8,0.93);padding:12px 16px;backdrop-filter:blur(10px);overflow-y:auto;scrollbar-width:thin;scrollbar-color:rgba(212,168,67,0.15) transparent}
.pnl::-webkit-scrollbar{width:3px}
.pnl::-webkit-scrollbar-thumb{background:rgba(212,168,67,0.15);border-radius:2px}
.pt{font-size:12px;color:#d4a843;letter-spacing:3px;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.pt::before{content:'';width:2px;height:9px;background:#d4a843;border-radius:1px}

/* Position rows */
.prow{display:flex;align-items:center;padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.015);font-size:12px;gap:8px}
.prow:hover{background:rgba(212,168,67,0.03)}
.psym{color:#e8e8ec;font-weight:700;min-width:80px;font-size:13px}
.pdir{font-size:10px;font-weight:700;padding:2px 7px;border-radius:2px;letter-spacing:0.5px}
.pdl{color:#00d4aa;background:rgba(0,212,170,0.08)}
.pds{color:#ff4757;background:rgba(255,71,87,0.08)}
.pent{color:#555568;font-size:12px;min-width:64px}
.ppnl{min-width:50px;text-align:right}
.prm{min-width:44px;text-align:right}
.ptp{display:flex;gap:2px}
.tps{width:16px;height:5px;border-radius:1px}
.tph{background:#00d4aa;box-shadow:0 0 4px rgba(0,212,170,0.25)}
.tpm{background:rgba(255,255,255,0.04)}
.page{color:#3a3a4a;font-size:12px;min-width:48px;text-align:right}

/* Intel cards */
.tg-card{border:1px solid rgba(212,168,67,0.08);border-radius:4px;padding:8px 10px;margin-bottom:6px;background:rgba(212,168,67,0.02)}
.tg-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.tg-type{font-size:11px;font-weight:700;letter-spacing:1px}
.tg-time{font-size:9px;color:#3a3a4a}
.tg-body{font-size:11px;color:#8888a0;line-height:1.6}
.tg-body .hl{color:#d4a843}
.tg-body .hlg{color:#00d4aa}
.tg-body .hlr{color:#ff4757}
.tg-sig{font-size:11px;color:#d4a843;font-style:italic;margin-top:6px;border-top:1px solid rgba(212,168,67,0.06);padding-top:6px}

/* Trade rows */
.trow{display:flex;align-items:center;padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.015);font-size:12px;gap:6px}
.trow:hover{background:rgba(212,168,67,0.03)}
.tsym{color:#e8e8ec;font-weight:500;min-width:68px}
.texit{font-size:10px;font-weight:700;letter-spacing:0.5px;min-width:34px;text-align:center}
.tval{min-width:50px;text-align:right}
.tage{color:#3a3a4a;font-size:10px;min-width:42px;text-align:right}

/* Log rows */
.lrow{display:flex;gap:6px;padding:3px 0;font-size:10px}
.lts{color:#2a2a3a;min-width:40px}
.ltag{font-size:9px;font-weight:700;letter-spacing:0.5px;min-width:36px;text-align:center}
.lmsg{color:#555568;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* Analytics grid */
.ag{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px 12px}
.ai{padding:3px 0}
.al{font-size:9px;color:#3a3a4a;letter-spacing:1.5px;text-transform:uppercase}
.av{font-size:24px;font-weight:500;font-family:'Outfit',sans-serif;margin-top:2px}
.an{font-size:8px;color:#2a2a3a;margin-top:2px}

/* Right panel sub-sections */
.sub-sect{margin-top:8px;padding-top:6px;border-top:1px solid rgba(255,255,255,0.03)}
.sub-lbl{font-size:8px;color:rgba(212,168,67,0.38);letter-spacing:3px;margin-bottom:6px}

.no-pos{color:#3a3a4a;font-size:9px;text-align:center;padding:16px 0}

/* Clickable rows */
a.prow,a.trow{text-decoration:none;color:inherit;display:flex}
</style>
</head>
<body>

<canvas class="bg" id="stars"></canvas>

<div class="hud">
  <!-- HEADER -->
  <div class="hdr">
    <div class="logo-g">
      <div class="eye"></div>
      <div><div class="bname">ORACLE</div><div class="bsub">QUANTITATIVE SIGNAL INTELLIGENCE</div></div>
    </div>
    <div style="display:flex;align-items:center;gap:14px">
      <div class="conn offline" id="connStatus"><div class="cdot offline" id="connDot"></div><span id="connLabel">CONNECTING</span></div>
      <div class="lbadge paper" id="modeBadge">--</div>
    </div>
    <div class="srow">
      <div class="st"><div class="stl">Uptime</div><div class="stv w" id="uptime">--</div></div>
      <div class="st"><div class="stl">Balance</div><div class="stv w" id="hdrBalance">--</div></div>
      <div class="st"><div class="stl">Trade</div><div class="stv w" id="tradeAmt">--</div></div>
      <div class="st"><div class="stl">P/L</div><div class="stv w" id="hdrPnl">--</div></div>
      <div class="st"><div class="stl">Win Rate</div><div class="stv w" id="hdrWr">--</div></div>
      <div class="st"><div class="stl">Open</div><div class="stv gd" id="hdrOpen">0</div></div>
    </div>
  </div>

  <!-- ORBITAL MID SECTION -->
  <div class="mid">
    <canvas class="orb-cv" id="orb"></canvas>
    <div class="outer-ring"></div>
    <div class="gate-ring"></div>
    <div class="core-ring">
      <div class="core-lbl" id="coreLbl">LIVE BALANCE</div>
      <div class="core-bal" id="coreBal">--</div>
      <div class="core-pnl w" id="corePnl">--</div>
      <div class="core-sub" id="coreSub">-- trades</div>
    </div>
  </div>

  <!-- BOTTOM PANELS -->
  <div class="bot">
    <!-- LEFT: POSITIONS -->
    <div class="pnl">
      <div class="pt">POSITIONS</div>
      <div id="positionsContainer"><div class="no-pos">No open positions</div></div>
      <div id="heatBar" style="margin-top:8px;padding-top:6px;border-top:1px solid rgba(255,255,255,0.03);display:none">
        <div style="display:flex;justify-content:space-between;font-size:10px"><span class="dm">Heat</span><span class="am" id="heatPct">0%</span></div>
        <div style="height:4px;background:rgba(255,255,255,0.04);border-radius:2px;margin-top:4px"><div id="heatFill" style="height:100%;width:0%;background:#ffb347;border-radius:2px;transition:width 0.6s"></div></div>
      </div>
    </div>

    <!-- CENTER: FLEET INTELLIGENCE -->
    <div class="pnl" style="overflow-y:auto">
      <div class="pt">FLEET INTELLIGENCE</div>
      <div id="intelCards"><div class="no-pos">Awaiting signals...</div></div>
    </div>

    <!-- RIGHT: ANALYTICS + TRADES + LOG -->
    <div class="pnl">
      <div class="pt">ANALYTICS</div>
      <div class="ag">
        <div class="ai"><div class="al">Trades</div><div class="av w" id="anTrades">--</div><div class="an" id="anTradesSub">--</div></div>
        <div class="ai"><div class="al">Win Rate</div><div class="av w" id="anWr">--</div><div class="an">target: 55%</div></div>
        <div class="ai"><div class="al">Avg R</div><div class="av w" id="anAvgR">--</div></div>
        <div class="ai"><div class="al">Total P/L</div><div class="av w" id="anPnl">--</div><div class="an" id="anPnlSub">--</div></div>
        <div class="ai"><div class="al">Max DD</div><div class="av w" id="anDD">--</div></div>
        <div class="ai"><div class="al">Positions</div><div class="av gd" id="anPos">--</div></div>
      </div>
      <div class="sub-sect">
        <div class="sub-lbl">RECENT TRADES</div>
        <div id="recentTrades"><div style="font-size:10px;color:#3a3a4a;text-align:center">No closed trades</div></div>
      </div>
      <div class="sub-sect">
        <div class="sub-lbl">EVENT LOG</div>
        <div id="logContainer" style="max-height:120px;overflow-y:auto"></div>
      </div>
      <div class="sub-sect">
        <div class="sub-lbl">AI ENGINE</div>
        <div style="display:flex;justify-content:space-between;font-size:11px;padding:3px 0"><span class="dm">DQN</span><span id="dqnStatus" class="dm">--</span></div>
        <div style="display:flex;justify-content:space-between;font-size:11px;padding:3px 0"><span class="dm">DD Mult</span><span id="aiDdMult" class="w">--</span></div>
        <div style="display:flex;justify-content:space-between;font-size:11px;padding:3px 0"><span class="dm">Breaker</span><span id="aiCb" class="dm">--</span></div>
      </div>
    </div>
  </div>
</div>

<script>
// =====================================================================
// ORACLE — Orbital Dashboard v2
// =====================================================================
const C = {
  gold:'#d4a843', green:'#00d4aa', red:'#ff4757', amber:'#ffb347',
  dim:'#555568', purple:'#b084f4', cyan:'#56d4e0', bg:'#040608'
};
const RGM_CLR = {bull:C.green, bear:C.red, range:C.amber, chop:C.dim};

const SIG_NAMES = {
  a:'EMA Cross Up',b:'Bull Engulfing',c:'RSI<30 Cross',d:'BB Lower Bounce',
  e:'EMA Pullback',f:'MACD Hist Rise',g:'RSI Bull Range',h:'BB %B Recovery',
  i:'MFI Oversold',j:'OBV Breakout',k:'BB Squeeze Up',l:'Hist Bull Div',
  m:'MFI Bull Range',n:'Volume Surge',o:'ADX Trend Str',p:'Stoch Oversold',
  q:'Hammer Candle',r:'MACD Zero Cross',s:'ATR Expansion',t:'Chop Exit',
  u:'Stoch Momentum',v:'EMA200 Reclaim',w:'OBV Accumulation',x:'Triple Confluence',
  y:'BB Mid Reclaim',z:'Vol Trend Confirm','2':'Chop+MFI'
};

const VOICE_WIN = [
  "The signals aligned. The brain learns. We move forward.",
  "Conviction pays. Oracle sees what the noise hides.",
  "Patience rewarded. The math holds."
];
const VOICE_LOSS = [
  "Market gave, market took. We learn from this one.",
  "Stopped out. No signal is perfect \u2014 but the system adapts.",
  "Loss absorbed. Brain recalibrates. We go again."
];
const VOICE_TP1 = [
  "First target secured. Letting the rest ride.",
  "TP1 hit. Risk is off the table now."
];
const VOICE_GEN = [
  "Whatever it takes.",
  "Built honest, runs honest.",
  "I got scammed twice by signal services. Paid real money for signals that were fabricated. So I built an honest one. \u2014 GoldenEye Intelligence",
  "27 signals. 7 gates. No shortcuts."
];

function pickVoice(arr, seed) { return arr[Math.abs(seed) % arr.length]; }
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

// =====================================================================
// STARFIELD
// =====================================================================
const starsCanvas = document.getElementById('stars');
const sctx = starsCanvas.getContext('2d');
let stars = [];

function initStars() {
  starsCanvas.width = window.innerWidth;
  starsCanvas.height = window.innerHeight;
  stars = [];
  for (let i = 0; i < 400; i++) {
    stars.push({
      x: Math.random() * starsCanvas.width,
      y: Math.random() * starsCanvas.height,
      r: Math.random() * 1.2 + 0.2,
      a: Math.random() * 0.6 + 0.1,
      s: Math.random() * 0.002 + 0.001,
      p: Math.random() * Math.PI * 2
    });
  }
}

function drawStars(t) {
  sctx.clearRect(0, 0, starsCanvas.width, starsCanvas.height);
  for (const s of stars) {
    const flicker = 0.5 + 0.5 * Math.sin(t * s.s + s.p);
    const alpha = s.a * flicker;
    sctx.beginPath();
    sctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
    sctx.fillStyle = `rgba(200,200,220,${alpha})`;
    sctx.fill();
    if (s.r > 1) {
      sctx.beginPath();
      sctx.arc(s.x, s.y, s.r * 3, 0, Math.PI * 2);
      sctx.fillStyle = `rgba(200,200,220,${alpha * 0.08})`;
      sctx.fill();
    }
  }
}

// =====================================================================
// ORBITAL CANVAS
// =====================================================================
const orbCanvas = document.getElementById('orb');
const octx = orbCanvas.getContext('2d');
let symbolData = [];

function initOrb() {
  orbCanvas.width = orbCanvas.parentElement.clientWidth;
  orbCanvas.height = orbCanvas.parentElement.clientHeight;
}

function drawOrbitals(t) {
  const W = orbCanvas.width, H = orbCanvas.height;
  const cx = W / 2, cy = H / 2;
  octx.clearRect(0, 0, W, H);

  // Use the full available space — radii as fractions of the smaller dimension
  const minDim = Math.min(W, H);
  const radii = [minDim * 0.18, minDim * 0.30, minDim * 0.42, minDim * 0.54];

  // Draw orbital tracks
  for (const r of radii) {
    octx.beginPath();
    octx.arc(cx, cy, r, 0, Math.PI * 2);
    octx.strokeStyle = 'rgba(212,168,67,0.03)';
    octx.lineWidth = 0.5;
    octx.stroke();
  }

  // Scanning sweep (golden conic gradient)
  if (typeof octx.createConicGradient === 'function') {
    const sweepAngle = (t * 0.0003) % (Math.PI * 2);
    const sweepGrad = octx.createConicGradient(sweepAngle, cx, cy);
    sweepGrad.addColorStop(0, 'rgba(212,168,67,0.06)');
    sweepGrad.addColorStop(0.08, 'rgba(212,168,67,0.0)');
    sweepGrad.addColorStop(1, 'rgba(212,168,67,0.0)');
    octx.beginPath();
    octx.arc(cx, cy, radii[3] + 30, 0, Math.PI * 2);
    octx.fillStyle = sweepGrad;
    octx.fill();
  }

  // Symbol nodes
  const syms = symbolData;
  if (!syms.length) return;

  // Sort: positions first (inner ring), then by regime activity
  const sorted = [...syms].sort((a, b) => {
    if (a.has_pos && !b.has_pos) return -1;
    if (!a.has_pos && b.has_pos) return 1;
    return (b.sig || 0) - (a.sig || 0);
  });

  // Distribute: positions get ring 1, rest spread across rings 1-3
  // Ring 0 is exclusion zone (too close to center balance display)
  const posSyms = sorted.filter(s => s.has_pos);
  const nonPosSyms = sorted.filter(s => !s.has_pos);

  // Label collision tracking
  const placedLabels = [];
  function labelOverlaps(x, y) {
    for (const lbl of placedLabels) {
      if (Math.abs(x - lbl.x) < 50 && Math.abs(y - lbl.y) < 18) return true;
    }
    return false;
  }

  function drawNode(s, orbit, idxInOrbit, countInOrbit) {
    const baseAngle = (idxInOrbit / countInOrbit) * Math.PI * 2 - Math.PI / 2;
    const speed = (4 - orbit) * 0.00003;
    const angle = baseAngle + t * speed;
    const r = radii[orbit];
    const x = cx + Math.cos(angle) * r;
    const y = cy + Math.sin(angle) * r;

    const rgm = (s.regime || 'chop').toLowerCase();
    const rgmClr = RGM_CLR[rgm] || C.dim;
    const sigCount = s.sig || 0;
    const hasPos = s.has_pos || false;
    const pnl = parseFloat(s.pnl) || 0;

    // Node radius
    let nodeR = 4;
    if (hasPos) nodeR = 12;
    else if (sigCount >= 4) nodeR = 8;
    else if (sigCount >= 3) nodeR = 6.5;
    else if (sigCount >= 2) nodeR = 5;
    else if (sigCount >= 1) nodeR = 4.5;

    // Glow halo for positions
    if (hasPos) {
      const pnlClr = pnl >= 0 ? C.green : C.red;
      const grd = octx.createRadialGradient(x, y, nodeR, x, y, nodeR + 20);
      grd.addColorStop(0, pnlClr + '35');
      grd.addColorStop(1, pnlClr + '00');
      octx.beginPath();
      octx.arc(x, y, nodeR + 20, 0, Math.PI * 2);
      octx.fillStyle = grd;
      octx.fill();
      octx.beginPath();
      octx.arc(x, y, nodeR + 3, 0, Math.PI * 2);
      octx.strokeStyle = pnlClr + '55';
      octx.lineWidth = 0.8;
      octx.stroke();
    }

    // Node dot
    octx.beginPath();
    octx.arc(x, y, nodeR, 0, Math.PI * 2);
    const dotColor = hasPos ? (pnl >= 0 ? C.green : C.red) : rgmClr;
    octx.globalAlpha = sigCount === 0 && !hasPos ? 0.3 : 0.65 + Math.min(sigCount, 4) * 0.08;
    octx.fillStyle = dotColor;
    octx.fill();
    octx.globalAlpha = 1;

    // Label — check for overlap and offset if needed
    const name = (s.symbol || '').replace('/USD', '');
    let labelY = y - nodeR - 7;
    if (labelOverlaps(x, labelY)) labelY -= 16;
    octx.font = hasPos ? '700 12px "Outfit", sans-serif' : '600 10px "Outfit", sans-serif';
    octx.fillStyle = hasPos ? '#e8e8ec' : '#7a7a90';
    octx.textAlign = 'center';
    octx.fillText(name, x, labelY);
    placedLabels.push({x, y: labelY});

    // P/L for active positions
    if (hasPos) {
      octx.font = '500 10px "JetBrains Mono", monospace';
      octx.fillStyle = pnl >= 0 ? C.green : C.red;
      const pnlStr = (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2);
      octx.fillText(pnlStr, x, y + nodeR + 14);
    }

    // Regime dot for non-position symbols with signals
    if (!hasPos && sigCount > 0) {
      octx.beginPath();
      octx.arc(x + nodeR + 5, y, 2.5, 0, Math.PI * 2);
      octx.fillStyle = rgmClr;
      octx.globalAlpha = 0.5;
      octx.fill();
      octx.globalAlpha = 1;
    }
  }

  // Draw positions on ring 1 (first real ring, outside exclusion zone)
  posSyms.forEach((s, i) => drawNode(s, 1, i, Math.max(posSyms.length, 1)));

  // Distribute non-position symbols across rings 0, 2, 3
  const rings = [0, 2, 3];
  const perRing = Math.ceil(nonPosSyms.length / rings.length);
  nonPosSyms.forEach((s, i) => {
    const ringIdx = Math.min(rings.length - 1, Math.floor(i / perRing));
    const orbit = rings[ringIdx];
    const startIdx = ringIdx * perRing;
    const countInRing = Math.min(perRing, nonPosSyms.length - startIdx);
    const idxInRing = i - startIdx;
    drawNode(s, orbit, idxInRing, countInRing);
  });
}

// =====================================================================
// ANIMATION LOOP
// =====================================================================
function animLoop(t) {
  drawStars(t);
  drawOrbitals(t);
  requestAnimationFrame(animLoop);
}

window.addEventListener('resize', () => { initStars(); initOrb(); });
initStars();
initOrb();
requestAnimationFrame(animLoop);

// =====================================================================
// UPTIME TICKER
// =====================================================================
let uptimeSeconds = 0;
function tickUptime() {
  uptimeSeconds++;
  const d = Math.floor(uptimeSeconds / 86400);
  const h = Math.floor((uptimeSeconds % 86400) / 3600);
  const m = Math.floor((uptimeSeconds % 3600) / 60);
  let txt;
  if (d > 0) txt = d + 'd ' + String(h).padStart(2,'0') + 'h ' + String(m).padStart(2,'0') + 'm';
  else txt = String(h).padStart(2,'0') + 'h ' + String(m).padStart(2,'0') + 'm';
  document.getElementById('uptime').textContent = txt;
}
setInterval(tickUptime, 1000);

// =====================================================================
// RENDER HELPERS
// =====================================================================

function renderPositions(pos) {
  const container = document.getElementById('positionsContainer');
  const heatBar = document.getElementById('heatBar');
  if (!pos || !pos.length) {
    container.innerHTML = '<div class="no-pos">No open positions</div>';
    heatBar.style.display = 'none';
    return;
  }

  let totalHeat = 0;
  container.innerHTML = pos.map(p => {
    const dir = (p.direction || 'long').toLowerCase();
    const dirCls = dir === 'long' ? 'pdl' : 'pds';
    const dirLbl = dir === 'long' ? 'L' : 'S';
    const pnl = p.pnl || 0;
    const pnlCls = pnl >= 0 ? 'g' : 'r';
    const rMult = p.r_mult || 0;
    const rCls = rMult >= 0 ? 'g' : 'r';
    const tpHit = p.tp_hit || 0;
    totalHeat += Math.abs(pnl);

    // Age
    const ageS = p.opened_at ? Math.max(0, Math.floor(Date.now()/1000 - p.opened_at)) : 0;
    const ageH = Math.floor(ageS / 3600);
    const ageM = Math.floor((ageS % 3600) / 60);
    const ageStr = ageH > 0 ? ageH + 'h' + String(ageM).padStart(2,'0') + 'm' : ageM + 'm';

    const slug = encodeURIComponent(p.symbol);

    return '<a href="/position/' + slug + '" class="prow">'
      + '<span class="psym">' + esc(p.symbol) + '</span>'
      + '<span class="pdir ' + dirCls + '">' + dirLbl + '</span>'
      + '<span class="pent">$' + esc(p.entry) + '</span>'
      + '<span class="ppnl ' + pnlCls + '">' + (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2) + '</span>'
      + '<span class="prm ' + rCls + '">' + (rMult >= 0 ? '+' : '') + rMult.toFixed(2) + 'R</span>'
      + '<span class="ptp">'
        + '<span class="tps ' + (tpHit >= 1 ? 'tph' : 'tpm') + '"></span>'
        + '<span class="tps ' + (tpHit >= 2 ? 'tph' : 'tpm') + '"></span>'
        + '<span class="tps ' + (tpHit >= 3 ? 'tph' : 'tpm') + '"></span>'
      + '</span>'
      + '<span class="page">' + ageStr + '</span>'
      + '</a>';
  }).join('');

  // Heat bar
  heatBar.style.display = 'block';
  const heatPctVal = pos.length > 0 ? Math.min(100, (pos.length / 20) * 100) : 0;
  document.getElementById('heatPct').textContent = heatPctVal.toFixed(1) + '%';
  document.getElementById('heatFill').style.width = heatPctVal + '%';
}

function renderIntelCards(tradeLog, positions) {
  const container = document.getElementById('intelCards');
  const cards = [];
  const now = Date.now() / 1000;

  // POSITION OPENED cards from current open positions
  if (positions && positions.length) {
    positions.forEach(p => {
      const ageS = p.opened_at ? Math.max(0, Math.floor(now - p.opened_at)) : 0;
      const dir = (p.direction || 'long').toUpperCase();
      const dirCls = dir === 'LONG' ? 'hlg' : 'hlr';
      const rgm = (p.regime || 'range').toUpperCase();
      const sigStr = (p.signals || []).join(', ') || 'adaptive';
      const confStr = p.confidence ? p.confidence.toFixed(2) : '--';
      const timeStr = p.opened_at ? new Date(p.opened_at * 1000).toUTCString().slice(17, 22) + ' UTC' : '--';
      const seed = (p.symbol || '').length + Math.floor(p.entry || 0);

      cards.push({
        ts: p.opened_at || now,
        type: 'open',
        html: '<div class="tg-card">'
          + '<div class="tg-head"><span class="tg-type g">POSITION OPENED</span><span class="tg-time">' + timeStr + '</span></div>'
          + '<div class="tg-body">'
          + '<span class="' + dirCls + '">' + dir + '</span> <span class="hl">' + esc(p.symbol) + '</span> @ $' + esc(p.entry) + '<br>'
          + 'Conviction: ' + confStr + ' &middot; Regime: <span class="hlg">' + rgm + '</span><br>'
          + 'Signals: <span class="hl">' + esc(sigStr) + '</span>'
          + '</div>'
          + '<div class="tg-sig">"' + pickVoice(VOICE_GEN, seed) + '" &mdash; GoldenEye</div>'
          + '</div>'
      });
    });
  }

  // POSITION CLOSED cards from trade log
  if (tradeLog && tradeLog.length) {
    const recentTrades = tradeLog.slice(-10);
    recentTrades.forEach((tr, i) => {
      const exit = (tr.exit || '?').toUpperCase();
      const rv = tr.r || 0;
      const pv = tr.pnl || 0;
      const isWin = pv >= 0;
      const sym = tr.sym || '?';
      const ts = tr.t || now;
      const timeStr = new Date(ts * 1000).toUTCString().slice(17, 22) + ' UTC';
      const seed = sym.length + i;

      let voiceArr = isWin ? VOICE_WIN : VOICE_LOSS;
      if (exit === 'TP1') voiceArr = VOICE_TP1;

      const pnlCls = pv >= 0 ? 'hlg' : 'hlr';
      const exitCls = isWin ? 'g' : 'r';

      const detailParts = [];
      if (tr.detail && typeof tr.detail === 'object') {
        const d = tr.detail;
        if (d.entry) detailParts.push('Entry: $' + Number(d.entry).toFixed(2));
        if (d.exit_price) detailParts.push('Exit: $' + Number(d.exit_price).toFixed(2));
        if (d.duration_h) detailParts.push(Number(d.duration_h).toFixed(1) + 'h');
        if (d.sigs && d.sigs.length) detailParts.push('Sigs: ' + d.sigs.join(','));
      } else if (tr.detail && typeof tr.detail === 'string') {
        detailParts.push(esc(tr.detail));
      }

      cards.push({
        ts: ts,
        type: 'close',
        html: '<div class="tg-card">'
          + '<div class="tg-head"><span class="tg-type ' + exitCls + '">POSITION CLOSED</span><span class="tg-time">' + timeStr + '</span></div>'
          + '<div class="tg-body">'
          + '<span class="' + pnlCls + '">' + exit + '</span> <span class="hl">' + esc(sym) + '</span>'
          + ' &middot; PnL: <span class="' + pnlCls + '">' + (pv >= 0 ? '+' : '') + '$' + Math.abs(pv).toFixed(2) + '</span>'
          + ' &middot; R: <span class="' + pnlCls + '">' + (rv >= 0 ? '+' : '') + rv.toFixed(2) + 'R</span>'
          + (detailParts.length ? '<br>' + detailParts.join(' ') : '')
          + '</div>'
          + '<div class="tg-sig">"' + pickVoice(voiceArr, seed) + '" &mdash; GoldenEye</div>'
          + '</div>'
      });
    });
  }

  // Sort by timestamp descending, limit to 8
  cards.sort((a, b) => b.ts - a.ts);
  const shown = cards.slice(0, 8);

  if (!shown.length) {
    container.innerHTML = '<div class="no-pos">Awaiting signals...</div>';
    return;
  }

  container.innerHTML = shown.map(c => c.html).join('');
}

function renderRecentTrades(tradeLog) {
  const container = document.getElementById('recentTrades');
  if (!tradeLog || !tradeLog.length) {
    container.innerHTML = '<div style="font-size:10px;color:#3a3a4a;text-align:center">No closed trades</div>';
    return;
  }
  const now = Date.now() / 1000;
  const last6 = tradeLog.slice(-6).reverse();
  container.innerHTML = last6.map(t => {
    const exitType = (t.exit || '?').toUpperCase();
    const exitCls = (exitType === 'SL' || exitType.includes('DECAY') || exitType.includes('TIME')) ? 'r' : 'g';
    const rv = t.r || 0;
    const pv = t.pnl || 0;
    const rCls = rv >= 0 ? 'g' : 'r';
    const pCls = pv >= 0 ? 'g' : 'r';
    const ago = now - (t.t || now);
    let ageStr;
    if (ago < 3600) ageStr = Math.floor(ago / 60) + 'm';
    else if (ago < 86400) ageStr = (ago / 3600).toFixed(1) + 'h';
    else ageStr = Math.floor(ago / 86400) + 'd';

    return '<div class="trow">'
      + '<span class="tsym">' + esc(t.sym || '?') + '</span>'
      + '<span class="texit ' + exitCls + '">' + exitType + '</span>'
      + '<span class="tval ' + rCls + '">' + (rv >= 0 ? '+' : '') + rv.toFixed(2) + 'R</span>'
      + '<span class="tval ' + pCls + '">' + (pv >= 0 ? '+' : '') + '$' + Math.abs(pv).toFixed(2) + '</span>'
      + '<span class="tage">' + ageStr + '</span>'
      + '</div>';
  }).join('');
}

function renderLog(logs) {
  const container = document.getElementById('logContainer');
  if (!logs || !logs.length) {
    container.innerHTML = '<div style="font-size:10px;color:#3a3a4a">Waiting for events...</div>';
    return;
  }
  // Parse log lines and assign tags
  container.innerHTML = logs.slice(-15).reverse().map(line => {
    const str = String(line);
    let tag = 'SIG', tagCls = 'gd';
    const upper = str.toUpperCase();
    if (upper.includes('EXEC') || upper.includes('OPEN') || upper.includes('CLOSE') || upper.includes('FILLED')) { tag = 'EXEC'; tagCls = 'g'; }
    else if (upper.includes('CORR') || upper.includes('BLOCKED') || upper.includes('COOLDOWN')) { tag = 'CORR'; tagCls = 'am'; }
    else if (upper.includes('GATE') || upper.includes('REJECT') || upper.includes('ATR')) { tag = 'GATE'; tagCls = 'r'; }
    else if (upper.includes('REGIME') || upper.includes('HMM') || upper.includes('BULL') || upper.includes('BEAR')) { tag = 'RGM'; tagCls = 'pp'; }
    else if (upper.includes('WARN') || upper.includes('ERROR') || upper.includes('FAIL')) { tag = 'WARN'; tagCls = 'r'; }
    else if (upper.includes('EOD') || upper.includes('DAILY') || upper.includes('SUMMARY')) { tag = 'EOD'; tagCls = 'gd'; }

    // Try to extract time from beginning of line
    const timeMatch = str.match(/^(\d{2}:\d{2})/);
    const timeStr = timeMatch ? timeMatch[1] : '';
    const msgStr = timeMatch ? str.slice(timeMatch[0].length).trim() : str;

    return '<div class="lrow">'
      + '<span class="lts">' + esc(timeStr) + '</span>'
      + '<span class="ltag ' + tagCls + '">' + tag + '</span>'
      + '<span class="lmsg">' + esc(msgStr) + '</span>'
      + '</div>';
  }).join('');
}

// =====================================================================
// MAIN REFRESH LOOP
// =====================================================================
let _refreshing = false;
let _connected = false;

async function refresh() {
  if (_refreshing) return;
  _refreshing = true;
  try {
    const [tuiR, logR] = await Promise.all([
      fetch('/api/tui'),
      fetch('/api/logs')
    ]);
    if (!tuiR.ok || !logR.ok) throw new Error('API error');

    const d = await tuiR.json();
    const logs = await logR.json();
    const h = d.health || {};
    const a = d.analytics || {};
    const pos = d.positions || [];
    const syms = d.symbols || [];
    const isLive = h.trading_mode === 'live';

    // --- Connection status ---
    const kOk = h.status === 'healthy' || h.status === 'ok' || h.kraken_api === 'connected';
    const connEl = document.getElementById('connStatus');
    const dotEl = document.getElementById('connDot');
    const lblEl = document.getElementById('connLabel');
    const badgeEl = document.getElementById('modeBadge');
    if (kOk) {
      connEl.className = 'conn online';
      dotEl.className = 'cdot online';
      lblEl.textContent = isLive ? 'KRAKEN LIVE' : 'KRAKEN PAPER';
      _connected = true;
    } else {
      connEl.className = 'conn offline';
      dotEl.className = 'cdot offline';
      lblEl.textContent = 'OFFLINE';
      _connected = false;
    }
    badgeEl.textContent = isLive ? 'LIVE' : 'PAPER';
    badgeEl.className = 'lbadge ' + (isLive ? 'live' : 'paper');

    // --- Uptime ---
    if (h.uptime_seconds) uptimeSeconds = Math.floor(h.uptime_seconds);

    // --- Header stats ---
    const bal = isLive ? (h.live_balance || 0) : (h.paper_balance || 0);
    const tradeAmt = (h.live_trade_amt || (bal * 0.03)) || 0;
    const startBal = isLive ? (h.live_balance || 100) : 10000;
    const totalPnl = a.total_pnl || 0;
    const trades = a.trades || 0;
    const wins = a.wins || 0;
    const wr = trades > 0 ? (wins / trades * 100) : 0;

    document.getElementById('tradeAmt').textContent = '$' + Math.round(tradeAmt);

    const balEl = document.getElementById('hdrBalance');
    balEl.textContent = '$' + bal.toFixed(2);
    balEl.className = 'stv ' + (bal >= startBal ? 'g' : 'r');

    const pnlEl = document.getElementById('hdrPnl');
    pnlEl.textContent = (totalPnl >= 0 ? '+' : '') + '$' + totalPnl.toFixed(2);
    pnlEl.className = 'stv ' + (totalPnl >= 0 ? 'g' : 'r');

    const wrEl = document.getElementById('hdrWr');
    wrEl.textContent = trades > 0 ? wr.toFixed(1) + '%' : '--';
    wrEl.className = 'stv ' + (wr >= 55 ? 'g' : wr > 0 ? 'am' : 'w');

    document.getElementById('hdrOpen').textContent = (h.open_positions || pos.length);

    // --- Core ring (center of orbital) ---
    document.getElementById('coreLbl').textContent = isLive ? 'LIVE BALANCE' : 'PAPER BALANCE';
    document.getElementById('coreBal').textContent = '$' + bal.toFixed(2);
    const corePnlEl = document.getElementById('corePnl');
    corePnlEl.textContent = (totalPnl >= 0 ? '+' : '') + '$' + totalPnl.toFixed(2);
    corePnlEl.className = 'core-pnl ' + (totalPnl >= 0 ? 'g' : 'r');

    const maxDD = a.max_drawdown || 0;
    document.getElementById('coreSub').textContent = trades + ' trades lifetime \u00b7 ' + (trades > 0 ? wr.toFixed(0) + '% WR' : 'no WR') + ' \u00b7 ' + maxDD.toFixed(1) + '% DD';

    // --- Symbol data for orbital canvas ---
    symbolData = syms.map(s => {
      const matchPos = pos.find(p => p.symbol === s.symbol);
      return {
        symbol: s.symbol,
        price: s.price,
        regime: s.regime,
        sig: s.sig || 0,
        has_pos: s.has_pos || !!matchPos,
        pnl: matchPos ? (matchPos.pnl || 0) : 0
      };
    });

    // --- Bottom panels ---
    renderPositions(pos);
    renderIntelCards(a.trade_log || [], pos);
    renderRecentTrades(a.trade_log || []);
    renderLog(logs);

    // --- Analytics ---
    document.getElementById('anTrades').textContent = trades;
    document.getElementById('anTrades').className = 'av w';
    document.getElementById('anTradesSub').textContent = wins + 'W / ' + (a.losses || 0) + 'L';

    const anWrEl = document.getElementById('anWr');
    anWrEl.textContent = trades > 0 ? wr.toFixed(1) + '%' : '--';
    anWrEl.className = 'av ' + (wr >= 55 ? 'g' : wr > 0 ? 'am' : 'w');

    const avgR = a.avg_r || 0;
    const anAvgREl = document.getElementById('anAvgR');
    anAvgREl.textContent = (avgR >= 0 ? '+' : '') + avgR.toFixed(2) + 'R';
    anAvgREl.className = 'av ' + (avgR > 0 ? 'g' : avgR < 0 ? 'r' : 'w');

    const anPnlEl = document.getElementById('anPnl');
    anPnlEl.textContent = (totalPnl >= 0 ? '+' : '') + '$' + totalPnl.toFixed(2);
    anPnlEl.className = 'av ' + (totalPnl >= 0 ? 'g' : 'r');
    const pnlPct = startBal > 0 ? (totalPnl / startBal * 100) : 0;
    document.getElementById('anPnlSub').textContent = pnlPct.toFixed(1) + '% return';

    const anDDEl = document.getElementById('anDD');
    anDDEl.textContent = maxDD.toFixed(1) + '%';
    anDDEl.className = 'av ' + (maxDD > 10 ? 'r' : maxDD > 5 ? 'am' : 'g');

    const oPos = h.open_positions || pos.length;
    const mPos = h.max_positions || 20;
    document.getElementById('anPos').textContent = oPos + ' / ' + mPos;

    // --- AI engine ---
    document.getElementById('dqnStatus').innerHTML = kOk
      ? '<span class="g">\u25CF online</span>'
      : '<span class="r">\u25CF offline</span>';

    const ddm = h.drawdown_mult != null ? h.drawdown_mult : 1.0;
    const ddmEl = document.getElementById('aiDdMult');
    ddmEl.textContent = ddm.toFixed(2) + 'x';
    ddmEl.className = ddm >= 1 ? 'w' : ddm >= 0.5 ? 'am' : 'r';

    const cbActive = h.circuit_breaker_active;
    const cbEl = document.getElementById('aiCb');
    cbEl.textContent = cbActive ? 'ACTIVE' : 'OFF';
    cbEl.className = cbActive ? 'r' : 'dm';

  } catch(e) {
    console.error('Dashboard refresh error:', e);
    if (_connected) {
      _connected = false;
      document.getElementById('connStatus').className = 'conn offline';
      document.getElementById('connDot').className = 'cdot offline';
      document.getElementById('connLabel').textContent = 'OFFLINE';
      document.getElementById('dqnStatus').innerHTML = '<span class="r">\u25CF offline</span>';
    }
    document.getElementById('logContainer').innerHTML =
      '<div class="lrow"><span class="lmsg" style="color:#ff4757">Dashboard offline \u2014 is trekbot.py running?</span></div>';
  } finally {
    _refreshing = false;
  }
}

// INIT
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""

# =============================================================================
# SYMBOL DETAIL  /symbol/<sym>  -- Per-pair analytics
# =============================================================================

SYMBOL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ORACLE \u2013 {{ symbol }}</title>
  {{ fonts | safe }}
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
  <style>{{ style | safe }}
    body { overflow-y: auto; font-family: 'Outfit', system-ui, sans-serif; font-weight: 300; }
    .sym-header { display: flex; align-items: center; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
    .sym-name { font-size: 2rem; font-weight: 700; color: var(--sym); }
    .sym-price { font-size: 1.62rem; font-weight: 600; }
    .pos-card { background: var(--bg2); border: 1px solid var(--accent); border-radius: 8px; padding: 16px; margin-bottom: 20px; }
    .pos-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px; }
    .pos-lbl { color: var(--muted); font-size: .85rem; text-transform: uppercase; letter-spacing: .05em; }
    .pos-val { font-weight: 600; margin-top: 2px; }
    .dir-dots { display: flex; gap: 4px; margin-bottom: 20px; }
    .dir-dot { width: 18px; height: 18px; border-radius: 3px; }
    .dir-dot-w { background: var(--green); }
    .dir-dot-l { background: var(--red); }
    .dir-dot-empty { background: var(--bg3); border: 1px solid var(--border); }
  </style>
</head>
<body>
  <div class="container fade-in">
    <a href="/" class="back-link">&#8592; Command Center</a>

    <div class="sym-header">
      <span class="sym-name" id="sym-name">{{ symbol }}</span>
      <span class="sym-price" id="sym-price">--</span>
      <span class="badge badge-dim" id="sym-regime">--</span>
      <span class="mono" id="sym-wr" style="font-weight:600;font-size:1.12rem">--</span>
    </div>

    <div id="pos-section" style="display:none">
      <div class="sec-title">Open Position</div>
      <div class="pos-card"><div class="pos-grid" id="pos-grid">Loading...</div></div>
    </div>

    <div class="sec-title">Indicators</div>
    <div class="ind-grid" id="ind-grid">Loading...</div>

    <div class="sec-title">Last 10 Long Trades</div>
    <div class="dir-dots" id="dir-dots"></div>

    <div class="sec-title">Signal Performance</div>
    <div class="card" style="margin-bottom:16px">
      <canvas id="sigChart" height="120"></canvas>
    </div>

    <div class="card" style="padding:0;overflow:hidden;margin-bottom:24px">
      <table>
        <thead><tr><th>Sig</th><th>Description</th><th style="text-align:right">W/F</th><th style="text-align:right">WR%</th></tr></thead>
        <tbody id="sig-body"><tr><td colspan="4" class="no-data">Loading...</td></tr></tbody>
      </table>
    </div>
  </div>

  <script>
  const SIG_LABELS = {{ sig_labels | tojson }};
  const SYMBOL = {{ symbol | tojson }};
  const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  let chart = null;

  let _refreshing = false;
  async function refresh() {
    if (_refreshing) return;
    _refreshing = true;
    try {
      const r = await fetch('/api/symbol/' + encodeURIComponent(SYMBOL));
      const d = await r.json();
      if (d.error) { document.getElementById('sym-name').textContent = SYMBOL + ' (not found)'; return; }

      // Header
      const pc = d.up ? 'up' : 'dn';
      const arrow = d.up ? '\\u2191' : '\\u2193';
      document.getElementById('sym-price').className = 'sym-price ' + pc;
      document.getElementById('sym-price').textContent = arrow + ' $' + d.price;
      const rgmEl = document.getElementById('sym-regime');
      rgmEl.textContent = d.regime;
      rgmEl.className = 'badge badge-' + ({bull:'green',bear:'red',range:'amber',chop:'dim'}[d.regime]||'dim');
      const wrEl = document.getElementById('sym-wr');
      wrEl.textContent = d.wr > 0 ? d.wr.toFixed(0) + '% WR' : '-- WR';
      wrEl.style.color = d.wr >= 50 ? 'var(--green)' : d.wr > 0 ? 'var(--amber)' : 'var(--dim)';

      // Position
      const pSec = document.getElementById('pos-section');
      if (d.position) {
        pSec.style.display = '';
        const p = d.position;
        const tpS = (lvl, val) => {
          if (!val) return '<span class="d">\\u2014</span>';
          return '<span class="' + (p.tp_hit >= lvl ? 'g bold' : 'm') + '">$' + esc(val) + (p.tp_hit >= lvl ? ' \\u2713' : '') + '</span>';
        };
        const ageS = p.opened_at ? Math.floor(Date.now()/1000 - p.opened_at) : 0;
        const ageH = Math.floor(ageS/3600), ageM = Math.floor((ageS%3600)/60);
        const age = ageH > 0 ? ageH+'h '+ageM+'m' : ageM+'m';
        const plC = p.pnl >= 0 ? 'g' : 'r';
        const plS = (p.pnl >= 0 ? '+' : '') + p.pnl.toFixed(2);
        document.getElementById('pos-grid').innerHTML =
          '<div><div class="pos-lbl">Direction</div><div class="pos-val g bold">'+p.direction.toUpperCase()+'</div></div>'
          +'<div><div class="pos-lbl">Entry</div><div class="pos-val t">$'+esc(p.entry)+'</div></div>'
          +'<div><div class="pos-lbl">P/L</div><div class="pos-val '+plC+'">$'+plS+'</div></div>'
          +'<div><div class="pos-lbl">Stop Loss</div><div class="pos-val r">$'+esc(p.sl)+'</div></div>'
          +'<div><div class="pos-lbl">TP1</div><div class="pos-val">'+tpS(1,p.tp1)+'</div></div>'
          +'<div><div class="pos-lbl">TP2</div><div class="pos-val">'+tpS(2,p.tp2)+'</div></div>'
          +'<div><div class="pos-lbl">TP3</div><div class="pos-val">'+tpS(3,p.tp3)+'</div></div>'
          +'<div><div class="pos-lbl">Age</div><div class="pos-val t">'+age+'</div></div>'
          +'<div><div class="pos-lbl">Signals</div><div class="pos-val gld">'+(p.signals||[]).join(', ').toUpperCase()+'</div></div>'
          +'<div><div class="pos-lbl">Confidence</div><div class="pos-val t">'+(p.confidence*100).toFixed(0)+'%</div></div>';
      } else { pSec.style.display = 'none'; }

      // Indicators
      const ind = d.indicators || {};
      const items = [
        {k:'rsi', l:'RSI', c: v=>v>70?'var(--red)':v<30?'var(--green)':'var(--text)'},
        {k:'macd_hist', l:'MACD Hist', c: v=>v>=0?'var(--green)':'var(--red)'},
        {k:'atr_pct', l:'ATR %', c:()=>'var(--text)', f:v=>(v*100).toFixed(2)+'%'},
        {k:'adx', l:'ADX', c: v=>v>25?'var(--green)':'var(--dim)'},
        {k:'bb_pctb', l:'BB %B', c: v=>v>0.8?'var(--amber)':v<0.2?'var(--green)':'var(--text)', f:v=>v.toFixed(3)},
        {k:'mfi', l:'MFI', c: v=>v<20?'var(--green)':v>80?'var(--red)':'var(--text)'},
        {k:'stoch_k', l:'Stoch %K', c: v=>v<20?'var(--green)':v>80?'var(--red)':'var(--text)'},
        {k:'e9', l:'EMA 9', c:()=>'var(--text)'},
        {k:'e21', l:'EMA 21', c:()=>'var(--text)'},
        {k:'e200', l:'EMA 200', c:()=>'var(--text)'},
      ];
      document.getElementById('ind-grid').innerHTML = items.map(i => {
        const v = ind[i.k];
        const val = v != null ? (i.f ? i.f(v) : v.toFixed(2)) : '\\u2014';
        const color = v != null ? i.c(v) : 'var(--dim)';
        return '<div class="ind-item"><div class="ind-label">'+i.l+'</div><div class="ind-val" style="color:'+color+'">'+val+'</div></div>';
      }).join('');

      // Dir history dots
      const dh = (d.dir_history||{}).long || [];
      const dots = [];
      for (let i=0; i<10; i++) {
        if (i < dh.length) dots.push('<div class="dir-dot '+(dh[i]?'dir-dot-w':'dir-dot-l')+'"></div>');
        else dots.push('<div class="dir-dot dir-dot-empty"></div>');
      }
      document.getElementById('dir-dots').innerHTML = dots.join('');

      // Signal table + chart
      const ps = d.per_signal || {};
      const letters = 'abcdefghijklmnopqrstuvwxyz2';
      const rows = [], cLabels = [], cWrs = [];
      letters.split('').forEach(s => {
        const sd = ps[s] || {f:0,w:0};
        const wr = sd.f > 0 ? (sd.w/sd.f*100) : 0;
        const wrs = sd.f > 0 ? wr.toFixed(0) : '\\u2014';
        const wrc = sd.f > 0 ? (wr >= 50 ? 'g' : 'a') : 'd';
        rows.push('<tr><td class="mono bold">'+s.toUpperCase()+'</td><td class="m">'+(SIG_LABELS[s]||'')+'</td>'
          +'<td style="text-align:right" class="mono">'+(sd.f > 0 ? '<span class="g">'+sd.w+'</span><span class="d">/'+sd.f+'</span>' : '<span class="d">0/0</span>')+'</td>'
          +'<td style="text-align:right" class="'+wrc+'">'+(sd.f>0?wrs+'%':'\\u2014')+'</td></tr>');
        if (sd.f > 0) { cLabels.push(s.toUpperCase()); cWrs.push(+wr.toFixed(1)); }
      });
      document.getElementById('sig-body').innerHTML = rows.join('');

      if (chart) {
        chart.data.labels = cLabels;
        chart.data.datasets[0].data = cWrs;
        chart.data.datasets[0].backgroundColor = cWrs.map(w=>w>=50?'rgba(34,197,94,.35)':'rgba(245,158,11,.35)');
        chart.data.datasets[0].borderColor = cWrs.map(w=>w>=50?'#22c55e':'#f59e0b');
        chart.update();
      } else {
        chart = new Chart(document.getElementById('sigChart').getContext('2d'), {
          type:'bar', data:{labels:cLabels, datasets:[{label:'Win Rate %', data:cWrs,
            backgroundColor:cWrs.map(w=>w>=50?'rgba(34,197,94,.35)':'rgba(245,158,11,.35)'),
            borderColor:cWrs.map(w=>w>=50?'#22c55e':'#f59e0b'), borderWidth:1, borderRadius:3}]},
          options:{responsive:true, plugins:{legend:{display:false}},
            scales:{y:{min:0,max:100,grid:{color:'#252530'},ticks:{color:'#8a8a9e',callback:v=>v+'%'}},
                    x:{grid:{color:'#252530'},ticks:{color:'#8a8a9e'}}}}
        });
      }
    } catch(e) { console.error('refresh error', e); }
    finally { _refreshing = false; }
  }
  refresh(); setInterval(refresh, 2000);
  </script>
</body>
</html>"""

# =============================================================================
# SIGNAL DETAIL  /signal/<sig>  -- Per-signal analytics across all symbols
# =============================================================================

SIGNAL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ORACLE \u2013 Signal {{ signal.upper() }}</title>
  {{ fonts | safe }}
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
  <style>{{ style | safe }}
    body { overflow-y: auto; font-family: 'Outfit', system-ui, sans-serif; font-weight: 300; }
    .sig-header { display: flex; align-items: center; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
    .sig-letter { font-size: 2.5rem; font-weight: 700; color: var(--accent); }
    .sig-name { font-size: 1.5rem; font-weight: 600; color: var(--text); }
    .stat-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 20px; }
    .stat-card { background: var(--bg2); border: 1px solid var(--border); border-radius: 6px; padding: 12px; text-align: center; }
    .stat-lbl { color: var(--muted); font-size: .85rem; text-transform: uppercase; letter-spacing: .05em; }
    .stat-val { font-size: 1.62rem; font-weight: 700; margin-top: 4px; }
  </style>
</head>
<body>
  <div class="container fade-in">
    <a href="/" class="back-link">&#8592; Command Center</a>

    <div class="sig-header">
      <span class="sig-letter" id="sig-letter">{{ signal.upper() }}</span>
      <span class="sig-name" id="sig-name">Loading...</span>
    </div>

    <div class="stat-row" id="stat-row">
      <div class="stat-card"><div class="stat-lbl">Total Fired</div><div class="stat-val t" id="st-fired">--</div></div>
      <div class="stat-card"><div class="stat-lbl">Total Wins</div><div class="stat-val g" id="st-wins">--</div></div>
      <div class="stat-card"><div class="stat-lbl">Total Losses</div><div class="stat-val r" id="st-losses">--</div></div>
      <div class="stat-card"><div class="stat-lbl">Win Rate</div><div class="stat-val" id="st-wr">--</div></div>
    </div>

    <div class="sec-title">Win Rate by Symbol</div>
    <div class="card" style="margin-bottom:16px">
      <canvas id="symChart" height="140"></canvas>
    </div>

    <div class="sec-title">Per-Symbol Breakdown</div>
    <div class="card" style="padding:0;overflow:hidden;margin-bottom:24px">
      <table>
        <thead><tr><th>Symbol</th><th style="text-align:right">Fired</th><th style="text-align:right">Wins</th><th style="text-align:right">Losses</th><th style="text-align:right">WR%</th></tr></thead>
        <tbody id="sym-body"><tr><td colspan="5" class="no-data">Loading...</td></tr></tbody>
      </table>
    </div>
  </div>

  <script>
  const SIG = {{ signal | tojson }};
  const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  let chart = null;

  let _refreshing = false;
  async function refresh() {
    if (_refreshing) return;
    _refreshing = true;
    try {
      const r = await fetch('/api/signal/' + SIG);
      const d = await r.json();
      if (d.error) return;

      document.getElementById('sig-name').textContent = d.name;
      document.getElementById('st-fired').textContent = d.total_fired;
      document.getElementById('st-wins').textContent = d.total_wins;
      document.getElementById('st-losses').textContent = d.total_fired - d.total_wins;
      const wrEl = document.getElementById('st-wr');
      wrEl.textContent = d.wr > 0 ? d.wr.toFixed(1) + '%' : '\\u2014';
      wrEl.style.color = d.wr >= 50 ? 'var(--green)' : d.wr > 0 ? 'var(--amber)' : 'var(--dim)';

      // Table -- sorted by fired desc
      const syms = (d.per_symbol || []).filter(s => s.fired > 0).sort((a,b) => b.fired - a.fired);
      if (syms.length) {
        document.getElementById('sym-body').innerHTML = syms.map(s => {
          const wrc = s.wr >= 50 ? 'g' : s.wr > 0 ? 'a' : 'd';
          return '<tr><td><a href="/symbol/'+encodeURIComponent(s.symbol)+'" style="color:var(--sym);text-decoration:none;border-bottom:1px dotted var(--dim)">'+esc(s.symbol)+'</a></td>'
            +'<td style="text-align:right" class="mono">'+s.fired+'</td>'
            +'<td style="text-align:right" class="mono g">'+s.wins+'</td>'
            +'<td style="text-align:right" class="mono r">'+(s.fired-s.wins)+'</td>'
            +'<td style="text-align:right" class="mono '+wrc+'">'+s.wr.toFixed(1)+'%</td></tr>';
        }).join('');
      } else {
        document.getElementById('sym-body').innerHTML = '<tr><td colspan="5" class="no-data">No data yet</td></tr>';
      }

      // Chart
      const cLabels = syms.map(s => s.symbol);
      const cWrs = syms.map(s => s.wr);
      if (chart) {
        chart.data.labels = cLabels;
        chart.data.datasets[0].data = cWrs;
        chart.data.datasets[0].backgroundColor = cWrs.map(w=>w>=50?'rgba(34,197,94,.35)':'rgba(245,158,11,.35)');
        chart.data.datasets[0].borderColor = cWrs.map(w=>w>=50?'#22c55e':'#f59e0b');
        chart.update();
      } else if (cLabels.length) {
        chart = new Chart(document.getElementById('symChart').getContext('2d'), {
          type:'bar', data:{labels:cLabels, datasets:[{label:'Win Rate %', data:cWrs,
            backgroundColor:cWrs.map(w=>w>=50?'rgba(34,197,94,.35)':'rgba(245,158,11,.35)'),
            borderColor:cWrs.map(w=>w>=50?'#22c55e':'#f59e0b'), borderWidth:1, borderRadius:3}]},
          options:{responsive:true, plugins:{legend:{display:false}},
            scales:{y:{min:0,max:100,grid:{color:'#252530'},ticks:{color:'#8a8a9e',callback:v=>v+'%'}},
                    x:{grid:{color:'#252530'},ticks:{color:'#8a8a9e',maxRotation:45}}}}
        });
      }
    } catch(e) { console.error('refresh error', e); }
    finally { _refreshing = false; }
  }
  refresh(); setInterval(refresh, 2000);
  </script>
</body>
</html>"""

# =============================================================================
# POSITION DETAIL  /position/<sym>  -- Live TP progress & trade status
# =============================================================================

POSITION_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ORACLE \u2013 Position {{ symbol }}</title>
  {{ fonts | safe }}
  <style>{{ style | safe }}
    body { overflow-y: auto; font-family: 'Outfit', system-ui, sans-serif; font-weight: 300; }
    .pos-header { display: flex; align-items: center; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
    .pos-sym { font-size: 2rem; font-weight: 700; color: var(--sym); }
    .pos-dir { font-size: 1.38rem; font-weight: 700; }
    .pos-price { font-size: 1.62rem; font-weight: 600; }
    .no-pos { color: var(--dim); text-align: center; padding: 60px 20px; font-size: 1.38rem; }
    .clock-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin-bottom: 20px; }
    .clock-card { background: var(--bg2); border: 1px solid var(--border); border-radius: 6px; padding: 14px; }
    .clock-lbl { color: var(--muted); font-size: .85rem; text-transform: uppercase; letter-spacing: .05em; }
    .clock-val { font-size: 1.75rem; font-weight: 700; margin-top: 4px; }
    .clock-sub { color: var(--dim); font-size: .9rem; margin-top: 2px; }
  </style>
</head>
<body>
  <div class="container fade-in">
    <a href="/" class="back-link">&#8592; Command Center</a>
    <div id="content">
      <div class="no-pos" id="no-pos">Loading position data...</div>
    </div>
  </div>

  <script>
  const SIG_LABELS = {{ sig_labels | tojson }};
  const SYMBOL = {{ symbol | tojson }};
  const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

  function fmtAge(s) {
    const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
    return h > 0 ? h+'h '+String(m).padStart(2,'0')+'m' : m+'m';
  }

  function buildProgressBar(d) {
    const lo = d.sl_raw, hi = d.tp3_raw || d.tp2_raw || d.tp1_raw || d.entry_raw * 1.05;
    const range = hi - lo || 1;
    const pct = v => Math.max(0, Math.min(100, (v - lo) / range * 100));
    const pp = pct(d.price_raw);
    const fillC = d.price_raw >= d.entry_raw ? 'var(--green)' : 'var(--red)';
    const markers = [
      {v: d.sl_raw, l:'SL', p:pct(d.sl_raw), c:'var(--red)', pv:d.sl},
      {v: d.entry_raw, l:'Entry', p:pct(d.entry_raw), c:'var(--muted)', pv:d.entry},
    ];
    if (d.tp1_raw) markers.push({v:d.tp1_raw, l:'TP1', p:pct(d.tp1_raw), c:d.tp_hit>=1?'var(--green)':'var(--dim)', pv:d.tp1});
    if (d.tp2_raw) markers.push({v:d.tp2_raw, l:'TP2', p:pct(d.tp2_raw), c:d.tp_hit>=2?'var(--green)':'var(--dim)', pv:d.tp2});
    if (d.tp3_raw) markers.push({v:d.tp3_raw, l:'TP3', p:pct(d.tp3_raw), c:d.tp_hit>=3?'var(--green)':'var(--dim)', pv:d.tp3});
    let h = '<div class="tp-track" style="margin-top:20px;margin-bottom:24px">';
    h += '<div class="tp-fill" style="width:'+pp+'%;background:'+fillC+';opacity:.15"></div>';
    markers.forEach(m => {
      h += '<div class="tp-marker" style="left:'+m.p+'%"><div class="tp-marker-line" style="background:'+m.c+'"></div>'
        +'<div class="tp-marker-label" style="color:'+m.c+'">'+m.l+'</div>'
        +'<div class="tp-marker-val" style="color:'+m.c+'">$'+esc(m.pv)+'</div></div>';
    });
    h += '<div class="price-needle" style="left:'+pp+'%"></div>';
    h += '<div class="price-tag" style="left:'+pp+'%;background:'+(d.pnl>=0?'var(--green)':'var(--red)')+';color:var(--bg)">$'+esc(d.price)+'</div>';
    h += '</div>';
    return h;
  }

  let _refreshing = false;
  async function refresh() {
    if (_refreshing) return;
    _refreshing = true;
    try {
      const r = await fetch('/api/position/' + encodeURIComponent(SYMBOL));
      const d = await r.json();
      if (d.error) {
        document.getElementById('content').innerHTML = '<div class="no-pos">No open position for '+esc(SYMBOL)+'<br><a href="/symbol/'+encodeURIComponent(SYMBOL)+'" style="color:var(--accent);margin-top:12px;display:inline-block">View symbol page &#8594;</a></div>';
        return;
      }
      const pc = d.up ? 'up' : 'dn';
      const arrow = d.up ? '\\u2191' : '\\u2193';
      const plC = d.pnl >= 0 ? 'g' : 'r';
      const plS = (d.pnl >= 0 ? '+' : '') + d.pnl.toFixed(2);
      const rC = d.r_mult >= 0 ? 'g' : 'r';

      let html = '<div class="pos-header">'
        +'<span class="pos-sym"><a href="/symbol/'+encodeURIComponent(d.symbol)+'" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--dim)">'+esc(d.symbol)+'</a></span>'
        +'<span class="pos-dir g bold">'+d.direction.toUpperCase()+'</span>'
        +'<span class="pos-price '+pc+'">'+arrow+' $'+esc(d.price)+'</span>'
        +'<span class="badge badge-'+(d.pnl>=0?'green':'red')+'">$'+plS+'</span>'
        +'<span class="badge badge-'+(d.regime==='bull'?'green':d.regime==='bear'?'red':'amber')+'">'+esc(d.regime)+'</span>'
        +'</div>';

      // TP progress bar
      html += '<div class="sec-title">TP Progress</div>';
      html += buildProgressBar(d);

      // Summary cards
      html += '<div class="sum-grid">'
        +'<div class="sum-card"><div class="sum-lbl">P/L</div><div class="sum-val '+plC+'">$'+plS+'</div></div>'
        +'<div class="sum-card"><div class="sum-lbl">P/L %</div><div class="sum-val '+plC+'">'+((d.pnl_pct||0)>=0?'+':'')+((d.pnl_pct||0)).toFixed(2)+'%</div></div>'
        +'<div class="sum-card"><div class="sum-lbl">R-Multiple</div><div class="sum-val '+rC+'">'+(d.r_mult||0).toFixed(2)+'R</div></div>'
        +'<div class="sum-card"><div class="sum-lbl">Size</div><div class="sum-val t">'+(d.size||0).toFixed(4)+'</div></div>'
        +'<div class="sum-card"><div class="sum-lbl">Confidence</div><div class="sum-val t">'+((d.confidence||0)*100).toFixed(0)+'%</div></div>'
        +'<div class="sum-card"><div class="sum-lbl">Auction Score</div><div class="sum-val t">'+(d.auction_score||0).toFixed(2)+'</div></div>'
        +'</div>';

      // 6-Factor Scores (includes whale score via order_flow)
      if (d.factors) {
        const fLabels = {trend:'Trend',momentum:'Momentum',structure:'Structure',volume:'Volume',volatility:'Volatility',order_flow:'Order Flow'};
        const fColors = {trend:'#c9a84c',momentum:'#b8884a',structure:'#8a8a9e',volume:'#5a8a9a',volatility:'#9a6a3a',order_flow:'#22c55e'};
        html += '<div class="sec-title">Entry Factor Scores</div><div class="card" style="padding:12px;margin-bottom:20px">';
        Object.entries(fLabels).forEach(([k,lbl]) => {
          const v = d.factors[k];
          if (v == null) return;
          const pct = Math.round(v * 100);
          const c = fColors[k] || 'var(--text)';
          html += '<div style="display:flex;align-items:center;gap:10px;padding:5px 0">'
            +'<span style="width:90px;color:var(--muted);font-size:1.02rem;text-transform:uppercase;letter-spacing:.04em">'+lbl+'</span>'
            +'<div style="flex:1;background:var(--bg);border-radius:3px;height:14px;overflow:hidden"><div style="height:14px;border-radius:3px;width:'+pct+'%;background:'+c+';opacity:.7"></div></div>'
            +'<span style="width:45px;text-align:right;font-weight:600;font-size:1.06rem;color:'+c+'">'+v.toFixed(2)+'</span>'
            +'</div>';
        });
        if (d.ai_score != null) html += '<div style="display:flex;align-items:center;gap:10px;padding:5px 0"><span style="width:90px;color:var(--muted);font-size:1.02rem;text-transform:uppercase;letter-spacing:.04em">FinBERT</span><div style="flex:1;background:var(--bg);border-radius:3px;height:14px;overflow:hidden"><div style="height:14px;border-radius:3px;width:'+Math.round(d.ai_score*100)+'%;background:#e879f9;opacity:.7"></div></div><span style="width:45px;text-align:right;font-weight:600;font-size:1.06rem;color:#e879f9">'+d.ai_score.toFixed(2)+'</span></div>';
        if (d.llm_score != null) html += '<div style="display:flex;align-items:center;gap:10px;padding:5px 0"><span style="width:90px;color:var(--muted);font-size:1.02rem;text-transform:uppercase;letter-spacing:.04em">Ollama</span><div style="flex:1;background:var(--bg);border-radius:3px;height:14px;overflow:hidden"><div style="height:14px;border-radius:3px;width:'+Math.round(d.llm_score*100)+'%;background:#fb923c;opacity:.7"></div></div><span style="width:45px;text-align:right;font-weight:600;font-size:1.06rem;color:#fb923c">'+d.llm_score.toFixed(2)+'</span></div>';
        html += '</div>';

        // Order Flow Sub-Components (Whale Score, Funding Rate, Order Book)
        const hasOF = d.whale_score != null || d.funding_rate != null || d.of_score != null;
        if (hasOF) {
          html += '<div class="sec-title">Order Flow Components</div><div class="sum-grid">';
          if (d.whale_score != null) {
            const wsC = d.whale_score >= 60 ? 'var(--green)' : d.whale_score >= 30 ? 'var(--amber)' : 'var(--dim)';
            html += '<div class="sum-card"><div class="sum-lbl">&#x1F433; Whale Score</div><div class="sum-val" style="color:'+wsC+'">'+d.whale_score.toFixed(0)+'/100</div></div>';
          }
          if (d.of_score != null) {
            const obC = d.of_score >= 0.6 ? 'var(--green)' : d.of_score <= 0.4 ? 'var(--red)' : 'var(--muted)';
            html += '<div class="sum-card"><div class="sum-lbl">Order Book</div><div class="sum-val" style="color:'+obC+'">'+d.of_score.toFixed(3)+'</div></div>';
          }
          if (d.funding_rate != null) {
            const frPct = (d.funding_rate * 100).toFixed(4);
            const frC = d.funding_rate < 0 ? 'var(--green)' : d.funding_rate > 0.0001 ? 'var(--red)' : 'var(--muted)';
            html += '<div class="sum-card"><div class="sum-lbl">Funding Rate</div><div class="sum-val" style="color:'+frC+'">'+frPct+'%</div></div>';
          }
          html += '</div>';
        }
      }

      // SL/TP levels detail
      html += '<div class="sec-title">Levels</div><div class="card" style="padding:12px;margin-bottom:20px">';
      const slMoved = d.sl !== d.orig_sl;
      let slNote = '';
      if (d.trail_active) slNote = 'Trailing (Keltner EMA21-ATR)';
      else if (slMoved) slNote = 'Moved to breakeven+0.5ATR';
      else slNote = 'Original';
      html += '<div class="level-row"><span class="level-tag r">SL</span><span class="level-price">$'+esc(d.sl)+'</span><span class="level-note">'+slNote+'</span></div>';
      if (slMoved) html += '<div class="level-row"><span class="level-tag d">Orig SL</span><span class="level-price d">$'+esc(d.orig_sl)+'</span><span class="level-note">Entry stop</span></div>';
      [['tp1','TP1','0.75R',1],['tp2','TP2','1.25R',2],['tp3','TP3','2.0R',3]].forEach(([k,lbl,desc,lvl]) => {
        const v = d[k];
        if (!v) return;
        const hit = d.tp_hit >= lvl;
        const cls = hit ? 'g bold' : 'm';
        const check = hit ? '<span class="level-check g">\\u2713</span>' : '<span class="level-check d">\\u2014</span>';
        html += '<div class="level-row"><span class="level-tag gld">'+lbl+'</span><span class="level-price '+cls+'">$'+esc(v)+'</span>'+check+'<span class="level-note">'+desc+'</span></div>';
      });
      html += '</div>';

      // Time clocks
      html += '<div class="sec-title">Time Clocks</div><div class="clock-grid">';
      html += '<div class="clock-card"><div class="clock-lbl">Position Age</div><div class="clock-val t">'+fmtAge(d.age_s)+'</div></div>';
      if (d.tp1_deadline_h !== null && d.tp1_deadline_h !== undefined) {
        const urgC = d.tp1_deadline_h < 12 ? 'r' : d.tp1_deadline_h < 24 ? 'a' : 'g';
        html += '<div class="clock-card"><div class="clock-lbl">TP1 Deadline</div><div class="clock-val '+urgC+'">'+d.tp1_deadline_h.toFixed(1)+'h</div><div class="clock-sub">Exits at 96h if no TP1</div></div>';
      }
      if (d.tp_stall_deadline_h !== null && d.tp_stall_deadline_h !== undefined) {
        const urgC = d.tp_stall_deadline_h < 8 ? 'r' : d.tp_stall_deadline_h < 16 ? 'a' : 'g';
        html += '<div class="clock-card"><div class="clock-lbl">Stall Deadline</div><div class="clock-val '+urgC+'">'+d.tp_stall_deadline_h.toFixed(1)+'h</div><div class="clock-sub">Exits 48h after last TP</div></div>';
      }
      if (d.since_last_tp_h !== null && d.since_last_tp_h !== undefined) {
        html += '<div class="clock-card"><div class="clock-lbl">Since Last TP</div><div class="clock-val a">'+d.since_last_tp_h.toFixed(1)+'h</div></div>';
      }
      html += '</div>';

      // Entry signals
      html += '<div class="sec-title">Entry Signals</div><div class="sig-chips">';
      (d.signals||[]).forEach(s => {
        html += '<a href="/signal/'+s+'" class="sig-chip">'+s.toUpperCase()+' '+esc(SIG_LABELS[s]||'')+'</a>';
      });
      html += '</div>';

      // Indicators
      const ind = d.indicators || {};
      const items = [
        {k:'rsi', l:'RSI', c: v=>v>70?'var(--red)':v<30?'var(--green)':'var(--text)'},
        {k:'macd_hist', l:'MACD Hist', c: v=>v>=0?'var(--green)':'var(--red)'},
        {k:'atr_pct', l:'ATR %', c:()=>'var(--text)', f:v=>(v*100).toFixed(2)+'%'},
        {k:'adx', l:'ADX', c: v=>v>25?'var(--green)':'var(--dim)'},
        {k:'bb_pctb', l:'BB %B', c: v=>v>0.8?'var(--amber)':v<0.2?'var(--green)':'var(--text)', f:v=>v.toFixed(3)},
        {k:'mfi', l:'MFI', c: v=>v<20?'var(--green)':v>80?'var(--red)':'var(--text)'},
        {k:'stoch_k', l:'Stoch %K', c: v=>v<20?'var(--green)':v>80?'var(--red)':'var(--text)'},
      ];
      html += '<div class="sec-title">Current Indicators</div><div class="ind-grid">';
      items.forEach(i => {
        const v = ind[i.k];
        const val = v != null ? (i.f ? i.f(v) : v.toFixed(2)) : '\\u2014';
        const color = v != null ? i.c(v) : 'var(--dim)';
        html += '<div class="ind-item"><div class="ind-label">'+i.l+'</div><div class="ind-val" style="color:'+color+'">'+val+'</div></div>';
      });
      html += '</div>';

      document.getElementById('content').innerHTML = html;
    } catch(e) {
      console.error('refresh error', e);
      document.getElementById('content').innerHTML = '<div class="no-pos">Error loading position data</div>';
    } finally { _refreshing = false; }
  }
  refresh(); setInterval(refresh, 2000);
  </script>
</body>
</html>"""

# =============================================================================
# TRADE DETAIL  /trade/<idx>  -- Closed trade post-mortem
# =============================================================================

TRADE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ORACLE \u2013 Trade #{{ idx }}</title>
  {{ fonts | safe }}
  <style>{{ style | safe }}
    body { overflow-y: auto; font-family: 'Outfit', system-ui, sans-serif; font-weight: 300; }
    .trade-header { display: flex; align-items: center; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
    .trade-sym { font-size: 2rem; font-weight: 700; color: var(--sym); }
    .trade-dir { font-size: 1.38rem; font-weight: 700; }
    .no-data { padding: 60px 20px; font-size: 1.38rem; }
    .factor-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; }
    .factor-lbl { width: 90px; color: var(--ash); font-family: var(--font-mono); font-size: .8rem; text-transform: uppercase; letter-spacing: .1em; }
    .factor-bar-bg { flex: 1; background: var(--bg); border-radius: 3px; height: 14px; overflow: hidden; }
    .factor-bar-fill { height: 14px; border-radius: 3px; transition: width .3s; }
    .factor-val { width: 45px; text-align: right; font-weight: 600; font-size: 1.06rem; }
  </style>
</head>
<body>
  <div class="container fade-in">
    <a href="/" class="back-link">&#8592; Command Center</a>
    <div id="content">
      <div class="no-data">Loading trade data...</div>
    </div>
  </div>

  <script>
  const SIG_LABELS = {{ sig_labels | tojson }};
  const TRADE_IDX = {{ idx }};
  const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

  function fmtPrice(v) {
    if (v >= 1000) return v.toFixed(2);
    if (v >= 1) return v.toFixed(4);
    return v.toFixed(6);
  }

  function buildProgressBar(d) {
    const entry = d.entry, exit = d.exit_price, sl = d.sl;
    const tp1 = d.tp1, tp2 = d.tp2, tp3 = d.tp3;
    const hi = tp3 || tp2 || tp1 || entry * 1.05;
    const lo = sl;
    const range = hi - lo || 1;
    const pct = v => Math.max(0, Math.min(100, (v - lo) / range * 100));
    const ep = pct(exit);
    const fillC = exit >= entry ? 'var(--green)' : 'var(--red)';
    const markers = [
      {l:'SL', p:pct(sl), c:'var(--red)', pv:fmtPrice(sl)},
      {l:'Entry', p:pct(entry), c:'var(--muted)', pv:fmtPrice(entry)},
    ];
    if (tp1) markers.push({l:'TP1', p:pct(tp1), c:d.tp_hit>=1?'var(--green)':'var(--dim)', pv:fmtPrice(tp1)});
    if (tp2) markers.push({l:'TP2', p:pct(tp2), c:d.tp_hit>=2?'var(--green)':'var(--dim)', pv:fmtPrice(tp2)});
    if (tp3) markers.push({l:'TP3', p:pct(tp3), c:d.tp_hit>=3?'var(--green)':'var(--dim)', pv:fmtPrice(tp3)});
    let h = '<div class="tp-track" style="margin-top:20px;margin-bottom:24px">';
    h += '<div class="tp-fill" style="width:'+ep+'%;background:'+fillC+';opacity:.15"></div>';
    markers.forEach(m => {
      h += '<div class="tp-marker" style="left:'+m.p+'%"><div class="tp-marker-line" style="background:'+m.c+'"></div>'
        +'<div class="tp-marker-label" style="color:'+m.c+'">'+m.l+'</div>'
        +'<div class="tp-marker-val" style="color:'+m.c+'">$'+m.pv+'</div></div>';
    });
    h += '<div class="price-needle" style="left:'+ep+'%"></div>';
    h += '<div class="price-tag" style="left:'+ep+'%;background:'+(exit>=entry?'var(--green)':'var(--red)')+';color:var(--bg)">Exit $'+fmtPrice(exit)+'</div>';
    h += '</div>';
    return h;
  }

  async function load() {
    try {
      const r = await fetch('/api/trade/' + TRADE_IDX);
      const tr = await r.json();
      if (tr.error) {
        document.getElementById('content').innerHTML = '<div class="no-data">Trade #'+TRADE_IDX+' not found</div>';
        return;
      }
      const d = tr.detail || tr;
      const hasDet = d.entry !== undefined;
      const plC = tr.pnl >= 0 ? 'g' : 'r';
      const plS = (tr.pnl >= 0 ? '+' : '') + tr.pnl.toFixed(2);
      const rC = tr.r >= 0 ? 'g' : 'r';
      const exitC = (tr.exit||'').startsWith('TP') ? 'g' : tr.exit==='SL' ? 'r' : 'a';
      const ago = Date.now()/1000 - (tr.t||0);
      const ageStr = ago < 3600 ? Math.floor(ago/60)+'m ago' : ago < 86400 ? Math.floor(ago/3600)+'h ago' : Math.floor(ago/86400)+'d ago';

      let html = '<div class="trade-header">'
        +'<span class="trade-sym"><a href="/symbol/'+encodeURIComponent(tr.sym)+'" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--dim)">'+esc(tr.sym)+'</a></span>'
        +'<span class="trade-dir g bold">'+tr.dir+'</span>'
        +'<span class="badge badge-'+(tr.pnl>=0?'green':'red')+'">$'+plS+'</span>'
        +'<span class="badge badge-'+exitC+'">'+esc(tr.exit)+'</span>'
        +'<span class="m" style="font-size:1.06rem">'+ageStr+'</span>'
        +'</div>';

      // Summary cards
      html += '<div class="sum-grid">'
        +'<div class="sum-card"><div class="sum-lbl">P/L</div><div class="sum-val '+plC+'">$'+plS+'</div></div>'
        +'<div class="sum-card"><div class="sum-lbl">R-Multiple</div><div class="sum-val '+rC+'">'+(tr.r>=0?'+':'')+tr.r.toFixed(2)+'R</div></div>'
        +'<div class="sum-card"><div class="sum-lbl">Exit Type</div><div class="sum-val '+exitC+'">'+esc(tr.exit)+'</div></div>';
      if (hasDet) {
        html += '<div class="sum-card"><div class="sum-lbl">Duration</div><div class="sum-val t">'+d.duration_h.toFixed(1)+'h</div></div>'
          +'<div class="sum-card"><div class="sum-lbl">Confidence</div><div class="sum-val t">'+(d.confidence*100).toFixed(0)+'%</div></div>'
          +'<div class="sum-card"><div class="sum-lbl">Regime</div><div class="sum-val '
          +({bull:'g',bear:'r',range:'a',chop:'d'}[d.regime]||'d')+'">'+esc(d.regime||'\u2014')+'</div></div>'
          +'<div class="sum-card"><div class="sum-lbl">Size</div><div class="sum-val t">'+d.size.toFixed(4)+'</div></div>'
          +'<div class="sum-card"><div class="sum-lbl">TPs Hit</div><div class="sum-val '+(d.tp_hit>0?'g':'d')+'">'+d.tp_hit+'/3</div></div>';
      }
      html += '</div>';

      if (hasDet) {
        // TP progress bar
        html += '<div class="sec-title">Exit Map</div>';
        html += buildProgressBar(d);

        // Levels
        html += '<div class="sec-title">Levels</div><div class="card" style="padding:12px;margin-bottom:20px">';
        html += '<div class="level-row"><span class="level-tag m">Entry</span><span class="level-price t">$'+fmtPrice(d.entry)+'</span></div>';
        html += '<div class="level-row"><span class="level-tag '+exitC+'">Exit</span><span class="level-price '+plC+'">$'+fmtPrice(d.exit_price)+'</span><span class="level-note">'+esc(tr.exit)+'</span></div>';
        html += '<div class="level-row"><span class="level-tag r">SL</span><span class="level-price">$'+fmtPrice(d.sl)+'</span></div>';
        [['tp1','TP1',1],['tp2','TP2',2],['tp3','TP3',3]].forEach(([k,lbl,lvl]) => {
          if (!d[k]) return;
          const hit = d.tp_hit >= lvl;
          const cls = hit ? 'g bold' : 'm';
          const check = hit ? '<span class="level-check g">\\u2713</span>' : '<span class="level-check d">\\u2014</span>';
          html += '<div class="level-row"><span class="level-tag gld">'+lbl+'</span><span class="level-price '+cls+'">$'+fmtPrice(d[k])+'</span>'+check+'</div>';
        });
        html += '</div>';

        // Factor scores
        const factors = d.factors || {};
        const fKeys = [['trend','Trend',0.27],['momentum','Momentum',0.22],['structure','Structure',0.18],['volume','Volume',0.13],['volatility','Volatility',0.08],['order_flow','Order Flow',0.12]];
        if (Object.keys(factors).length) {
          html += '<div class="sec-title">Entry Factor Scores</div><div class="card" style="padding:12px;margin-bottom:20px">';
          fKeys.forEach(([k,lbl,wt]) => {
            const v = factors[k] || 0;
            const pct = (v * 100).toFixed(0);
            const c = v >= 0.6 ? 'var(--green)' : v >= 0.4 ? 'var(--amber)' : 'var(--red)';
            html += '<div class="factor-row">'
              +'<span class="factor-lbl">'+lbl+' <span class="d" style="font-size:.85rem">('+Math.round(wt*100)+'%)</span></span>'
              +'<div class="factor-bar-bg"><div class="factor-bar-fill" style="width:'+pct+'%;background:'+c+'"></div></div>'
              +'<span class="factor-val" style="color:'+c+'">'+v.toFixed(2)+'</span>'
              +'</div>';
          });
          html += '</div>';
        }

        // Entry signals
        const sigs = d.sigs || [];
        if (sigs.length) {
          html += '<div class="sec-title">Entry Signals</div><div class="sig-chips">';
          sigs.forEach(s => {
            html += '<a href="/signal/'+s+'" class="sig-chip">'+s.toUpperCase()+' '+(SIG_LABELS[s]||'')+'</a>';
          });
          html += '</div>';
        }
      } else {
        html += '<div class="no-data" style="padding:20px">Detailed trade data not available (trade occurred before detail logging was added)</div>';
      }

      document.getElementById('content').innerHTML = html;
    } catch(e) {
      console.error('load error', e);
      document.getElementById('content').innerHTML = '<div class="no-data">Error loading trade data</div>';
    }
  }
  load();
  </script>
</body>
</html>"""

# =============================================================================
# Shared data builder
# =============================================================================

def fetch_positions():
    """Fetch active positions from the bot's :8080/positions endpoint."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/positions", timeout=5)
        return resp.json() if resp.status_code == 200 else []
    except Exception:
        return []

# =============================================================================
# Routes
# =============================================================================

@app.route('/')
def public():
    return AUDIT_TEMPLATE

@app.route('/symbol/<path:sym>')
def symbol_detail(sym):
    return render_template_string(
        SYMBOL_TEMPLATE,
        fonts=FONTS, style=BASE_STYLE,
        symbol=sym, sig_labels=SIG_LABELS,
    )

@app.route('/position/<path:sym>')
def position_detail(sym):
    return render_template_string(
        POSITION_TEMPLATE,
        fonts=FONTS, style=BASE_STYLE,
        symbol=sym, sig_labels=SIG_LABELS,
    )

@app.route('/trade/<int:idx>')
def trade_detail(idx):
    return render_template_string(
        TRADE_TEMPLATE,
        fonts=FONTS, style=BASE_STYLE,
        idx=idx, sig_labels=SIG_LABELS,
    )

@app.route('/signal/<sig>')
def signal_detail(sig):
    return render_template_string(
        SIGNAL_TEMPLATE,
        fonts=FONTS, style=BASE_STYLE,
        signal=sig.lower(),
    )

@app.route('/api/health')
def api_health():
    data = fetch_health()
    if data:
        return jsonify(data)
    return jsonify({'status': 'unavailable', 'error': 'Bot not reachable \u2014 is goldeneye.py running?'}), 503

@app.route('/api/positions')
def api_positions():
    positions = fetch_positions()
    return jsonify(positions)

@app.route('/api/tui')
def api_tui():
    """Aggregate all panel data in one call for the command center."""
    from concurrent.futures import ThreadPoolExecutor

    def _fetch_symbols():
        try:
            resp = requests.get(f"{GOLDENEYE_URL}/symbols", timeout=5)
            return resp.json() if resp.status_code == 200 else []
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=4) as pool:
        fh = pool.submit(fetch_health)
        fa = pool.submit(fetch_analytics)
        fp = pool.submit(fetch_positions)
        fs = pool.submit(_fetch_symbols)

    return jsonify({
        'health': fh.result(),
        'analytics': fa.result(),
        'positions': fp.result(),
        'symbols': fs.result(),
    })

@app.route('/api/logs')
def api_logs():
    """Proxy to bot's /logs endpoint."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/logs", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify([])

@app.route('/api/messages')
def api_messages():
    """Proxy to subscriber API's /api/messages endpoint."""
    try:
        resp = requests.get(f"{SUB_API_URL}/api/messages", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify([])

@app.route('/api/position/<path:sym>')
def api_position(sym):
    """Proxy to bot's /position/<sym> endpoint."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/position/{quote(sym, safe='')}", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({'error': 'Position not found'}), 404

@app.route('/api/trade/<int:idx>')
def api_trade(idx):
    """Proxy to bot's /trade/<idx> endpoint."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/trade/{idx}", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({'error': 'Trade not found'}), 404

@app.route('/api/symbol/<path:sym>')
def api_symbol(sym):
    """Proxy to bot's /symbol/<sym> endpoint."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/symbol/{quote(sym, safe='')}", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({'error': 'Symbol not found'}), 404

@app.route('/api/signal/<sig>')
def api_signal(sig):
    """Proxy to bot's /signal/<sig> endpoint."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/signal/{sig}", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({'error': 'Signal not found'}), 404

@app.route('/api/equity')
def api_equity():
    """Proxy to bot's /api/equity endpoint (equity curve time-series)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/api/equity", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify([])

@app.route('/api/trade_markers')
def api_trade_markers():
    """Proxy to bot's /api/trade_markers endpoint (per-trade open/close markers)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/api/trade_markers", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify([])

@app.route('/api/funnel')
def api_funnel():
    """Proxy to bot's /api/funnel endpoint (gate funnel snapshot)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/api/funnel", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({'window_seconds': 60, 'records': 0, 'entered': 0, 'gates': []})

@app.route('/api/decisions')
def api_decisions():
    """Proxy to bot's /decisions endpoint (decision log ring buffer)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/decisions", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify([])

@app.route('/api/analytics')
def api_analytics():
    """Proxy to bot's /analytics endpoint (lifetime trading metrics)."""
    data = fetch_analytics()
    return jsonify(data)

@app.route('/api/symbols')
def api_symbols():
    """Proxy to bot's /symbols endpoint (per-symbol price + regime)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/symbols", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify([])

if __name__ == '__main__':
    port = int(os.getenv('DASHBOARD_PORT', 8050))
    logger.info(f"ORACLE Dashboard starting on port {port}")
    logger.info(f"Command Center: http://localhost:{port}/")
    app.run(host='0.0.0.0', port=port, debug=False)
