import os
import logging
import requests
from urllib.parse import quote
from flask import Flask, render_template_string, jsonify, request

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
# PUBLIC TRACK RECORD — /verify
# =============================================================================
# Deliberately plain. This page's only job is to be believed: every closed
# trade, fees counted, losses shown with the same weight as wins. No controls,
# no internals, nothing that could be mistaken for a sales page.

VERIFY_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GoldenEye — Verified Track Record</title>
{{ fonts|safe }}
<style>
  :root{--bg:#070707;--panel:#0e0e0e;--line:#1b1b1b;--gold:#c8a96e;--dim:#606060;
        --text:#efefef;--pos:#4caf7d;--neg:#c0392b;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Rajdhani',system-ui,sans-serif;
       padding:28px 16px 64px;line-height:1.5}
  .wrap{max-width:1000px;margin:0 auto}
  .mono{font-family:'JetBrains Mono',monospace}
  header{border-left:3px solid var(--gold);padding-left:16px;margin-bottom:8px}
  h1{font-family:'Orbitron',sans-serif;font-size:26px;font-weight:900;letter-spacing:.06em}
  .sub{color:var(--dim);font-size:14px;letter-spacing:.04em}
  .promise{background:var(--panel);border-left:3px solid var(--gold);padding:16px 20px;
           margin:22px 0;color:#d4b896;font-size:15px}
  .promise b{color:var(--gold)}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
  .stat{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:14px}
  .stat .k{color:var(--dim);font-size:11px;letter-spacing:.14em;text-transform:uppercase}
  .stat .v{font-size:26px;font-weight:700;margin-top:4px}
  .pos{color:var(--pos)} .neg{color:var(--neg)} .gold{color:var(--gold)}
  h2{font-family:'JetBrains Mono',monospace;font-size:13px;letter-spacing:.16em;
     color:var(--gold);margin:30px 0 10px;text-transform:uppercase}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;color:var(--dim);font-size:11px;letter-spacing:.1em;
     text-transform:uppercase;padding:8px 10px;border-bottom:1px solid var(--line)}
  td{padding:9px 10px;border-bottom:1px solid #121212}
  td.num{text-align:right}
  tr:hover{background:#0c0c0c}
  .tag{font-size:10px;padding:2px 7px;border-radius:3px;background:#161616;color:var(--dim);
       letter-spacing:.06em}
  .note{color:var(--dim);font-size:13px;margin-top:10px}
  footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);
         color:var(--dim);font-size:12px}
  .curve{width:100%;height:150px;display:block;margin:6px 0 2px}
  .banner{background:#1a1207;border:1px solid #3a2c10;color:#e8a838;padding:11px 16px;
          border-radius:5px;margin-bottom:18px;font-size:14px}
  .empty{color:var(--dim);padding:26px 0;text-align:center;font-size:14px}
  @media(max-width:620px){.hide-sm{display:none}h1{font-size:21px}}
</style></head>
<body><div class="wrap">
  <header>
    <h1>GOLDENEYE</h1>
    <div class="sub">Verified Track Record &middot; every closed trade, fees counted</div>
  </header>

  <div id="mode-banner"></div>

  <div class="promise">
    <b>What this page is.</b> Every trade below was published to Telegram
    <b>before</b> its outcome was known &mdash; entry, stop and targets, timestamped
    by Telegram itself. Nothing here is back-tested, curve-fitted, or selected
    after the fact. Losses appear exactly as prominently as wins, because a
    track record that hides them is not a track record.
  </div>

  <div class="grid" id="stats"></div>

  <h2>Cumulative net P&amp;L</h2>
  <svg class="curve" id="curve" preserveAspectRatio="none"></svg>
  <div class="note" id="curve-note"></div>

  <h2>Every closed trade</h2>
  <div id="table-wrap"></div>

  <footer>
    <div id="foot-meta"></div>
    <div style="margin-top:8px">
      Net P&amp;L is after Kraken taker fees on both legs (0.40% each way).
      R-multiple measures price movement against the stop distance set at entry.
      Past results never guarantee future results &mdash; this is published for
      transparency, not as financial advice.
    </div>
  </footer>
</div>
<script>
const $=s=>document.querySelector(s);
const money=v=>(v>=0?'+':'-')+'$'+Math.abs(v).toFixed(2);
const cls=v=>v>=0?'pos':'neg';
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const EXIT_LABEL={TP:'Target hit',SL:'Stop loss',EXH:'Exhausted',FDEC:'Signal decay',
  AI:'AI risk exit',TIME:'Time stop',RECON_SL:'Stop loss',RECON_TP:'Target hit',
  TP1:'TP1 hit',TP2:'TP2 hit',TP3:'TP3 hit'};

function drawCurve(cum){
  const svg=$('#curve');
  if(!cum||cum.length<2){svg.innerHTML='';$('#curve-note').textContent=
    'The curve appears once at least two trades have closed.';return;}
  const W=1000,H=150,P=6;
  const min=Math.min(0,...cum),max=Math.max(0,...cum),span=(max-min)||1;
  const x=i=>P+i*(W-2*P)/(cum.length-1);
  const y=v=>H-P-((v-min)/span)*(H-2*P);
  const pts=cum.map((v,i)=>x(i)+','+y(v)).join(' ');
  const up=cum[cum.length-1]>=0, col=up?'#4caf7d':'#c0392b';
  const zero=y(0);
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  svg.innerHTML=
    `<line x1="0" y1="${zero}" x2="${W}" y2="${zero}" stroke="#242424" stroke-width="1"/>`+
    `<polygon points="${x(0)},${zero} ${pts} ${x(cum.length-1)},${zero}" fill="${col}" opacity=".13"/>`+
    `<polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2"/>`;
  $('#curve-note').textContent='Each point is one closed trade, in order.';
}

fetch('/api/track-record').then(r=>r.json()).then(d=>{
  if(d.online===false&&d.error){
    $('#mode-banner').innerHTML='<div class="banner">The bot is currently offline. '+
      'Figures below are the last known state.</div>';
  }else if(d.mode==='paper'){
    $('#mode-banner').innerHTML='<div class="banner"><b>Paper trading.</b> '+
      'These are simulated fills with real market prices, real spreads and real '+
      'Kraken fees applied &mdash; but no live capital is at risk yet. '+
      'Stated plainly here rather than buried.</div>';
  }

  const n=d.trades||0;
  $('#stats').innerHTML=[
    ['Closed trades',n,''],
    ['Win rate',n?d.win_rate.toFixed(1)+'%':'—','gold'],
    ['Net P&L',n?money(d.net_pnl):'—',n?cls(d.net_pnl):''],
    ['Fees paid',n?'-$'+d.fees_paid.toFixed(2):'—','neg'],
    ['Avg R',n?(d.avg_r>=0?'+':'')+d.avg_r.toFixed(2)+'R':'—',n?cls(d.avg_r):''],
    ['Expectancy / trade',n?money(d.expectancy):'—',n?cls(d.expectancy):''],
    ['Max drawdown',n?money(d.max_drawdown):'—','neg'],
    ['Record',n?d.wins+'W / '+d.losses+'L':'—',''],
  ].map(([k,v,c])=>`<div class="stat"><div class="k">${k}</div>
      <div class="v ${c}">${v}</div></div>`).join('');

  drawCurve(d.cumulative||[]);

  const rows=(d.trade_log||[]);
  if(!rows.length){
    $('#table-wrap').innerHTML='<div class="empty">No closed trades yet. '+
      'This table fills itself as trades close &mdash; nothing is added by hand.</div>';
  }else{
    $('#table-wrap').innerHTML='<table><thead><tr>'+
      '<th>Date (UTC)</th><th>Pair</th><th>Side</th>'+
      '<th class="num">R</th><th class="num hide-sm">Gross</th>'+
      '<th class="num hide-sm">Fees</th><th class="num">Net</th><th>Exit</th>'+
      '</tr></thead><tbody>'+rows.map(t=>{
        const dt=new Date((t.ts||0)*1000);
        const ds=isNaN(dt)?'—':dt.toISOString().slice(0,16).replace('T',' ');
        const ex=EXIT_LABEL[String(t.exit||'').toUpperCase()]||esc(t.exit||'—');
        return `<tr><td class="mono">${ds}</td><td>${esc(t.sym)}</td>`+
          `<td><span class="tag">${esc(t.dir)}</span></td>`+
          `<td class="num mono ${cls(t.r)}">${t.r>=0?'+':''}${t.r.toFixed(2)}R</td>`+
          `<td class="num mono hide-sm ${cls(t.gross)}">${money(t.gross)}</td>`+
          `<td class="num mono neg hide-sm">-$${Math.abs(t.fees).toFixed(2)}</td>`+
          `<td class="num mono ${cls(t.net)}">${money(t.net)}</td>`+
          `<td>${ex}</td></tr>`;
      }).join('')+'</tbody></table>';
  }

  $('#foot-meta').textContent='Live from the running bot. '+
    (d.symbol_count?d.symbol_count+' pairs tracked. ':'')+
    'Updated '+new Date().toISOString().slice(0,16).replace('T',' ')+' UTC.';
}).catch(()=>{
  $('#mode-banner').innerHTML='<div class="banner">Track record is temporarily '+
    'unavailable &mdash; the bot is not reachable right now.</div>';
});
</script></body></html>
"""

# =============================================================================
# AUDIT DASHBOARD — CINEMATIC COMMAND DISPLAY v3
# =============================================================================

AUDIT_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GOLDENEYE — Mission Control</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
/* ============================================================
   GOLDENEYE MISSION CONTROL — v5
   Design system: one accent (gold) for identity, semantic color
   only for state (green=profit, red=loss, cyan=live/active).
   Everything else is a neutral ramp. Density without noise.
   ============================================================ */

:root {
  /* Neutral ramp — cool-shifted near-black through paper white */
  --n-000: #06070a;
  --n-050: #0a0c11;
  --n-100: #0f1117;
  --n-150: #14161e;
  --n-200: #1a1d26;
  --n-250: #21242f;
  --n-300: #2a2e3a;
  --n-400: #3d4250;
  --n-500: #565c6d;
  --n-600: #7a8091;
  --n-700: #a4aab8;
  --n-800: #d2d6de;
  --n-900: #f2f4f8;

  /* Identity */
  --gold: #d4a72c;
  --gold-hi: #f0c04a;
  --gold-dim: #8c6d17;

  /* Semantic — state only */
  --pos: #26d0a0;
  --pos-dim: #13725a;
  --neg: #ff4d6a;
  --neg-dim: #8c1f31;
  --live: #22d3ee;
  --live-dim: #0e7490;
  --warn: #f5a524;

  /* Regime palette — distinct hues, equal weight */
  --r-bull: #26d0a0;
  --r-bear: #ff4d6a;
  --r-range: #a78bfa;
  --r-chop: #7a8091;

  --bg: var(--n-000);
  --panel: var(--n-100);
  --panel-hi: var(--n-150);
  --line: #1e222c;
  --line-hi: #2c313e;

  --font-ui: 'Inter', system-ui, -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, monospace;

  --r-sm: 4px;
  --r-md: 7px;
  --r-lg: 10px;

  --gap: 10px;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

html, body {
  height: 100%;
  background: var(--bg);
  color: var(--n-800);
  font-family: var(--font-ui);
  font-size: 14px;
  overflow: hidden;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

/* Subtle vignette so the grid edges recede */
body::before {
  content: '';
  position: fixed; inset: 0; pointer-events: none; z-index: 0;
  background: radial-gradient(ellipse 120% 90% at 50% 0%, rgba(212,167,44,0.045), transparent 60%);
}

/* ---------- App shell ---------- */
.app {
  position: relative; z-index: 1;
  height: 100vh;
  display: grid;
  grid-template-rows: auto 1fr;
  gap: var(--gap);
  padding: var(--gap);
}

/* ============================================================
   TOP BAR
   ============================================================ */
.topbar {
  display: flex;
  align-items: stretch;
  gap: 8px;
  background: linear-gradient(180deg, var(--n-150), var(--n-100));
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  padding: 9px 14px;
  min-height: 56px;
}

.brand {
  display: flex; align-items: center; gap: 11px;
  padding-right: 16px;
  border-right: 1px solid var(--line);
  flex-shrink: 0;
}
.brand-mark {
  width: 30px; height: 30px; flex-shrink: 0;
  position: relative;
}
.brand-mark svg { display: block; width: 100%; height: 100%; }
.brand-text {
  font-size: 17px; font-weight: 800; letter-spacing: 0.16em;
  background: linear-gradient(180deg, var(--gold-hi), var(--gold));
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
  line-height: 1;
}
.brand-sub {
  font-family: var(--font-mono);
  font-size: 8.5px; letter-spacing: 0.22em; color: var(--n-500);
  margin-top: 3px;
}

/* Stat cluster */
.stats {
  display: flex; align-items: center; gap: 0;
  flex: 1; min-width: 0;
  overflow: hidden;
}
.stat {
  display: flex; flex-direction: column; justify-content: center;
  padding: 0 15px;
  border-right: 1px solid var(--line);
  min-width: 0;
}
.stat:last-child { border-right: none; }
.stat-k {
  font-family: var(--font-mono);
  font-size: 8.5px; letter-spacing: 0.16em; color: var(--n-500);
  text-transform: uppercase; white-space: nowrap;
  margin-bottom: 3px;
}
.stat-v {
  font-family: var(--font-mono);
  font-size: 15px; font-weight: 600; color: var(--n-900);
  line-height: 1; white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.stat-v.sm { font-size: 13px; }
.stat-v .unit { font-size: 10px; color: var(--n-500); font-weight: 400; }

/* Connection pill */
.conn {
  display: inline-flex; align-items: center; gap: 7px;
  font-family: var(--font-mono); font-size: 11px; font-weight: 600;
  letter-spacing: 0.06em;
}
.dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--pos); flex-shrink: 0;
  box-shadow: 0 0 0 0 rgba(38,208,160,0.6);
  animation: pulse 2.4s ease-out infinite;
}
.dot.bad { background: var(--neg); animation: none; box-shadow: none; }
@keyframes pulse {
  0%   { box-shadow: 0 0 0 0 rgba(38,208,160,0.55); }
  70%  { box-shadow: 0 0 0 7px rgba(38,208,160,0); }
  100% { box-shadow: 0 0 0 0 rgba(38,208,160,0); }
}

/* Thread meter */
.threads { display: flex; align-items: center; gap: 8px; }
.tmeter { display: flex; gap: 1.5px; }
.tbar {
  width: 2.5px; height: 15px; border-radius: 1px;
  background: var(--n-300);
}
.tbar.on { background: var(--live); box-shadow: 0 0 4px rgba(34,211,238,0.5); }

/* Mode chip */
.chip {
  display: inline-flex; align-items: center;
  padding: 3px 9px; border-radius: 20px;
  font-family: var(--font-mono); font-size: 10px; font-weight: 700;
  letter-spacing: 0.1em; text-transform: uppercase;
  border: 1px solid;
}
.chip.paper { color: var(--live); border-color: rgba(34,211,238,0.35); background: rgba(34,211,238,0.08); }
.chip.live  { color: var(--neg);  border-color: rgba(255,77,106,0.4);  background: rgba(255,77,106,0.1); }

/* Clock */
.clock { text-align: right; padding-left: 15px; border-left: 1px solid var(--line); flex-shrink: 0; }
.clock-main {
  font-family: var(--font-mono); font-size: 17px; font-weight: 600;
  color: var(--n-900); line-height: 1; font-variant-numeric: tabular-nums;
}
.clock-main .tz { font-size: 9px; color: var(--n-500); margin-right: 5px; font-weight: 500; }
.clock-sub {
  font-family: var(--font-mono); font-size: 10px; color: var(--n-500);
  margin-top: 4px; font-variant-numeric: tabular-nums;
}

/* ============================================================
   GRID LAYOUT — 12 col, 2 row bands
   ============================================================ */
.grid {
  display: grid;
  grid-template-columns: repeat(12, 1fr);
  grid-template-rows: minmax(0, 1.15fr) minmax(0, 1fr);
  gap: var(--gap);
  min-height: 0;
}

.area-equity  { grid-column: 1 / 8;  grid-row: 1; }
.area-command { grid-column: 8 / 13; grid-row: 1; }
.area-intel   { grid-column: 1 / 5;  grid-row: 2; }
.area-stream  { grid-column: 5 / 9;  grid-row: 2; }
.area-market  { grid-column: 9 / 13; grid-row: 2; }

/* ---------- Panel ---------- */
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  display: flex; flex-direction: column;
  min-height: 0; min-width: 0;
  overflow: hidden;
}
.panel-head {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 13px;
  border-bottom: 1px solid var(--line);
  background: linear-gradient(180deg, var(--n-150), transparent);
  flex-shrink: 0;
}
.panel-title {
  font-size: 10.5px; font-weight: 700; letter-spacing: 0.15em;
  text-transform: uppercase; color: var(--n-700);
  display: flex; align-items: center; gap: 8px;
}
.panel-title::before {
  content: ''; width: 2.5px; height: 12px; border-radius: 2px;
  background: var(--gold);
  box-shadow: 0 0 8px rgba(212,167,44,0.5);
}
.panel-tools { margin-left: auto; display: flex; align-items: center; gap: 6px; }
.panel-body { flex: 1; min-height: 0; overflow: hidden; position: relative; }
.panel-body.scroll { overflow-y: auto; }

/* Tag / badge */
.tag {
  font-family: var(--font-mono); font-size: 9px; font-weight: 600;
  letter-spacing: 0.1em; text-transform: uppercase;
  padding: 3px 7px; border-radius: var(--r-sm);
  background: var(--n-200); color: var(--n-600);
  border: 1px solid var(--line-hi);
  white-space: nowrap;
}
.tag.gold { color: var(--gold); border-color: rgba(212,167,44,0.3); background: rgba(212,167,44,0.08); }
.tag.pos  { color: var(--pos); border-color: rgba(38,208,160,0.3); background: rgba(38,208,160,0.08); }
.tag.neg  { color: var(--neg); border-color: rgba(255,77,106,0.3); background: rgba(255,77,106,0.08); }
.tag.live { color: var(--live); border-color: rgba(34,211,238,0.3); background: rgba(34,211,238,0.08); }

/* Segmented control */
.seg {
  display: inline-flex; background: var(--n-200);
  border: 1px solid var(--line-hi); border-radius: var(--r-sm);
  overflow: hidden;
}
.seg button {
  font-family: var(--font-mono); font-size: 9px; font-weight: 600;
  letter-spacing: 0.08em; text-transform: uppercase;
  padding: 4px 9px; border: none; background: transparent;
  color: var(--n-600); cursor: pointer; transition: all 0.13s;
}
.seg button:hover { color: var(--n-800); background: var(--n-250); }
.seg button.on { background: var(--gold); color: var(--n-000); }

/* Scrollbars */
::-webkit-scrollbar { width: 7px; height: 7px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--n-300); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--n-400); }

/* ============================================================
   EQUITY PANEL
   ============================================================ */
.eq-wrap { display: flex; flex-direction: column; height: 100%; }
.eq-summary {
  display: flex; align-items: center; gap: 0;
  padding: 11px 14px 12px;
  border-bottom: 1px solid var(--line);
  flex-shrink: 0;
}
.eq-cell { padding-right: 22px; margin-right: 22px; border-right: 1px solid var(--line); }
.eq-cell:last-child { border-right: none; }
.eq-k {
  font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 0.16em;
  color: var(--n-500); text-transform: uppercase; margin-bottom: 5px;
}
.eq-v {
  font-family: var(--font-mono); font-size: 22px; font-weight: 700;
  color: var(--n-900); line-height: 1; font-variant-numeric: tabular-nums;
}
.eq-v.big { font-size: 27px; }
.eq-v.pos { color: var(--pos); }
.eq-v.neg { color: var(--neg); }
.eq-v .sub { font-size: 12px; color: var(--n-500); font-weight: 500; margin-left: 5px; }
.eq-chart { flex: 1; min-height: 0; position: relative; }
#eqCanvas { display: block; width: 100%; height: 100%; }
.eq-empty {
  position: absolute; inset: 0; display: flex; align-items: center;
  justify-content: center; flex-direction: column; gap: 8px;
  font-family: var(--font-mono); font-size: 11px; color: var(--n-500);
}

/* Chart tooltip */
.eq-tip {
  position: absolute; pointer-events: none; z-index: 20;
  background: rgba(10,12,17,0.96);
  border: 1px solid var(--line-hi); border-radius: var(--r-md);
  padding: 8px 11px; font-family: var(--font-mono); font-size: 10.5px;
  color: var(--n-800); white-space: nowrap; opacity: 0;
  transition: opacity 0.1s; box-shadow: 0 6px 24px rgba(0,0,0,0.6);
  line-height: 1.6;
}
.eq-tip .t-lab { color: var(--n-500); margin-right: 7px; }

/* ============================================================
   COMMAND PANEL
   ============================================================ */
/* Command shell: scrollable body + pinned fleet footer.
   The footer must never be pushed below the fold — it carries the
   regime mix and sanity verdict, which are glanceable-or-useless. */
.cmd { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.cmd-scroll { flex: 1; min-height: 0; overflow-y: auto; }

/* Standby hero */
.standby { padding: 15px 14px; border-bottom: 1px solid var(--line); }
.standby-row { display: flex; align-items: center; gap: 9px; margin-bottom: 4px; }
.standby-title {
  font-size: 12px; font-weight: 700; letter-spacing: 0.18em;
  text-transform: uppercase; color: var(--n-700);
}
.standby-last { font-family: var(--font-mono); font-size: 10.5px; color: var(--n-500); }
.standby-last b { color: var(--n-800); font-weight: 600; }

/* Scanner sweep bar */
.sweep {
  height: 2px; border-radius: 2px; margin-top: 10px;
  background: var(--n-250); overflow: hidden; position: relative;
}
.sweep::after {
  content: ''; position: absolute; top: 0; bottom: 0; width: 34%;
  background: linear-gradient(90deg, transparent, var(--gold), transparent);
  animation: sweep 2.6s ease-in-out infinite;
}
@keyframes sweep {
  0%   { left: -34%; }
  100% { left: 100%; }
}

/* Candidate card */
.cand {
  margin: 12px 12px 12px;
  background: linear-gradient(180deg, var(--n-150), var(--n-100));
  border: 1px solid var(--line-hi);
  border-left: 2.5px solid var(--gold);
  border-radius: var(--r-md);
  padding: 12px;
}
.cand-top { display: flex; align-items: baseline; gap: 10px; margin-bottom: 9px; }
.cand-sym {
  font-family: var(--font-mono); font-size: 17px; font-weight: 700;
  color: var(--gold-hi); letter-spacing: 0.02em;
}
.cand-dir {
  font-family: var(--font-mono); font-size: 9px; font-weight: 700;
  letter-spacing: 0.1em; padding: 2px 6px; border-radius: 3px;
}
.cand-dir.long  { color: var(--pos); background: rgba(38,208,160,0.12); border: 1px solid rgba(38,208,160,0.3); }
.cand-dir.short { color: var(--neg); background: rgba(255,77,106,0.12); border: 1px solid rgba(255,77,106,0.3); }
.cand-conf { margin-left: auto; font-family: var(--font-mono); font-size: 11px; color: var(--n-600); }
.cand-conf b { color: var(--n-900); font-size: 13px; font-weight: 600; }

/* Gate progress */
.gates { margin-bottom: 10px; }
.gates-head {
  display: flex; justify-content: space-between; align-items: baseline;
  font-family: var(--font-mono); font-size: 9.5px; color: var(--n-500);
  letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 6px;
}
.gates-head b { color: var(--n-800); font-size: 11px; }
.gate-track { display: flex; gap: 2px; height: 5px; }
.gate-seg { flex: 1; border-radius: 1px; background: var(--n-300); }
.gate-seg.pass { background: var(--pos); }
.gate-seg.fail { background: var(--neg); box-shadow: 0 0 6px rgba(255,77,106,0.5); }

/* Blocker line */
.blocker {
  display: flex; align-items: center; gap: 8px;
  font-family: var(--font-mono); font-size: 11px;
  padding: 7px 9px; border-radius: var(--r-sm);
  background: rgba(255,77,106,0.07);
  border: 1px solid rgba(255,77,106,0.2);
  color: var(--neg); margin-bottom: 9px;
}
.blocker .x { font-weight: 700; }
.blocker .why { color: var(--n-600); margin-left: auto; font-size: 10px; }

/* Factor bars — 2 columns so label and value never collide */
.factors { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px 16px; margin-bottom: 10px; }
.fac-k {
  font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 0.1em;
  color: var(--n-500); text-transform: uppercase;
  display: flex; justify-content: space-between; gap: 8px; margin-bottom: 4px;
}
.fac-k b { color: var(--n-800); font-weight: 600; }
.fac-bar { height: 3px; background: var(--n-300); border-radius: 2px; overflow: hidden; }
.fac-fill { height: 100%; border-radius: 2px; transition: width 0.4s ease; }

/* Memory verdict */
.memory {
  font-family: var(--font-mono); font-size: 10.5px;
  padding: 8px 10px; border-radius: var(--r-sm);
  background: var(--n-200); border: 1px solid var(--line-hi);
  line-height: 1.5;
}
.memory .verdict { font-weight: 700; }
.memory .verdict.yes { color: var(--pos); }
.memory .verdict.no  { color: var(--neg); }
.memory .verdict.na  { color: var(--n-600); }
.memory .detail { color: var(--n-600); }

/* Queue */
.queue { padding: 12px; }
.sec-label {
  font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 0.18em;
  color: var(--n-500); text-transform: uppercase; margin-bottom: 8px;
}
.q-row {
  display: flex; align-items: center; gap: 9px;
  padding: 7px 9px; border-radius: var(--r-sm);
  background: var(--n-150); border: 1px solid var(--line);
  margin-bottom: 5px;
  font-family: var(--font-mono); font-size: 11px;
}
.q-sym { font-weight: 600; color: var(--n-800); min-width: 58px; }
.q-mini { display: flex; gap: 1.5px; flex: 1; }
.q-seg { height: 4px; flex: 1; border-radius: 1px; background: var(--n-300); }
.q-seg.pass { background: var(--pos-dim); }
.q-seg.fail { background: var(--neg); }
.q-block { color: var(--n-600); font-size: 9.5px; }

/* Position card (when open) */
.pos-card { margin: 12px; }
.pos-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.pos-sym { font-family: var(--font-mono); font-size: 19px; font-weight: 700; color: var(--n-900); }
.pos-pnl { margin-left: auto; text-align: right; }
.pos-pnl-v { font-family: var(--font-mono); font-size: 21px; font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums; }
.pos-pnl-r { font-family: var(--font-mono); font-size: 11px; color: var(--n-500); margin-top: 3px; }

/* Price ladder — inset so the outermost SL/TP labels never clip */
.ladder { position: relative; height: 74px; margin: 14px 26px 16px; }
.ladder-track {
  position: absolute; left: 0; right: 0; top: 34px; height: 4px;
  background: var(--n-300); border-radius: 2px;
}
.ladder-fill { position: absolute; top: 0; bottom: 0; border-radius: 2px; }
.lad-mark { position: absolute; top: 26px; transform: translateX(-50%); }
.lad-tick { width: 2px; height: 20px; border-radius: 1px; margin: 0 auto; }
.lad-lab {
  font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 0.08em;
  text-align: center; margin-top: 4px; white-space: nowrap;
}
.lad-px { font-family: var(--font-mono); font-size: 9px; color: var(--n-600); text-align: center; margin-top: 1px; }
.lad-now {
  position: absolute; top: 8px; transform: translateX(-50%);
  display: flex; flex-direction: column; align-items: center; z-index: 3;
}
.lad-now-dot {
  width: 9px; height: 9px; border-radius: 50%; background: var(--n-900);
  border: 2px solid var(--bg); box-shadow: 0 0 10px rgba(255,255,255,0.5);
}
.lad-now-lab {
  font-family: var(--font-mono); font-size: 9.5px; font-weight: 600;
  color: var(--n-900); margin-bottom: 4px; white-space: nowrap;
}

.pos-meta { display: grid; grid-template-columns: repeat(3, 1fr); gap: 13px 10px; }
.pm-k { font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 0.12em; color: var(--n-500); text-transform: uppercase; margin-bottom: 4px; }
.pm-v { font-family: var(--font-mono); font-size: 13px; font-weight: 600; color: var(--n-800); }

/* Fleet state strip — pinned to the panel floor, never clipped */
.fleet {
  margin-top: auto; padding: 10px 12px;
  border-top: 1px solid var(--line);
  background: var(--n-050);
  flex-shrink: 0;
}
.fleet-row { display: flex; align-items: center; gap: 8px; margin-bottom: 7px; }
.fleet-row:last-child { margin-bottom: 0; }
.fleet-k {
  font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 0.12em;
  color: var(--n-500); text-transform: uppercase;
}
.fleet-row > .fleet-k:first-child { min-width: 48px; }
.fleet-v { font-family: var(--font-mono); font-size: 10.5px; color: var(--n-800); font-weight: 500; }
.fleet-facts { gap: 14px; flex-wrap: wrap; }

/* Regime distribution bar */
.regbar { display: flex; height: 6px; border-radius: 3px; overflow: hidden; flex: 1; gap: 1.5px; }
.regbar span { display: block; }
.reglegend { display: flex; gap: 10px; flex-wrap: wrap; }
.rl { display: flex; align-items: center; gap: 4px; font-family: var(--font-mono); font-size: 9.5px; color: var(--n-600); }
.rl i { width: 7px; height: 7px; border-radius: 2px; display: block; }

/* ============================================================
   INTEL PANEL (conviction + signal intelligence)
   ============================================================ */
.intel { display: flex; flex-direction: column; height: 100%; }

.conviction {
  display: flex; align-items: center; gap: 14px;
  padding: 13px; border-bottom: 1px solid var(--line);
  flex-shrink: 0;
}
.gauge { position: relative; width: 78px; height: 78px; flex-shrink: 0; }
.gauge svg { transform: rotate(-90deg); display: block; }
.gauge-val {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
}
.gauge-num {
  font-family: var(--font-mono); font-size: 23px; font-weight: 700;
  color: var(--n-900); line-height: 1; font-variant-numeric: tabular-nums;
}
.gauge-lab {
  font-family: var(--font-mono); font-size: 7.5px; letter-spacing: 0.14em;
  color: var(--n-500); text-transform: uppercase; margin-top: 3px;
}

.conv-stats { flex: 1; display: grid; grid-template-columns: 1fr 1fr; gap: 9px 12px; min-width: 0; }
.cs-k { font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 0.13em; color: var(--n-500); text-transform: uppercase; }
.cs-v { font-family: var(--font-mono); font-size: 15px; font-weight: 600; color: var(--n-900); margin-top: 3px; font-variant-numeric: tabular-nums; }
.cs-v.pos { color: var(--pos); } .cs-v.neg { color: var(--neg); }

/* Chokepoint bar — which gate is killing the most candidates */
.chokepoint {
  padding: 9px 13px; border-bottom: 1px solid var(--line);
  flex-shrink: 0;
}
.chk-head {
  display: flex; align-items: baseline; gap: 8px; margin-bottom: 7px;
  font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 0.16em;
  color: var(--n-500); text-transform: uppercase;
}
.chk-head b { color: var(--neg); font-size: 10.5px; letter-spacing: 0.04em; }
.chk-head .n { margin-left: auto; color: var(--n-600); letter-spacing: 0.04em; }
.chk-rows { display: flex; flex-direction: column; gap: 4px; }
.chk-row { display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 9.5px; }
.chk-name { color: var(--n-700); min-width: 76px; }
.chk-bar { flex: 1; height: 4px; background: var(--n-250); border-radius: 2px; overflow: hidden; }
.chk-fill { height: 100%; border-radius: 2px; background: var(--neg); opacity: 0.85; }
.chk-n { color: var(--n-600); min-width: 26px; text-align: right; }
.chk-gap {
  margin-top: 7px; padding-top: 6px;
  border-top: 1px dashed var(--line-hi);
  font-family: var(--font-mono); font-size: 9.5px; color: var(--n-600);
}
.chk-gap b { color: var(--warn); font-weight: 600; }
.chk-shadow {
  margin-top: 5px; font-family: var(--font-mono); font-size: 9.5px;
  line-height: 1.5;
}
.sv-k {
  color: var(--n-500); letter-spacing: 0.1em; text-transform: uppercase;
  font-size: 8.5px;
}
.sv-good    { color: var(--pos); font-weight: 600; }
.sv-bad     { color: var(--neg); font-weight: 600; }
.sv-neutral { color: var(--n-700); font-weight: 600; }
.sv-wait    { color: var(--warn); font-weight: 600; }
.sv-dim     { color: var(--n-600); }

/* Signal intelligence list */
.sig-list { flex: 1; min-height: 0; overflow-y: auto; padding: 10px; }
.sig-card {
  background: var(--n-150); border: 1px solid var(--line);
  border-radius: var(--r-md); padding: 10px; margin-bottom: 8px;
}
.sig-card:last-child { margin-bottom: 0; }
.sig-top { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.sig-sym { font-family: var(--font-mono); font-size: 13px; font-weight: 700; color: var(--n-900); }
.sig-conf { margin-left: auto; font-family: var(--font-mono); font-size: 11px; color: var(--n-600); }
.sig-conf b { color: var(--n-900); font-weight: 600; }

.sig-track { display: flex; gap: 2px; height: 5px; margin-bottom: 7px; }
.sig-seg { flex: 1; border-radius: 1px; background: var(--n-300); position: relative; }
.sig-seg.pass { background: var(--pos-dim); }
.sig-seg.fail { background: var(--neg); }

.sig-foot {
  display: flex; align-items: center; gap: 8px;
  font-family: var(--font-mono); font-size: 10px; color: var(--n-600);
}
.sig-foot .blocked { color: var(--neg); }
.sig-foot .letters { color: var(--gold); letter-spacing: 0.06em; }
.sig-foot .mem { margin-left: auto; }

/* ============================================================
   DECISION STREAM
   ============================================================ */
.stream { height: 100%; overflow-y: auto; padding: 8px 10px; }
.ds-row {
  display: grid;
  grid-template-columns: 44px 1fr auto;
  gap: 9px; align-items: baseline;
  padding: 7px 9px; border-radius: var(--r-sm);
  border-left: 2px solid var(--n-400);
  background: var(--n-150);
  margin-bottom: 5px;
  font-family: var(--font-mono); font-size: 11px;
  animation: slideIn 0.25s ease-out;
}
@keyframes slideIn {
  from { opacity: 0; transform: translateY(-5px); }
  to   { opacity: 1; transform: translateY(0); }
}
.ds-row.entered { border-left-color: var(--pos); background: rgba(38,208,160,0.06); }
.ds-row.blocked { border-left-color: var(--neg-dim); }
.ds-row.shifted { border-left-color: var(--warn); }
.ds-shift {
  font-size: 9px; color: var(--warn);
  background: rgba(245,165,36,0.1);
  border: 1px solid rgba(245,165,36,0.28);
  padding: 1px 5px; border-radius: 3px;
}
.ds-time { color: var(--n-500); font-size: 10px; }
.ds-body { min-width: 0; }
.ds-line1 { display: flex; align-items: baseline; gap: 7px; flex-wrap: wrap; }
.ds-sym { font-weight: 700; color: var(--n-900); }
.ds-dir { font-size: 9px; padding: 1px 4px; border-radius: 2px; font-weight: 700; }
.ds-dir.long  { color: var(--pos); background: rgba(38,208,160,0.12); }
.ds-dir.short { color: var(--neg); background: rgba(255,77,106,0.12); }
.ds-sigs { color: var(--gold); font-size: 10px; }
.ds-verb { color: var(--n-600); font-size: 10px; }
.ds-verb .gate { color: var(--neg); }
.ds-line2 {
  color: var(--n-500); font-size: 9.5px; margin-top: 3px;
  display: flex; gap: 10px; flex-wrap: wrap;
}
.ds-line2 b { color: var(--n-700); font-weight: 500; }
.ds-right { text-align: right; display: flex; flex-direction: column; align-items: flex-end; gap: 3px; }
.ds-rep {
  font-size: 9px; color: var(--n-600); background: var(--n-250);
  padding: 1px 5px; border-radius: 8px; border: 1px solid var(--line-hi);
}
.ds-mem { font-size: 9.5px; }
.ds-mem.good { color: var(--pos); }
.ds-mem.bad  { color: var(--neg); }
.ds-mem.none { color: var(--n-500); }

/* ============================================================
   MARKET PANEL (symbol grid + brain)
   ============================================================ */
.market { display: flex; flex-direction: column; height: 100%; }
.sym-grid {
  flex: 1; min-height: 0; overflow-y: auto;
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 3px; padding: 7px; align-content: start;
}
.sym {
  display: flex; align-items: center; gap: 6px;
  padding: 4px 7px; border-radius: var(--r-sm);
  background: var(--n-150); border: 1px solid var(--line);
  font-family: var(--font-mono); font-size: 10.5px;
  cursor: default; transition: border-color 0.15s, background 0.15s;
  min-width: 0;
}
.sym:hover { border-color: var(--line-hi); background: var(--n-200); }
.sym.haspos { border-color: rgba(212,167,44,0.45); background: rgba(212,167,44,0.06); }
.sym-name { font-weight: 700; color: var(--n-800); min-width: 42px; flex-shrink: 0; }
.sym-px { color: var(--n-600); font-size: 10px; flex: 1; text-align: right; font-variant-numeric: tabular-nums; overflow: hidden; text-overflow: ellipsis; }
.sym-reg {
  font-size: 8px; font-weight: 700; letter-spacing: 0.06em;
  padding: 2px 5px; border-radius: 3px; flex-shrink: 0;
}
.sym-reg.bull  { color: var(--r-bull);  background: rgba(38,208,160,0.12); }
.sym-reg.bear  { color: var(--r-bear);  background: rgba(255,77,106,0.12); }
.sym-reg.range { color: var(--r-range); background: rgba(167,139,250,0.14); }
.sym-reg.chop  { color: var(--r-chop);  background: rgba(122,128,145,0.14); }

/* Brain strip */
.brain {
  flex-shrink: 0; padding: 9px 11px;
  border-top: 1px solid var(--line);
  background: var(--n-050);
}
.brain-head { display: flex; align-items: center; gap: 8px; margin-bottom: 7px; }
.brain-title {
  font-family: var(--font-mono); font-size: 9px; letter-spacing: 0.16em;
  color: var(--n-500); text-transform: uppercase;
}
.brain-tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; margin-bottom: 7px; }
.bt {
  text-align: center; padding: 5px 3px; border-radius: var(--r-sm);
  background: var(--n-150); border: 1px solid var(--line);
}
.bt-k { font-family: var(--font-mono); font-size: 7.5px; letter-spacing: 0.1em; color: var(--n-500); text-transform: uppercase; }
.bt-v { font-family: var(--font-mono); font-size: 14px; font-weight: 700; color: var(--n-800); margin-top: 2px; line-height: 1; }
.bt.armed { border-color: rgba(38,208,160,0.4); background: rgba(38,208,160,0.07); }
.bt.armed .bt-v { color: var(--pos); }

.arm-wrap { }
.arm-head {
  display: flex; justify-content: space-between; align-items: baseline;
  font-family: var(--font-mono); font-size: 9px; color: var(--n-500);
  margin-bottom: 5px;
}
.arm-head b { color: var(--n-800); }
.arm-track { height: 4px; background: var(--n-250); border-radius: 2px; overflow: hidden; }
.arm-fill {
  height: 100%; border-radius: 2px;
  background: linear-gradient(90deg, var(--gold-dim), var(--gold));
  transition: width 0.5s ease;
}

/* Utility */
.mono { font-family: var(--font-mono); }
.dimtext { color: var(--n-500); }
.empty {
  display: flex; align-items: center; justify-content: center;
  height: 100%; font-family: var(--font-mono); font-size: 11px;
  color: var(--n-500); flex-direction: column; gap: 6px; padding: 20px;
  text-align: center;
}

/* Stale-data overlay */
.stale { position: relative; }
.stale::after {
  content: 'FEED STALE'; position: absolute; top: 8px; right: 10px;
  font-family: var(--font-mono); font-size: 8.5px; letter-spacing: 0.14em;
  color: var(--warn); background: rgba(245,165,36,0.12);
  border: 1px solid rgba(245,165,36,0.3); padding: 2px 6px;
  border-radius: 3px; z-index: 10;
}

/* Responsive fallback for smaller screens */
/* Keep the symbol grid 2-up as long as it fits — a single column
   shows only ~8 of 20 pairs, which defeats the point of the grid. */
@media (max-width: 1180px) {
  .sym-grid { grid-template-columns: 1fr; }
}
@media (max-width: 1200px) {
  .grid { grid-template-columns: repeat(6, 1fr); grid-template-rows: auto auto auto; }
  .area-equity  { grid-column: 1 / 7; grid-row: 1; min-height: 300px; }
  .area-command { grid-column: 1 / 4; grid-row: 2; min-height: 380px; }
  .area-intel   { grid-column: 4 / 7; grid-row: 2; }
  .area-stream  { grid-column: 1 / 4; grid-row: 3; min-height: 300px; }
  .area-market  { grid-column: 4 / 7; grid-row: 3; }
  html, body { overflow: auto; }
  .app { height: auto; min-height: 100vh; }
}
</style>
</head>
<body>
<div class="app">

  <!-- ===================== TOP BAR ===================== -->
  <header class="topbar">
    <div class="brand">
      <div class="brand-mark">
        <svg viewBox="0 0 32 32" fill="none">
          <circle cx="16" cy="16" r="14" stroke="#d4a72c" stroke-width="1.4" opacity="0.35"/>
          <circle cx="16" cy="16" r="9.5" stroke="#d4a72c" stroke-width="1.1" opacity="0.6"/>
          <circle cx="16" cy="16" r="4.5" fill="#d4a72c" opacity="0.9"/>
          <circle cx="16" cy="16" r="1.8" fill="#06070a"/>
          <path d="M16 0.5v6M16 25.5v6M0.5 16h6M25.5 16h6" stroke="#d4a72c" stroke-width="1.2" opacity="0.75"/>
        </svg>
      </div>
      <div>
        <div class="brand-text">GOLDENEYE</div>
        <div class="brand-sub">MISSION CONTROL</div>
      </div>
    </div>

    <div class="stats">
      <div class="stat">
        <div class="stat-k">Exchange</div>
        <div class="conn"><span class="dot" id="connDot"></span><span id="connText">CONNECTING</span></div>
      </div>
      <div class="stat">
        <div class="stat-k">Equity</div>
        <div class="stat-v" id="tEquity">—</div>
      </div>
      <div class="stat">
        <div class="stat-k">Session P/L</div>
        <div class="stat-v" id="tSession">—</div>
      </div>
      <div class="stat">
        <div class="stat-k">Open / Max</div>
        <div class="stat-v" id="tPos">—</div>
      </div>
      <div class="stat">
        <div class="stat-k">Heat</div>
        <div class="stat-v sm" id="tHeat">—</div>
      </div>
      <div class="stat">
        <div class="stat-k">Threads</div>
        <div class="threads">
          <div class="tmeter" id="tMeter"></div>
          <div class="stat-v sm" id="tThreads">—</div>
        </div>
      </div>
      <div class="stat">
        <div class="stat-k">Mode</div>
        <div><span class="chip paper" id="tMode">—</span></div>
      </div>
      <div class="stat">
        <div class="stat-k">Uptime</div>
        <div class="stat-v sm" id="tUptime">—</div>
      </div>
      <div class="stat">
        <div class="stat-k">CPU / Mem</div>
        <div class="stat-v sm" id="tSys">—</div>
      </div>
    </div>

    <div class="clock">
      <div class="clock-main"><span class="tz">MDT</span><span id="clkMdt">--:--:--</span></div>
      <div class="clock-sub">UTC <span id="clkUtc">--:--</span></div>
    </div>
  </header>

  <!-- ===================== GRID ===================== -->
  <div class="grid">

    <!-- EQUITY -->
    <section class="panel area-equity">
      <div class="panel-head">
        <div class="panel-title">Equity Curve</div>
        <div class="panel-tools">
          <span class="tag" id="eqRange">—</span>
          <div class="seg" id="eqSeg">
            <button data-w="6">6H</button>
            <button data-w="24" class="on">24H</button>
            <button data-w="72">3D</button>
            <button data-w="0">ALL</button>
          </div>
        </div>
      </div>
      <div class="panel-body">
        <div class="eq-wrap">
          <div class="eq-summary">
            <div class="eq-cell">
              <div class="eq-k">Balance</div>
              <div class="eq-v big" id="eqBal">—</div>
            </div>
            <div class="eq-cell">
              <div class="eq-k">Net P/L</div>
              <div class="eq-v" id="eqPnl">—</div>
            </div>
            <div class="eq-cell">
              <div class="eq-k">Peak</div>
              <div class="eq-v" id="eqPeak">—</div>
            </div>
            <div class="eq-cell">
              <div class="eq-k">Drawdown</div>
              <div class="eq-v" id="eqDd">—</div>
            </div>
            <div class="eq-cell">
              <div class="eq-k">Unrealized</div>
              <div class="eq-v" id="eqUpnl">—</div>
            </div>
          </div>
          <div class="eq-chart" id="eqChart">
            <canvas id="eqCanvas"></canvas>
            <div class="eq-tip" id="eqTip"></div>
          </div>
        </div>
      </div>
    </section>

    <!-- COMMAND -->
    <section class="panel area-command">
      <div class="panel-head">
        <div class="panel-title">Command</div>
        <div class="panel-tools"><span class="tag" id="cmdTag">—</span></div>
      </div>
      <div class="panel-body">
        <div class="cmd" id="cmdBody">
          <div class="empty">INITIALIZING</div>
        </div>
      </div>
    </section>

    <!-- INTEL -->
    <section class="panel area-intel">
      <div class="panel-head">
        <div class="panel-title">Conviction &amp; Signal Intel</div>
        <div class="panel-tools"><span class="tag" id="intelTag">TOP 4</span></div>
      </div>
      <div class="panel-body">
        <div class="intel">
          <div class="conviction">
            <div class="gauge">
              <svg width="78" height="78" viewBox="0 0 78 78">
                <circle cx="39" cy="39" r="33" fill="none" stroke="#2a2e3a" stroke-width="6"/>
                <circle id="gaugeArc" cx="39" cy="39" r="33" fill="none" stroke="#d4a72c"
                        stroke-width="6" stroke-linecap="round"
                        stroke-dasharray="207.3" stroke-dashoffset="207.3"
                        style="transition: stroke-dashoffset 0.6s ease, stroke 0.4s"/>
              </svg>
              <div class="gauge-val">
                <div class="gauge-num" id="gaugeNum">—</div>
                <div class="gauge-lab" id="gaugeLab">Conviction</div>
              </div>
            </div>
            <div class="conv-stats">
              <div><div class="cs-k">Trades</div><div class="cs-v" id="cvTrades">—</div></div>
              <div><div class="cs-k">Win Rate</div><div class="cs-v" id="cvWr">—</div></div>
              <div><div class="cs-k">Avg R</div><div class="cs-v" id="cvR">—</div></div>
              <div><div class="cs-k">Net P/L</div><div class="cs-v" id="cvPnl">—</div></div>
            </div>
          </div>
          <div class="chokepoint" id="chokepoint"></div>
          <div class="sig-list" id="sigList">
            <div class="empty">NO CANDIDATES</div>
          </div>
        </div>
      </div>
    </section>

    <!-- STREAM -->
    <section class="panel area-stream">
      <div class="panel-head">
        <div class="panel-title">Decision Board</div>
        <div class="panel-tools">
          <span class="tag" id="streamRate">—</span>
          <div class="seg" id="streamSeg">
            <button data-f="all" class="on">ALL</button>
            <button data-f="entered">FILLS</button>
          </div>
        </div>
      </div>
      <div class="panel-body">
        <div class="stream" id="streamBody">
          <div class="empty">AWAITING DECISIONS</div>
        </div>
      </div>
    </section>

    <!-- MARKET -->
    <section class="panel area-market">
      <div class="panel-head">
        <div class="panel-title">Market &amp; Brain</div>
        <div class="panel-tools"><span class="tag" id="mktTag">—</span></div>
      </div>
      <div class="panel-body">
        <div class="market">
          <div class="sym-grid" id="symGrid">
            <div class="empty">LOADING UNIVERSE</div>
          </div>
          <div class="brain">
            <div class="brain-head">
              <div class="brain-title">Global Bayesian Brain</div>
              <span class="tag" id="brainTag" style="margin-left:auto">—</span>
            </div>
            <div class="brain-tiles" id="brainTiles"></div>
            <div class="arm-wrap">
              <div class="arm-head">
                <span id="armLabel">OVERRIDE ARMING</span>
                <b id="armCount">—</b>
              </div>
              <div class="arm-track"><div class="arm-fill" id="armFill" style="width:0%"></div></div>
            </div>
          </div>
        </div>
      </div>
    </section>

  </div>
</div>

<script>
'use strict';

/* ============================================================
   GOLDENEYE MISSION CONTROL — client
   Data contract notes:
   - /api/equity carries warm-up rows (bal=0) and a stale default
     balance regime (bal=10000) from before the live balance was
     set. We isolate the CURRENT balance regime so the chart shows
     real performance instead of a config artifact.
   - /api/decisions repeats the same symbol/gate every scan tick;
     we collapse consecutive duplicates into one row + repeat count.
   ============================================================ */

const $  = id => document.getElementById(id);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

const GATE_ORDER = ['correlation','factor_floors','confluence','dir_wr','regime_mult',
  'whale','ai_conf','kill_switch','max_positions','drawdown','heat_cap','sentiment',
  'atr_fees','min_size','notional','duplicate'];

const REGIME_COLOR = { bull:'#26d0a0', bear:'#ff4d6a', range:'#a78bfa', chop:'#7a8091' };
const FACTOR_KEYS = ['trend','momentum','volume','volatility','structure','order_flow'];

/* ---------- formatting ---------- */
function money(v, dp) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  dp = dp === undefined ? 2 : dp;
  const s = Math.abs(v).toLocaleString('en-US', {minimumFractionDigits:dp, maximumFractionDigits:dp});
  return (v < 0 ? '-$' : '$') + s;
}
function signed(v, dp) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  dp = dp === undefined ? 2 : dp;
  const s = Math.abs(v).toLocaleString('en-US', {minimumFractionDigits:dp, maximumFractionDigits:dp});
  return (v < 0 ? '−$' : '+$') + s;
}
function pct(v, dp) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  return v.toFixed(dp === undefined ? 1 : dp) + '%';
}
function shortSym(s) { return String(s || '').replace('/USD','').replace('/USDT',''); }
function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function agoTxt(ts) {
  const d = Date.now()/1000 - ts;
  if (d < 60)    return Math.max(0, Math.floor(d)) + 's';
  if (d < 3600)  return Math.floor(d/60) + 'm';
  if (d < 86400) return Math.floor(d/3600) + 'h';
  return Math.floor(d/86400) + 'd';
}
function hhmm(ts) {
  const d = new Date(ts * 1000);
  return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
}
function uptimeTxt(sec) {
  if (!sec && sec !== 0) return '—';
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60);
  return h >= 24 ? Math.floor(h/24)+'d '+(h%24)+'h' : h+'h '+m+'m';
}

/* ---------- resilient fetch ---------- */
async function grab(url, fallback) {
  try {
    const r = await fetch(url, {cache:'no-store'});
    if (!r.ok) return fallback;
    return await r.json();
  } catch (e) { return fallback; }
}

/* ---------- shared state ---------- */
const S = {
  health: null, equity: [], markers: [], decisions: [], analytics: null,
  symbols: [], positions: [], brain: null, sanity: null, funnel: null, shadow: null,
  eqWindow: 24, streamFilter: 'all',
  lastGood: 0
};

/* ============================================================
   CLOCK
   ============================================================ */
function tickClock() {
  const now = new Date();
  const utcMs = now.getTime() + now.getTimezoneOffset()*60000;
  const mdt = new Date(utcMs - 6*3600000);   // MDT = UTC-6 fixed
  const utc = new Date(utcMs);
  const p = n => String(n).padStart(2,'0');
  $('clkMdt').textContent = p(mdt.getHours())+':'+p(mdt.getMinutes())+':'+p(mdt.getSeconds());
  $('clkUtc').textContent = p(utc.getHours())+':'+p(utc.getMinutes());
}
setInterval(tickClock, 1000); tickClock();

/* ============================================================
   TOP BAR
   ============================================================ */
function renderTop() {
  const h = S.health;
  const dot = $('connDot'), txt = $('connText');

  if (!h || h.bot_online === false || h.error) {
    dot.className = 'dot bad';
    txt.textContent = 'OFFLINE';
    ['tEquity','tSession','tPos','tHeat','tThreads','tUptime','tSys'].forEach(i => $(i).textContent = '—');
    return;
  }

  const connected = (h.kraken_api === 'connected');
  dot.className = connected ? 'dot' : 'dot bad';
  txt.textContent = connected ? 'KRAKEN LIVE' : 'KRAKEN DOWN';

  const eq = h.equity !== undefined ? h.equity : h.paper_balance;
  $('tEquity').textContent = money(eq);

  const sp = h.circuit_breaker_daily_pnl;
  const spEl = $('tSession');
  spEl.textContent = signed(sp);
  spEl.style.color = sp > 0 ? 'var(--pos)' : (sp < 0 ? 'var(--neg)' : 'var(--n-900)');

  $('tPos').textContent = (h.open_positions !== undefined ? h.open_positions : 0) + ' / ' + (h.max_positions || 20);
  $('tHeat').textContent = money(h.portfolio_heat || 0);

  // Thread meter
  const ok = h.threads_healthy || 0, tot = h.total_threads || 0;
  $('tThreads').textContent = ok + '/' + tot;
  const meter = $('tMeter');
  if (meter.childElementCount !== tot) {
    meter.innerHTML = '';
    for (let i = 0; i < tot; i++) {
      const b = document.createElement('div');
      b.className = 'tbar';
      meter.appendChild(b);
    }
  }
  Array.from(meter.children).forEach((b,i) => {
    b.className = 'tbar' + (i < ok ? ' on' : '');
  });

  const mode = (h.trading_mode || 'paper').toUpperCase();
  const mEl = $('tMode');
  mEl.textContent = mode;
  mEl.className = 'chip ' + (mode === 'LIVE' ? 'live' : 'paper');

  $('tUptime').textContent = uptimeTxt(h.uptime_seconds);
  $('tSys').textContent = Math.round(h.cpu_percent||0) + '% / ' + Math.round(h.memory_percent||0) + '%';
}

/* ============================================================
   EQUITY CHART
   ============================================================
   Data hygiene: the feed contains warm-up rows (bal=0) and a
   stale default-balance regime (bal=10000) recorded before the
   live balance was configured. Plotting them raw produces a
   fake cliff. We keep only the trailing regime whose scale
   matches the current balance.
   ============================================================ */
function cleanEquity(raw, currentBal) {
  let rows = (raw || []).filter(r => r && typeof r.ts === 'number' && r.bal > 0);
  if (!rows.length) return [];
  rows.sort((a,b) => a.ts - b.ts);

  // Anchor to the current balance if we know it, else the last row.
  const anchor = (currentBal && currentBal > 0) ? currentBal : rows[rows.length-1].bal;

  // Walk backwards while values stay within the same order of magnitude
  // as the anchor. A jump of >3x marks a balance reset, not a trade.
  let start = rows.length - 1;
  for (let i = rows.length - 1; i >= 0; i--) {
    const ratio = rows[i].bal / anchor;
    if (ratio > 3 || ratio < 0.33) break;
    start = i;
  }
  let out = rows.slice(start);

  // Guard: if the cut left too little to draw, fall back to whatever
  // shares the final row's magnitude.
  if (out.length < 2 && rows.length >= 2) {
    const last = rows[rows.length-1].bal;
    out = rows.filter(r => r.bal / last <= 3 && r.bal / last >= 0.33);
  }
  return out;
}

function windowEquity(rows, hours) {
  if (!hours || !rows.length) return rows;
  const cutoff = rows[rows.length-1].ts - hours*3600;
  const w = rows.filter(r => r.ts >= cutoff);
  return w.length >= 2 ? w : rows.slice(-2);
}

let eqGeom = null;   // for hit-testing the tooltip

function drawEquity() {
  const cv = $('eqCanvas'), box = $('eqChart');
  if (!cv || !box) return;
  const dpr = window.devicePixelRatio || 1;
  const W = box.clientWidth, H = box.clientHeight;
  if (W <= 0 || H <= 0) return;
  cv.width = W*dpr; cv.height = H*dpr;
  cv.style.width = W+'px'; cv.style.height = H+'px';
  const c = cv.getContext('2d');
  c.setTransform(dpr,0,0,dpr,0,0);
  c.clearRect(0,0,W,H);

  const bal = S.health ? (S.health.equity !== undefined ? S.health.equity : S.health.paper_balance) : null;
  const cleaned = cleanEquity(S.equity, bal);
  const rows = windowEquity(cleaned, S.eqWindow);

  const old = box.querySelector('.eq-empty');
  if (old) old.remove();

  if (rows.length < 2) {
    const d = document.createElement('div');
    d.className = 'eq-empty';
    d.innerHTML = '<div>AWAITING EQUITY HISTORY</div><div style="color:var(--n-600);font-size:10px">need 2+ samples in window</div>';
    box.appendChild(d);
    $('eqRange').textContent = '—';
    eqGeom = null;
    return;
  }

  const padL = 62, padR = 16, padT = 16, padB = 26;
  const plotW = W - padL - padR, plotH = H - padT - padB;

  // Series: equity = balance + unrealized
  const vals = rows.map(r => r.bal + (r.upnl || 0));
  let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  if (hi - lo < 1e-9) { hi = lo + 1; lo = lo - 1; }
  const pad = (hi - lo) * 0.14;
  lo -= pad; hi += pad;

  const t0 = rows[0].ts, t1 = rows[rows.length-1].ts;
  const span = Math.max(1, t1 - t0);
  const X = ts => padL + ((ts - t0)/span) * plotW;
  const Y = v  => padT + (1 - (v - lo)/(hi - lo)) * plotH;

  eqGeom = {rows, vals, X, Y, padL, padR, padT, padB, plotW, plotH, t0, t1, span, lo, hi, W, H};

  // ---- grid + y labels
  c.font = '10px "JetBrains Mono", monospace';
  c.textAlign = 'right'; c.textBaseline = 'middle';
  const TICKS = 5;
  for (let i = 0; i <= TICKS; i++) {
    const v = lo + (hi-lo)*(i/TICKS), y = Y(v);
    c.strokeStyle = 'rgba(255,255,255,0.045)';
    c.lineWidth = 1;
    c.beginPath(); c.moveTo(padL, y+0.5); c.lineTo(W-padR, y+0.5); c.stroke();
    c.fillStyle = '#565c6d';
    c.fillText('$' + v.toFixed(v < 100 ? 2 : 0), padL - 9, y);
  }

  // ---- x labels
  c.textAlign = 'center'; c.textBaseline = 'top';
  const XT = 5;
  for (let i = 0; i <= XT; i++) {
    const ts = t0 + span*(i/XT), x = X(ts);
    c.fillStyle = '#565c6d';
    c.fillText(hhmm(ts), x, H - padB + 7);
  }

  // ---- baseline (starting equity) reference
  const startV = vals[0];
  const yStart = Y(startV);
  c.strokeStyle = 'rgba(212,167,44,0.28)';
  c.lineWidth = 1; c.setLineDash([4,4]);
  c.beginPath(); c.moveTo(padL, yStart+0.5); c.lineTo(W-padR, yStart+0.5); c.stroke();
  c.setLineDash([]);

  const endV = vals[vals.length-1];
  const up = endV >= startV;
  const line = up ? '#26d0a0' : '#ff4d6a';

  // ---- area fill
  const grad = c.createLinearGradient(0, padT, 0, padT+plotH);
  grad.addColorStop(0, up ? 'rgba(38,208,160,0.26)' : 'rgba(255,77,106,0.26)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  c.beginPath();
  c.moveTo(X(rows[0].ts), Y(vals[0]));
  for (let i = 1; i < rows.length; i++) c.lineTo(X(rows[i].ts), Y(vals[i]));
  c.lineTo(X(rows[rows.length-1].ts), padT+plotH);
  c.lineTo(X(rows[0].ts), padT+plotH);
  c.closePath();
  c.fillStyle = grad; c.fill();

  // ---- line
  c.beginPath();
  c.moveTo(X(rows[0].ts), Y(vals[0]));
  for (let i = 1; i < rows.length; i++) c.lineTo(X(rows[i].ts), Y(vals[i]));
  c.strokeStyle = line; c.lineWidth = 1.8;
  c.lineJoin = 'round'; c.lineCap = 'round';
  c.shadowColor = up ? 'rgba(38,208,160,0.5)' : 'rgba(255,77,106,0.5)';
  c.shadowBlur = 9;
  c.stroke();
  c.shadowBlur = 0;

  // ---- trade markers within window
  const mk = (S.markers || []).filter(m => m.ts >= t0 && m.ts <= t1);
  mk.forEach(m => {
    const x = X(m.ts);
    // place marker on the curve
    let yi = 0;
    for (let i = 0; i < rows.length; i++) { if (rows[i].ts <= m.ts) yi = i; }
    const y = Y(vals[yi]);
    const isOpen = m.type === 'open';
    const win = (m.pnl || 0) >= 0;
    c.beginPath();
    if (isOpen) {
      c.arc(x, y, 3, 0, Math.PI*2);
      c.fillStyle = '#22d3ee';
    } else {
      // triangle: up for win, down for loss
      const s = 4.5;
      if (win) { c.moveTo(x, y-s); c.lineTo(x+s, y+s*0.8); c.lineTo(x-s, y+s*0.8); }
      else     { c.moveTo(x, y+s); c.lineTo(x+s, y-s*0.8); c.lineTo(x-s, y-s*0.8); }
      c.closePath();
      c.fillStyle = win ? '#26d0a0' : '#ff4d6a';
    }
    c.fill();
    c.strokeStyle = '#06070a'; c.lineWidth = 1; c.stroke();
  });

  // ---- head dot
  const hx = X(rows[rows.length-1].ts), hy = Y(endV);
  c.beginPath(); c.arc(hx, hy, 8, 0, Math.PI*2);
  c.fillStyle = up ? 'rgba(38,208,160,0.16)' : 'rgba(255,77,106,0.16)'; c.fill();
  c.beginPath(); c.arc(hx, hy, 3.6, 0, Math.PI*2);
  c.fillStyle = line; c.fill();
  c.strokeStyle = '#06070a'; c.lineWidth = 1.6; c.stroke();

  // ---- head value label
  const lbl = money(endV);
  c.font = '600 12px "JetBrains Mono", monospace';
  c.textAlign = 'right'; c.textBaseline = 'bottom';
  const tw = c.measureText(lbl).width;
  const bx = Math.min(hx + 8, W - padR), by = hy - 11;
  c.fillStyle = 'rgba(6,7,10,0.85)';
  c.fillRect(bx - tw - 7, by - 13, tw + 10, 17);
  c.fillStyle = line;
  c.fillText(lbl, bx - 2, by + 1);

  // summary + range tag
  const hrs = (t1 - t0)/3600;
  $('eqRange').textContent = rows.length + ' pts · ' + (hrs >= 24 ? (hrs/24).toFixed(1)+'d' : hrs.toFixed(1)+'h');

  const peak = Math.max.apply(null, vals);
  const dd = peak > 0 ? (peak - endV)/peak*100 : 0;
  const net = endV - startV;

  $('eqBal').textContent = money(endV);
  const pEl = $('eqPnl');
  pEl.textContent = signed(net);
  pEl.className = 'eq-v ' + (net > 0 ? 'pos' : net < 0 ? 'neg' : '');
  $('eqPeak').textContent = money(peak);
  const dEl = $('eqDd');
  dEl.textContent = pct(dd);
  dEl.className = 'eq-v ' + (dd > 10 ? 'neg' : '');

  const up_ = S.health ? (S.health.unrealized_pnl || 0) : 0;
  const uEl = $('eqUpnl');
  uEl.textContent = signed(up_);
  uEl.className = 'eq-v ' + (up_ > 0 ? 'pos' : up_ < 0 ? 'neg' : '');
}

/* ---- chart tooltip ---- */
(function initEqTip(){
  const box = $('eqChart'), tip = $('eqTip');
  if (!box) return;
  box.addEventListener('mousemove', e => {
    if (!eqGeom) { tip.style.opacity = 0; return; }
    const r = box.getBoundingClientRect();
    const mx = e.clientX - r.left;
    const g = eqGeom;
    if (mx < g.padL || mx > g.W - g.padR) { tip.style.opacity = 0; return; }
    const tsAt = g.t0 + ((mx - g.padL)/g.plotW)*g.span;
    let bi = 0, bd = Infinity;
    for (let i = 0; i < g.rows.length; i++) {
      const d = Math.abs(g.rows[i].ts - tsAt);
      if (d < bd) { bd = d; bi = i; }
    }
    const row = g.rows[bi], v = g.vals[bi];
    tip.innerHTML =
      '<div><span class="t-lab">EQ</span><b>' + money(v) + '</b></div>' +
      '<div><span class="t-lab">BAL</span>' + money(row.bal) + '</div>' +
      (row.upnl ? '<div><span class="t-lab">UPNL</span>' + signed(row.upnl) + '</div>' : '') +
      '<div><span class="t-lab">POS</span>' + (row.pos||0) + '</div>' +
      '<div><span class="t-lab">TIME</span>' + hhmm(row.ts) + '</div>';
    tip.style.opacity = 1;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let lx = g.X(row.ts) + 14;
    if (lx + tw > g.W - 6) lx = g.X(row.ts) - tw - 14;
    let ly = g.Y(v) - th/2;
    ly = clamp(ly, 4, g.H - th - 4);
    tip.style.left = lx + 'px';
    tip.style.top  = ly + 'px';
  });
  box.addEventListener('mouseleave', () => { tip.style.opacity = 0; });
})();

/* segmented range control */
$('eqSeg').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  Array.from($('eqSeg').children).forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  S.eqWindow = parseInt(b.dataset.w, 10);
  drawEquity();
});

/* ============================================================
   COMMAND PANEL
   ============================================================ */
/* Counterfactual scorecard for the gate that's blocking the most.
   Shadow trades track what blocked entries WOULD have done, so a gate
   can be judged on outcomes instead of opinion. Deliberately refuses to
   render a verdict below the sample floor — a 1-trade "result" is noise,
   and showing it as a verdict would be worse than showing nothing. */
function shadowVerdictHtml(gateName) {
  const sv = S.shadow && S.shadow.gates ? S.shadow.gates[gateName] : null;
  if (!sv || !sv.blocked_total) return '';
  const need = sv.min_sample || 20;

  if (!sv.resolved || sv.resolved < need) {
    const have = sv.resolved || 0;
    return '<div class="chk-shadow">' +
      '<span class="sv-k">counterfactual</span> ' +
      '<span class="sv-wait">' + have + '/' + need + ' resolved</span> ' +
      '<span class="sv-dim">— ' + sv.blocked_total + ' blocked tracked' +
      (sv.expired ? ', ' + sv.expired + ' expired' : '') + '</span>' +
    '</div>';
  }

  const bad = sv.avg_r > 0.10;          // gate rejected winners
  const good = sv.avg_r < -0.10;        // gate rejected losers
  const cls = bad ? 'sv-bad' : good ? 'sv-good' : 'sv-neutral';
  return '<div class="chk-shadow">' +
    '<span class="sv-k">counterfactual</span> ' +
    '<span class="' + cls + '">' + esc(sv.verdict) + '</span> ' +
    '<span class="sv-dim">— blocked trades: ' + sv.wins + 'W/' + sv.losses + 'L · ' +
    pct(sv.win_rate || 0, 0) + ' WR · ' + (sv.avg_r >= 0 ? '+' : '') + sv.avg_r.toFixed(2) + 'R avg</span>' +
  '</div>';
}

/* Single source of truth for "what is the bot closest to taking".
   Ranked by gates cleared first (a setup 11/12 through is nearer than a
   high-confidence one blocked early), then by confidence. Deduped to the
   most recent record per symbol so one noisy symbol can't fill the list. */
function rankCandidates(decs) {
  const latest = new Map();
  for (const d of decs) {
    const prev = latest.get(d.symbol);
    if (!prev || d.timestamp > prev.timestamp) latest.set(d.symbol, d);
  }
  return Array.from(latest.values()).sort((a,b) => {
    const ga = (a.gates_passed||[]).length, gb = (b.gates_passed||[]).length;
    if (gb !== ga) return gb - ga;
    return (b.confidence||0) - (a.confidence||0);
  });
}

function gateTrack(passed, failed, cls) {
  const pset = new Set((passed||[]).map(g => String(g).split(':')[0]));
  const fset = new Set((failed||[]).map(g => String(g).split(':')[0]));
  return GATE_ORDER.map(g => {
    let k = '';
    if (fset.has(g)) k = ' fail';
    else if (pset.has(g)) k = ' pass';
    return '<div class="' + cls + k + '" title="' + esc(g) + '"></div>';
  }).join('');
}

function factorBars(f) {
  if (!f) return '';
  return FACTOR_KEYS.map(k => {
    const v = f[k];
    if (v === undefined || v === null) return '';
    const p = clamp(v*100, 0, 100);
    const col = v >= 0.6 ? 'var(--pos)' : v >= 0.4 ? 'var(--gold)' : 'var(--neg)';
    const label = k === 'order_flow' ? 'FLOW' : k.slice(0,4).toUpperCase();
    return '<div>' +
      '<div class="fac-k"><span>' + label + '</span><b>' + v.toFixed(2) + '</b></div>' +
      '<div class="fac-bar"><div class="fac-fill" style="width:' + p + '%;background:' + col + '"></div></div>' +
    '</div>';
  }).join('');
}

function memoryLine(m) {
  if (!m || !m.n) {
    return '<div class="memory"><span class="verdict na">NO PRECEDENT</span> ' +
           '<span class="detail">— no similar setups in the trade log yet</span></div>';
  }
  const wr = (m.win_rate !== undefined && m.win_rate !== null) ? m.win_rate : 0;
  const good = wr >= 50;
  const verdict = good ? 'MEMORY AGREES' : 'MEMORY DISAGREES';
  return '<div class="memory">' +
    '<span class="verdict ' + (good?'yes':'no') + '">' + verdict + '</span> ' +
    '<span class="detail">— ' + m.n + ' similar · ' + m.wins + 'W/' + m.losses + 'L · ' +
    pct(wr, 0) + ' WR · ' + (m.avg_r !== null && m.avg_r !== undefined ? (m.avg_r>=0?'+':'') + m.avg_r.toFixed(2) + 'R' : '—') + ' avg</span>' +
  '</div>';
}

function renderCommand() {
  const body = $('cmdBody');
  const pos = S.positions || [];

  // ---------- OPEN POSITION MODE ----------
  if (pos.length) {
    $('cmdTag').textContent = pos.length + ' OPEN';
    $('cmdTag').className = 'tag gold';
    body.innerHTML = '<div class="cmd-scroll">' + pos.map(p => renderPosition(p)).join('') + '</div>' + fleetStrip();
    return;
  }

  // ---------- STANDBY MODE ----------
  $('cmdTag').textContent = 'STANDBY';
  $('cmdTag').className = 'tag';

  const decs = S.decisions || [];
  const byScore = rankCandidates(decs);
  const top = byScore[0];
  const lastClose = (S.markers||[]).filter(m => m.type === 'close').slice(-1)[0];

  let html = '<div class="cmd-scroll"><div class="standby">' +
    '<div class="standby-row"><span class="standby-title">Awaiting Signal</span></div>';
  if (lastClose) {
    html += '<div class="standby-last">Last fill: <b>' + esc(shortSym(lastClose.sym)) + '</b> ' +
      (lastClose.pnl >= 0 ? '<b style="color:var(--pos)">' : '<b style="color:var(--neg)">') +
      signed(lastClose.pnl) + '</b> · ' +
      (lastClose.r !== null && lastClose.r !== undefined ? (lastClose.r>=0?'+':'') + lastClose.r.toFixed(2) + 'R' : '') +
      ' · ' + agoTxt(lastClose.ts) + ' ago</div>';
  } else {
    html += '<div class="standby-last">No closed trades yet this session</div>';
  }
  html += '<div class="sweep"></div></div>';

  if (top) {
    const passed = (top.gates_passed||[]).length;
    const total = passed + (top.gates_failed||[]).length;
    const fail = (top.gates_failed||[])[0];
    const failName = fail ? String(fail).split(':')[0] : null;
    const failVal  = fail ? String(fail).split(':')[1] : null;

    html += '<div class="cand">' +
      '<div class="cand-top">' +
        '<span class="cand-sym">' + esc(shortSym(top.symbol)) + '</span>' +
        '<span class="cand-dir ' + esc(top.direction) + '">' + esc(String(top.direction||'').toUpperCase()) + '</span>' +
        '<span class="cand-conf">conf <b>' + (top.confidence!==undefined ? top.confidence.toFixed(3) : '—') + '</b></span>' +
      '</div>' +
      '<div class="gates">' +
        '<div class="gates-head"><span>Gate Progress</span><b>' + passed + ' / ' + total + '</b></div>' +
        '<div class="gate-track">' + gateTrack(top.gates_passed, top.gates_failed, 'gate-seg') + '</div>' +
      '</div>';

    if (failName) {
      html += '<div class="blocker"><span class="x">✕</span><span>' + esc(failName) + '</span>' +
        (failVal ? '<span class="why">' + esc(failVal) + '</span>' : '') + '</div>';
    }

    html += '<div class="factors">' + factorBars(top.factors) + '</div>';
    html += memoryLine(top.memory);
    html += '</div>';
  }

  // Queue — the next best setups behind the leader (already 1-per-symbol)
  const queue = byScore.slice(1, 5);
  if (queue.length) {
    html += '<div class="queue"><div class="sec-label">Next in Queue</div>' +
      queue.map(d => {
        const f = (d.gates_failed||[])[0];
        return '<div class="q-row">' +
          '<span class="q-sym">' + esc(shortSym(d.symbol)) + '</span>' +
          '<span class="q-mini">' + gateTrack(d.gates_passed, d.gates_failed, 'q-seg') + '</span>' +
          '<span class="q-block">' + esc(f ? String(f).split(':')[0] : 'ready') + '</span>' +
        '</div>';
      }).join('') + '</div>';
  }

  html += '</div>' + fleetStrip();   // close cmd-scroll, then pin footer
  body.innerHTML = html;
}

function renderPosition(p) {
  const sym = shortSym(p.symbol || p.sym);
  const entry = p.entry || p.entry_price || 0;
  const cur   = p.current || p.price || p.current_price || entry;
  const sl    = p.sl || p.stop_loss || 0;
  const pnl   = p.pnl !== undefined ? p.pnl : (p.unrealized_pnl || 0);
  const r     = p.r !== undefined ? p.r : (p.r_multiple || 0);
  const tps   = [p.tp1, p.tp2, p.tp3].filter(v => v);
  const dir   = (p.direction || p.dir || 'long').toLowerCase();
  const win   = pnl >= 0;

  // ladder scale spans SL .. max(TP) with the live price marked
  const hiT = tps.length ? Math.max.apply(null, tps) : entry * 1.02;
  let lo = Math.min(sl || entry*0.98, entry, cur);
  let hi = Math.max(hiT, entry, cur);
  if (hi - lo < 1e-12) { hi = entry*1.01; lo = entry*0.99; }
  const span = hi - lo;
  const P = v => clamp(((v - lo)/span)*100, 0, 100);

  let marks = '';
  if (sl) {
    marks += '<div class="lad-mark" style="left:' + P(sl) + '%">' +
      '<div class="lad-tick" style="background:var(--neg)"></div>' +
      '<div class="lad-lab" style="color:var(--neg)">SL</div>' +
      '<div class="lad-px">' + sl.toPrecision(6) + '</div></div>';
  }
  marks += '<div class="lad-mark" style="left:' + P(entry) + '%">' +
    '<div class="lad-tick" style="background:var(--gold)"></div>' +
    '<div class="lad-lab" style="color:var(--gold)">ENTRY</div>' +
    '<div class="lad-px">' + entry.toPrecision(6) + '</div></div>';
  tps.forEach((t,i) => {
    marks += '<div class="lad-mark" style="left:' + P(t) + '%">' +
      '<div class="lad-tick" style="background:var(--pos)"></div>' +
      '<div class="lad-lab" style="color:var(--pos)">TP' + (i+1) + '</div>' +
      '<div class="lad-px">' + t.toPrecision(6) + '</div></div>';
  });

  const fillFrom = Math.min(P(entry), P(cur)), fillTo = Math.max(P(entry), P(cur));

  return '<div class="pos-card">' +
    '<div class="pos-head">' +
      '<span class="pos-sym">' + esc(sym) + '</span>' +
      '<span class="cand-dir ' + esc(dir) + '">' + esc(dir.toUpperCase()) + '</span>' +
      '<div class="pos-pnl">' +
        '<div class="pos-pnl-v" style="color:' + (win?'var(--pos)':'var(--neg)') + '">' + signed(pnl) + '</div>' +
        '<div class="pos-pnl-r">' + (r>=0?'+':'') + Number(r).toFixed(2) + 'R</div>' +
      '</div>' +
    '</div>' +
    '<div class="ladder">' +
      '<div class="ladder-track">' +
        '<div class="ladder-fill" style="left:' + fillFrom + '%;width:' + (fillTo-fillFrom) + '%;background:' + (win?'var(--pos)':'var(--neg)') + '"></div>' +
      '</div>' + marks +
      '<div class="lad-now" style="left:' + P(cur) + '%">' +
        '<div class="lad-now-lab">' + cur.toPrecision(6) + '</div>' +
        '<div class="lad-now-dot"></div>' +
      '</div>' +
    '</div>' +
    '<div class="pos-meta">' +
      '<div><div class="pm-k">Size</div><div class="pm-v">' + money(p.notional || p.size_usd || 0) + '</div></div>' +
      '<div><div class="pm-k">Regime</div><div class="pm-v" style="color:' + (REGIME_COLOR[p.regime]||'var(--n-700)') + '">' + esc(String(p.regime||'—').toUpperCase()) + '</div></div>' +
      '<div><div class="pm-k">Held</div><div class="pm-v">' + (p.duration_h !== undefined ? Number(p.duration_h).toFixed(1)+'h' : (p.entry_ts ? agoTxt(p.entry_ts) : '—')) + '</div></div>' +
      '<div><div class="pm-k">Stop Risk</div><div class="pm-v">' + (sl && entry ? pct(Math.abs(entry-sl)/entry*100, 2) : '—') + '</div></div>' +
      '<div><div class="pm-k">To TP1</div><div class="pm-v">' + (tps.length && cur ? pct((tps[0]-cur)/cur*100, 2) : '—') + '</div></div>' +
      '<div><div class="pm-k">Signals</div><div class="pm-v" style="color:var(--gold)">' + esc((p.signals||[]).join(',') || '—') + '</div></div>' +
    '</div>' +
  '</div>';
}

function fleetStrip() {
  const sanity = S.sanity || {};
  const counts = sanity.regime_counts || {};
  const total = Object.values(counts).reduce((a,b) => a+b, 0) || 1;
  const order = ['bull','bear','range','chop'];

  const bar = order.map(k => {
    const n = counts[k] || 0;
    if (!n) return '';
    return '<span style="flex:' + n + ';background:' + REGIME_COLOR[k] + '"></span>';
  }).join('');

  const legend = order.filter(k => counts[k]).map(k =>
    '<span class="rl"><i style="background:' + REGIME_COLOR[k] + '"></i>' + k.toUpperCase() + ' ' + counts[k] + '</span>'
  ).join('');

  const sumTxt = sanity.summary || '—';
  const sumColor = /RED/i.test(sumTxt) ? 'var(--neg)' : /yellow/i.test(sumTxt) ? 'var(--warn)' : 'var(--pos)';

  const fn = S.funnel || {};
  const scanned = S.symbols ? S.symbols.length : 0;

  // Compact: mix bar + legend on one line, then a single facts line.
  return '<div class="fleet">' +
    '<div class="fleet-row">' +
      '<span class="fleet-k">Regime</span>' +
      '<div class="regbar">' + bar + '</div>' +
    '</div>' +
    '<div class="fleet-row"><div class="reglegend">' + legend + '</div></div>' +
    '<div class="fleet-row fleet-facts">' +
      '<span><span class="fleet-k">Scan</span> <span class="fleet-v">' + scanned + '</span></span>' +
      '<span><span class="fleet-k">60s</span> <span class="fleet-v">' + (fn.records||0) + '→' + (fn.entered||0) + '</span></span>' +
      '<span style="margin-left:auto"><span class="fleet-k">Sanity</span> <span class="fleet-v" style="color:' + sumColor + '">' + esc(sumTxt) + '</span></span>' +
    '</div>' +
  '</div>';
}

/* ============================================================
   INTEL PANEL
   ============================================================ */
function renderIntel() {
  const a = S.analytics || {};

  // Conviction tracks the SAME candidate the Command panel is showing
  // (ranked by gates cleared, then confidence) so the two panels never
  // disagree about what the bot is currently looking at.
  const decs = S.decisions || [];
  let score = null, label = 'Conviction';
  if (decs.length) {
    const best = rankCandidates(decs)[0];
    score = Math.round((best.confidence||0) * 100);
    label = 'Top Conf';
  } else if (a.win_rate !== undefined && a.trades) {
    score = Math.round(a.win_rate);
    label = 'Hist WR';
  }

  const arc = $('gaugeArc');
  const CIRC = 2*Math.PI*33;
  if (score === null) {
    $('gaugeNum').textContent = '—';
    arc.style.strokeDashoffset = CIRC;
  } else {
    $('gaugeNum').textContent = score;
    arc.style.strokeDashoffset = CIRC * (1 - clamp(score,0,100)/100);
    // Confidence is scored against the regime floors the bot actually uses
    // (chop demands 0.65, bull only 0.40), so a raw 0.39 is ordinary — not
    // an alarm. Red is reserved for genuinely weak setups.
    arc.setAttribute('stroke', score >= 55 ? '#26d0a0' : score >= 30 ? '#d4a72c' : '#ff4d6a');
  }
  $('gaugeLab').textContent = label;

  $('cvTrades').textContent = a.trades !== undefined ? a.trades : '—';
  $('cvWr').textContent = a.win_rate !== undefined ? pct(a.win_rate) : '—';
  const rEl = $('cvR');
  rEl.textContent = a.avg_r !== undefined ? (a.avg_r>=0?'+':'') + Number(a.avg_r).toFixed(2) + 'R' : '—';
  rEl.className = 'cs-v ' + (a.avg_r > 0 ? 'pos' : a.avg_r < 0 ? 'neg' : '');
  const pEl = $('cvPnl');
  pEl.textContent = a.total_pnl !== undefined ? signed(a.total_pnl) : '—';
  pEl.className = 'cs-v ' + (a.total_pnl > 0 ? 'pos' : a.total_pnl < 0 ? 'neg' : '');

  // ---- Chokepoint: which gate rejects the most candidates.
  // Answers "why isn't it trading?" directly, from the decision log.
  const chk = $('chokepoint');
  const tally = new Map();
  for (const d of decs) {
    const g = (d.gates_failed || [])[0];
    if (!g) continue;
    const name = String(g).split(':')[0];
    tally.set(name, (tally.get(name) || 0) + 1);
  }
  const ranked = Array.from(tally.entries()).sort((a,b) => b[1]-a[1]);
  if (!ranked.length) {
    chk.innerHTML = '<div class="chk-head"><span>Chokepoint</span>' +
      '<b style="color:var(--pos)">NONE — ALL GATES CLEAR</b></div>';
  } else {
    const worstN = ranked[0][1];
    const blocked = decs.filter(d => (d.gates_failed||[]).length).length;

    // "How far off is it?" — for gates that report a numeric value we can
    // score the best candidate against the threshold the bot actually uses.
    // Without this the panel says nothing is passing but not whether the
    // market is marginally short or nowhere close.
    const GATE_TARGET = { atr_fees: 0.0080 };   // round-trip fee floor (get_fee 0.40% × 2)
    const worstName = ranked[0][0];
    const target = GATE_TARGET[worstName];
    let gapHtml = '';
    if (target) {
      let best = 0;
      for (const d of decs) {
        for (const g of (d.gates_failed||[])) {
          if (g.indexOf(worstName + ':') !== 0) continue;
          const v = parseFloat(g.split(':')[1]);
          if (!isNaN(v) && v > best) best = v;
        }
      }
      if (best > 0) {
        const ratio = best/target;
        const need = target/best;
        gapHtml = '<div class="chk-gap">' +
          'best candidate at <b>' + (ratio*100).toFixed(0) + '%</b> of threshold · ' +
          'needs <b>' + need.toFixed(1) + '×</b> more movement' +
        '</div>';
      }
    }

    chk.innerHTML =
      '<div class="chk-head"><span>Chokepoint</span><b>' + esc(worstName) + '</b>' +
      '<span class="n">' + blocked + ' of ' + decs.length + ' blocked</span></div>' +
      '<div class="chk-rows">' + ranked.slice(0,3).map(([name,n]) =>
        '<div class="chk-row">' +
          '<span class="chk-name">' + esc(name) + '</span>' +
          '<span class="chk-bar"><span class="chk-fill" style="width:' + (n/worstN*100).toFixed(1) + '%"></span></span>' +
          '<span class="chk-n">' + n + '</span>' +
        '</div>').join('') + '</div>' + gapHtml + shadowVerdictHtml(worstName);
  }

  // Signal intelligence — same ranking as Command, top N distinct symbols
  const cards = rankCandidates(decs).slice(0, 4);

  const list = $('sigList');
  if (!cards.length) {
    list.innerHTML = '<div class="empty">NO CANDIDATES IN WINDOW</div>';
    return;
  }
  $('intelTag').textContent = 'TOP ' + cards.length;

  list.innerHTML = cards.map(d => {
    const passed = (d.gates_passed||[]).length;
    const total = passed + (d.gates_failed||[]).length;
    const f = (d.gates_failed||[])[0];
    const m = d.memory;
    let memTxt = '', memCls = 'none';
    if (m && m.n) {
      memCls = m.win_rate >= 50 ? 'good' : 'bad';
      memTxt = m.n + '×' + pct(m.win_rate||0, 0);
    } else { memTxt = 'no precedent'; }

    return '<div class="sig-card">' +
      '<div class="sig-top">' +
        '<span class="sig-sym">' + esc(shortSym(d.symbol)) + '</span>' +
        '<span class="cand-dir ' + esc(d.direction) + '">' + esc(String(d.direction||'').toUpperCase()) + '</span>' +
        '<span class="sig-conf">' + passed + '/' + total + ' gates · conf <b>' + (d.confidence!==undefined?d.confidence.toFixed(3):'—') + '</b></span>' +
      '</div>' +
      '<div class="sig-track">' + gateTrack(d.gates_passed, d.gates_failed, 'sig-seg') + '</div>' +
      '<div class="sig-foot">' +
        (f ? '<span class="blocked">✕ ' + esc(String(f).split(':')[0]) + '</span>' : '<span style="color:var(--pos)">✓ all gates</span>') +
        '<span class="letters">[' + esc((d.signals||[]).join(',')) + ']</span>' +
        '<span class="mem ds-mem ' + memCls + '">' + esc(memTxt) + '</span>' +
      '</div>' +
    '</div>';
  }).join('');
}

/* ============================================================
   DECISION STREAM
   ============================================================
   The bot re-evaluates the same handful of symbols every scan
   tick, so the raw log is ~99% redundant (measured: 1000 records
   over 81 min = 6 distinct symbol+gate states). Rendering it
   verbatim produces a wall of identical rows that scrolls fast
   and says nothing.

   Instead we roll up by symbol into a live status board: one row
   per symbol carrying its CURRENT verdict, how many times it has
   been evaluated, and how long it has been stuck in that state.
   A state change (different blocking gate) is what's actually
   newsworthy, so those are flagged and sorted to the top.
   ============================================================ */
function gateOf(d) {
  const g = (d.gates_failed || [])[0];
  return g ? String(g).split(':')[0] : (d.result === 'ENTERED' ? 'ENTERED' : 'pass');
}

function rollup(decs) {
  // decs arrives newest-first
  const bySym = new Map();
  for (const d of decs) {
    let e = bySym.get(d.symbol);
    if (!e) {
      // first sighting == most recent == current state
      e = Object.assign({}, d);
      e._n = 1;
      e._last = d.timestamp;
      e._first = d.timestamp;
      e._gate = gateOf(d);
      e._changed = false;
      e._prevGate = null;
      bySym.set(d.symbol, e);
      continue;
    }
    e._n++;
    if (d.timestamp < e._first) e._first = d.timestamp;
    // Walk back through history: the first differing gate marks
    // when this symbol last changed verdict.
    const g = gateOf(d);
    if (!e._changed && g !== e._gate) {
      e._changed = true;
      e._prevGate = g;
      e._since = e._sinceCandidate !== undefined ? e._sinceCandidate : d.timestamp;
    }
    if (!e._changed) e._sinceCandidate = d.timestamp;
  }

  const out = Array.from(bySym.values());
  for (const e of out) {
    // how long the current verdict has held
    e._heldSince = e._changed ? (e._since !== undefined ? e._since : e._first) : e._first;
  }

  // Fills first, then recent state changes, then most-recently-seen.
  out.sort((a,b) => {
    const ae = a.result === 'ENTERED', be = b.result === 'ENTERED';
    if (ae !== be) return ae ? -1 : 1;
    if (a._changed !== b._changed) return a._changed ? -1 : 1;
    return b._last - a._last;
  });
  return out;
}

function renderStream() {
  const body = $('streamBody');
  let decs = (S.decisions || []).slice();

  if (!decs.length) {
    body.innerHTML = '<div class="empty">AWAITING DECISIONS</div>';
    $('streamRate').textContent = '—';
    return;
  }

  decs.sort((a,b) => b.timestamp - a.timestamp);
  if (S.streamFilter === 'entered') decs = decs.filter(d => d.result === 'ENTERED');

  const rows = rollup(decs);

  // evaluation rate over the log's span
  const all = S.decisions;
  const span = Math.max(1, all[all.length-1].timestamp - all[0].timestamp);
  const rate = all.length / (span/60);
  $('streamRate').textContent = (rate >= 1 ? rate.toFixed(0) + '/min' : (rate*60).toFixed(0) + '/hr') +
                               ' · ' + rows.length + ' sym';

  if (!rows.length) {
    body.innerHTML = '<div class="empty">NO FILLS IN WINDOW</div>';
    return;
  }

  body.innerHTML = rows.map(d => {
    const entered = d.result === 'ENTERED';
    const f = (d.gates_failed||[])[0];
    const fName = f ? String(f).split(':')[0] : null;
    const fVal  = f ? String(f).split(':')[1] : null;

    const m = d.memory;
    let memTxt = 'no precedent', memCls = 'none';
    if (m && m.n) {
      memCls = m.win_rate >= 50 ? 'good' : 'bad';
      memTxt = m.n + ' sim · ' + pct(m.win_rate||0, 0);
    }

    const fac = d.factors || {};
    const facTxt = FACTOR_KEYS.filter(k => fac[k] !== undefined)
      .map(k => '<b>' + (k==='order_flow'?'FLW':k.slice(0,3).toUpperCase()) + '</b> ' + fac[k].toFixed(2))
      .join(' · ');

    const passed = (d.gates_passed||[]).length;
    const total  = passed + (d.gates_failed||[]).length;
    const held   = agoTxt(d._heldSince);

    return '<div class="ds-row ' + (entered ? 'entered' : 'blocked') + (d._changed ? ' shifted' : '') + '">' +
      '<div class="ds-time">' + agoTxt(d._last) + '</div>' +
      '<div class="ds-body">' +
        '<div class="ds-line1">' +
          '<span class="ds-sym">' + esc(shortSym(d.symbol)) + '</span>' +
          '<span class="ds-dir ' + esc(d.direction) + '">' + esc(String(d.direction||'').charAt(0).toUpperCase()) + '</span>' +
          '<span class="ds-sigs">[' + esc((d.signals||[]).join(',')) + ']</span>' +
          (entered
            ? '<span class="ds-verb" style="color:var(--pos)">✓ ENTERED</span>'
            : '<span class="ds-verb">' + passed + '/' + total + ' gates · held at <span class="gate">' + esc(fName||'—') + '</span>' + (fVal ? ' <span class="dimtext">' + esc(fVal) + '</span>' : '') + '</span>') +
          (d._changed ? '<span class="ds-shift">↻ was ' + esc(d._prevGate) + '</span>' : '') +
        '</div>' +
        '<div class="ds-line2">' +
          '<span>conf <b>' + (d.confidence!==undefined?d.confidence.toFixed(3):'—') + '</b></span>' +
          '<span style="color:' + (REGIME_COLOR[d.regime]||'var(--n-500)') + '">' + esc(String(d.regime||'').toUpperCase()) + '</span>' +
          '<span>' + facTxt + '</span>' +
        '</div>' +
      '</div>' +
      '<div class="ds-right">' +
        '<span class="ds-rep">×' + d._n + ' · ' + held + '</span>' +
        '<span class="ds-mem ' + memCls + '">' + esc(memTxt) + '</span>' +
      '</div>' +
    '</div>';
  }).join('');
}

$('streamSeg').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  Array.from($('streamSeg').children).forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  S.streamFilter = b.dataset.f;
  renderStream();
});

/* ============================================================
   MARKET + BRAIN
   ============================================================ */
function renderMarket() {
  const syms = S.symbols || [];
  const grid = $('symGrid');
  const posSet = new Set((S.positions||[]).map(p => p.symbol || p.sym));

  if (!syms.length) {
    grid.innerHTML = '<div class="empty">LOADING UNIVERSE</div>';
  } else {
    $('mktTag').textContent = syms.length + ' PAIRS';
    grid.innerHTML = syms.map(s => {
      const reg = String(s.regime || 'chop').toLowerCase();
      const has = posSet.has(s.symbol);
      return '<div class="sym' + (has ? ' haspos' : '') + '" title="' + esc(s.symbol) + (s.wr ? ' · WR ' + s.wr + '%' : '') + '">' +
        '<span class="sym-name">' + esc(shortSym(s.symbol)) + '</span>' +
        '<span class="sym-px">' + esc(s.price) + '</span>' +
        '<span class="sym-reg ' + esc(reg) + '">' + esc(reg.toUpperCase()) + '</span>' +
      '</div>';
    }).join('');
  }

  // ---- brain
  const b = S.brain;
  const tiles = $('brainTiles');
  if (!b) { tiles.innerHTML = ''; return; }

  const n = b.regime_n || {};
  const thr = (b.override_threshold && b.override_threshold.min_regime_n) || 30;
  const order = ['bull','bear','range','chop'];

  tiles.innerHTML = order.map(k => {
    const v = n[k] || 0;
    const armed = v >= thr;
    return '<div class="bt' + (armed ? ' armed' : '') + '">' +
      '<div class="bt-k" style="color:' + REGIME_COLOR[k] + '">' + k.toUpperCase() + '</div>' +
      '<div class="bt-v">' + v + '</div>' +
    '</div>';
  }).join('');

  const best = order.reduce((m,k) => (n[k]||0) > (n[m]||0) ? k : m, 'bull');
  const bestN = n[best] || 0;
  const armed = bestN >= thr;

  $('brainTag').textContent = armed ? 'ARMED' : 'STANDBY';
  $('brainTag').className = 'tag ' + (armed ? 'pos' : '');
  $('armLabel').textContent = armed
    ? 'OVERRIDE ACTIVE — ' + best.toUpperCase()
    : 'ARMING · best regime ' + best.toUpperCase();
  $('armCount').textContent = armed ? bestN + '/' + thr : (thr - bestN) + ' trades to arm';
  $('armFill').style.width = clamp(bestN/thr*100, 0, 100) + '%';
}

/* ============================================================
   POLLING
   ============================================================ */
async function pollFast() {
  const [h, p] = await Promise.all([
    grab('/api/health', null),
    grab('/api/positions', [])
  ]);
  if (h) S.health = h;
  S.positions = Array.isArray(p) ? p : (p && p.positions) || [];
  renderTop();
  renderCommand();
}

async function pollMid() {
  const [d, a, sy, fn] = await Promise.all([
    grab('/api/decisions?limit=250', []),
    grab('/api/analytics', {}),
    grab('/api/symbols', []),
    grab('/api/funnel', {})
  ]);
  S.decisions = Array.isArray(d) ? d : [];
  S.analytics = a && a.analytics ? a.analytics : a;
  S.symbols = Array.isArray(sy) ? sy : [];
  S.funnel = fn || {};
  renderIntel();
  renderStream();
  renderMarket();
  renderCommand();
}

async function pollSlow() {
  const [eq, mk, br, sa, sh] = await Promise.all([
    grab('/api/equity', []),
    grab('/api/trade_markers', []),
    grab('/api/brain/global', null),
    grab('/api/regime/sanity', null),
    grab('/api/shadow/verdict', null)
  ]);
  if (Array.isArray(eq)) S.equity = eq;
  if (Array.isArray(mk)) S.markers = mk;
  if (br) S.brain = br;
  if (sa) S.sanity = sa;
  if (sh) S.shadow = sh;
  drawEquity();
  renderMarket();
  renderCommand();
  renderIntel();
}

/* redraw chart on resize (debounced) */
let rzT = null;
window.addEventListener('resize', () => {
  clearTimeout(rzT);
  rzT = setTimeout(drawEquity, 120);
});

/* boot */
(async function boot() {
  await Promise.all([pollFast(), pollMid(), pollSlow()]);
  setInterval(pollFast, 2000);
  setInterval(pollMid,  5000);
  setInterval(pollSlow, 15000);
})();
</script>
</body>
</html>
"""

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
  "27 signals. 16 gates. No shortcuts."
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
    // EQUITY (cash + unrealized), not cash alone: with a position open, cash
    // freezes at the last closed trade and hides what the position is doing.
    // Falls back to cash if an older bot build has no `equity` field.
    const cash = isLive ? (h.live_balance || 0) : (h.paper_balance || 0);
    const bal = (h.equity != null) ? h.equity : cash;
    const unreal = h.unrealized_pnl || 0;
    const tradeAmt = (h.live_trade_amt || (bal * 0.03)) || 0;
    // Colour by whether open positions are up or down, i.e. equity vs the cash
    // it came from. The old baseline was a hardcoded `10000` (the pre-2026-07-28
    // paper balance), which after the drop to $500 would have read red forever.
    const startBal = cash;
    const totalPnl = a.total_pnl || 0;
    const trades = a.trades || 0;
    const wins = a.wins || 0;
    const wr = trades > 0 ? (wins / trades * 100) : 0;

    document.getElementById('tradeAmt').textContent = '$' + Math.round(tradeAmt);

    const balEl = document.getElementById('hdrBalance');
    // Append the unrealized component so a moving balance is explained rather
    // than mysterious; omitted when flat, since there is nothing to explain.
    balEl.textContent = '$' + bal.toFixed(2) +
      (Math.abs(unreal) >= 0.005
        ? ' (' + (unreal >= 0 ? '+' : '') + unreal.toFixed(2) + ')'
        : '');
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
    """Fetch active positions from the bot's /positions endpoint.
    Returns None when the bot is unreachable so callers can distinguish
    'feed down' from 'no open positions'."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/positions", timeout=5)
        return resp.json() if resp.status_code == 200 else None
    except Exception:
        return None

# =============================================================================
# Routes
# =============================================================================

@app.route('/')
def public():
    return AUDIT_TEMPLATE

# =============================================================================
# Public track record — /verify
# =============================================================================
# The subscriber-facing proof page. Read-only, no internals, no controls: just
# every closed trade and the honest aggregate. Linked from every Telegram card
# footer so anyone can check the claims without asking.

@app.route('/api/track-record')
def api_track_record():
    """Aggregate public track record. Losses included, fees counted."""
    a = fetch_analytics()
    if not a.get('bot_online') and a.get('error'):
        return jsonify({'online': False, 'error': a.get('error')}), 502

    log = a.get('trade_log', []) or []
    FEE = 0.0040  # Kraken taker, both legs — same rate the bot trades on

    trades, cum, running = [], [], 0.0
    wins = losses = 0
    gross_sum = fees_sum = 0.0
    best = worst = None

    for t in log:
        det = t.get('detail', {}) or {}
        net = float(t.get('pnl', 0) or 0)
        entry = float(det.get('entry', 0) or 0)
        exit_p = float(det.get('exit_price', 0) or 0)
        size = float(det.get('size', 0) or 0)
        fees = FEE * size * (abs(entry) + abs(exit_p)) if entry else 0.0
        gross = net + fees
        gross_sum += gross
        fees_sum += fees
        running += net
        cum.append(round(running, 4))
        if net > 0:
            wins += 1
        else:
            losses += 1
        rec = {
            'ts': t.get('t', 0),
            'sym': t.get('sym', '?'),
            'dir': 'LONG' if str(t.get('dir', 'L')).upper().startswith('L') else 'SHORT',
            'r': round(float(t.get('r', 0) or 0), 2),
            'net': round(net, 2),
            'gross': round(gross, 2),
            'fees': round(fees, 2),
            'exit': t.get('exit', ''),
            'entry': entry,
            'exit_price': exit_p,
            'duration_h': det.get('duration_h', 0),
            'confidence': det.get('confidence', 0),
            'regime': det.get('regime', ''),
            'sigs': det.get('sigs', []),
        }
        trades.append(rec)
        if best is None or net > best['net']:
            best = rec
        if worst is None or net < worst['net']:
            worst = rec

    total = wins + losses
    net_sum = round(running, 2)
    r_list = [t['r'] for t in trades]
    avg_r = round(sum(r_list) / len(r_list), 3) if r_list else 0.0
    win_r = [t['r'] for t in trades if t['net'] > 0]
    loss_r = [t['r'] for t in trades if t['net'] <= 0]
    # Max drawdown on the cumulative NET curve (the money curve, not gross)
    peak = 0.0
    max_dd = 0.0
    for v in cum:
        peak = max(peak, v)
        max_dd = min(max_dd, v - peak)

    return jsonify({
        'online': bool(a.get('bot_online')),
        'mode': (fetch_health() or {}).get('trading_mode', 'unknown'),
        'trades': total,
        'wins': wins,
        'losses': losses,
        'win_rate': round(wins / total * 100, 1) if total else 0.0,
        'net_pnl': net_sum,
        'gross_pnl': round(gross_sum, 2),
        'fees_paid': round(fees_sum, 2),
        'avg_r': avg_r,
        'avg_win_r': round(sum(win_r) / len(win_r), 2) if win_r else 0.0,
        'avg_loss_r': round(sum(loss_r) / len(loss_r), 2) if loss_r else 0.0,
        'max_drawdown': round(max_dd, 2),
        'expectancy': round(net_sum / total, 3) if total else 0.0,
        'best': best,
        'worst': worst,
        'cumulative': cum,
        'trade_log': list(reversed(trades)),  # newest first for the table
        'uptime_hours': a.get('uptime_hours', 0),
        'symbol_count': a.get('symbol_count', 0),
    })


@app.route('/verify')
def verify_page():
    return render_template_string(VERIFY_TEMPLATE, fonts=FONTS)


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
    if positions is None:
        # Bot unreachable — 502 so the JS gj() helper maps it to null (visible
        # degraded state) instead of a fake "no positions" empty list.
        return jsonify([]), 502
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

    _pos = fp.result()
    return jsonify({
        'health': fh.result(),
        'analytics': fa.result(),
        'positions': _pos if _pos is not None else [],
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
    return jsonify([]), 502  # upstream down — let the client see it, not fake-empty data

@app.route('/api/messages')
def api_messages():
    """Proxy to subscriber API's /api/messages endpoint."""
    try:
        resp = requests.get(f"{SUB_API_URL}/api/messages", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify([]), 502  # upstream down — let the client see it, not fake-empty data

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
    return jsonify([]), 502  # upstream down — let the client see it, not fake-empty data

@app.route('/api/trade_markers')
def api_trade_markers():
    """Proxy to bot's /api/trade_markers endpoint (per-trade open/close markers)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/api/trade_markers", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify([]), 502  # upstream down — let the client see it, not fake-empty data

@app.route('/api/funnel')
def api_funnel():
    """Proxy to bot's /api/funnel endpoint (gate funnel snapshot)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/api/funnel", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({'window_seconds': 60, 'records': 0, 'entered': 0, 'gates': []}), 502  # upstream down

@app.route('/api/decisions')
def api_decisions():
    """Proxy to bot's /decisions endpoint (decision log ring buffer).

    The bot's buffer holds ~1000 records (~500KB). The dashboard polls this
    every 5s but only ever renders a per-symbol rollup of the recent tail, so
    shipping the whole buffer wastes ~100MB/hour of loopback transfer. Default
    to the newest `limit` records; pass limit=0 for the untrimmed log.
    """
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/decisions", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            try:
                limit = int(request.args.get('limit', 250))
            except (TypeError, ValueError):
                limit = 250
            if limit > 0 and isinstance(data, list) and len(data) > limit:
                data = data[-limit:]          # ring buffer is oldest-first
            return jsonify(data)
    except Exception:
        pass
    return jsonify([]), 502  # upstream down — let the client see it, not fake-empty data

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
    return jsonify([]), 502  # upstream down — let the client see it, not fake-empty data

@app.route('/api/brain/global')
def api_brain_global():
    """Proxy to bot's /api/brain/global endpoint (fleet Bayesian factor model)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/api/brain/global", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({}), 502  # upstream down — let the client see it, not fake-empty data

@app.route('/api/brain')
def api_brain():
    """Proxy to bot's /api/brain endpoint (per-symbol + per-signal Bayesian stats)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/api/brain", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({}), 502  # upstream down — let the client see it, not fake-empty data

@app.route('/api/expectancy')
def api_expectancy():
    """Proxy to bot's /api/expectancy endpoint (fleet expectancy + rankings)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/api/expectancy", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({}), 502  # upstream down — let the client see it, not fake-empty data

@app.route('/api/trades')
def api_trades():
    """Proxy to bot's /api/trades endpoint (today's trade list for broadcaster/EOD)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/api/trades", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({"trades": []}), 502  # upstream down

@app.route('/api/events/recent')
def api_events_recent():
    """Proxy to bot's /api/events/recent endpoint (event bus tail — currently stubbed on bot side)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/api/events/recent", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify([]), 502  # upstream down — let the client see it, not fake-empty data

@app.route('/api/ohlc/<path:sym>')
def api_ohlc(sym):
    """Proxy to bot's /api/ohlc/<sym> endpoint (recent candles + position overlay)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/api/ohlc/{quote(sym, safe='')}", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({'symbol': sym, 'candles': [], 'position': None}), 502  # upstream down

@app.route('/api/autopsy/recent')
def api_autopsy_recent():
    """Proxy to bot's /api/autopsy/recent endpoint (post-trade autopsies)."""
    try:
        limit = request.args.get('limit', '20')
        resp = requests.get(f"{GOLDENEYE_URL}/api/autopsy/recent?limit={limit}", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify([]), 502  # upstream down — let the client see it, not fake-empty data

@app.route('/api/shadow/verdict')
def api_shadow_verdict():
    """Proxy to bot's /api/shadow/verdict (counterfactual gate scorecard)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/api/shadow/verdict", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({'gates': {}, 'recent': []}), 502  # upstream down


@app.route('/api/regime/sanity')
def api_regime_sanity():
    """Proxy to bot's /api/regime/sanity endpoint (HMM vs actual-return sanity check)."""
    try:
        resp = requests.get(f"{GOLDENEYE_URL}/api/regime/sanity", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({}), 502  # upstream down — let the client see it, not fake-empty data

@app.route('/api/memory/similar')
def api_memory_similar():
    """Proxy to bot's /api/memory/similar endpoint (fleet memory query by factor vector)."""
    try:
        qs = request.query_string.decode('utf-8') if request.query_string else ''
        url = f"{GOLDENEYE_URL}/api/memory/similar"
        if qs: url += '?' + qs
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({'summary': None, 'matches': []}), 502  # upstream down

if __name__ == '__main__':
    port = int(os.getenv('DASHBOARD_PORT', 8050))
    logger.info(f"ORACLE Dashboard starting on port {port}")
    logger.info(f"Command Center: http://localhost:{port}/")
    # Deliberately LAN-visible (view layer, watched from other devices); the bot
    # APIs on 18095/18096 are loopback-only and reached via this dashboard proxy.
    app.run(host='0.0.0.0', port=port, debug=False)
