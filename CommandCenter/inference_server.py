#!/usr/bin/env python3
"""
Fleet AI Inference Server
=========================
Port 9001. Uses Ollama (Tesla P4) for fleet-wide AI analysis.

Endpoints:
  POST /api/ai/trade-journal    — journal entry for a trade
  POST /api/ai/post-mortem      — analyze batch of trades
  POST /api/ai/fleet-assessment — assess current fleet state
  GET  /health                  — status + model info
"""

import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.request import Request, urlopen

PORT = 9001
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL = None


def detect_model():
    """Find the best available Ollama model."""
    global MODEL
    preferred = ["gemma2:2b", "phi4-mini:latest", "mistral-nemo:latest",
                 "qwen3.5:4b", "deepseek-r1:7b"]
    try:
        req = Request("http://localhost:11434/api/tags")
        resp = urlopen(req, timeout=5)
        data = json.loads(resp.read())
        available = [m["name"] for m in data.get("models", [])]
        for p in preferred:
            if p in available:
                MODEL = p
                return
        if available:
            MODEL = available[0]
    except Exception:
        pass


def query_ollama(prompt, system=None, format_json=True, timeout=120):
    """Send prompt to Ollama, return parsed response."""
    # num_gpu=0 forces CPU inference. Ollama v0.32.5's CUDA runtime crashes
    # (0xc0000005 in llama-server) loading ANY model on the Tesla P4
    # (Pascal, compute 6.1) — every AI endpoint returned a wrapped 500.
    # CPU gemma2:2b is fast enough for journals/assessments. Re-enable the
    # GPU by removing this option once Ollama loads on the P4 again.
    payload = {"model": MODEL, "prompt": prompt, "stream": False,
               "options": {"num_gpu": 0}}
    if system:
        payload["system"] = system
    # Note: "format":"json" causes empty responses on some models (qwen3.5)
    # Instead, we instruct via system prompt and parse the response

    text = ""
    try:
        req = Request(OLLAMA_URL,
                      data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
        resp = urlopen(req, timeout=timeout)
        data = json.loads(resp.read())
        text = data.get("response", "").strip()

        if format_json:
            # Strip markdown fences if model wraps JSON
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            return json.loads(text.strip())
        return {"text": text}
    except json.JSONDecodeError:
        return {"error": "Invalid JSON from model", "raw": text[:500]}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

JOURNAL_SYSTEM = (
    "You are a concise quantitative trade analyst for a crypto trading fleet. "
    "Given trade details, respond in JSON with exactly these fields: "
    '{"journal": "2-3 sentence analysis", "grade": "A/B/C/D/F", "lesson": "one sentence takeaway"} '
    "No markdown, no preamble. JSON only."
)

POSTMORTEM_SYSTEM = (
    "You are a quantitative portfolio analyst reviewing crypto trades. "
    "Identify patterns in wins vs losses. Respond in JSON: "
    '{"winning_patterns": ["..."], "losing_patterns": ["..."], '
    '"suggestions": ["actionable 1", "2", "3"], "summary": "one paragraph assessment"} '
    "No markdown, no preamble. JSON only."
)

FLEET_SYSTEM = (
    "You are the chief risk officer of an 11-bot crypto trading fleet. "
    "Given fleet state including AEGIS self-assessment, PHITEX thermodynamic readings, "
    "portfolio allocation, and whale activity, provide a brief assessment. Respond in JSON: "
    '{"assessment": "2 sentence summary", "risk_factors": ["..."], '
    '"opportunities": ["..."], "aegis_appropriate": true/false, '
    '"recommended_action": "one sentence"} '
    "No markdown, no preamble. JSON only."
)


# ---------------------------------------------------------------------------
# Request formatting
# ---------------------------------------------------------------------------

def fmt_trade(t):
    f = t.get("factors", t.get("detail", {}).get("factors", {}))
    return (
        f"Trade: {t.get('symbol', t.get('sym', '?'))} {t.get('direction', t.get('dir', '?'))}\n"
        f"Entry: ${t.get('entry_price', t.get('entry', 0))}, "
        f"Exit: ${t.get('exit_price', 0)}\n"
        f"PnL: ${t.get('pnl', 0):.2f}, R: {t.get('r', t.get('r_mult', 0)):.1f}R\n"
        f"Signals: {t.get('signals', t.get('sigs', []))}\n"
        f"Regime: {t.get('regime', '?')}\n"
        f"Factors: trend={f.get('trend', '?')} momentum={f.get('momentum', '?')} "
        f"volume={f.get('volume', '?')}\n"
        f"Exit reason: {t.get('exit_reason', t.get('exit', '?'))}\n"
        f"Duration: {t.get('duration_h', '?')}h"
    )


def fmt_trades_batch(trades):
    lines = [f"Analyzing {len(trades)} trades:\n"]
    for i, t in enumerate(trades[:30], 1):
        pnl = t.get("pnl", 0)
        result = "WIN" if pnl > 0 else "LOSS"
        f = t.get("factors", {})
        lines.append(
            f"{i}. {t.get('symbol', t.get('sym', '?'))} "
            f"{t.get('direction', t.get('dir', '?'))} {result} "
            f"${pnl:.2f} R={t.get('r', 0):.1f} "
            f"exit={t.get('exit_reason', t.get('exit', '?'))} "
            f"regime={t.get('regime', '?')} "
            f"trend={f.get('trend', '?')} mom={f.get('momentum', '?')}"
        )
    return "\n".join(lines)


def fmt_fleet_state(state):
    bots = state.get("bots", [])
    port = state.get("portfolio", {})
    total = max(port.get("total", 10000), 1)
    lines = [
        f"Fleet: {sum(1 for b in bots if b.get('alive'))}/{len(bots)} alive",
        f"Portfolio: ${total:,.0f} pool, "
        f"${port.get('deployed', 0):,.0f} deployed "
        f"({port.get('deployed', 0) / total * 100:.0f}%)",
        f"Reservations: {port.get('active_reservations', 0)}",
        f"AEGIS: {state.get('aegis_score', '?')} ({state.get('aegis_regime', '?')})",
        f"PHITEX fleet: {state.get('phitex_score', '?')}",
        f"Whale alerts: {state.get('whale_count', 0)}",
        "\nBot details:",
    ]
    for b in bots:
        if b.get("alive"):
            n = b.get("normalized", {}) or {}
            lines.append(
                f"  {b['name']}: eq={n.get('equity', '--')} "
                f"wr={n.get('win_rate', '--')} pos={n.get('open_positions', '--')}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class InferenceHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "running", "model": MODEL, "port": PORT})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return

        if self.path == "/api/ai/trade-journal":
            trade = body.get("trade", body)
            result = query_ollama(fmt_trade(trade), JOURNAL_SYSTEM)
            self._json(200, result)

        elif self.path == "/api/ai/post-mortem":
            trades = body.get("trades", [])
            result = query_ollama(fmt_trades_batch(trades), POSTMORTEM_SYSTEM, timeout=180)
            self._json(200, result)

        elif self.path == "/api/ai/fleet-assessment":
            state = body.get("fleet_state", body)
            result = query_ollama(fmt_fleet_state(state), FLEET_SYSTEM)
            self._json(200, result)

        else:
            self._json(404, {"error": "not found"})

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, fmt, *args):
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    detect_model()
    if MODEL is None:
        print("ERROR: No Ollama model found. Is Ollama running?")
        print("  Start: ollama serve")
        sys.exit(1)

    print(f"\n  AI INFERENCE SERVER v1.0")
    print(f"  Model: {MODEL}")
    print(f"  Port:  {PORT}")
    print(f"  http://localhost:{PORT}/health")
    print(f"  Press Ctrl+C to stop\n")

    server = ThreadedServer(("0.0.0.0", PORT), InferenceHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Inference server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
