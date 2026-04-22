"""
OracleCardRenderer — PNG signal cards for GoldenEye Intelligence.

Six card types rendered at 2400px with Pillow, downsampled to 1200px via LANCZOS.
Pure ImageDraw primitives. No matplotlib. No external charting.

Cards: POSITION OPENED, POSITION CLOSED (win/loss), WHALE ALERT,
       END OF DAY (trades), END OF DAY (flat).
"""

from PIL import Image, ImageDraw, ImageFont
import io
import math
import random
import os

# ---------------------------------------------------------------------------
# Font resolution — Windows system fonts
# ---------------------------------------------------------------------------
_FONTS_DIR = "C:/Windows/Fonts"

def _font(name, size):
    """Load a TrueType font by short name, fall back to default."""
    mapping = {
        "sans":       "segoeui.ttf",
        "sans-bold":  "segoeuib.ttf",
        "sans-light": "segoeuil.ttf",
        "sans-semi":  "segoeuisl.ttf",
        "mono":       "consola.ttf",
        "mono-bold":  "consolab.ttf",
        "courier":    "cour.ttf",
        "courier-bold": "courbd.ttf",
    }
    fname = mapping.get(name, name)
    path = os.path.join(_FONTS_DIR, fname)
    try:
        return ImageFont.truetype(path, size)
    except (OSError, IOError):
        return ImageFont.load_default()


class OracleCardRenderer:

    # Render dimensions
    RENDER_W = 2400
    OUTPUT_W = 1200
    MARGIN = 96
    SECTION_GAP = 64
    ROW_H = 72

    # Colors — RGB
    BG =             (7,   7,   7)
    PANEL =          (14,  14,  14)
    PANEL_WARM =     (13,  11,  9)
    ACCENT =         (200, 169, 110)
    ACCENT_DIM =     (107, 90,  56)
    TEXT_PRIMARY =   (239, 239, 239)
    TEXT_SECONDARY = (96,  96,  96)
    TEXT_POSITIVE =  (76,  175, 125)
    TEXT_NEGATIVE =  (192, 57,  43)
    TEXT_WARNING =   (232, 168, 56)
    TEXT_VOICE =     (212, 184, 150)
    SEPARATOR =      (22,  22,  22)

    # Typography sizes at render scale (2x)
    HERO_SIZE =       240
    HERO_LABEL_SIZE = 60
    HEADER_SIZE =     72
    SUBHEADER_SIZE =  60
    SECTION_SIZE =    56
    DATA_SIZE =       64
    VOICE_SIZE =      68
    FOOTER_SIZE =     52
    WORDMARK_SIZE =   80

    # Bars
    BAR_H =  36
    BAR_RADIUS = 18

    INNER_W = RENDER_W - 2 * MARGIN

    # ------------------------------------------------------------------
    # Canvas + finalization
    # ------------------------------------------------------------------

    MAX_H = 8000  # oversized canvas — will be cropped

    def _new_canvas(self, height=None):
        h = height or self.MAX_H
        img = Image.new("RGB", (self.RENDER_W, h), self.BG)
        draw = ImageDraw.Draw(img)
        return img, draw

    def _crop_and_finalize(self, img, content_bottom):
        """Crop canvas to content height, add footer, noise, downsample."""
        footer_h = self.MARGIN + 120  # 40px extra padding above footer
        total_h = content_bottom + footer_h
        cropped = img.crop((0, 0, self.RENDER_W, total_h))
        draw = ImageDraw.Draw(cropped)
        self._draw_footer(draw, total_h)
        self._draw_noise_grain(cropped)
        out = self._downsample(cropped)
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    def _downsample(self, img):
        h = int(img.height * self.OUTPUT_W / self.RENDER_W)
        return img.resize((self.OUTPUT_W, h), Image.LANCZOS)

    def _finalize(self, img):
        self._draw_noise_grain(img)
        out = self._downsample(img)
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    # ------------------------------------------------------------------
    # Noise grain — Law 2
    # ------------------------------------------------------------------

    def _draw_noise_grain(self, img):
        pixels = img.load()
        w, h = img.size
        for _ in range(60000):
            x = random.randint(0, w - 1)
            y = random.randint(0, h - 1)
            sz = random.randint(1, 2)
            alpha = random.uniform(0.02, 0.03)
            tone = random.randint(180, 255)
            r, g, b = pixels[x, y]
            nr = min(255, int(r + (tone - r) * alpha))
            ng = min(255, int(g + (tone - g) * alpha))
            nb = min(255, int(b + (tone - b) * alpha))
            pixels[x, y] = (nr, ng, nb)
            if sz == 2 and x + 1 < w and y + 1 < h:
                for dx, dy in [(1, 0), (0, 1)]:
                    px, py = x + dx, y + dy
                    r2, g2, b2 = pixels[px, py]
                    pixels[px, py] = (
                        min(255, int(r2 + (tone - r2) * alpha)),
                        min(255, int(g2 + (tone - g2) * alpha)),
                        min(255, int(b2 + (tone - b2) * alpha)),
                    )

    # ------------------------------------------------------------------
    # Radial depth — Law 1
    # ------------------------------------------------------------------

    def _draw_radial_depth(self, img, cx, cy):
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        opacities = [0.06, 0.04, 0.03, 0.02, 0.01]
        radii = [80, 140, 200, 280, 380]
        for radius, opacity in zip(radii, opacities):
            a = int(255 * opacity)
            color = (self.ACCENT[0], self.ACCENT[1], self.ACCENT[2], a)
            rx = int(radius * 1.8)
            ry = radius
            od.ellipse(
                [cx - rx, cy - ry, cx + rx, cy + ry],
                fill=color,
            )
        img.paste(Image.alpha_composite(
            img.convert("RGBA"), overlay
        ).convert("RGB"))

    # ------------------------------------------------------------------
    # Rounded rect
    # ------------------------------------------------------------------

    def _rounded_rect(self, draw, bbox, radius, fill, outline=None):
        x0, y0, x1, y1 = bbox
        r = min(radius, (x1 - x0) // 2, (y1 - y0) // 2)
        if r < 1:
            draw.rectangle(bbox, fill=fill, outline=outline)
            return
        draw.rounded_rectangle(bbox, radius=r, fill=fill, outline=outline)

    # ------------------------------------------------------------------
    # Header block
    # ------------------------------------------------------------------

    def _draw_header(self, draw, img, card_type, subtitle, ts, date):
        y = self.MARGIN
        x = self.MARGIN
        bar_h = 100
        draw.rectangle([x, y, x + 4, y + bar_h], fill=self.ACCENT)

        fnt_type = _font("sans-bold", self.HEADER_SIZE)
        fnt_ts = _font("mono", self.SUBHEADER_SIZE)
        fnt_sub = _font("sans-semi", self.SUBHEADER_SIZE)
        fnt_date = _font("mono", self.SECTION_SIZE)

        # Card type + timestamp on same line
        draw.text((x + 24, y), card_type, fill=self.TEXT_PRIMARY, font=fnt_type)
        draw.text((self.RENDER_W - self.MARGIN, y + 6), ts,
                  fill=self.TEXT_SECONDARY, font=fnt_ts, anchor="ra")

        # Subtitle + date on second line
        draw.text((x + 24, y + 60), subtitle,
                  fill=self.TEXT_SECONDARY, font=fnt_sub)
        draw.text((self.RENDER_W - self.MARGIN, y + 60 + 6), date,
                  fill=self.TEXT_SECONDARY, font=fnt_date, anchor="ra")

        return y + bar_h + self.SECTION_GAP

    # ------------------------------------------------------------------
    # Hero panel — Law 1
    # ------------------------------------------------------------------

    def _draw_hero(self, draw, img, value, label, y, color=None):
        panel_h = 300
        px = self.MARGIN
        pw = self.INNER_W
        self._rounded_rect(draw, [px, y, px + pw, y + panel_h], 8, self.PANEL)

        cx = self.RENDER_W // 2
        cy = y + panel_h // 2 - 10
        self._draw_radial_depth(img, cx, cy)
        draw = ImageDraw.Draw(img)

        fnt_hero = _font("sans-light", self.HERO_SIZE)
        fnt_lbl = _font("mono", self.HERO_LABEL_SIZE)

        hero_color = color or self.ACCENT
        draw.text((cx, cy - 20), str(value), fill=hero_color,
                  font=fnt_hero, anchor="mm")
        draw.text((cx, cy + self.HERO_SIZE // 2 + 10), label,
                  fill=self.TEXT_SECONDARY, font=fnt_lbl, anchor="mm")

        return y + panel_h + self.SECTION_GAP, draw

    # ------------------------------------------------------------------
    # Secondary hero (smaller, below main hero)
    # ------------------------------------------------------------------

    def _draw_secondary_hero(self, draw, value, y, color=None):
        fnt = _font("sans-light", int(self.HERO_SIZE * 0.55))
        cx = self.RENDER_W // 2
        c = color or self.ACCENT
        draw.text((cx, y), str(value), fill=c, font=fnt, anchor="mm")
        return y + int(self.HERO_SIZE * 0.55) + 16

    # ------------------------------------------------------------------
    # Separator
    # ------------------------------------------------------------------

    def _draw_separator(self, draw, y):
        draw.line(
            [(self.MARGIN, y), (self.RENDER_W - self.MARGIN, y)],
            fill=self.SEPARATOR, width=1,
        )
        return y + self.SECTION_GAP // 2

    # ------------------------------------------------------------------
    # Section header
    # ------------------------------------------------------------------

    def _draw_section_header(self, draw, text, y, extra=None):
        fnt = _font("mono-bold", self.SECTION_SIZE)
        draw.text((self.MARGIN, y), text, fill=self.ACCENT_DIM, font=fnt)
        if extra:
            fnt_e = _font("mono", self.SECTION_SIZE)
            bbox = fnt.getbbox(text)
            tw = bbox[2] - bbox[0]
            draw.text((self.MARGIN + tw + 16, y), extra,
                      fill=self.TEXT_SECONDARY, font=fnt_e)
        return y + self.SECTION_SIZE + 20

    # ------------------------------------------------------------------
    # Dot leaders — Law 3
    # ------------------------------------------------------------------

    def _draw_dot_leaders(self, draw, x, y, w, key, value, value_color=None):
        fnt_key = _font("mono", self.DATA_SIZE)
        fnt_val = _font("mono-bold", self.DATA_SIZE)
        fnt_dot = _font("courier", self.DATA_SIZE)

        vc = value_color or self.TEXT_PRIMARY

        key_bbox = fnt_key.getbbox(key)
        key_w = key_bbox[2] - key_bbox[0]
        val_bbox = fnt_val.getbbox(str(value))
        val_w = val_bbox[2] - val_bbox[0]

        draw.text((x, y), key, fill=self.TEXT_SECONDARY, font=fnt_key)

        val_x = x + w - val_w
        draw.text((val_x, y), str(value), fill=vc, font=fnt_val)

        dot_bbox = fnt_dot.getbbox(".")
        dot_w = dot_bbox[2] - dot_bbox[0]
        gap = dot_w + 2
        dot_start = x + key_w + 12
        dot_end = val_x - 12
        dot_color = (50, 50, 50)
        dx = dot_start
        while dx < dot_end:
            draw.text((dx, y), ".", fill=dot_color, font=fnt_dot)
            dx += gap

        return y + self.ROW_H

    # ------------------------------------------------------------------
    # Signal bars — Law 4
    # ------------------------------------------------------------------

    def _draw_signal_bars(self, draw, signals, y):
        y = self._draw_section_header(draw, "SIGNALS", y,
                                      extra=f"(top {len(signals)})")
        bar_w = self.INNER_W
        fnt = _font("mono-bold", 36)
        fnt_val = _font("mono", 36)

        for sig in signals[:6]:
            name = sig.get("name", "?")
            strength = sig.get("strength", 0.0)

            bx = self.MARGIN
            by = y
            # Background bar
            self._rounded_rect(draw,
                               [bx, by, bx + bar_w, by + self.BAR_H],
                               self.BAR_RADIUS, (22, 22, 22))
            # Fill bar
            fill_w = max(self.BAR_RADIUS * 2, int(bar_w * strength))
            self._rounded_rect(draw,
                               [bx, by, bx + fill_w, by + self.BAR_H],
                               self.BAR_RADIUS, self.ACCENT)

            val_str = f"{strength:.2f}"
            val_bbox = fnt_val.getbbox(val_str)
            val_w = val_bbox[2] - val_bbox[0]

            if strength > 0.40:
                # Label inside bar
                draw.text((bx + 16, by - 2), name,
                          fill=(14, 14, 14), font=fnt)
                draw.text((bx + fill_w - val_w - 12, by - 2),
                          val_str, fill=(14, 14, 14), font=fnt_val)
            else:
                # Label outside
                draw.text((bx + fill_w + 10, by - 2), name,
                          fill=self.TEXT_SECONDARY, font=fnt)
                draw.text((bx + bar_w - val_w, by - 2),
                          val_str, fill=self.TEXT_SECONDARY, font=fnt_val)

            y += self.BAR_H + 12

        return y + 12

    # ------------------------------------------------------------------
    # Gate grid
    # ------------------------------------------------------------------

    def _draw_gate_grid(self, draw, gates, y):
        total = len(gates)
        passed = sum(1 for g in gates if g.get("passed", True))
        y = self._draw_section_header(draw, "ENTRY GATES",
                                      y, extra=f"{passed} / {total}")

        fnt = _font("mono", 48)
        cols = 3
        col_w = self.INNER_W // cols
        mark_size = 20  # half-size of the check/cross icon
        for i, gate in enumerate(gates):
            col = i % cols
            row = i // cols
            gx = self.MARGIN + col * col_w
            gy = y + row * 64

            ok = gate.get("passed", True)
            mc = self.ACCENT if ok else self.TEXT_NEGATIVE
            # Center the mark vertically with the text
            mx = gx + mark_size
            my = gy + 24
            if ok:
                # Checkmark: short line down-right, then longer line up-right
                draw.line([(mx - 12, my), (mx - 2, my + 10)],
                          fill=mc, width=4)
                draw.line([(mx - 2, my + 10), (mx + 14, my - 8)],
                          fill=mc, width=4)
            else:
                # X mark: two crossing lines
                draw.line([(mx - 10, my - 10), (mx + 10, my + 10)],
                          fill=mc, width=4)
                draw.line([(mx - 10, my + 10), (mx + 10, my - 10)],
                          fill=mc, width=4)

            name = gate.get("name", "?")
            draw.text((gx + 48, gy), name,
                      fill=self.TEXT_SECONDARY, font=fnt)

        rows = math.ceil(len(gates) / cols)
        return y + rows * 64 + 20

    # ------------------------------------------------------------------
    # Record bar (win/loss segment bar)
    # ------------------------------------------------------------------

    def _draw_record_bar(self, draw, wins, losses, win_rate, net_total, y):
        y = self._draw_section_header(draw, "SESSION RECORD", y)

        total = wins + losses
        if total == 0:
            fnt = _font("mono", self.DATA_SIZE)
            draw.text((self.RENDER_W // 2, y + 20), "No trades",
                      fill=self.TEXT_SECONDARY, font=fnt, anchor="mm")
            return y + 60

        # Win rate large centered above bar
        fnt_wr = _font("sans-light", 96)
        cx = self.RENDER_W // 2
        wr_str = f"{win_rate:.0f}%"
        draw.text((cx, y + 10), wr_str, fill=self.ACCENT,
                  font=fnt_wr, anchor="mm")
        y += 80

        # Segment bar
        bar_x = self.MARGIN
        bar_w = self.INNER_W
        bar_h = 44
        seg_gap = 3
        total_gap = seg_gap * (total - 1)
        seg_w = (bar_w - total_gap) / total if total > 0 else bar_w

        sx = bar_x
        for i in range(total):
            c = self.TEXT_POSITIVE if i < wins else self.TEXT_NEGATIVE
            self._rounded_rect(draw,
                               [int(sx), y, int(sx + seg_w), y + bar_h],
                               4, c)
            sx += seg_w + seg_gap

        y += bar_h + 8

        # Labels below
        fnt_sm = _font("mono", 44)
        draw.text((self.MARGIN, y), f"{wins} wins",
                  fill=self.TEXT_POSITIVE, font=fnt_sm)
        loss_str = f"{losses} loss{'es' if losses != 1 else ''}"
        draw.text((self.RENDER_W - self.MARGIN, y), loss_str,
                  fill=self.TEXT_NEGATIVE, font=fnt_sm, anchor="ra")
        y += 52

        # Net all time centered below
        fnt_net = _font("mono-bold", self.DATA_SIZE)
        sign = "+" if net_total >= 0 else ""
        nc = self.TEXT_POSITIVE if net_total >= 0 else self.TEXT_NEGATIVE
        draw.text((cx, y), f"Net all time:  {sign}${abs(net_total):.2f}",
                  fill=nc, font=fnt_net, anchor="mm")
        y += 72

        return y

    # ------------------------------------------------------------------
    # Whale scale visualization
    # ------------------------------------------------------------------

    def _draw_whale_scale(self, draw, magnitude, y):
        y = self._draw_section_header(draw, "MAGNITUDE", y)

        zones = ["LOW", "MODERATE", "HIGH", "EXTREME"]
        active_idx = zones.index(magnitude) if magnitude in zones else 0
        zone_w = self.INNER_W // len(zones)
        fnt = _font("mono", 40)
        fnt_sm = _font("mono", 32)

        bar_y = y + 48
        bar_h = 32

        for i, z in enumerate(zones):
            zx = self.MARGIN + i * zone_w
            c = self.ACCENT if i == active_idx else (30, 30, 30)
            self._rounded_rect(draw,
                               [zx + 4, bar_y, zx + zone_w - 4, bar_y + bar_h],
                               6, c)
            # Zone label above
            lc = self.TEXT_PRIMARY if i == active_idx else self.TEXT_SECONDARY
            draw.text((zx + zone_w // 2, bar_y - 8), z,
                      fill=lc, font=fnt, anchor="mb")

        # Arrow below active zone
        ax = self.MARGIN + active_idx * zone_w + zone_w // 2
        ay = bar_y + bar_h + 8
        draw.polygon(
            [(ax - 8, ay + 12), (ax + 8, ay + 12), (ax, ay)],
            fill=self.ACCENT,
        )
        draw.text((ax, ay + 20), "THIS ALERT",
                  fill=self.TEXT_SECONDARY, font=fnt_sm, anchor="ma")

        return ay + 56

    # ------------------------------------------------------------------
    # Sparkline — cumulative P/L
    # ------------------------------------------------------------------

    def _draw_sparkline(self, draw, values, x, y, w, h):
        if not values or len(values) < 2:
            return y + h

        min_v = min(values)
        max_v = max(values)
        span = max_v - min_v if max_v != min_v else 1

        def px(i, v):
            fx = x + (i / (len(values) - 1)) * w
            fy = y + h - ((v - min_v) / span) * (h - 24)
            return int(fx), int(fy)

        # Determine color
        is_positive = values[-1] >= values[0]
        line_color = self.ACCENT if is_positive else self.TEXT_NEGATIVE

        # Zero baseline
        if min_v < 0 < max_v:
            zy = y + h - ((0 - min_v) / span) * (h - 24)
            draw.line([(x, int(zy)), (x + w, int(zy))],
                      fill=self.SEPARATOR, width=1)

        # Area fill (draw as polygon)
        points = [px(i, v) for i, v in enumerate(values)]
        bottom_y = y + h
        fill_poly = points + [(points[-1][0], bottom_y), (points[0][0], bottom_y)]
        fill_color = (line_color[0], line_color[1], line_color[2])
        # Draw semi-transparent fill by blending
        fill_img = Image.new("RGBA", (w + 1, h + 1), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fill_img)
        shifted = [(p[0] - x, p[1] - y) for p in fill_poly]
        fd.polygon(shifted, fill=(fill_color[0], fill_color[1], fill_color[2], 30))
        # Paste onto the draw's image
        target = draw._image
        overlay = Image.new("RGBA", target.size, (0, 0, 0, 0))
        overlay.paste(fill_img, (x, y))
        composite = Image.alpha_composite(target.convert("RGBA"), overlay)
        target.paste(composite.convert("RGB"))

        # Redraw handle
        draw = ImageDraw.Draw(target)

        # Line
        for i in range(len(points) - 1):
            draw.line([points[i], points[i + 1]], fill=line_color, width=3)

        # Dots at each trade
        for pt in points:
            draw.ellipse(
                [pt[0] - 6, pt[1] - 6, pt[0] + 6, pt[1] + 6],
                fill=line_color,
            )

        # Start / end labels
        fnt = _font("mono", 40)
        draw.text((x, y + h + 4), f"${values[0]:.2f}",
                  fill=self.TEXT_SECONDARY, font=fnt)
        sign = "+" if values[-1] >= 0 else ""
        draw.text((x + w, y + h + 4), f"{sign}${values[-1]:.2f}",
                  fill=line_color, font=fnt, anchor="ra")

        return y + h + 40, draw

    # ------------------------------------------------------------------
    # Trade bars — vertical bars for each trade
    # ------------------------------------------------------------------

    def _draw_trade_bars(self, draw, trades, x, y, w, h):
        if not trades:
            return y + h

        n = len(trades)
        gap = 8
        bar_w = max(12, (w - gap * (n - 1)) // n)
        max_abs = max(abs(t.get("net", 0)) for t in trades) or 1

        zero_y = y + h // 2

        # Zero line
        draw.line([(x, zero_y), (x + w, zero_y)],
                  fill=self.SEPARATOR, width=1)

        cumulative = []
        running = 0
        cum_points = []

        for i, t in enumerate(trades):
            net = t.get("net", 0)
            running += net
            cumulative.append(running)

            bx = x + i * (bar_w + gap)
            bar_h_px = int((abs(net) / max_abs) * (h // 2 - 10))
            c = self.TEXT_POSITIVE if net >= 0 else self.TEXT_NEGATIVE

            if net >= 0:
                self._rounded_rect(draw,
                                   [bx, zero_y - bar_h_px, bx + bar_w, zero_y],
                                   4, c)
            else:
                self._rounded_rect(draw,
                                   [bx, zero_y, bx + bar_w, zero_y + bar_h_px],
                                   4, c)

            cum_points.append((bx + bar_w // 2, zero_y - int(
                (running / (max(abs(r) for r in cumulative) or 1))
                * (h // 2 - 20)
            )))

        # Cumulative P/L overlay line
        for i in range(len(cum_points) - 1):
            draw.line([cum_points[i], cum_points[i + 1]],
                      fill=self.ACCENT, width=3)

        return y + h + 16

    # ------------------------------------------------------------------
    # Voice line — Law 5
    # ------------------------------------------------------------------

    def _draw_voice_line(self, draw, text, y):
        draw.line(
            [(self.MARGIN, y), (self.RENDER_W - self.MARGIN, y)],
            fill=self.ACCENT_DIM, width=1,
        )
        y += 32

        fnt = _font("sans", self.VOICE_SIZE)
        # Wrap text
        lines = self._wrap_text(text, fnt, self.INNER_W)
        for line in lines:
            draw.text((self.MARGIN, y), line,
                      fill=self.TEXT_VOICE, font=fnt)
            y += self.VOICE_SIZE + 8
        y += 24

        draw.line(
            [(self.MARGIN, y), (self.RENDER_W - self.MARGIN, y)],
            fill=self.ACCENT_DIM, width=1,
        )
        return y + 32

    # ------------------------------------------------------------------
    # Personal note panel
    # ------------------------------------------------------------------

    def _draw_personal_note(self, draw, img, y):
        note_lines = [
            "Thanks for being here today.",
            "",
            "I got scammed twice by signal services.",
            "Paid real money for calls that were",
            "fabricated or just made up.",
            "So I built an honest one.",
            "",
            "Whatever it sees, you see.",
            "Good days and bad ones.",
            "",
            "\u2014 GoldenEye Intelligence",
        ]

        fnt = _font("sans", self.VOICE_SIZE)
        line_h = self.VOICE_SIZE + 10
        text_h = len(note_lines) * line_h
        pad = 48
        panel_h = text_h + pad * 2

        px = self.MARGIN
        pw = self.INNER_W

        # Panel background
        self._rounded_rect(draw,
                           [px, y, px + pw, y + panel_h],
                           8, self.PANEL_WARM)

        # Gold left border
        draw.rectangle([px, y, px + 4, y + panel_h], fill=self.ACCENT)

        ty = y + pad
        for line in note_lines:
            if line:
                draw.text((px + 40, ty), line,
                          fill=self.TEXT_VOICE, font=fnt)
            ty += line_h

        return y + panel_h + self.SECTION_GAP

    # ------------------------------------------------------------------
    # Footer — every card
    # ------------------------------------------------------------------

    def _draw_footer(self, draw, h, channel="Fleet Intelligence",
                     stats="19 signals \u00b7 live \u00b7 Kraken"):
        y = h - self.MARGIN - 40
        draw.line(
            [(self.MARGIN, y - 32), (self.RENDER_W - self.MARGIN, y - 32)],
            fill=self.SEPARATOR, width=1,
        )

        fnt_wm = _font("sans-bold", self.WORDMARK_SIZE)
        fnt_dim = _font("sans", self.WORDMARK_SIZE)
        fnt_right = _font("mono", self.FOOTER_SIZE)

        # GOLDENEYE  diamond  INTELLIGENCE
        gx = self.MARGIN
        draw.text((gx, y), "GOLDENEYE", fill=self.ACCENT, font=fnt_wm)
        ge_bbox = fnt_wm.getbbox("GOLDENEYE")
        ge_w = ge_bbox[2] - ge_bbox[0]

        # Draw diamond manually — four lines forming a diamond shape
        dia_sz = 14  # half-size of diamond
        dia_cx = gx + ge_w + 20 + dia_sz
        dia_cy = y + self.WORDMARK_SIZE // 2
        draw.polygon(
            [(dia_cx, dia_cy - dia_sz),      # top
             (dia_cx + dia_sz, dia_cy),       # right
             (dia_cx, dia_cy + dia_sz),       # bottom
             (dia_cx - dia_sz, dia_cy)],      # left
            fill=self.ACCENT_DIM,
        )
        dia_w = dia_sz * 2

        draw.text((gx + ge_w + 20 + dia_w + 20, y + 8),
                  "INTELLIGENCE", fill=self.TEXT_SECONDARY, font=fnt_dim)

        # Right side
        right_text = f"{channel}  \u00b7  {stats}"
        draw.text((self.RENDER_W - self.MARGIN, y + 12), right_text,
                  fill=self.TEXT_SECONDARY, font=fnt_right, anchor="ra")

    # ------------------------------------------------------------------
    # Best / Worst trade block
    # ------------------------------------------------------------------

    def _draw_best_worst(self, draw, best, worst, y):
        fnt_lbl = _font("mono-bold", self.SECTION_SIZE)
        fnt_data = _font("mono", 48)
        fnt_why = _font("sans", 44)

        for tag, trade, tc in [("BEST TRADE", best, self.TEXT_POSITIVE),
                               ("WORST TRADE", worst, self.TEXT_NEGATIVE)]:
            if trade is None:
                continue
            draw.text((self.MARGIN, y), tag, fill=self.ACCENT_DIM, font=fnt_lbl)
            y += 44

            d_glyph = "\u25b2" if trade.get("direction", "LONG") == "LONG" else "\u25bc"
            info = (f"Pair ......... {trade.get('pair', '?')}  "
                    f"{d_glyph} {trade.get('direction', '?')}    "
                    f"Net: {'+' if trade.get('net',0)>=0 else ''}"
                    f"${abs(trade.get('net',0)):.2f}")
            draw.text((self.MARGIN, y), info, fill=tc, font=fnt_data)
            y += 40

            why = trade.get("why", "")
            if why:
                lines = self._wrap_text(why, fnt_why, self.INNER_W - 20)
                for wl in lines:
                    draw.text((self.MARGIN, y), wl,
                              fill=self.TEXT_SECONDARY, font=fnt_why)
                    y += 40

            y += 16

            # Separator between best and worst
            if tag == "BEST TRADE" and worst:
                draw.line(
                    [(self.MARGIN, y), (self.RENDER_W - self.MARGIN, y)],
                    fill=self.SEPARATOR, width=1,
                )
                y += 24

        return y + 8

    # ------------------------------------------------------------------
    # Fleet status strip
    # ------------------------------------------------------------------

    def _draw_fleet_strip(self, draw, regime, deployed_pct, y):
        draw.line(
            [(self.MARGIN, y), (self.RENDER_W - self.MARGIN, y)],
            fill=self.SEPARATOR, width=1,
        )
        y += 20
        fnt = _font("mono", 48)
        strip = (f"Regime: {regime}  \u00b7  "
                 f"Deployed: {deployed_pct:.0f}%  \u00b7  "
                 f"Oracle: WATCHING")
        draw.text((self.RENDER_W // 2, y), strip,
                  fill=self.TEXT_SECONDARY, font=fnt, anchor="mm")
        y += 52
        draw.line(
            [(self.MARGIN, y), (self.RENDER_W - self.MARGIN, y)],
            fill=self.SEPARATOR, width=1,
        )
        return y + self.SECTION_GAP // 2

    # ------------------------------------------------------------------
    # Text wrapping utility
    # ------------------------------------------------------------------

    def _wrap_text(self, text, font, max_w):
        words = text.split()
        lines = []
        current = ""
        for word in words:
            test = f"{current} {word}".strip()
            bbox = font.getbbox(test)
            tw = bbox[2] - bbox[0]
            if tw <= max_w:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or [""]

    # ==================================================================
    # CARD 1 — POSITION OPENED
    # ==================================================================

    def render_position_opened(self, data):
        conviction = data.get("conviction", 0)
        conv_pct = f"{int(conviction * 100)}%"
        voice = data.get("voice", "")

        img, draw = self._new_canvas()

        y = self._draw_header(draw, img, "POSITION OPENED",
                              f"Stalker \u00b7 {data.get('pair', '?')}",
                              data.get("timestamp", ""),
                              data.get("date", ""))

        y, draw = self._draw_hero(draw, img, conv_pct, "CONVICTION", y)

        # Data block
        direction = data.get("direction", "LONG")
        d_glyph = "\u25b2" if direction == "LONG" else "\u25bc"
        d_color = self.TEXT_POSITIVE if direction == "LONG" else self.TEXT_NEGATIVE

        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Direction", f"{d_glyph} {direction}",
                                   d_color)
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Entry", f"{data.get('entry', 0):.4f}")
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Stop", f"{data.get('stop', 0):.4f}",
                                   self.TEXT_NEGATIVE)
        for i, t in enumerate(data.get("targets", [])):
            y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                       f"Target {i+1}", f"{t:.4f}",
                                       self.ACCENT)
        # Adaptive decimals — large-unit tickers (DOGE/PEPE) show whole numbers,
        # fractional-unit tickers (BTC/ETH) need more precision or they render as 0.00
        _sz = float(data.get('size', 0))
        _entry = float(data.get('entry', 0))
        _sz_usd = _sz * _entry
        if _sz >= 1000:   _sz_str = f"{_sz:,.0f}"
        elif _sz >= 1:    _sz_str = f"{_sz:,.4f}"
        elif _sz >= 0.01: _sz_str = f"{_sz:.6f}"
        else:             _sz_str = f"{_sz:.8f}"
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Size", f"{_sz_str} (${_sz_usd:.2f})")

        regime = data.get("regime", "?")
        regime_colors = {
            "BULL": self.TEXT_POSITIVE,
            "BEAR": self.TEXT_NEGATIVE,
            "RANGE": self.TEXT_WARNING,
            "CHOP": self.TEXT_SECONDARY,
        }
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Regime", regime,
                                   regime_colors.get(regime, self.TEXT_PRIMARY))

        y += self.SECTION_GAP // 2

        # Signal bars
        y = self._draw_signal_bars(draw, data.get("signals", []), y)

        # Gate grid
        gate_names = ["Correlation", "Quality Floor", "Confluence",
                      "Win Rate", "Regime", "Position", "Sentiment"]
        gates_passed = data.get("gates_passed", 7)
        gates = [{"name": g, "passed": i < gates_passed}
                 for i, g in enumerate(gate_names)]
        y = self._draw_gate_grid(draw, gates, y)

        # Voice
        y = self._draw_voice_line(draw, voice, y)

        return self._crop_and_finalize(img, y)

    # ==================================================================
    # CARD 2/3 — POSITION CLOSED (win or loss)
    # ==================================================================

    def render_position_closed(self, data):
        net = data.get("net_pnl", 0)
        r_mult = data.get("r_multiple", 0)
        gross = data.get("gross_pnl", 0)
        net_pct = data.get("net_pct", 0)
        gross_pct = data.get("gross_pct", 0)

        # Win/loss determined by R-multiple, not net dollars.
        # A TP1 hit with +0.4R is a WIN even if fees make net negative.
        is_win = r_mult > 0
        tp_hit = data.get("tp_hit", 0)
        if tp_hit > 0:
            is_win = True  # any TP hit is a win regardless of fees

        # Hero: R-multiple (the real measure of trade quality)
        r_sign = "+" if r_mult >= 0 else ""
        hero_val = f"{r_sign}{r_mult:.1f}R"
        hero_color = self.TEXT_POSITIVE if is_win else self.TEXT_NEGATIVE
        card_label = "POSITION CLOSED \u00b7 WIN" if is_win else "POSITION CLOSED \u00b7 LOSS"

        voice = data.get("voice", "")
        wins = data.get("wins", 0)
        losses = data.get("losses", 0)

        img, draw = self._new_canvas()

        y = self._draw_header(draw, img, card_label,
                              f"Vanguard \u00b7 {data.get('pair', '?')}",
                              data.get("timestamp", ""),
                              data.get("date", ""))

        y, draw = self._draw_hero(draw, img, hero_val, "R-MULTIPLE", y, hero_color)

        # Secondary hero: net % P&L
        pct_sign = "+" if net_pct >= 0 else ""
        sec_color = self.TEXT_POSITIVE if net_pct >= 0 else self.TEXT_NEGATIVE
        y = self._draw_secondary_hero(draw, f"{pct_sign}{net_pct:.1f}%", y, sec_color)

        y += 8

        # Data block
        direction = data.get("direction", "LONG")
        d_glyph = "\u25b2" if direction == "LONG" else "\u25bc"
        d_color = self.TEXT_POSITIVE if direction == "LONG" else self.TEXT_NEGATIVE

        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Direction", f"{d_glyph} {direction}",
                                   d_color)
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Entry", f"{data.get('entry', 0):.4f}")
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Exit", f"{data.get('exit', 0):.4f}",
                                   self.ACCENT)

        # Gross P/L as % with $ in parens
        gross_sign = "+" if gross >= 0 else "-"
        gross_pct_sign = "+" if gross_pct >= 0 else "-"
        gross_color = self.TEXT_POSITIVE if gross >= 0 else self.TEXT_NEGATIVE
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Gross P/L",
                                   f"{gross_pct_sign}{abs(gross_pct):.1f}% ({gross_sign}${abs(gross):.2f})",
                                   gross_color)

        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Fees", f"-${data.get('fees', 0):.2f}",
                                   self.TEXT_NEGATIVE)

        # Net P/L as % with $ in parens — avoid -$0.00
        net_display = abs(net)
        if net_display < 0.005:
            net_display = 0.0
        net_color = self.TEXT_POSITIVE if net >= 0 else self.TEXT_NEGATIVE
        net_sign = "+" if net >= 0 else "-"
        net_pct_sign = "+" if net_pct >= 0 else "-"
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Net P/L",
                                   f"{net_pct_sign}{abs(net_pct):.1f}% ({net_sign}${net_display:.2f})",
                                   net_color)

        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Duration", data.get("duration", "?"))
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Exit", data.get("exit_reason", "?"))

        y += self.SECTION_GAP // 2

        # Record bar
        y = self._draw_record_bar(draw, wins, losses,
                                  data.get("win_rate", 0),
                                  data.get("total_net", 0), y)

        # Voice
        y = self._draw_voice_line(draw, voice, y)

        return self._crop_and_finalize(img, y)

    # ==================================================================
    # CARD 4 — WHALE ALERT
    # ==================================================================

    def render_whale_alert(self, data):
        volume = data.get("volume", 0)
        hero_val = f"${volume:,.0f}"
        magnitude = data.get("magnitude", "HIGH")
        voice = data.get("voice", "")

        img, draw = self._new_canvas()

        card_type = ("\u25c6 WHALE ALERT" if magnitude == "EXTREME"
                     else "WHALE ALERT")
        y = self._draw_header(draw, img, card_type,
                              "Leviathan \u00b7 Detection",
                              data.get("timestamp", ""),
                              data.get("date", ""))

        y, draw = self._draw_hero(draw, img, hero_val, "VOLUME", y)

        # Data block
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Pair", data.get("pair", "?"))
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Volume", hero_val, self.ACCENT)
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Magnitude", magnitude,
                                   self.TEXT_WARNING)
        side = data.get("side") or "Unknown"
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Side", side)
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Source", "Leviathan")

        y += self.SECTION_GAP // 2

        # Whale scale
        y = self._draw_whale_scale(draw, magnitude, y)

        # Fleet strip
        y = self._draw_fleet_strip(draw,
                                   data.get("regime", "?"),
                                   data.get("deployed_pct", 0), y)

        # Voice
        y = self._draw_voice_line(draw, voice, y)

        return self._crop_and_finalize(img, y)

    # ==================================================================
    # CARD 5/6 — END OF DAY
    # ==================================================================

    def render_end_of_day(self, data, tier="free"):
        trades = data.get("trades", [])
        is_flat = len(trades) == 0
        wins = data.get("wins", 0)
        losses = data.get("losses", 0)
        win_rate = data.get("win_rate", 0)
        net_pnl = data.get("net_pnl", 0)

        if is_flat:
            return self._render_eod_flat(data)

        best = data.get("best_trade")
        worst = data.get("worst_trade")

        img, draw = self._new_canvas()

        y = self._draw_header(draw, img, "END OF DAY",
                              "GoldenEye Intelligence",
                              "",
                              data.get("date", ""))

        y, draw = self._draw_hero(draw, img, f"{win_rate:.0f}%",
                                  "WIN RATE", y)

        # Stats block
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Trades", str(len(trades)))
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Wins", str(wins), self.TEXT_POSITIVE)
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Losses", str(losses), self.TEXT_NEGATIVE)

        gross = data.get("gross_pnl", 0)
        gross_sign = "+" if gross >= 0 else "-"
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Gross P/L",
                                   f"{gross_sign}${abs(gross):.2f}")
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Fees", f"-${data.get('fees', 0):.2f}",
                                   self.TEXT_NEGATIVE)

        net_c = self.TEXT_POSITIVE if net_pnl >= 0 else self.TEXT_NEGATIVE
        net_sign = "+" if net_pnl >= 0 else "-"
        y = self._draw_dot_leaders(draw, self.MARGIN, y, self.INNER_W,
                                   "Net P/L",
                                   f"{net_sign}${abs(net_pnl):.2f}", net_c)

        y += self.SECTION_GAP

        # Sparkline — cumulative P/L
        y = self._draw_section_header(draw, "CUMULATIVE P/L", y)
        cum = []
        running = 0
        for t in trades:
            running += t.get("net", 0)
            cum.append(running)
        cum.insert(0, 0)
        y, draw = self._draw_sparkline(draw, cum,
                                       self.MARGIN, y,
                                       self.INNER_W, 160)

        y += self.SECTION_GAP

        # Trade bars
        y = self._draw_section_header(draw, "TRADE BREAKDOWN", y)
        y = self._draw_trade_bars(draw, trades,
                                  self.MARGIN, y,
                                  self.INNER_W, 240)

        y += self.SECTION_GAP // 2

        # Record bar
        y = self._draw_record_bar(draw, wins, losses, win_rate,
                                  net_pnl, y)

        y += self.SECTION_GAP // 2

        # Best/worst
        y = self._draw_best_worst(draw, best, worst, y)

        y += self.SECTION_GAP // 2

        # Personal note
        y = self._draw_personal_note(draw, img, y)

        return self._crop_and_finalize(img, y)

    def _render_eod_flat(self, data):
        flat_lines = [
            "THE ENGINE RAN ALL DAY",
            "",
            "27 signals evaluated on every setup.",
            "7 gates checked on every entry.",
            "Nothing qualified.",
            "",
            "Cash held.",
        ]
        philosophy = [
            "A bot that forces trades to avoid",
            "looking idle is a bot that loses money.",
            "",
            "Patience is part of the edge.",
        ]

        img, draw = self._new_canvas()

        y = self._draw_header(draw, img, "END OF DAY",
                              "GoldenEye Intelligence",
                              "",
                              data.get("date", ""))

        y, draw = self._draw_hero(draw, img, "0",
                                  "TRADES", y, self.TEXT_SECONDARY)

        # Center panel
        fnt = _font("sans", self.VOICE_SIZE)
        fnt_title = _font("sans-bold", 76)

        draw.text((self.RENDER_W // 2, y + 20), flat_lines[0],
                  fill=self.ACCENT, font=fnt_title, anchor="mm")
        y += 80

        for line in flat_lines[1:]:
            if line:
                draw.text((self.RENDER_W // 2, y), line,
                          fill=self.TEXT_VOICE, font=fnt, anchor="mm")
            y += self.VOICE_SIZE + 10
        y += 16

        # Separator
        draw.line(
            [(self.MARGIN + 200, y),
             (self.RENDER_W - self.MARGIN - 200, y)],
            fill=self.SEPARATOR, width=1,
        )
        y += 32

        for line in philosophy:
            if line:
                draw.text((self.RENDER_W // 2, y), line,
                          fill=self.TEXT_VOICE, font=fnt, anchor="mm")
            y += self.VOICE_SIZE + 10

        y += self.SECTION_GAP

        # Personal note
        y = self._draw_personal_note(draw, img, y)

        return self._crop_and_finalize(img, y)

    # ==================================================================
    # Copy value extraction — one list item per sendMessage
    # ==================================================================

    @staticmethod
    def copy_values_opened(data):
        """Return list of (label, value) for POSITION OPENED copy messages."""
        vals = []
        if data.get("entry") is not None:
            vals.append(("Entry", f"{data['entry']:.4f}"))
        if data.get("stop") is not None:
            vals.append(("Stop", f"{data['stop']:.4f}"))
        for i, t in enumerate(data.get("targets", []), 1):
            if t is not None:
                vals.append((f"Target {i}", f"{t:.4f}"))
        if data.get("size") is not None:
            # Adaptive decimals — matches render_position_opened size formatting
            _sz = float(data['size'])
            if _sz >= 1000:   _sz_str = f"{_sz:,.0f}"
            elif _sz >= 1:    _sz_str = f"{_sz:,.4f}"
            elif _sz >= 0.01: _sz_str = f"{_sz:.6f}"
            else:             _sz_str = f"{_sz:.8f}"
            vals.append(("Size", _sz_str))
        return vals

    @staticmethod
    def copy_values_closed(data):
        """POSITION CLOSED copy bubbles removed — Exit, Net P/L, and R-Multiple
        are already on the PNG card image, so the tap-to-copy messages were
        duplicating visible information. send_card_telegram handles an empty
        list gracefully (skips the sendMessage loop)."""
        return []

    @staticmethod
    def copy_values_whale(data):
        """Return list of (label, value) for WHALE ALERT copy messages."""
        vals = []
        vol = data.get("volume")
        if vol is not None:
            vals.append(("Volume", f"${vol:,.0f}"))
        return vals

    @staticmethod
    def copy_values_eod(data):
        """Return list of (label, value) for END OF DAY copy messages."""
        vals = []
        net = data.get("net_pnl")
        if net is not None and net != 0:
            sign = "+" if net >= 0 else "-"
            vals.append(("Net P/L", f"{sign}${abs(net):.2f}"))
        wr = data.get("win_rate")
        if wr is not None and data.get("trades"):
            vals.append(("Win Rate", f"{wr:.0f}%"))
        return vals


def send_card_telegram(token, chat_id, png_bytes, copy_values,
                       caption=""):
    """Send a PNG card via sendPhoto, then individual copy-value messages.

    Each copy value becomes its own Telegram message with a backtick code
    span that renders as a native tap-to-copy button on mobile.

    Args:
        token: Telegram bot token
        chat_id: Telegram chat ID
        png_bytes: rendered PNG as bytes
        copy_values: list of (label, value) tuples from copy_values_*()
        caption: optional photo caption
    """
    import urllib.request
    import json as _json
    import time
    import logging

    log = logging.getLogger("oracle.telegram")

    def _post_json(url, payload):
        data = _json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status == 200
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:
                    try:
                        body = _json.loads(e.read().decode("utf-8"))
                        wait = body.get("parameters", {}).get("retry_after", 5)
                    except Exception:
                        wait = 5
                    log.warning("Telegram 429, retrying after %ds", wait)
                    time.sleep(wait)
                    continue
                log.warning("Telegram HTTP error %d", e.code)
                return False
            except Exception as e:
                log.warning("Telegram send error: %s", e)
                return False
        return False

    # 1. Send the PNG via sendPhoto (multipart)
    boundary = "----OracleBoundary"
    parts = []
    parts.append(f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
                 f"{chat_id}\r\n")
    if caption:
        cap = caption[:1024]
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="caption"\r\n\r\n'
                     f"{cap}\r\n")
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="parse_mode"\r\n\r\n'
                     f"HTML\r\n")
    photo_header = (f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="photo"; '
                    f'filename="signal.png"\r\n'
                    f"Content-Type: image/png\r\n\r\n")
    photo_footer = f"\r\n--{boundary}--\r\n"

    body = b""
    for p in parts:
        body += p.encode("utf-8")
    body += photo_header.encode("utf-8")
    body += png_bytes
    body += photo_footer.encode("utf-8")

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    photo_ok = False
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            photo_ok = resp.status == 200
    except Exception as e:
        log.warning("sendPhoto failed: %s", e)

    if not photo_ok:
        log.error("Failed to send card image to Telegram")
        return False

    # 2. Send all copy values in one consolidated sendMessage — prevents
    # interleaving when multiple cards fire back-to-back, keeps per-value
    # backtick code spans so each is still tap-to-copy on mobile.
    msg_url = f"https://api.telegram.org/bot{token}/sendMessage"
    lines = []
    for label, value in copy_values:
        if not value or value == "--":
            continue
        lines.append(f"{label}: `{value}`")

    if lines:
        text = "\n".join(lines)
        ok = _post_json(msg_url, {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        })
        log.info("Card sent + copy block delivered (%d values, ok=%s)", len(lines), ok)
    else:
        log.info("Card sent (no copy values)")
    return True
