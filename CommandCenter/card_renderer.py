"""
card_renderer.py — Visual signal card generator for GoldenEye Intelligence.

Produces pixel-perfect PNG images for Telegram sendPhoto.
Paid tier gets images. Free tier stays text.

Design system:
  Canvas 800px wide, variable height, #0a0a0a background.
  Gold accent #c8a96e. Flat panels. No gradients on backgrounds.
"""

import io
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# ── Bot display names — internal names never shown to subscribers ────────────

BOT_DISPLAY_NAMES = {
    'confluence':    'Concord',
    'turtlesue':     'Stalker',
    'turtlebot':     'Stalker',
    'nexusbrain':    'Prism',
    'nexus_brain':   'Prism',
    'oracle':        'Atlas',
    'deepblue':      'Leviathan',
    'deep_blue':     'Leviathan',
    'gridzilla':     'Ironweb',
    'nexus':         'The Council',
    'aegis':         'Sovereign',
    'sentinel':      'Watcher',
    'trinity':       'Trident',
    'hivemind':      'Chorus',
    'hive_mind':     'Chorus',
    'phitex':        'Pulse',
    'rubberband':    'Slingshot',
    'contrarian':    'Heretic',
    'arbitrageur':   'Ghost',
    'chronos':       'Meridian',
    'inference':     'Inference',
}


def display_name(internal: str) -> str:
    """Map internal bot name to public display name."""
    return BOT_DISPLAY_NAMES.get(internal.lower().replace('-', '_'), internal.title())


# ── Design tokens ────────────────────────────────────────────────────────────

BG          = "#0a0a0a"
PANEL       = "#111111"
BORDER      = "#1e1e1e"
ACCENT      = "#c8a96e"
ACCENT_DIM  = "#7a6340"
TEXT_PRI    = "#e8e8e8"
TEXT_SEC    = "#666666"
TEXT_DANGER = "#c0392b"
TEXT_POS    = "#27ae60"
BAR_BG     = "#1a1a1a"

CANVAS_W    = 800
MARGIN      = 24
GUTTER      = 16
COL_LEFT    = MARGIN + 6  # after gold bar
COL_RIGHT   = CANVAS_W - MARGIN

# Key column for data rows
KEY_W       = 160

# Font paths (Windows)
_FONT_MONO  = "C:/Windows/Fonts/cour.ttf"
_FONT_MONOB = "C:/Windows/Fonts/courbd.ttf"
_FONT_SANS  = "C:/Windows/Fonts/arial.ttf"
_FONT_SANSB = "C:/Windows/Fonts/arialbd.ttf"


def _hex(color: str) -> Tuple[int, int, int]:
    """Parse hex color to RGB tuple."""
    c = color.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _human_regime(regime) -> str:
    """Translate internal regime labels to human-readable prose. Single source of truth."""
    return {
        "TRENDING":       "Trending (bullish bias)",
        "TRENDING_UP":    "Strong uptrend",
        "TRENDING_DOWN":  "Downtrend",
        "BULL":           "Bullish",
        "BEAR":           "Bearish",
        "RANGING":        "Range-bound (sideways)",
        "NORMAL":         "Neutral",
        "DEFENSIVE":      "Defensive (risk-off)",
        "CAUTIOUS":       "Cautious",
        "EQUILIBRIUM":    "Balanced",
        "MIXED":          "Mixed signals",
        "EXTREME_FEAR":   "Extreme fear",
        "EXTREME_GREED":  "Extreme greed",
        "HIGH_ACTIVITY":  "High activity",
    }.get((regime or "").upper().replace(" ", "_"), (regime or "Unknown").replace("_", " ").title())


class CardRenderer:
    """Renders signal cards as PNG bytes for Telegram."""

    def __init__(self):
        # Load fonts at multiple sizes
        self._fonts = {}
        self._load_fonts()
        # Live fleet count for the card footer. None = unknown, and the footer
        # then omits the line entirely rather than asserting a number.
        # Was hardcoded "19 bots · live" until 2026-08-05 — a literal that
        # claimed 19 live bots on every subscriber card regardless of actual
        # state (and 19 is unreachable anyway: bot_responder has no port, so
        # CC can only ever poll 18). No fake stats. Set via set_bots_alive().
        self._bots_alive: Optional[int] = None

    def set_bots_alive(self, n: Optional[int]) -> None:
        """Record the live bot count for footers.

        Call with aggregate.bots_alive from /api/master. Pass None when the
        count is unknown (CC unreachable, stale poll) — the footer omits the
        line rather than showing a stale or invented number.
        """
        self._bots_alive = n if isinstance(n, int) and n >= 0 else None

    def _bot_count_text(self) -> Optional[str]:
        """Footer bot-count string, or None when unknown (draw nothing)."""
        if self._bots_alive is None:
            return None
        return f"{self._bots_alive} bots · live"

    def _load_fonts(self):
        base_sizes = {
            "mono_11": (_FONT_MONO, 11),
            "mono_13": (_FONT_MONO, 13),
            "mono_14": (_FONT_MONO, 14),
            "mono_16": (_FONT_MONOB, 16),
            "sans_11": (_FONT_SANS, 11),
            "sans_12": (_FONT_SANS, 12),
            "sans_13": (_FONT_SANS, 13),
            "sans_14": (_FONT_SANS, 14),
            "sans_16": (_FONT_SANSB, 16),
            "sans_18": (_FONT_SANSB, 18),
            "sans_20": (_FONT_SANSB, 20),
            "mono_10": (_FONT_MONO, 10),
        }
        for key, (path, size) in base_sizes.items():
            try:
                self._fonts[key] = ImageFont.truetype(path, size)
                # 2x versions for hi-res rendering
                self._fonts[f"{key}_2x"] = ImageFont.truetype(path, size * 2)
            except (OSError, IOError):
                self._fonts[key] = ImageFont.load_default()
                self._fonts[f"{key}_2x"] = ImageFont.load_default()

    def _f(self, name: str) -> ImageFont.FreeTypeFont:
        return self._fonts.get(name, ImageFont.load_default())

    # ── Low-level drawing helpers ────────────────────────────────────────

    def _new_canvas(self, height: int) -> Tuple[Image.Image, ImageDraw.Draw]:
        img = Image.new("RGB", (CANVAS_W, height), _hex(BG))
        draw = ImageDraw.Draw(img)
        return img, draw

    def _draw_header(self, draw: ImageDraw.Draw, y: int,
                     title: str, subtitle: str,
                     timestamp: Optional[str] = None) -> int:
        """Draw header block with 4px gold left bar. Returns new y."""
        # Gold left bar
        bar_x = MARGIN
        bar_top = y
        bar_bot = y + 48
        draw.rectangle([bar_x, bar_top, bar_x + 4, bar_bot], fill=_hex(ACCENT))

        # Title
        tx = bar_x + 14
        draw.text((tx, y + 4), title.upper(), fill=_hex(TEXT_PRI),
                  font=self._f("sans_18"))

        # Subtitle
        draw.text((tx, y + 28), subtitle, fill=_hex(TEXT_SEC),
                  font=self._f("sans_13"))

        # Timestamp top-right
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
        ts_font = self._f("mono_11")
        ts_bbox = draw.textbbox((0, 0), timestamp, font=ts_font)
        ts_w = ts_bbox[2] - ts_bbox[0]
        draw.text((COL_RIGHT - ts_w, y + 6), timestamp,
                  fill=_hex(TEXT_SEC), font=ts_font)

        return bar_bot + 12

    def _draw_separator(self, draw: ImageDraw.Draw, y: int) -> int:
        """Draw 1px horizontal rule. Returns new y."""
        draw.line([(MARGIN, y), (COL_RIGHT, y)], fill=_hex(BORDER), width=1)
        return y + 10

    def _draw_kv(self, draw: ImageDraw.Draw, y: int,
                 key: str, value: str,
                 value_color: str = TEXT_PRI,
                 key_font: str = "mono_14",
                 val_font: str = "mono_14") -> int:
        """Draw a key: value row. Returns new y."""
        kf = self._f(key_font)
        vf = self._f(val_font)
        draw.text((COL_LEFT + 14, y), key, fill=_hex(TEXT_SEC), font=kf)
        draw.text((COL_LEFT + KEY_W, y), str(value), fill=_hex(value_color),
                  font=vf)
        return y + 26

    def _draw_bar(self, draw: ImageDraw.Draw, x: int, y: int,
                  width: int, value: float, max_value: float = 1.0,
                  height: int = 4,
                  bg: str = BAR_BG,
                  fill_color: str = ACCENT) -> None:
        """Draw a thin progress bar."""
        # Background
        draw.rectangle([x, y, x + width, y + height], fill=_hex(bg))
        # Fill
        ratio = max(0.0, min(1.0, value / max_value)) if max_value > 0 else 0
        fill_w = int(width * ratio)
        if fill_w > 0:
            draw.rectangle([x, y, x + fill_w, y + height], fill=_hex(fill_color))

    def _draw_labeled_bar(self, draw: ImageDraw.Draw, y: int,
                          label: str, value: float,
                          max_value: float = 1.0,
                          bar_width: int = 300,
                          show_value: bool = True) -> int:
        """Draw label + bar + value. Returns new y."""
        font = self._f("mono_13")
        lx = COL_LEFT + 14
        draw.text((lx, y - 2), f"{label:16s}", fill=_hex(TEXT_SEC), font=font)

        bar_x = lx + 180
        self._draw_bar(draw, bar_x, y + 4, bar_width, value, max_value,
                       height=6, fill_color=ACCENT)

        if show_value:
            val_s = f"{value:.2f}" if isinstance(value, float) else str(value)
            val_x = bar_x + bar_width + 10
            draw.text((val_x, y - 2), val_s, fill=_hex(TEXT_SEC), font=font)

        return y + 24

    def _draw_footer(self, draw: ImageDraw.Draw, y: int,
                     timestamp: Optional[str] = None,
                     show_bot_count: bool = True) -> int:
        """Draw the GoldenEye footer. Returns new y."""
        y = self._draw_separator(draw, y)
        y += 4

        # Left: GoldenEye Intelligence in accent
        gf = self._f("sans_12")
        draw.text((COL_LEFT + 14, y), "GoldenEye Intelligence",
                  fill=_hex(ACCENT), font=gf)

        # Center: Fleet Intelligence
        center_text = "Fleet Intelligence"
        cb = draw.textbbox((0, 0), center_text, font=gf)
        cw = cb[2] - cb[0]
        draw.text(((CANVAS_W - cw) // 2, y), center_text,
                  fill=_hex(TEXT_SEC), font=gf)

        # Right: timestamp
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
        tf = self._f("mono_10")
        tb = draw.textbbox((0, 0), timestamp, font=tf)
        tw = tb[2] - tb[0]
        draw.text((COL_RIGHT - tw, y + 2), timestamp,
                  fill=_hex(TEXT_SEC), font=tf)

        y += 20
        bot_text = self._bot_count_text() if show_bot_count else None
        if bot_text:
            bf = self._f("sans_11")
            bb = draw.textbbox((0, 0), bot_text, font=bf)
            bw = bb[2] - bb[0]
            draw.text((COL_RIGHT - bw, y), bot_text,
                      fill=_hex(TEXT_SEC), font=bf)
            y += 18

        return y + MARGIN

    def _to_png(self, img: Image.Image, actual_height: int) -> bytes:
        """Crop canvas to actual height and return PNG bytes."""
        cropped = img.crop((0, 0, CANVAS_W, actual_height))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    def _smart_price(self, v) -> str:
        if not isinstance(v, (int, float)):
            return "\u2014"
        if v > 100:
            return f"{v:,.2f}"
        elif v > 1:
            return f"{v:,.4f}"
        else:
            return f"{v:,.6f}"

    def _direction_color(self, direction: str) -> str:
        d = str(direction).upper()
        if d in ("LONG", "BUY"):
            return TEXT_POS
        elif d in ("SHORT", "SELL"):
            return TEXT_DANGER
        return ACCENT

    # ── Card renderers ───────────────────────────────────────────────────

    def render_high_conviction(self, data: dict) -> bytes:
        """Render HIGH CONVICTION SIGNAL card."""
        pair = data.get("pair", "\u2014")
        direction = str(data.get("direction", "")).upper()
        confidence = data.get("confidence", 0)
        bot_alignment = data.get("bot_alignment", [])
        regime = data.get("regime", "\u2014")
        aegis_score = data.get("aegis_score")
        adx = data.get("adx")
        hurst = data.get("hurst")
        council_active = data.get("council_active", [])
        whale_intel = data.get("whale_intel", "\u2014")
        bot_agreement = data.get("bot_agreement", 0)
        signal_clarity = data.get("signal_clarity", 0)
        decay_info = data.get("decay_info", "Fresh \u2014 0.0 half-lives")
        timestamp = data.get("timestamp")

        # Calculate height
        h = 60 + 12  # header
        h += 80  # pair/direction/confidence
        h += 10  # separator
        h += 16 + len(bot_alignment) * 26 + 10  # bot alignment section
        h += 10  # separator
        h += 26 * 3 + 10  # regime/council/whale
        h += 10  # separator
        h += 26 * 3 + 10  # entry context
        h += 24 * 2 + 10  # bars
        h += 10  # separator
        h += 60  # footer
        h += 20  # padding
        h = max(h, 500)

        img, draw = self._new_canvas(h)

        # Panel background
        draw.rectangle([MARGIN - 2, MARGIN - 2, COL_RIGHT + 2, h - MARGIN + 2],
                       outline=_hex(BORDER), width=1)
        draw.rectangle([MARGIN, MARGIN, COL_RIGHT, h - MARGIN],
                       fill=_hex(PANEL))

        y = MARGIN + 8
        y = self._draw_header(draw, y, "HIGH CONVICTION SIGNAL",
                              "Multi-system alignment detected", timestamp)

        # Main data
        y = self._draw_kv(draw, y, "Pair", pair)
        dir_label = direction if direction else "\u2014"
        y = self._draw_kv(draw, y, "Direction", dir_label,
                          value_color=self._direction_color(direction))
        conf_s = f"{confidence:.3f}" if isinstance(confidence, (int, float)) else "\u2014"
        y = self._draw_kv(draw, y, "Confidence", conf_s, value_color=ACCENT)

        y = self._draw_separator(draw, y)

        # Bot Alignment
        draw.text((COL_LEFT + 14, y), "Bot Alignment",
                  fill=_hex(TEXT_SEC), font=self._f("sans_13"))
        y += 22
        for bot in bot_alignment:
            name = display_name(bot.get("name", ""))
            signal = str(bot.get("signal", "")).upper()
            detail = bot.get("detail", "")
            nf = self._f("mono_14")
            draw.text((COL_LEFT + 14, y), f"{name:14s}", fill=_hex(TEXT_PRI), font=nf)
            draw.text((COL_LEFT + 14 + 155, y), f"{signal:8s}",
                      fill=_hex(self._direction_color(signal)), font=nf)
            draw.text((COL_LEFT + 14 + 240, y), detail,
                      fill=_hex(TEXT_SEC), font=self._f("mono_11"))
            y += 24

        y = self._draw_separator(draw, y + 4)

        # Regime / Council / Whale
        aegis_s = f"{aegis_score:.3f}" if isinstance(aegis_score, (int, float)) else ""
        regime_val = f"{regime}" + (f"  (AEGIS {aegis_s})" if aegis_s else "")
        y = self._draw_kv(draw, y, "Fleet Regime", regime_val)

        council_s = " \u00b7 ".join(council_active) if council_active else "\u2014"
        y = self._draw_kv(draw, y, "Council", council_s)
        y = self._draw_kv(draw, y, "Whale Intel", whale_intel)

        y = self._draw_separator(draw, y + 4)

        # Entry Context
        draw.text((COL_LEFT + 14, y), "Entry Context",
                  fill=_hex(TEXT_SEC), font=self._f("sans_13"))
        y += 22
        if adx is not None:
            adx_s = f"{adx:.1f}" if isinstance(adx, (int, float)) else str(adx)
            y = self._draw_kv(draw, y, "ADX", adx_s)
        if hurst is not None:
            hurst_s = f"{hurst:.3f}" if isinstance(hurst, (int, float)) else str(hurst)
            y = self._draw_kv(draw, y, "Hurst", hurst_s)
        y = self._draw_kv(draw, y, "Decay", str(decay_info))

        y += 6

        # Bars
        y = self._draw_labeled_bar(draw, y, "Bot Agreement", bot_agreement)
        y = self._draw_labeled_bar(draw, y, "Signal Clarity", signal_clarity)

        y += 6
        y = self._draw_footer(draw, y, timestamp)

        return self._to_png(img, y)

    def render_whale_alert(self, data: dict) -> bytes:
        """Render WHALE ALERT card."""
        pair = data.get("pair", "\u2014")
        magnitude = data.get("magnitude", "\u2014")
        volume = data.get("volume")
        side = data.get("side")
        aegis_regime = data.get("aegis_regime", "\u2014")
        aegis_score = data.get("aegis_score")
        deployed_pct = data.get("deployed_pct", 0)
        timestamp = data.get("timestamp")

        vol_s = f"${volume:,.0f}" if isinstance(volume, (int, float)) else "\u2014"
        aegis_s = f"{aegis_score:.3f}" if isinstance(aegis_score, (int, float)) else "\u2014"
        side_s = str(side).upper() if side else "\u2014"
        dep_s = f"{deployed_pct}%" if isinstance(deployed_pct, (int, float)) else "\u2014"

        h = 420
        img, draw = self._new_canvas(h)

        draw.rectangle([MARGIN - 2, MARGIN - 2, COL_RIGHT + 2, h - MARGIN + 2],
                       outline=_hex(BORDER), width=1)
        draw.rectangle([MARGIN, MARGIN, COL_RIGHT, h - MARGIN],
                       fill=_hex(PANEL))

        y = MARGIN + 8
        y = self._draw_header(draw, y, "WHALE ALERT",
                              "Institutional position detected", timestamp)

        y = self._draw_kv(draw, y, "Pair", pair)
        mag_color = ACCENT if str(magnitude).upper() == "EXTREME" else TEXT_PRI
        y = self._draw_kv(draw, y, "Magnitude", str(magnitude).upper(),
                          value_color=mag_color)
        y = self._draw_kv(draw, y, "Volume", vol_s)
        y = self._draw_kv(draw, y, "Side", side_s)

        y = self._draw_separator(draw, y + 4)

        # Fleet Response
        draw.text((COL_LEFT + 14, y), "Fleet Response",
                  fill=_hex(TEXT_SEC), font=self._f("sans_13"))
        y += 22
        y = self._draw_kv(draw, y, "AEGIS", f"{aegis_regime}  {aegis_s}")
        y = self._draw_kv(draw, y, "Deployed", dep_s)

        # Whale Risk bar
        risk_val = 0.9 if str(magnitude).upper() == "EXTREME" else 0.6
        y += 4
        draw.text((COL_LEFT + 14, y - 2), "Whale Risk",
                  fill=_hex(TEXT_SEC), font=self._f("mono_13"))
        bar_x = COL_LEFT + 14 + 180
        self._draw_bar(draw, bar_x, y + 4, 300, risk_val, 1.0, height=6,
                       fill_color=TEXT_DANGER if risk_val > 0.7 else ACCENT)
        risk_label = "HIGH" if risk_val > 0.7 else "MODERATE"
        draw.text((bar_x + 310 + 10, y - 2), risk_label,
                  fill=_hex(TEXT_DANGER if risk_val > 0.7 else TEXT_SEC),
                  font=self._f("mono_13"))
        y += 28

        y += 6
        y = self._draw_footer(draw, y, timestamp)

        return self._to_png(img, y)

    def render_regime_shift(self, data: dict) -> bytes:
        """Render FLEET REGIME SHIFT card."""
        previous = data.get("previous", "\u2014")
        current = data.get("current", "\u2014")
        score = data.get("score")
        max_deploy = data.get("max_deploy")
        components = data.get("components", {})
        sources = data.get("sources", "")
        timestamp = data.get("timestamp")

        score_s = f"{score:.3f}" if isinstance(score, (int, float)) else "\u2014"
        deploy_s = f"{max_deploy}%" if isinstance(max_deploy, (int, float)) else "\u2014"

        cur_upper = str(current).upper()
        cur_color = TEXT_DANGER if cur_upper in ("DEFENSIVE", "BEAR") else (
            TEXT_POS if cur_upper in ("BULL", "NORMAL") else ACCENT)

        # Components
        comp_items = [
            ("Tradability", components.get("tradability", components.get("consensus_entropy"))),
            ("Coherence", components.get("coherence", components.get("signal_coherence"))),
            ("Whale Div", components.get("whale_divergence", components.get("whale_div"))),
            ("Port Stress", components.get("portfolio_stress", components.get("port_stress"))),
            ("PHITEX Sig", components.get("phitex_signal", components.get("phitex_sig"))),
            ("Correlation", components.get("correlation")),
        ]
        comp_items = [(k, v) for k, v in comp_items if v is not None]

        h = 350 + len(comp_items) * 24 + (30 if sources else 0)
        img, draw = self._new_canvas(h)

        draw.rectangle([MARGIN - 2, MARGIN - 2, COL_RIGHT + 2, h - MARGIN + 2],
                       outline=_hex(BORDER), width=1)
        draw.rectangle([MARGIN, MARGIN, COL_RIGHT, h - MARGIN],
                       fill=_hex(PANEL))

        y = MARGIN + 8
        y = self._draw_header(draw, y, "FLEET REGIME SHIFT",
                              "AEGIS has updated fleet posture", timestamp)

        y = self._draw_kv(draw, y, "Previous", str(previous).upper())
        y = self._draw_kv(draw, y, "Current", str(current).upper(),
                          value_color=cur_color)
        y = self._draw_kv(draw, y, "Score", score_s)
        y = self._draw_kv(draw, y, "Max Deploy", deploy_s)

        y = self._draw_separator(draw, y + 4)

        if comp_items:
            draw.text((COL_LEFT + 14, y), "Component Breakdown",
                      fill=_hex(TEXT_SEC), font=self._f("sans_13"))
            y += 22
            for label, val in comp_items:
                v = float(val) if isinstance(val, (int, float)) else 0
                y = self._draw_labeled_bar(draw, y, label, v)

        if sources:
            y = self._draw_separator(draw, y + 4)
            y = self._draw_kv(draw, y, "Sources", str(sources))

        y += 6
        y = self._draw_footer(draw, y, timestamp)

        return self._to_png(img, y)

    def render_catastrophe(self, data: dict) -> bytes:
        """Render CATASTROPHE WARNING card."""
        cat_type = data.get("type", "Regime Fold")
        ews_score = data.get("ews_score", 0)
        threshold = data.get("threshold", 0.60)
        pair = data.get("pair", "\u2014")
        timestamp = data.get("timestamp")

        ews_s = f"{ews_score:.2f}" if isinstance(ews_score, (int, float)) else "\u2014"
        thresh_s = f"{threshold:.2f}" if isinstance(threshold, (int, float)) else "\u2014"

        h = 480
        img, draw = self._new_canvas(h)

        draw.rectangle([MARGIN - 2, MARGIN - 2, COL_RIGHT + 2, h - MARGIN + 2],
                       outline=_hex(BORDER), width=1)
        draw.rectangle([MARGIN, MARGIN, COL_RIGHT, h - MARGIN],
                       fill=_hex(PANEL))

        y = MARGIN + 8
        y = self._draw_header(draw, y, "\u25c6  CATASTROPHE WARNING",
                              "Thom Catastrophe Theory", timestamp)

        y = self._draw_kv(draw, y, "Type", str(cat_type))
        y = self._draw_kv(draw, y, "EWS Score", ews_s,
                          value_color=TEXT_DANGER)
        y = self._draw_kv(draw, y, "Threshold", thresh_s)
        y = self._draw_kv(draw, y, "Pair", pair)

        y += 10

        # Warning text block
        lines = [
            "The market state is approaching a",
            "bifurcation point. Discontinuous regime",
            "change probable \u2014 not a gradual shift.",
            "",
            "Reduce exposure. Do not add positions.",
        ]
        font = self._f("sans_14")
        for line in lines:
            if line:
                color = TEXT_DANGER if "Reduce" in line else TEXT_PRI
                draw.text((COL_LEFT + 14, y), line, fill=_hex(color), font=font)
            y += 22

        y += 6
        y = self._draw_footer(draw, y, timestamp)

        return self._to_png(img, y)

    @staticmethod
    def _trade_open_voice(direction: str, regime: str, deployed_pct,
                          bot_name, conviction, top_signals) -> str:
        """GoldenEye narrator voice for TRADE_OPEN image cards.
        Returns a 1-2 sentence paragraph in plain language. Max ~160 chars.
        """
        dir_word = "buying" if str(direction).upper() in ("LONG", "BUY") else "shorting"
        reg_human = _human_regime(regime)

        # Bot personality fragment
        bot_intros = {
            "trekbot":       "TrekBot spotted a multi-signal alignment",
            "trekbotshort":  "TrekBot's short-side scanner found a setup",
            "gridzilla":     "Gridzilla is opening a grid",
            "turtlesue":     "TurtleSue's turtle system triggered an entry",
            "nexusbrain":    "NexusBrain found multi-timeframe confluence",
            "rubberband":    "Rubberband caught a mean-reversion dip",
            "contrarian":    "Contrarian detected extreme sentiment",
            "arbitrageur":   "Arbitrageur found a statistical edge",
            "chronos":       "Chronos identified a session-timing opportunity",
        }
        bot_key = (bot_name or "").lower().replace(" ", "").replace("_", "").replace("-", "")
        bot_intro = bot_intros.get(bot_key, f"{bot_name or 'The fleet'} identified an entry")

        # Conviction modifier
        try:
            conv_val = float(conviction or 0)
        except Exception:
            conv_val = 0
        if conv_val >= 0.8:
            conv_tail = "with high confidence"
        elif conv_val >= 0.5:
            conv_tail = "with moderate conviction"
        else:
            conv_tail = "on lower conviction — small position"

        # Regime interpretation fragment
        reg_up = (regime or "").upper()
        if reg_up in ("TRENDING", "TRENDING_UP", "BULL"):
            regime_hint = f"{reg_human.lower()} conditions favor the move"
        elif reg_up in ("BEAR", "TRENDING_DOWN"):
            if str(direction).upper() in ("LONG", "BUY"):
                regime_hint = "trading against the bearish tape"
            else:
                regime_hint = "riding the downtrend"
        elif reg_up in ("RANGING", "NORMAL", "EQUILIBRIUM", "MIXED"):
            regime_hint = f"market is {reg_human.lower()} — watch for follow-through"
        else:
            regime_hint = f"fleet adjusting to {reg_human.lower()}"

        return f"{bot_intro} {conv_tail}. {regime_hint.capitalize()}."

    def render_trade_open(self, data: dict) -> bytes:
        """Render POSITION OPENED card with GoldenEye intelligence voice."""
        pair = data.get("pair", "\u2014")
        direction = str(data.get("direction", "")).upper()
        entry = data.get("entry_price", data.get("entry"))
        size = data.get("size", data.get("amount"))
        stop = data.get("stop_loss", data.get("stop"))
        regime = data.get("regime", data.get("fleet_regime", "\u2014"))
        bot = display_name(data.get("source", data.get("bot", "\u2014")))
        timestamp = data.get("timestamp")

        # Intelligence fields (enriched by IntelligenceBuilder)
        conviction = data.get("conviction", data.get("intel_score"))
        intel_score = data.get("intel_score", data.get("conviction"))
        ensemble_score = data.get("ensemble_score")
        ensemble_direction = data.get("ensemble_direction", "")
        top_signals = data.get("top_signals", [])
        deployed_pct = data.get("deployed_pct", data.get("_deployed_pct"))
        bots_alive = data.get("bots_alive", data.get("_bots_alive"))

        entry_s = self._smart_price(entry)
        size_s = f"{size:,.2f} units" if isinstance(size, (int, float)) else "\u2014"
        stop_s = self._smart_price(stop) if stop else "\u2014"

        # Risk % from entry to stop
        risk_s = "\u2014"
        if isinstance(entry, (int, float)) and isinstance(stop, (int, float)) and entry > 0:
            risk_pct = abs(stop - entry) / entry * 100
            risk_s = f"{risk_pct:.1f}%"

        # Conviction bar (visual)
        conv_val = conviction if isinstance(conviction, (int, float)) else (
            ensemble_score if isinstance(ensemble_score, (int, float)) else None)
        if isinstance(conv_val, (int, float)):
            filled = max(0, min(12, round(conv_val * 12)))
            conv_bar = "\u2593" * filled + "\u2591" * (12 - filled)
            conv_s = f"{conv_val:.2f}"
        else:
            conv_bar = ""
            conv_s = "\u2014"

        # GoldenEye voice line
        bot_name_raw = data.get("source", data.get("bot", ""))
        voice = self._trade_open_voice(
            direction, regime, deployed_pct, bot_name_raw,
            conviction, top_signals)

        # Top signals string
        signals_s = ""
        if top_signals:
            signals_s = " \u00b7 ".join(str(s).replace("_", " ") for s in top_signals[:3])

        # Deployed + bots context line
        ctx_parts = []
        if isinstance(deployed_pct, (int, float)):
            ctx_parts.append(f"Deployed {deployed_pct:.0f}%")
        if bots_alive:
            ctx_parts.append(f"{bots_alive} bots")
        ctx_s = " \u00b7 ".join(ctx_parts) if ctx_parts else ""

        # Dynamic height: base + extra rows for intelligence section
        h = 420
        if signals_s:
            h += 26
        if ctx_s:
            h += 26
        img, draw = self._new_canvas(h)

        draw.rectangle([MARGIN - 2, MARGIN - 2, COL_RIGHT + 2, h - MARGIN + 2],
                       outline=_hex(BORDER), width=1)
        draw.rectangle([MARGIN, MARGIN, COL_RIGHT, h - MARGIN],
                       fill=_hex(PANEL))

        y = MARGIN + 8
        subtitle = f"{bot} \u00b7 {pair}"
        y = self._draw_header(draw, y, "POSITION OPENED", subtitle, timestamp)

        # GoldenEye voice line — italic style via secondary color
        if voice:
            vf = self._f("sans_13")
            # Wrap at ~90 chars
            words = voice.split()
            lines_v = []
            current = ""
            for w in words:
                if len(current) + len(w) + 1 > 88:
                    lines_v.append(current.strip())
                    current = w + " "
                else:
                    current += w + " "
            if current.strip():
                lines_v.append(current.strip())
            for line in lines_v[:2]:
                draw.text((COL_LEFT + 14, y), line, fill=_hex(ACCENT), font=vf)
                y += 20
            y += 4

        y = self._draw_separator(draw, y)

        y = self._draw_kv(draw, y, "Direction", direction,
                          value_color=self._direction_color(direction))
        y = self._draw_kv(draw, y, "Entry", entry_s)
        y = self._draw_kv(draw, y, "Size", size_s)
        y = self._draw_kv(draw, y, "Stop", stop_s)
        if risk_s != "\u2014":
            y = self._draw_kv(draw, y, "Risk", risk_s)
        y = self._draw_kv(draw, y, "Regime", str(regime).upper())

        # Intelligence section
        y = self._draw_separator(draw, y)
        if conv_bar:
            y = self._draw_kv(draw, y, "Conviction", f"{conv_bar} {conv_s}",
                              value_color=ACCENT)
        else:
            y = self._draw_kv(draw, y, "Conviction", conv_s)
        if signals_s:
            y = self._draw_kv(draw, y, "Signals", signals_s, value_color=TEXT_SEC,
                              val_font="sans_13")
        if ctx_s:
            y = self._draw_kv(draw, y, "Fleet", ctx_s, value_color=TEXT_SEC,
                              val_font="sans_13")

        y += 6
        y = self._draw_footer(draw, y, timestamp)

        return self._to_png(img, y)

    @staticmethod
    def _trade_close_voice(pnl, exit_reason: str, duration_s, regime: str,
                           direction: str = "", pair: str = "") -> str:
        """GoldenEye narrator voice for TRADE_CLOSE image cards.
        Returns a 1-2 sentence paragraph in plain language. Max ~180 chars.
        """
        try:
            pnl_val = float(pnl or 0)
        except Exception:
            pnl_val = 0
        won = pnl_val > 0

        # Exit reason in plain language
        exit_reasons_human = {
            "take_profit":   "Hit the profit target",
            "tp":            "Hit the profit target",
            "stop_loss":     "Stopped out for protection",
            "sl":            "Stopped out for protection",
            "trailing_stop": "Trailing stop locked in the move",
            "manual":        "Manually closed",
            "timeout":       "Held too long without movement",
            "signal_decay":  "Original signal lost strength",
            "grid_cycle":    "Completed a grid cycle",
        }
        reason_key = (exit_reason or "").lower()
        reason_text = exit_reasons_human.get(reason_key, "Closed")

        # Duration in human form
        try:
            d = int(duration_s or 0)
        except Exception:
            d = 0
        if d >= 3600:
            dur_human = f"{d // 3600}h {(d % 3600) // 60}m"
        elif d >= 60:
            dur_human = f"{d // 60}m"
        else:
            dur_human = f"{d}s"

        # Outcome line
        pair_label = pair or "this trade"
        if won:
            outcome = f"Profitable exit on {pair_label} — earned ${abs(pnl_val):.2f} net"
        else:
            outcome = f"Closed {pair_label} at a ${abs(pnl_val):.2f} loss"

        return f"{outcome}. {reason_text} after {dur_human}."

    def render_trade_close(self, data: dict) -> bytes:
        """Render POSITION CLOSED card with GoldenEye voice and R-multiple."""
        pair = data.get("pair", "\u2014")
        direction = str(data.get("direction", "")).upper()
        pnl = data.get("pnl")
        entry = data.get("entry_price", data.get("entry"))
        exit_p = data.get("exit_price", data.get("close_price"))
        size = data.get("size_usd", data.get("size"))
        duration_s = data.get("duration_s", data.get("duration"))
        # No fee line on cards (2026-07-30): P/L is gross price movement \u2014
        # subscribers pay their own exchange's fees.
        regime = data.get("regime", data.get("fleet_regime", "\u2014"))
        bot = display_name(data.get("source", data.get("bot", "\u2014")))
        exit_reason = data.get("exit_reason", data.get("reason", ""))
        timestamp = data.get("timestamp")
        stop = data.get("stop_loss", data.get("stop"))

        pnl_s = f"${pnl:+.2f}" if isinstance(pnl, (int, float)) else "\u2014"
        pnl_won = isinstance(pnl, (int, float)) and pnl > 0
        result = "WIN" if pnl_won else "LOSS"
        result_color = TEXT_POS if pnl_won else TEXT_DANGER
        entry_s = self._smart_price(entry)
        exit_s = self._smart_price(exit_p)
        size_s = f"${size:,.2f}" if isinstance(size, (int, float)) else "\u2014"

        # Duration
        if isinstance(duration_s, (int, float)):
            hours = duration_s / 3600
            if hours >= 24:
                dur_s = f"{hours/24:.1f} days"
            elif hours >= 1:
                dur_s = f"{hours:.1f}h"
            else:
                dur_s = f"{duration_s/60:.0f}min"
        else:
            dur_s = "\u2014"

        # Return %
        ret_s = ""
        if isinstance(entry, (int, float)) and isinstance(exit_p, (int, float)) and entry > 0:
            if direction in ("LONG", "BUY"):
                ret_pct = (exit_p - entry) / entry * 100
            else:
                ret_pct = (entry - exit_p) / entry * 100
            ret_s = f"  ({ret_pct:+.2f}%)"

        # R-multiple: how many R units captured vs initial risk
        r_multiple_s = ""
        if (isinstance(pnl, (int, float)) and isinstance(entry, (int, float))
                and isinstance(stop, (int, float)) and entry > 0 and stop != entry):
            risk_per_unit = abs(stop - entry)
            if isinstance(size, (int, float)) and size > 0 and entry > 0:
                units = size / entry
                initial_risk = risk_per_unit * units
                if initial_risk > 0:
                    r_mult = pnl / initial_risk
                    r_multiple_s = f"{r_mult:+.2f}R"

        # GoldenEye voice
        voice = self._trade_close_voice(pnl, exit_reason, duration_s, regime,
                                        direction=direction, pair=pair)

        h = 520
        img, draw = self._new_canvas(h)

        draw.rectangle([MARGIN - 2, MARGIN - 2, COL_RIGHT + 2, h - MARGIN + 2],
                       outline=_hex(BORDER), width=1)
        draw.rectangle([MARGIN, MARGIN, COL_RIGHT, h - MARGIN],
                       fill=_hex(PANEL))

        y = MARGIN + 8
        subtitle = f"{bot} \u00b7 {pair}"
        title = f"POSITION CLOSED \u2014 {result}"
        y = self._draw_header(draw, y, title, subtitle, timestamp)

        # GoldenEye voice line — word-wrapped to prevent right-edge overflow
        if voice:
            vf = self._f("sans_13")
            words = voice.split()
            lines_v = []
            current = ""
            for w in words:
                if len(current) + len(w) + 1 > 88:
                    lines_v.append(current.strip())
                    current = w + " "
                else:
                    current += w + " "
            if current.strip():
                lines_v.append(current.strip())
            for line in lines_v[:2]:
                draw.text((COL_LEFT + 14, y), line, fill=_hex(ACCENT), font=vf)
                y += 20
            y += 4
            y = self._draw_separator(draw, y)

        pnl_display = pnl_s + ret_s
        if r_multiple_s:
            pnl_display += f"  {r_multiple_s}"
        y = self._draw_kv(draw, y, "P/L", pnl_display, value_color=result_color)
        y = self._draw_kv(draw, y, "Direction", direction,
                          value_color=self._direction_color(direction))
        y = self._draw_kv(draw, y, "Entry", entry_s)
        y = self._draw_kv(draw, y, "Exit", exit_s)
        y = self._draw_kv(draw, y, "Size", size_s)
        y = self._draw_kv(draw, y, "Duration", dur_s)
        if exit_reason:
            reason_clean = str(exit_reason).replace("_", " ").title()
            y = self._draw_kv(draw, y, "Reason", reason_clean)
        y = self._draw_kv(draw, y, "Regime", str(regime).upper())

        y += 6
        y = self._draw_footer(draw, y, timestamp)

        return self._to_png(img, y)

    def render_daily_summary(self, data: dict) -> bytes:
        """Render END OF DAY REPORT card."""
        pnl = data.get("fleet_pnl")
        trades = data.get("total_trades", 0)
        win_rate = data.get("win_rate")
        expectancy = data.get("expectancy")
        deployed = data.get("deployed_pct")
        regime = data.get("regime", "\u2014")
        aegis_score = data.get("aegis_score")
        top_bot = display_name(data.get("top_bot", "\u2014"))
        top_pair = data.get("top_pair", "\u2014")
        timestamp = data.get("timestamp")

        pnl_s = f"${pnl:+.2f}" if isinstance(pnl, (int, float)) else "\u2014"
        pnl_color = TEXT_POS if isinstance(pnl, (int, float)) and pnl > 0 else (
            TEXT_DANGER if isinstance(pnl, (int, float)) and pnl < 0 else TEXT_PRI)
        wr_s = f"{win_rate:.0%}" if isinstance(win_rate, float) and win_rate <= 1 else "\u2014"
        ev_s = f"${expectancy:+.2f}" if isinstance(expectancy, (int, float)) else "\u2014"
        dep_s = f"{deployed:.0f}%" if isinstance(deployed, (int, float)) else "\u2014"
        aegis_s = f"{aegis_score:.3f}" if isinstance(aegis_score, (int, float)) else "\u2014"

        h = 480
        img, draw = self._new_canvas(h)

        draw.rectangle([MARGIN - 2, MARGIN - 2, COL_RIGHT + 2, h - MARGIN + 2],
                       outline=_hex(BORDER), width=1)
        draw.rectangle([MARGIN, MARGIN, COL_RIGHT, h - MARGIN],
                       fill=_hex(PANEL))

        y = MARGIN + 8
        y = self._draw_header(draw, y, "END OF DAY REPORT",
                              "Daily fleet performance summary", timestamp)

        y = self._draw_kv(draw, y, "P/L Today", pnl_s, value_color=pnl_color)
        y = self._draw_kv(draw, y, "Trades", str(trades))
        y = self._draw_kv(draw, y, "Win Rate", wr_s)
        y = self._draw_kv(draw, y, "Avg P/L", ev_s + " per trade")
        y = self._draw_kv(draw, y, "Deployed", dep_s)
        y = self._draw_kv(draw, y, "Regime", str(regime).upper())

        y = self._draw_separator(draw, y + 4)

        # AEGIS bar
        if isinstance(aegis_score, (int, float)):
            y = self._draw_labeled_bar(draw, y, "Fleet Health", aegis_score)

        y += 4
        y = self._draw_kv(draw, y, "Best Bot", top_bot)
        y = self._draw_kv(draw, y, "Best Pair", top_pair)

        y += 6
        y = self._draw_footer(draw, y, timestamp)

        return self._to_png(img, y)

    def render_weekly_decomposition(self, data: dict) -> bytes:
        """Render WEEKLY DECOMPOSITION card."""
        total_pnl = data.get("total_pnl")
        total_trades = data.get("total_trades", 0)
        avg_win_rate = data.get("avg_win_rate")
        best_bot = display_name(data.get("best_bot", "\u2014"))
        worst_bot = display_name(data.get("worst_bot", "\u2014"))
        best_pair = data.get("best_pair", "\u2014")
        bot_breakdown = data.get("bot_breakdown", [])
        timestamp = data.get("timestamp")

        pnl_s = f"${total_pnl:+.2f}" if isinstance(total_pnl, (int, float)) else "\u2014"
        pnl_color = TEXT_POS if isinstance(total_pnl, (int, float)) and total_pnl > 0 else (
            TEXT_DANGER if isinstance(total_pnl, (int, float)) and total_pnl < 0 else TEXT_PRI)
        wr_s = f"{avg_win_rate:.0%}" if isinstance(avg_win_rate, float) and avg_win_rate <= 1 else "\u2014"

        h = 400 + len(bot_breakdown) * 26 + 40
        img, draw = self._new_canvas(h)

        draw.rectangle([MARGIN - 2, MARGIN - 2, COL_RIGHT + 2, h - MARGIN + 2],
                       outline=_hex(BORDER), width=1)
        draw.rectangle([MARGIN, MARGIN, COL_RIGHT, h - MARGIN],
                       fill=_hex(PANEL))

        y = MARGIN + 8
        y = self._draw_header(draw, y, "WEEKLY REPORT",
                              "7-day fleet performance decomposition", timestamp)

        y = self._draw_kv(draw, y, "Total P/L", pnl_s, value_color=pnl_color)
        y = self._draw_kv(draw, y, "Trades", str(total_trades))
        y = self._draw_kv(draw, y, "Win Rate", wr_s)
        y = self._draw_kv(draw, y, "Best Bot", best_bot)
        y = self._draw_kv(draw, y, "Worst Bot", worst_bot)
        y = self._draw_kv(draw, y, "Best Pair", best_pair)

        if bot_breakdown:
            y = self._draw_separator(draw, y + 4)
            draw.text((COL_LEFT + 14, y), "Bot Breakdown",
                      fill=_hex(TEXT_SEC), font=self._f("sans_13"))
            y += 22
            for bot in bot_breakdown:
                name = display_name(bot.get("name", ""))
                bpnl = bot.get("pnl", 0)
                bpnl_s = f"${bpnl:+.2f}" if isinstance(bpnl, (int, float)) else "\u2014"
                bc = TEXT_POS if isinstance(bpnl, (int, float)) and bpnl > 0 else TEXT_DANGER
                nf = self._f("mono_14")
                draw.text((COL_LEFT + 14, y), f"{name:14s}", fill=_hex(TEXT_PRI), font=nf)
                draw.text((COL_LEFT + 14 + 180, y), bpnl_s, fill=_hex(bc), font=nf)
                y += 24

        y += 6
        y = self._draw_footer(draw, y, timestamp)

        return self._to_png(img, y)

    # ── End of Day — full data visualization card ─────────────────────────

    def render_end_of_day(self, data: dict, tier: str = "paid") -> bytes:
        """Render END OF DAY card with charts. tier='paid' or 'free'."""
        S = 2  # scale factor for 2x rendering
        W2 = CANVAS_W * S
        M2 = MARGIN * S
        G2 = GUTTER * S

        date_s = data.get("date", datetime.now(timezone.utc).strftime("%d %b %Y"))
        trades = data.get("trades", [])
        # Gross semantics (2026-07-30): one P/L number — gross price
        # movement. No fee row; subscribers pay their own exchange.
        gross = data.get("gross")
        if not isinstance(gross, (int, float)):
            gross = data.get("net")
        n_trades = len(trades)
        wins = [t for t in trades if isinstance(t.get("net"), (int, float)) and t["net"] > 0]
        losses = [t for t in trades if isinstance(t.get("net"), (int, float)) and t["net"] <= 0]
        n_wins = len(wins)
        n_losses = len(losses)
        win_rate = (n_wins / n_trades * 100) if n_trades > 0 else 0.0

        best_trade = max(trades, key=lambda t: t.get("net", 0)) if trades else None
        worst_trade = min(trades, key=lambda t: t.get("net", 0)) if trades else None

        # Cumulative P/L series
        cum_pnl = []
        running = 0.0
        for t in trades:
            running += t.get("net", 0)
            cum_pnl.append(running)

        # Estimate total height at 2x
        h2 = M2 + 110 * S  # header
        h2 += 180 * S       # donut + stats row
        h2 += 20 * S        # gap
        h2 += 130 * S       # sparkline section
        h2 += 20 * S
        h2 += 160 * S       # bar chart section
        h2 += 20 * S
        h2 += 80 * S        # heatmap section
        h2 += 20 * S
        h2 += 160 * S       # best/worst trades
        h2 += 20 * S
        h2 += 200 * S       # personal note
        h2 += 80 * S        # footer
        if n_trades == 0:
            h2 = M2 + 700 * S  # shorter for no-trade days

        img = Image.new("RGB", (W2, h2), _hex(BG))
        draw = ImageDraw.Draw(img)

        # Helpers for 2x fonts
        def f2(name):
            return self._f(f"{name}_2x")

        def sep(y):
            draw.line([(M2, y), (W2 - M2, y)], fill=_hex(BORDER), width=S)
            return y + 10 * S

        CL = M2 + 6 * S  # content left
        CR = W2 - M2      # content right
        content_w = CR - CL - 14 * S

        # ── Panel background
        draw.rectangle([M2 - 2*S, M2 - 2*S, CR + 2*S, h2 - M2 + 2*S],
                       outline=_hex(BORDER), width=S)
        draw.rectangle([M2, M2, CR, h2 - M2], fill=_hex(PANEL))

        # ── Header
        y = M2 + 8 * S
        # Gold left bar
        draw.rectangle([M2, y, M2 + 4*S, y + 52*S], fill=_hex(ACCENT))
        tx = M2 + 14 * S
        draw.text((tx, y + 4*S), "END OF DAY", fill=_hex(TEXT_PRI), font=f2("sans_20"))
        draw.text((tx, y + 34*S), f"GoldenEye Intelligence", fill=_hex(ACCENT), font=f2("sans_13"))
        # Date left, time right
        draw.text((tx, y + 52*S), date_s, fill=_hex(TEXT_SEC), font=f2("sans_13"))
        ts_text = data.get("timestamp", "23:59 UTC")
        tb = draw.textbbox((0, 0), ts_text, font=f2("mono_11"))
        draw.text((CR - (tb[2] - tb[0]) - 14*S, y + 8*S), ts_text,
                  fill=_hex(TEXT_SEC), font=f2("mono_11"))
        y += 76 * S

        if n_trades == 0:
            # No-trade day — still show stats block with zeros
            y += 10 * S
            zero_stats = [
                ("Trades", "0", TEXT_PRI),
                ("Wins", "0", TEXT_PRI),
                ("Losses", "0", TEXT_PRI),
                ("Net P/L", "$0.00", TEXT_PRI),
            ]
            zx = CL + 14 * S
            for label, val, color in zero_stats:
                draw.text((zx, y), f"{label:8s}", fill=_hex(TEXT_SEC), font=f2("mono_14"))
                draw.text((zx + 110*S, y), val, fill=_hex(color), font=f2("mono_14"))
                y += 24 * S
            y += 10 * S
            msg = "Fleet held cash today \u2014 no setups met the threshold."
            draw.text((CL + 14*S, y), msg, fill=_hex(TEXT_SEC), font=f2("sans_14"))
            y += 40 * S
            y = sep(y)
            y = self._eod_personal_note(draw, y, S, CL, CR, f2)
            y += 10 * S
            y = self._eod_footer(draw, y, S, CL, CR, W2, M2, ts_text, f2)
            return self._downsample(img, y + M2, S)

        # ── Donut chart (left) + Stats (right)
        donut_size = 160 * S
        donut_x = CL + 20 * S
        donut_y = y + 10 * S
        self._draw_donut(draw, donut_x, donut_y, donut_size, win_rate,
                         n_wins, n_losses, S, f2)

        # Stats column right of donut
        sx = donut_x + donut_size + 30 * S
        sy = y + 16 * S
        stats_font = f2("mono_14")
        stats_val_font = f2("mono_14")
        stat_rows = [
            ("Trades", str(n_trades), TEXT_PRI),
            ("Wins", str(n_wins), TEXT_POS),
            ("Losses", str(n_losses), TEXT_DANGER),
        ]
        if isinstance(gross, (int, float)):
            color = TEXT_POS if gross >= 0 else TEXT_DANGER
            stat_rows.append(("P/L", f"{'+'if gross>=0 else ''}${abs(gross):.2f}", color))
        else:
            stat_rows.append(("P/L", "\u2014", TEXT_PRI))

        for label, val, color in stat_rows:
            draw.text((sx, sy), f"{label:8s}", fill=_hex(TEXT_SEC), font=stats_font)
            draw.text((sx + 110*S, sy), val, fill=_hex(color), font=stats_val_font)
            sy += 24 * S

        y += max(donut_size + 20*S, len(stat_rows) * 24*S + 20*S)

        # ── Cumulative P/L Sparkline
        y += 10 * S
        y = sep(y)
        draw.text((CL + 14*S, y), "CUMULATIVE P/L", fill=_hex(TEXT_SEC), font=f2("sans_13"))
        y += 22 * S
        spark_h = 80 * S
        spark_x = CL + 14 * S
        spark_w = content_w - 28 * S
        self._draw_sparkline(draw, img, spark_x, y, spark_w, spark_h,
                             cum_pnl, S, f2)
        # Start/end labels
        y_label = y + spark_h + 6 * S
        draw.text((spark_x, y_label), "Start: $0.00", fill=_hex(TEXT_SEC), font=f2("mono_10"))
        if cum_pnl:
            final = cum_pnl[-1]
            final_s = f"Close: {'+'if final>=0 else ''}${abs(final):.2f}"
            fb = draw.textbbox((0, 0), final_s, font=f2("mono_10"))
            draw.text((spark_x + spark_w - (fb[2] - fb[0]), y_label),
                      final_s, fill=_hex(TEXT_POS if final >= 0 else TEXT_DANGER),
                      font=f2("mono_10"))
        y = y_label + 24 * S

        # ── Trade Breakdown Bar Chart
        y = sep(y)
        draw.text((CL + 14*S, y), "TRADE BREAKDOWN", fill=_hex(TEXT_SEC), font=f2("sans_13"))
        y += 22 * S
        chart_h = 100 * S
        chart_x = CL + 14 * S
        chart_w = content_w - 28 * S
        self._draw_trade_bars(draw, chart_x, y, chart_w, chart_h,
                              trades, cum_pnl, S, f2)
        y += chart_h + 20 * S

        # ── Hourly Activity Heatmap
        y = sep(y)
        draw.text((CL + 14*S, y), "TRADE ACTIVITY", fill=_hex(TEXT_SEC), font=f2("sans_13"))
        y += 22 * S
        heat_h = 40 * S
        heat_x = CL + 14 * S
        heat_w = content_w - 28 * S
        self._draw_heatmap(draw, heat_x, y, heat_w, heat_h, trades, S, f2)
        y += heat_h + 24 * S

        # ── Best / Worst Trade
        y = sep(y)
        y = self._eod_best_worst(draw, y, S, CL, CR, best_trade, worst_trade,
                                 tier, f2)

        # ── Personal note
        y = sep(y)
        y = self._eod_personal_note(draw, y, S, CL, CR, f2)

        # ── Footer
        y += 6 * S
        y = sep(y)
        y = self._eod_footer(draw, y, S, CL, CR, W2, M2, ts_text, f2)

        return self._downsample(img, y + M2, S)

    # ── EOD sub-renderers ────────────────────────────────────────────────

    def _draw_donut(self, draw, x, y, size, win_rate, n_wins, n_losses, S, f2):
        """Draw win/loss donut ring chart."""
        stroke = 24 * S
        cx = x + size // 2
        cy = y + size // 2
        r = size // 2 - stroke // 2
        bbox = [cx - r, cy - r, cx + r, cy + r]

        # Background ring
        draw.arc(bbox, 0, 360, fill=_hex(BAR_BG), width=stroke)

        # Win arc (green, from top = -90 degrees)
        if win_rate > 0:
            win_angle = win_rate / 100 * 360
            draw.arc(bbox, -90, -90 + win_angle, fill=_hex(TEXT_POS), width=stroke)
        # Loss arc (red, from where win ends)
        if win_rate < 100:
            loss_start = -90 + (win_rate / 100 * 360)
            draw.arc(bbox, loss_start, 270, fill=_hex(TEXT_DANGER), width=stroke)

        # Center text — win rate
        wr_s = f"{win_rate:.1f}%"
        wrf = f2("sans_18")
        wb = draw.textbbox((0, 0), wr_s, font=wrf)
        ww = wb[2] - wb[0]
        wh = wb[3] - wb[1]
        draw.text((cx - ww // 2, cy - wh - 4*S), wr_s,
                  fill=_hex(TEXT_PRI), font=wrf)

        # W / L count below
        wl_s = f"{n_wins}W / {n_losses}L"
        wlf = f2("mono_11")
        wlb = draw.textbbox((0, 0), wl_s, font=wlf)
        wlw = wlb[2] - wlb[0]
        draw.text((cx - wlw // 2, cy + 4*S), wl_s,
                  fill=_hex(TEXT_SEC), font=wlf)

    def _draw_sparkline(self, draw, img, x, y, w, h, cum_pnl, S, f2):
        """Draw cumulative P/L sparkline with area fill."""
        if not cum_pnl:
            return

        n = len(cum_pnl)
        min_v = min(0, min(cum_pnl))
        max_v = max(0, max(cum_pnl))
        span = max_v - min_v if max_v != min_v else 1.0

        # Zero line y position
        zero_y = y + int(h * (max_v / span))
        draw.line([(x, zero_y), (x + w, zero_y)], fill=_hex("#2a2a2a"), width=S)

        # Build points
        points = []
        for i, v in enumerate(cum_pnl):
            px = x + int(w * i / max(n - 1, 1))
            py = y + int(h * ((max_v - v) / span))
            points.append((px, py))

        # Determine line color
        final = cum_pnl[-1]
        line_color = TEXT_POS if final >= 0 else TEXT_DANGER

        # Area fill with transparency
        if len(points) >= 2:
            fill_color = _hex(line_color) + (50,)  # 20% opacity
            fill_img = Image.new("RGBA", img.size, (0, 0, 0, 0))
            fill_draw = ImageDraw.Draw(fill_img)
            polygon = list(points) + [(points[-1][0], zero_y), (points[0][0], zero_y)]
            fill_draw.polygon(polygon, fill=fill_color)
            img.paste(Image.alpha_composite(
                img.convert("RGBA"), fill_img).convert("RGB"))
            # Re-create draw since we pasted
            # We need to draw on the img directly after paste
            draw.__init__(img)

        # Line
        if len(points) >= 2:
            draw.line(points, fill=_hex(line_color), width=2 * S)

        # Start dot
        if points:
            px, py = points[0]
            draw.ellipse([px - 3*S, py - 3*S, px + 3*S, py + 3*S],
                         fill=_hex(line_color))
        # End dot + value
        if len(points) > 1:
            px, py = points[-1]
            draw.ellipse([px - 3*S, py - 3*S, px + 3*S, py + 3*S],
                         fill=_hex(line_color))

    def _draw_trade_bars(self, draw, x, y, w, h, trades, cum_pnl, S, f2):
        """Draw P/L bar chart with cumulative overlay."""
        if not trades:
            return

        n = len(trades)
        pnl_values = [t.get("net", 0) for t in trades]
        max_abs = max(abs(v) for v in pnl_values) if pnl_values else 1
        if max_abs == 0:
            max_abs = 1

        # Zero line at vertical center
        zero_y = y + h // 2
        draw.line([(x, zero_y), (x + w, zero_y)], fill=_hex("#2a2a2a"), width=S)

        bar_gap = 4 * S
        total_gap = bar_gap * (n - 1) if n > 1 else 0
        bar_w = max(4 * S, (w - total_gap) // n)

        # Y-axis labels
        top_label = f"+${max_abs:.0f}" if max_abs >= 1 else f"+${max_abs:.2f}"
        bot_label = f"-${max_abs:.0f}" if max_abs >= 1 else f"-${max_abs:.2f}"
        lf = f2("mono_10")
        draw.text((x, y - 2*S), top_label, fill=_hex(TEXT_SEC), font=lf)
        bb = draw.textbbox((0, 0), bot_label, font=lf)
        draw.text((x, y + h + 2*S), bot_label, fill=_hex(TEXT_SEC), font=lf)

        half_h = h // 2
        for i, pnl in enumerate(pnl_values):
            bx = x + i * (bar_w + bar_gap)
            bar_h = int(half_h * abs(pnl) / max_abs)
            color = TEXT_POS if pnl > 0 else TEXT_DANGER
            if pnl > 0:
                draw.rectangle([bx, zero_y - bar_h, bx + bar_w, zero_y],
                               fill=_hex(color))
            elif pnl < 0:
                draw.rectangle([bx, zero_y, bx + bar_w, zero_y + bar_h],
                               fill=_hex(color))

        # Cumulative P/L overlay line
        if cum_pnl and len(cum_pnl) == n:
            max_cum = max(abs(v) for v in cum_pnl) if cum_pnl else 1
            if max_cum == 0:
                max_cum = 1
            cum_points = []
            for i, v in enumerate(cum_pnl):
                cx = x + i * (bar_w + bar_gap) + bar_w // 2
                cy = zero_y - int(half_h * v / max_cum)
                cum_points.append((cx, cy))
            if len(cum_points) >= 2:
                draw.line(cum_points, fill=_hex(ACCENT), width=2 * S)

    def _draw_heatmap(self, draw, x, y, w, h, trades, S, f2):
        """Draw 24-column hourly activity heatmap."""
        # Count trades per hour
        hourly = [0] * 24
        for t in trades:
            hr = t.get("hour")
            if isinstance(hr, int) and 0 <= hr < 24:
                hourly[hr] += 1
        max_count = max(hourly) if any(hourly) else 1

        col_gap = 2 * S
        col_w = (w - 23 * col_gap) // 24

        for i in range(24):
            cx = x + i * (col_w + col_gap)
            count = hourly[i]
            if count == 0:
                color = BAR_BG
            else:
                # Interpolate gold intensity
                ratio = count / max_count
                base = _hex(ACCENT_DIM)
                bright = _hex(ACCENT)
                r = int(base[0] + (bright[0] - base[0]) * ratio)
                g = int(base[1] + (bright[1] - base[1]) * ratio)
                b = int(base[2] + (bright[2] - base[2]) * ratio)
                color = f"#{r:02x}{g:02x}{b:02x}"

            bar_h = max(4*S, int(h * (count / max_count))) if count > 0 else 4*S
            if count == 0:
                bar_h = 4 * S
            draw.rectangle([cx, y + h - bar_h, cx + col_w, y + h],
                           fill=_hex(color) if color == BAR_BG else (r, g, b))

        # Hour labels
        lf = f2("mono_10")
        label_y = y + h + 4 * S
        for hr in [0, 6, 12, 18, 23]:
            lx = x + hr * (col_w + col_gap)
            draw.text((lx, label_y), f"{hr:02d}", fill=_hex(TEXT_SEC), font=lf)

    def _eod_best_worst(self, draw, y, S, CL, CR, best, worst, tier, f2):
        """Draw best/worst trade section."""
        kf = f2("mono_13")
        vf = f2("mono_13")
        hf = f2("sans_13")
        indent = CL + 14 * S

        for label, trade, color in [("Best Trade", best, TEXT_POS),
                                     ("Worst Trade", worst, TEXT_DANGER)]:
            if not trade:
                continue
            draw.text((indent, y), label, fill=_hex(TEXT_SEC), font=f2("sans_14"))
            y += 24 * S

            pair = trade.get("pair", "\u2014")
            net_v = trade.get("net", 0)
            net_s = f"{'+'if net_v>=0 else '-'}${abs(net_v):.2f}"
            side = str(trade.get("side", "\u2014")).upper()

            if tier == "paid":
                bot = display_name(trade.get("bot", "\u2014"))
                # Two-column layout: Bot + Pair on one row, Side + Net on next
                draw.text((indent, y), "Bot", fill=_hex(TEXT_SEC), font=kf)
                draw.text((indent + 60*S, y), bot, fill=_hex(TEXT_PRI), font=vf)
                draw.text((indent + 280*S, y), "Pair", fill=_hex(TEXT_SEC), font=kf)
                draw.text((indent + 340*S, y), pair, fill=_hex(TEXT_PRI), font=vf)
                y += 22 * S
                draw.text((indent, y), "Side", fill=_hex(TEXT_SEC), font=kf)
                draw.text((indent + 60*S, y), side,
                          fill=_hex(self._direction_color(side)), font=vf)
                draw.text((indent + 280*S, y), "Net", fill=_hex(TEXT_SEC), font=kf)
                draw.text((indent + 340*S, y), net_s, fill=_hex(color), font=vf)
                y += 22 * S

                # Why field
                why = trade.get("exit_reason", trade.get("why", ""))
                if why:
                    draw.text((indent, y), "Why", fill=_hex(TEXT_SEC), font=kf)
                    # Word wrap the why text
                    why_x = indent + 60 * S
                    max_why_w = CR - why_x - 14 * S
                    wrapped = self._wrap_text(str(why), f2("mono_11"), max_why_w, draw)
                    for line in wrapped[:3]:  # max 3 lines
                        draw.text((why_x, y), line, fill=_hex(TEXT_SEC),
                                  font=f2("mono_11"))
                        y += 18 * S
            else:
                # Free tier — pair and net only
                draw.text((indent, y), "Pair", fill=_hex(TEXT_SEC), font=kf)
                draw.text((indent + 60*S, y), pair, fill=_hex(TEXT_PRI), font=vf)
                draw.text((indent + 280*S, y), "Net", fill=_hex(TEXT_SEC), font=kf)
                draw.text((indent + 340*S, y), net_s, fill=_hex(color), font=vf)
                y += 22 * S

            y += 10 * S

        if tier == "free":
            draw.text((indent, y), "Full analysis \u2192 Fleet Intelligence",
                      fill=_hex(ACCENT_DIM), font=f2("sans_12"))
            y += 22 * S

        return y

    def _eod_personal_note(self, draw, y, S, CL, CR, f2):
        """Draw the personal note block."""
        indent = CL + 14 * S
        font = f2("sans_13")
        lines = [
            "Thanks for being here today.",
            "I got scammed twice by signal services.",
            "Paid real money for signals that were",
            "fabricated or just made up. So I built",
            "an honest one. Whatever it sees, you",
            "see. Good days and bad ones.",
            "",
            "\u2014 GoldenEye Intelligence",
        ]
        for line in lines:
            if line:
                color = ACCENT if line.startswith("\u2014") else TEXT_SEC
                draw.text((indent, y), line, fill=_hex(color), font=font)
            y += 20 * S
        return y

    def _eod_footer(self, draw, y, S, CL, CR, W2, M2, ts_text, f2):
        """Draw the EOD footer."""
        y += 4 * S
        gf = f2("sans_12")
        draw.text((CL + 14*S, y), "GoldenEye Intelligence",
                  fill=_hex(ACCENT), font=gf)
        ct = "Fleet Intelligence"
        cb = draw.textbbox((0, 0), ct, font=gf)
        draw.text(((W2 - (cb[2] - cb[0])) // 2, y), ct,
                  fill=_hex(TEXT_SEC), font=gf)
        tf = f2("mono_10")
        tb = draw.textbbox((0, 0), ts_text, font=tf)
        draw.text((CR - (tb[2] - tb[0]) - 14*S, y + 2*S), ts_text,
                  fill=_hex(TEXT_SEC), font=tf)
        y += 22 * S
        bot_text = self._bot_count_text()
        if not bot_text:
            return y
        bf = f2("sans_11")
        bb = draw.textbbox((0, 0), bot_text, font=bf)
        draw.text((CR - (bb[2] - bb[0]) - 14*S, y), bot_text,
                  fill=_hex(TEXT_SEC), font=bf)
        return y + 20 * S

    def _downsample(self, img: Image.Image, actual_h2: int, S: int) -> bytes:
        """Crop to actual height, downsample 2x to 1x, return PNG bytes."""
        cropped = img.crop((0, 0, CANVAS_W * S, actual_h2))
        final = cropped.resize((CANVAS_W, actual_h2 // S), Image.LANCZOS)
        buf = io.BytesIO()
        final.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    def _wrap_text(self, text: str, font, max_w: int,
                   draw: ImageDraw.Draw) -> List[str]:
        """Simple word-wrap for text within max_w pixels."""
        words = text.replace("\u00b7", " \u00b7").split()
        lines = []
        current = ""
        for word in words:
            test = f"{current} {word}".strip()
            bb = draw.textbbox((0, 0), test, font=font)
            if bb[2] - bb[0] <= max_w:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines if lines else [text]

    def render_aegis_update(self, data: dict) -> bytes:
        """Render AEGIS / FLEET REGIME SHIFT card (alias for regime_shift)."""
        return self.render_regime_shift(data)

    # ── Copyable code blocks for Telegram follow-up messages ─────────────

    def copyable_trade_open(self, data: dict) -> Optional[str]:
        """Return individual backtick-wrapped values for one-tap copy.

        Each value gets its own code span so Telegram renders a separate
        copy button per value. Only includes values that exist and aren't
        dashes — don't send a dash as a copyable value.
        Returns None if no values exist at all.
        """
        entry = data.get("entry_price", data.get("entry", data.get("price",
                         data.get("avg_entry"))))
        stop = data.get("stop_loss", data.get("stop", data.get("current_stop")))
        tp = data.get("take_profit", data.get("tp", data.get("target")))
        size = data.get("size", data.get("amount", data.get("total_size")))

        blocks = []
        if isinstance(entry, (int, float)):
            blocks.append(f"Entry\n`{self._smart_price(entry)}`")
        if isinstance(stop, (int, float)):
            blocks.append(f"Stop\n`{self._smart_price(stop)}`")
        if isinstance(tp, (int, float)):
            blocks.append(f"T/P\n`{self._smart_price(tp)}`")
        if isinstance(size, (int, float)):
            size_s = f"{size:,.2f}" if size > 1 else f"{size:.6f}"
            blocks.append(f"Size\n`{size_s}`")

        if not blocks:
            return None
        return "\n\n".join(blocks)

    def copyable_trade_close(self, data: dict) -> Optional[str]:
        """Return individual backtick-wrapped values for one-tap copy.

        Returns None if no values exist at all.
        """
        exit_p = data.get("exit_price", data.get("close_price"))
        pnl = data.get("pnl")
        # No fee/net copy blocks (2026-07-30): P/L is gross — subscribers
        # pay their own exchange's fees.

        blocks = []
        if isinstance(exit_p, (int, float)):
            blocks.append(f"Exit\n`{self._smart_price(exit_p)}`")
        if isinstance(pnl, (int, float)):
            pnl_s = f"{'+' if pnl >= 0 else '-'}${abs(pnl):.2f}"
            blocks.append(f"P/L\n`{pnl_s}`")

        if not blocks:
            return None
        return "\n\n".join(blocks)


# ── Convenience for direct testing ───────────────────────────────────────────

if __name__ == "__main__":
    r = CardRenderer()

    # HIGH CONVICTION
    png = r.render_high_conviction({
        "pair": "BTC/USD",
        "direction": "LONG",
        "confidence": 0.847,
        "bot_alignment": [
            {"name": "TrekBot", "signal": "LONG", "detail": "27-signal adaptive"},
            {"name": "NexusBrain", "signal": "LONG", "detail": "50-pair confluence"},
            {"name": "Trinity", "signal": "TREND", "detail": "multi-timeframe"},
        ],
        "regime": "NORMAL",
        "aegis_score": 0.361,
        "adx": 32.3,
        "hurst": 0.650,
        "council_active": ["Einstein active", "Causal flow"],
        "whale_intel": "Neutral",
        "bot_agreement": 0.65,
        "signal_clarity": 0.50,
    })
    with open("test_conviction.png", "wb") as f:
        f.write(png)
    print(f"HIGH CONVICTION: {len(png)} bytes")

    # WHALE ALERT
    png = r.render_whale_alert({
        "pair": "OP/USD",
        "magnitude": "EXTREME",
        "volume": 692821,
        "side": None,
        "aegis_regime": "CAUTIOUS",
        "aegis_score": 0.232,
        "deployed_pct": 0,
    })
    with open("test_whale.png", "wb") as f:
        f.write(png)
    print(f"WHALE ALERT: {len(png)} bytes")

    # REGIME SHIFT
    png = r.render_regime_shift({
        "previous": "CAUTIOUS",
        "current": "DEFENSIVE",
        "score": 0.241,
        "max_deploy": 30,
        "components": {
            "tradability": 0.302,
            "coherence": 0.500,
            "whale_divergence": 0.000,
            "portfolio_stress": 0.000,
            "phitex_signal": 0.341,
        },
        "sources": "trinity:RANGING \u00b7 hivemind:bull",
    })
    with open("test_regime.png", "wb") as f:
        f.write(png)
    print(f"REGIME SHIFT: {len(png)} bytes")

    # CATASTROPHE WARNING
    png = r.render_catastrophe({
        "pair": "ETH/USD",
        "type": "Regime Fold",
        "ews_score": 0.74,
        "threshold": 0.60,
    })
    with open("test_catastrophe.png", "wb") as f:
        f.write(png)
    print(f"CATASTROPHE: {len(png)} bytes")

    # TRADE OPEN
    png = r.render_trade_open({
        "pair": "AAVEUSD",
        "direction": "SHORT",
        "entry_price": 87.89,
        "size": 16.52,
        "stop": None,
        "regime": "CAUTIOUS",
        "source": "TurtleSue",
    })
    with open("test_trade_open.png", "wb") as f:
        f.write(png)
    print(f"TRADE OPEN: {len(png)} bytes")

    # TRADE CLOSE
    png = r.render_trade_close({
        "pair": "BTC/USD",
        "direction": "LONG",
        "pnl": 12.47,
        "entry_price": 83200.00,
        "exit_price": 83450.00,
        "size_usd": 500.00,
        "duration_s": 7200,
        "regime": "NORMAL",
        "source": "TrekBot",
    })
    with open("test_trade_close.png", "wb") as f:
        f.write(png)
    print(f"TRADE CLOSE: {len(png)} bytes")

    # DAILY SUMMARY
    png = r.render_daily_summary({
        "fleet_pnl": -3.22,
        "total_trades": 4,
        "win_rate": 0.50,
        "expectancy": -0.81,
        "deployed_pct": 12,
        "regime": "CAUTIOUS",
        "aegis_score": 0.361,
        "top_bot": "TrekBot",
        "top_pair": "BTC/USD",
    })
    with open("test_daily.png", "wb") as f:
        f.write(png)
    print(f"DAILY SUMMARY: {len(png)} bytes")

    print("All test cards rendered.")
