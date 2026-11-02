#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HearThis Player – GTK4 + GStreamer (flump3dec + local cache)
- cover „Teraz gra” u góry po prawej + info streamu
- mini-ikona i pasek postępu przy wyszukiwaniu
- undo/redo historii nawigacji (poprzednie / następne wyniki)
"""

import sys
import math
import json
import hashlib
import threading
from pathlib import Path
from copy import deepcopy

import requests
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("GdkPixbuf", "2.0")

# pycairo wymagane do DrawingArea (winyl/CD) – foreign struct cairo.Context
HAS_CAIRO = False
try:
    import cairo  # noqa: F401  # pycairo
    gi.require_foreign("cairo")
    HAS_CAIRO = True
except Exception as _cairo_err:
    print("UWAGA: brak pycairo – okładka vinyl/cd bez rysowania Cairo:", _cairo_err)
    print("  Zainstaluj:  sudo apt install python3-cairo   lub  pip install pycairo")

from gi.repository import Gtk, Gio, GLib, Gdk, Gst, GObject, GdkPixbuf

Gst.init(None)

API_BASE = "https://api-v2.hearthis.at"
CACHE_DIR = Path.home() / ".cache" / "hearthis_player"
AUDIO_CACHE = CACHE_DIR / "audio"
CONFIG_FILE = CACHE_DIR / "config.json"

for d in (CACHE_DIR, AUDIO_CACHE):
    d.mkdir(parents=True, exist_ok=True)


# ─────────────────────────── Config ───────────────────────────
class Config:
    def __init__(self):
        self.username = None
        self.cache_enabled = False  # stream domyślnie; cache opcjonalnie
        self.cover_style = "vinyl"  # normal | vinyl | cd
        self.spin_enabled = True
        self.load()

    def load(self):
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                self.username = data.get("username")
                self.cache_enabled = bool(data.get("cache_enabled", False))
                self.cover_style = data.get("cover_style") or "vinyl"
                if self.cover_style not in ("normal", "vinyl", "cd"):
                    self.cover_style = "vinyl"
                self.spin_enabled = bool(data.get("spin_enabled", True))
            except Exception:
                pass

    def save(self):
        CONFIG_FILE.write_text(
            json.dumps({
                "username": self.username,
                "cache_enabled": self.cache_enabled,
                "cover_style": self.cover_style,
                "spin_enabled": self.spin_enabled,
            }, indent=2),
            encoding="utf-8",
        )

    def clear(self):
        self.username = None
        # cache_enabled zostaje
        self.save()

    @property
    def has_user(self):
        return bool(self.username)


# ─────────────────────────── Helpers ───────────────────────────
def fetch_json(url, params=None, timeout=20):
    params = dict(params or {})
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"API error {url}: {e}")
        return None


def track_id_key(track: dict) -> str:
    return str(
        track.get("id")
        or track.get("permalink")
        or hashlib.md5((track.get("stream_url") or "").encode()).hexdigest()
    )


def track_cache_path(track: dict) -> Path:
    return AUDIO_CACHE / f"{track_id_key(track)}.mp3"


def track_meta_path(track: dict) -> Path:
    return AUDIO_CACHE / f"{track_id_key(track)}.json"


def track_cover_path(track: dict) -> Path:
    return AUDIO_CACHE / f"{track_id_key(track)}.cover.jpg"


def stream_url_of(track: dict) -> str | None:
    return track.get("stream_url") or track.get("download_url") or track.get("preview_url")


def save_track_meta(track: dict):
    """Zapis tytułu, artysty, okładki do cache (gdy włączony)."""
    meta = {
        "id": track.get("id"),
        "title": track.get("title"),
        "duration": track.get("duration"),
        "genre": track.get("genre") or track.get("category"),
        "user": (track.get("user") or {}).get("username"),
        "permalink": (track.get("user") or {}).get("permalink"),
        "artwork_url": track.get("artwork_url") or track.get("thumb") or track.get("artwork_url_retina"),
        "stream_url": stream_url_of(track),
    }
    try:
        track_meta_path(track).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print("meta save error:", e)
    art = meta.get("artwork_url")
    if art and str(art).startswith("http"):
        cpath = track_cover_path(track)
        if not cpath.exists():
            try:
                r = requests.get(art, timeout=15)
                r.raise_for_status()
                cpath.write_bytes(r.content)
            except Exception as e:
                print("cover save error:", e)


def download_track(track: dict, progress_cb=None) -> Path | None:
    path = track_cache_path(track)
    if path.exists() and path.stat().st_size > 1024:
        if progress_cb:
            progress_cb(1.0)
        return path
    url = stream_url_of(track)
    if not url:
        return None
    try:
        with requests.get(url, stream=True, timeout=90) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
                        if progress_cb and total:
                            progress_cb(min(1.0, done / total))
                        elif progress_cb and not total:
                            progress_cb(-1)
        if progress_cb:
            progress_cb(1.0)
        save_track_meta(track)
        return path
    except Exception as e:
        print("Download failed:", e)
        path.unlink(missing_ok=True)
        return None


def texture_from_bytes(b: bytes, max_side: int | None = None):
    if not b:
        return None
    try:
        loader = GdkPixbuf.PixbufLoader.new()
        loader.write(b)
        loader.close()
        pix = loader.get_pixbuf()
        if not pix:
            return None
        if max_side and (pix.get_width() > max_side or pix.get_height() > max_side):
            w, h = pix.get_width(), pix.get_height()
            if w >= h:
                nw, nh = max_side, max(1, int(h * max_side / w))
            else:
                nh, nw = max_side, max(1, int(w * max_side / h))
            pix = pix.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
        return Gdk.Texture.new_for_pixbuf(pix)
    except Exception:
        return None


COVER_SIZE = 112  # stały rozmiar okładki „Teraz gra”


def fetch_texture(url: str, max_side: int | None = None):
    if not url or not str(url).startswith("http"):
        return None
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        return texture_from_bytes(r.content, max_side=max_side)
    except Exception:
        return None


def placeholder_texture():
    try:
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon = theme.lookup_icon(
            "avatar-default-symbolic",
            [],
            96,
            1,
            Gtk.TextDirection.NONE,
            Gtk.IconLookupFlags.FORCE_SIZE,
        )
        if icon:
            pix = icon.load_icon()
            if pix:
                return Gdk.Texture.new_for_pixbuf(pix)
    except Exception:
        pass
    return None


def artist_permalink(track: dict) -> str | None:
    user = track.get("user") or {}
    return user.get("permalink") or user.get("username")


def fmt_dur(sec: int) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


# ─────────────────────────── Fade ───────────────────────────
class Fade:
    @staticmethod
    def out(widget, duration_ms=200, then=None):
        start = GLib.get_monotonic_time()
        try:
            start_op = widget.get_opacity()
        except Exception:
            start_op = 1.0

        def step():
            elapsed = (GLib.get_monotonic_time() - start) / 1000.0
            t = min(1.0, elapsed / duration_ms)
            widget.set_opacity(start_op * (1.0 - t))
            if t < 1.0:
                return True
            widget.set_opacity(0.0)
            if then:
                then()
            return False

        GLib.timeout_add(16, step)

    @staticmethod
    def in_(widget, duration_ms=260):
        start = GLib.get_monotonic_time()
        widget.set_opacity(0.0)
        widget.set_visible(True)

        def step():
            elapsed = (GLib.get_monotonic_time() - start) / 1000.0
            t = min(1.0, elapsed / duration_ms)
            e = 1.0 - (1.0 - t) * (1.0 - t)
            widget.set_opacity(e)
            return t < 1.0

        GLib.timeout_add(16, step)


# ─────────────────────────── Nav history (undo / redo) ───────────────────────────
class NavHistory:
    """Stos stanów widoku: mode, param, type, page, tracks, artist_username, label."""

    def __init__(self, max_size=40):
        self.stack = []
        self.index = -1
        self.max_size = max_size
        self._suppress = False  # True podczas przywracania

    def push(self, state: dict):
        if self._suppress:
            return
        # obcinamy „przyszłość”
        if self.index < len(self.stack) - 1:
            self.stack = self.stack[: self.index + 1]
        self.stack.append(deepcopy(state))
        if len(self.stack) > self.max_size:
            self.stack.pop(0)
        else:
            self.index += 1
        if self.index >= len(self.stack):
            self.index = len(self.stack) - 1

    def can_undo(self):
        return self.index > 0

    def can_redo(self):
        return 0 <= self.index < len(self.stack) - 1

    def undo(self):
        if not self.can_undo():
            return None
        self.index -= 1
        return deepcopy(self.stack[self.index])

    def redo(self):
        if not self.can_redo():
            return None
        self.index += 1
        return deepcopy(self.stack[self.index])



# ─────────────────────────── Cover disc (vinyl / CD, spin BPM) ───────────────────────────
class CoverDiscView(Gtk.DrawingArea):
    """Proceduralny winyl / CD + opcjonalny obrót wg BPM (linie + shade, bez assetów)."""

    __gtype_name__ = "CoverDiscView"

    def __init__(self, size=COVER_SIZE):
        super().__init__()
        self._size = size
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_size_request(size, size)
        self.set_hexpand(False)
        self.set_vexpand(False)
        self._pixbuf = None  # GdkPixbuf okładki (kwadrat)
        self._angle = 0.0
        self._spinning = False
        self._bpm = 120.0
        self._style = "vinyl"  # normal | vinyl | cd
        self._tick_id = None
        self._last_ts = 0
        if HAS_CAIRO:
            self.set_draw_func(self._draw)
        else:
            # bez pycairo – puste tło, okładka i tak w pixbuf (narysujemy prostokąt w ticku nie)
            self.set_draw_func(self._draw_fallback)

    def set_style(self, style: str):
        self._style = style if style in ("normal", "vinyl", "cd") else "vinyl"
        self.queue_draw()

    def set_bpm(self, bpm):
        try:
            b = float(bpm)
            self._bpm = b if b > 20 else 120.0
        except Exception:
            self._bpm = 120.0

    def set_cover_texture(self, tex):
        """Przyjmuje Gdk.Texture lub None → pixbuf do rysowania."""
        self._pixbuf = None
        if tex is not None:
            try:
                # Texture → pixbuf
                downloader = Gdk.TextureDownloader.new(tex)
                # prostsza ścieżka: save do bytes
            except Exception:
                pass
            try:
                # GTK4: texture.save_to_png_bytes / lub pixbuf z paintable
                if hasattr(tex, "save_to_png_bytes"):
                    b = bytes(tex.save_to_png_bytes().get_data())
                    self._pixbuf = texture_bytes_to_pixbuf(b)
                else:
                    # fallback: narysuj texture do pixbuf przez paintable size
                    w = tex.get_width()
                    h = tex.get_height()
                    # bez konwersji – zostaw None, drawing użyje placeholdera
                    self._pixbuf = None
            except Exception:
                self._pixbuf = None
        self.queue_draw()

    def set_cover_pixbuf(self, pixbuf):
        self._pixbuf = pixbuf
        self.queue_draw()

    def set_spinning(self, on: bool):
        self._spinning = bool(on)
        if self._spinning:
            self._last_ts = GLib.get_monotonic_time()
            if not self._tick_id:
                self._tick_id = GLib.timeout_add(33, self._tick)
        else:
            if self._tick_id:
                GLib.source_remove(self._tick_id)
                self._tick_id = None
            self.queue_draw()

    def _tick(self):
        if not self._spinning:
            self._tick_id = None
            return False
        now = GLib.get_monotonic_time()
        dt = (now - self._last_ts) / 1_000_000.0
        self._last_ts = now
        # 1 obrót na beat * (bpm/60) – winyl ~33⅓ RPM ≈ 0.556 rev/s przy 33.3
        # Używamy BPM: pełny obrót co 4 beaty (takt 4/4) → revs/s = bpm/240
        revs_per_sec = self._bpm / 240.0
        self._angle = (self._angle + revs_per_sec * dt * 2 * math.pi) % (2 * math.pi)
        self.queue_draw()
        return True

    def _draw_fallback(self, area, cr, width, height, data=None):
        # Jeśli foreign cairo nie działa – nie rysuj (unikaj TypeError spam)
        return

    def _draw(self, area, cr, width, height, data=None):
        cx, cy = width / 2.0, height / 2.0
        R = min(width, height) / 2.0 - 1.0
        style = self._style

        if style == "normal":
            self._draw_normal(cr, cx, cy, R)
            return

        cr.save()
        cr.translate(cx, cy)
        if self._spinning or self._angle:
            cr.rotate(self._angle)
        cr.translate(-cx, -cy)

        if style == "vinyl":
            self._draw_vinyl(cr, cx, cy, R)
        else:
            self._draw_cd(cr, cx, cy, R)
        cr.restore()

        # stały cień / gloss na wierzchu (nie obraca się) – „gramofon/światło”
        self._draw_shade(cr, cx, cy, R, style)

    def _draw_normal(self, cr, cx, cy, R):
        # kwadratowa okładka bez dysku
        s = R * 1.6
        x, y = cx - s / 2, cy - s / 2
        if self._pixbuf:
            self._paint_pixbuf_rect(cr, self._pixbuf, x, y, s, s)
        else:
            cr.set_source_rgb(0.2, 0.2, 0.22)
            cr.rectangle(x, y, s, s)
            cr.fill()

    def _draw_vinyl(self, cr, cx, cy, R):
        # talerz
        cr.arc(cx, cy, R, 0, 2 * math.pi)
        cr.set_source_rgb(0.06, 0.06, 0.07)
        cr.fill()

        # rowki (koncentryczne linie)
        cr.set_line_width(0.7)
        for i in range(8, int(R * 0.92), 3):
            t = i / R
            g = 0.10 + 0.04 * math.sin(i * 0.4)
            cr.set_source_rgba(g, g, g + 0.02, 0.85)
            cr.arc(cx, cy, i, 0, 2 * math.pi)
            cr.stroke()

        # label (środek) z okładką
        label_r = R * 0.38
        cr.arc(cx, cy, label_r, 0, 2 * math.pi)
        cr.clip()
        if self._pixbuf:
            self._paint_pixbuf_circle(cr, self._pixbuf, cx, cy, label_r)
        else:
            cr.set_source_rgb(0.35, 0.12, 0.12)
            cr.arc(cx, cy, label_r, 0, 2 * math.pi)
            cr.fill()
        cr.reset_clip()

        # obwódka labela
        cr.set_line_width(1.2)
        cr.set_source_rgb(0.25, 0.25, 0.28)
        cr.arc(cx, cy, label_r, 0, 2 * math.pi)
        cr.stroke()

        # wrzeciono (dziurka)
        cr.arc(cx, cy, max(2.5, R * 0.04), 0, 2 * math.pi)
        cr.set_source_rgb(0.12, 0.12, 0.14)
        cr.fill()
        cr.arc(cx, cy, max(1.2, R * 0.02), 0, 2 * math.pi)
        cr.set_source_rgb(0.45, 0.45, 0.48)
        cr.fill()

    def _draw_cd(self, cr, cx, cy, R):
        # tarcza CD – srebrny gradient (symulacja)
        cr.arc(cx, cy, R, 0, 2 * math.pi)
        pat = cairo_radial(cx, cy, R * 0.15, cx, cy, R)
        if pat:
            pat.add_color_stop_rgb(0.0, 0.75, 0.78, 0.82)
            pat.add_color_stop_rgb(0.45, 0.55, 0.58, 0.62)
            pat.add_color_stop_rgb(0.75, 0.70, 0.72, 0.76)
            pat.add_color_stop_rgb(1.0, 0.40, 0.42, 0.46)
            cr.set_source(pat)
        else:
            cr.set_source_rgb(0.6, 0.62, 0.66)
        cr.fill()

        # delikatne pierścienie danych
        cr.set_line_width(0.6)
        for i in range(int(R * 0.35), int(R * 0.95), 4):
            a = 0.08 + 0.06 * ((i // 4) % 2)
            cr.set_source_rgba(0.9, 0.92, 0.95, a)
            cr.arc(cx, cy, i, 0, 2 * math.pi)
            cr.stroke()

        # rainbow-ish łuk (shade)
        cr.set_line_width(R * 0.08)
        cr.set_source_rgba(0.4, 0.7, 0.95, 0.12)
        cr.arc(cx, cy, R * 0.7, -0.8, 0.6)
        cr.stroke()
        cr.set_source_rgba(0.95, 0.5, 0.3, 0.10)
        cr.arc(cx, cy, R * 0.7, 1.2, 2.4)
        cr.stroke()

        # obszar okładki / label
        label_r = R * 0.42
        cr.arc(cx, cy, label_r, 0, 2 * math.pi)
        cr.clip()
        if self._pixbuf:
            self._paint_pixbuf_circle(cr, self._pixbuf, cx, cy, label_r)
        else:
            cr.set_source_rgb(0.25, 0.28, 0.32)
            cr.arc(cx, cy, label_r, 0, 2 * math.pi)
            cr.fill()
        cr.reset_clip()

        cr.set_line_width(1.0)
        cr.set_source_rgba(0.3, 0.3, 0.35, 0.9)
        cr.arc(cx, cy, label_r, 0, 2 * math.pi)
        cr.stroke()

        # hole
        hole = max(3.0, R * 0.07)
        cr.arc(cx, cy, hole, 0, 2 * math.pi)
        cr.set_source_rgb(0.12, 0.12, 0.14)
        cr.fill()
        cr.arc(cx, cy, hole * 0.45, 0, 2 * math.pi)
        cr.set_source_rgb(0.5, 0.5, 0.55)
        cr.fill()

    def _draw_shade(self, cr, cx, cy, R, style):
        # stałe światło z lewego góry + cień
        cr.arc(cx, cy, R, 0, 2 * math.pi)
        cr.clip()
        # gloss
        cr.rectangle(0, 0, cx * 2, cy * 2)
        pat = None
        try:
            import cairo as _cairo
            pat = _cairo.LinearGradient(cx - R, cy - R, cx + R, cy + R)
            pat.add_color_stop_rgba(0.0, 1, 1, 1, 0.18)
            pat.add_color_stop_rgba(0.35, 1, 1, 1, 0.04)
            pat.add_color_stop_rgba(0.7, 0, 0, 0, 0.0)
            pat.add_color_stop_rgba(1.0, 0, 0, 0, 0.25)
            cr.set_source(pat)
            cr.paint()
        except Exception:
            cr.set_source_rgba(1, 1, 1, 0.08)
            cr.paint()
        cr.reset_clip()
        # obramowanie
        cr.set_line_width(1.0)
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.arc(cx, cy, R, 0, 2 * math.pi)
        cr.stroke()

    def _paint_pixbuf_circle(self, cr, pixbuf, cx, cy, r):
        side = max(2, int(r * 2))
        scaled = pixbuf.scale_simple(side, side, GdkPixbuf.InterpType.BILINEAR)
        cr.save()
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.clip()
        try:
            Gdk.cairo_set_source_pixbuf(cr, scaled, cx - r, cy - r)
        except Exception:
            # fallback surface
            try:
                import cairo
                surf = Gdk.cairo_surface_create_from_pixbuf(scaled, 1, None)
                cr.set_source_surface(surf, cx - r, cy - r)
            except Exception:
                cr.set_source_rgb(0.3, 0.3, 0.35)
                cr.paint()
                cr.restore()
                return
        cr.paint()
        cr.restore()

    def _paint_pixbuf_rect(self, cr, pixbuf, x, y, w, h):
        scaled = pixbuf.scale_simple(max(1, int(w)), max(1, int(h)), GdkPixbuf.InterpType.BILINEAR)
        try:
            Gdk.cairo_set_source_pixbuf(cr, scaled, x, y)
            cr.paint()
        except Exception:
            cr.set_source_rgb(0.25, 0.25, 0.28)
            cr.rectangle(x, y, w, h)
            cr.fill()


def cairo_radial(cx0, cy0, r0, cx1, cy1, r1):
    try:
        import cairo
        return cairo.RadialGradient(cx0, cy0, r0, cx1, cy1, r1)
    except Exception:
        return None


def texture_bytes_to_pixbuf(b: bytes):
    try:
        loader = GdkPixbuf.PixbufLoader.new()
        loader.write(b)
        loader.close()
        return loader.get_pixbuf()
    except Exception:
        return None


def texture_to_pixbuf(tex):
    if tex is None:
        return None
    try:
        if hasattr(tex, "save_to_png_bytes"):
            gbytes = tex.save_to_png_bytes()
            data = bytes(gbytes.get_data())
            return texture_bytes_to_pixbuf(data)
    except Exception as e:
        print("texture_to_pixbuf:", e)
    # fallback: snapshot via downloader not always available
    return None


def load_pixbuf_from_url_or_file(url=None, path=None, max_side=COVER_SIZE):
    try:
        if path and Path(path).exists():
            data = Path(path).read_bytes()
        elif url and str(url).startswith("http"):
            r = requests.get(url, timeout=12)
            r.raise_for_status()
            data = r.content
        else:
            return None
        loader = GdkPixbuf.PixbufLoader.new()
        loader.write(data)
        loader.close()
        pix = loader.get_pixbuf()
        if not pix:
            return None
        if max_side and (pix.get_width() > max_side or pix.get_height() > max_side):
            w, h = pix.get_width(), pix.get_height()
            if w >= h:
                nw, nh = max_side, max(1, int(h * max_side / w))
            else:
                nh, nw = max_side, max(1, int(w * max_side / h))
            pix = pix.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
        return pix
    except Exception as e:
        print("load_pixbuf:", e)
        return None


# ─────────────────────────── Track Row ───────────────────────────
class TrackRow(Gtk.ListBoxRow):
    def __init__(self, track: dict, on_artist_click=None):
        super().__init__()
        self.track = track
        self.on_artist_click = on_artist_click
        self.set_activatable(True)

        title = track.get("title") or "Unknown"
        user = (track.get("user") or {}).get("username") or ""
        genre = track.get("genre") or track.get("category") or ""
        dur = int(track.get("duration") or 0)
        dur_str = fmt_dur(dur)

        self.pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER)
        self.pic.set_size_request(48, 48)

        title_l = Gtk.Label(label=title, xalign=0, ellipsize=3)
        title_l.add_css_class("heading")

        artist_btn = Gtk.Button(label=user or "—", css_classes=["flat", "link"])
        artist_btn.set_halign(Gtk.Align.START)
        artist_btn.set_has_frame(False)
        if user and on_artist_click:
            artist_btn.connect("clicked", self._artist_clicked)

        meta = Gtk.Label(
            label=f"{genre}  ·  {dur_str}".strip(" ·"), xalign=0, ellipsize=3
        )
        meta.add_css_class("dim-label")
        meta.add_css_class("caption")

        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        v.append(title_l)
        v.append(artist_btn)
        v.append(meta)

        h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        h.set_margin_start(8)
        h.set_margin_end(8)
        h.set_margin_top(5)
        h.set_margin_bottom(5)
        h.append(self.pic)
        h.append(v)
        self.set_child(h)

        art = (
            track.get("artwork_url")
            or track.get("thumb")
            or (track.get("images") or {}).get("thumbnail")
        )
        if art:
            threading.Thread(target=self._load, args=(art,), daemon=True).start()

    def _artist_clicked(self, *_):
        permalink = artist_permalink(self.track)
        if permalink and self.on_artist_click:
            self.on_artist_click(permalink)

    def _load(self, url):
        tex = fetch_texture(url, max_side=96)
        if tex:
            GLib.idle_add(self.pic.set_paintable, tex)


# ─────────────────────────── GStreamer (flump3dec + seek) ───────────────────────────
def _gst_escape_path(path: str) -> str:
    """Escape path for gst-parse-launch (spaces, quotes, backslash)."""
    return path.replace("\\", "\\\\").replace('"', '\\"')


def _pick_audiosink() -> str:
    for name in ("pipewiresink", "pulsesink", "autoaudiosink"):
        if Gst.ElementFactory.find(name):
            return name
    return "autoaudiosink"


class LocalGstPlayer(GObject.GObject):
    """
    Stream: souphttpsrc ! mpegaudioparse ! flump3dec ! …
    Plik:   filesrc      ! mpegaudioparse ! flump3dec ! …
    flump3dec bez URI – dane z pads (souphttpsrc / filesrc).
    """

    def __init__(self):
        super().__init__()
        self.pipeline = None
        self.on_eos = None
        self.on_error = None
        self._volume = 1.0
        self._muted = False
        self._duration_s = 0
        self._sink_name = _pick_audiosink()

    def set_known_duration(self, seconds: int):
        self._duration_s = max(0, int(seconds or 0))

    def _attach_bus(self):
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_msg)
        vol = self.pipeline.get_by_name("vol")
        if vol:
            vol.set_property("volume", 0.0 if self._muted else self._volume)

    def _build_file(self, filepath: str):
        self.stop()
        loc = _gst_escape_path(filepath)
        desc = (
            f'filesrc location="{loc}" ! '
            f"queue max-size-time=2000000000 ! "
            f"mpegaudioparse ! flump3dec ! "
            f"audioconvert ! audioresample ! "
            f"volume name=vol ! "
            f"queue ! {self._sink_name} sync=true"
        )
        try:
            self.pipeline = Gst.parse_launch(desc)
        except Exception as e:
            print("file pipeline error:", e)
            desc = (
                f'filesrc location="{loc}" ! '
                f"flump3dec ! audioconvert ! audioresample ! "
                f"volume name=vol ! {self._sink_name}"
            )
            self.pipeline = Gst.parse_launch(desc)
        self._attach_bus()

    def _build_stream(self, url: str):
        self.stop()
        loc = _gst_escape_path(url)
        # souphttpsrc → flump3dec (bez zapisu na dysk)
        desc = (
            f'souphttpsrc location="{loc}" is-live=false iradio-mode=false ! '
            f"queue max-size-bytes=2097152 max-size-time=0 max-size-buffers=0 ! "
            f"mpegaudioparse ! flump3dec ! "
            f"audioconvert ! audioresample ! "
            f"volume name=vol ! "
            f"queue ! {self._sink_name} sync=true"
        )
        try:
            self.pipeline = Gst.parse_launch(desc)
        except Exception as e:
            print("stream pipeline error, fallback:", e)
            desc = (
                f'souphttpsrc location="{loc}" ! '
                f"flump3dec ! audioconvert ! audioresample ! "
                f"volume name=vol ! {self._sink_name}"
            )
            self.pipeline = Gst.parse_launch(desc)
        self._attach_bus()

    def _on_msg(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.EOS and self.on_eos:
            self.on_eos()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print("GStreamer ERROR:", err, dbg)
            if self.on_error:
                self.on_error(str(err))

    def play_file(self, path: str, known_duration: int = 0):
        if known_duration:
            self._duration_s = known_duration
        self._build_file(path)
        self.pipeline.set_state(Gst.State.PAUSED)
        self.pipeline.get_state(2 * Gst.SECOND)
        self.pipeline.set_state(Gst.State.PLAYING)

    def play_url(self, url: str, known_duration: int = 0):
        if known_duration:
            self._duration_s = known_duration
        self._build_stream(url)
        self.pipeline.set_state(Gst.State.PAUSED)
        self.pipeline.get_state(3 * Gst.SECOND)
        self.pipeline.set_state(Gst.State.PLAYING)

    def play(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.PLAYING)

    def pause(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.PAUSED)

    def stop(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def state(self):
        if not self.pipeline:
            return Gst.State.NULL
        return self.pipeline.get_state(Gst.CLOCK_TIME_NONE)[1]

    def set_volume(self, v: float):
        self._volume = max(0.0, min(1.0, v))
        if self.pipeline:
            vol = self.pipeline.get_by_name("vol")
            if vol:
                vol.set_property("volume", 0.0 if self._muted else self._volume)

    def set_muted(self, m: bool):
        self._muted = m
        self.set_volume(self._volume)

    def query_pos_dur(self):
        if not self.pipeline:
            return 0, self._duration_s
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        ok2, dur = self.pipeline.query_duration(Gst.Format.TIME)
        pos_s = int(pos // Gst.SECOND) if ok else 0
        if ok2 and dur > 0:
            dur_s = int(dur // Gst.SECOND)
            self._duration_s = dur_s
        else:
            dur_s = self._duration_s
        return pos_s, dur_s

    def seek_seconds(self, sec: int):
        if not self.pipeline:
            return
        sec = max(0, int(sec))
        # FLUSH + KEY_UNIT – stabilne z flump3dec + mpegaudioparse
        ok = self.pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            sec * Gst.SECOND,
        )
        if not ok:
            # drugi attempt z ACCURATE
            self.pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                sec * Gst.SECOND,
            )


# ─────────────────────────── App ───────────────────────────
class HearThisApp(Gtk.Application):
    def __init__(self):
        super().__init__(
            application_id="org.hearthis.Player",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.connect("activate", self.on_activate)

        self.cfg = Config()
        self.player = LocalGstPlayer()
        self.player.on_eos = self._on_eos
        self.history = NavHistory()

        self.tracks = []
        self.selected = []
        self.current_playlist = "all"
        self.current_index = -1
        self.is_seeking = False
        self.current_mode = None
        self.current_param = None
        self.current_type = None
        self.page = 1
        self.per_page = 20
        self.artist_username = None
        self.view_label = ""
        self._download_lock = threading.Lock()
        self._play_gen = 0
        self._current_track = None

    def on_activate(self, app):
        self.win = Gtk.ApplicationWindow(application=app, title="HearThis Player")
        self.win.set_default_size(1280, 840)

        css = Gtk.CssProvider()
        css.load_from_data(
            b"""
            .title-large { font-size: 1.3em; font-weight: bold; }
            .now-playing { font-weight: 600; }
            button.link { padding: 0; min-height: 0; }
            button.link label { color: @accent_color; text-decoration: underline; }
            row:selected { background: alpha(@accent_bg_color, 0.22); }
            .stream-info { font-size: 0.85em; }
            .cache-label { font-size: 0.8em; }
            .cover-box {
                min-width: 112px;
                min-height: 112px;
            }
            """
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # ── Header ──
        header = Gtk.HeaderBar()
        self.win.set_titlebar(header)

        # Undo / Redo
        self.btn_undo = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Poprzednie wyniki")
        self.btn_undo.connect("clicked", self.on_undo)
        self.btn_undo.set_sensitive(False)
        header.pack_start(self.btn_undo)

        self.btn_redo = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Następne wyniki")
        self.btn_redo.connect("clicked", self.on_redo)
        self.btn_redo.set_sensitive(False)
        header.pack_start(self.btn_redo)

        # Search + mini icon
        self.search_entry = Gtk.Entry(
            placeholder_text="Szukaj artysty / tytułu…", width_chars=24
        )
        self.search_entry.connect("activate", self.on_search)
        header.pack_start(self.search_entry)

        self.btn_search = Gtk.Button(icon_name="system-search-symbolic", tooltip_text="Szukaj")
        self.btn_search.connect("clicked", self.on_search)
        header.pack_start(self.btn_search)

        # Mini status icon (busy / idle)
        self.mini_icon = Gtk.Image.new_from_icon_name("media-optical-symbolic")
        self.mini_icon.set_pixel_size(18)
        self.mini_icon.set_tooltip_text("Gotowy")
        header.pack_start(self.mini_icon)

        # Pasek postępu cache – obok wyszukiwania
        self.cache_bar = Gtk.ProgressBar()
        self.cache_bar.set_show_text(True)
        self.cache_bar.set_fraction(0)
        self.cache_bar.set_size_request(140, -1)
        self.cache_bar.set_valign(Gtk.Align.CENTER)
        self.cache_bar.set_visible(False)
        header.pack_start(self.cache_bar)

        # User
        self.user_label = Gtk.Label(label="—")
        self.btn_user = Gtk.Button(label="Preferencje")
        self.btn_user.connect("clicked", self.on_user_clicked)
        header.pack_end(self.btn_user)
        header.pack_end(self.user_label)

        self.mute_btn = Gtk.ToggleButton(icon_name="audio-volume-high-symbolic")
        self.mute_btn.connect("toggled", self.on_mute)
        header.pack_end(self.mute_btn)
        self.vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.vol.set_value(100)
        self.vol.set_size_request(90, -1)
        self.vol.connect("value-changed", self.on_volume)
        header.pack_end(self.vol)
        self.btn_stop = Gtk.Button(icon_name="media-playback-stop-symbolic")
        self.btn_stop.connect("clicked", self.on_stop)
        header.pack_end(self.btn_stop)
        self.btn_pp = Gtk.Button(icon_name="media-playback-start-symbolic")
        self.btn_pp.connect("clicked", self.on_play_pause)
        header.pack_end(self.btn_pp)

        # ── Main ──
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main.set_hexpand(True)
        main.set_vexpand(True)

        # TOP – kompaktowy, NIE rozciąga się w pionie (lista bierze resztę)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        top.set_margin_start(12)
        top.set_margin_end(12)
        top.set_margin_top(8)
        top.set_margin_bottom(6)
        top.set_vexpand(False)
        top.set_hexpand(True)
        top.set_valign(Gtk.Align.START)

        top_left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        top_left.set_hexpand(True)
        top_left.set_vexpand(False)
        top_left.set_valign(Gtk.Align.START)
        self.view_title = Gtk.Label(label="Popularne", xalign=0, css_classes=["title-4"])
        top_left.append(self.view_title)

        feeds = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, mode in (("Popularne", "popular"), ("Nowe", "new")):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _, m=mode: self.load_feed(m))
            feeds.append(b)
        self.btn_my = Gtk.Button(label="Moje uploady")
        self.btn_my.connect("clicked", self.on_my_uploads)
        self.btn_my.set_sensitive(False)
        feeds.append(self.btn_my)
        top_left.append(feeds)

        gbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.genre_dd = Gtk.DropDown()
        self.genre_dd.set_hexpand(True)
        btn_g = Gtk.Button(label="Gatunek")
        btn_g.connect("clicked", self.on_load_genre)
        gbox.append(self.genre_dd)
        gbox.append(btn_g)
        top_left.append(gbox)

        abox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for lab, t in (("Utwory", "tracks"), ("Lajki", "likes"), ("Reshare", "reshares")):
            b = Gtk.Button(label=lab)
            b.connect("clicked", lambda _, tt=t: self.load_artist_type(tt))
            abox.append(b)
        top_left.append(abox)
        top.append(top_left)

        # Now Playing – cover o SZTYWNYM rozmiarze (Picture inaczej rozpycha top)
        self.now_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.now_box.set_halign(Gtk.Align.END)
        self.now_box.set_valign(Gtk.Align.START)
        self.now_box.set_vexpand(False)
        self.now_box.set_hexpand(False)

        # Cover disc view – winyl / CD / normal + spin BPM (wymaga pycairo)
        cover_frame = Gtk.Box()
        cover_frame.add_css_class("cover-box")
        cover_frame.set_size_request(COVER_SIZE, COVER_SIZE)
        cover_frame.set_hexpand(False)
        cover_frame.set_vexpand(False)
        cover_frame.set_halign(Gtk.Align.CENTER)
        cover_frame.set_valign(Gtk.Align.CENTER)
        if HAS_CAIRO:
            self.now_cover = CoverDiscView(size=COVER_SIZE)
            self.now_cover.set_style(self.cfg.cover_style)
            self._cover_is_disc = True
        else:
            self.now_cover = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN)
            self.now_cover.set_size_request(COVER_SIZE, COVER_SIZE)
            self._cover_is_disc = False
        cover_frame.append(self.now_cover)
        self.now_box.append(cover_frame)

        now_info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        now_info.set_valign(Gtk.Align.CENTER)
        now_info.set_size_request(200, -1)
        now_info.set_hexpand(False)
        self.now_title = Gtk.Label(xalign=0, wrap=True, ellipsize=3, css_classes=["now-playing"])
        self.now_title.set_max_width_chars(26)
        self.now_artist_btn = Gtk.Button(label="", css_classes=["flat", "link"])
        self.now_artist_btn.set_halign(Gtk.Align.START)
        self.now_artist_btn.connect("clicked", self._on_now_artist)
        self.stream_info = Gtk.Label(xalign=0, wrap=True, css_classes=["stream-info", "dim-label"])
        self.stream_info.set_max_width_chars(28)
        self.status = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"])
        now_info.append(self.now_title)
        now_info.append(self.now_artist_btn)
        now_info.append(self.stream_info)
        now_info.append(self.status)
        self.now_box.append(now_info)
        top.append(self.now_box)

        main.append(top)

        # Content paned – musi brać całą wysokość, żeby ScrolledWindow działał
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_hexpand(True)
        paned.set_vexpand(True)
        paned.set_resize_start_child(True)
        paned.set_shrink_start_child(False)
        paned.set_position(780)

        # LEFT – lists
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        left.set_hexpand(True)
        left.set_vexpand(True)

        self.notebook = Gtk.Notebook()
        self.notebook.set_hexpand(True)
        self.notebook.set_vexpand(True)
        self.notebook.set_scrollable(True)

        scroll_all = Gtk.ScrolledWindow()
        scroll_all.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll_all.set_hexpand(True)
        scroll_all.set_vexpand(True)
        scroll_all.set_propagate_natural_height(False)
        scroll_all.set_min_content_height(200)
        self.list_all = Gtk.ListBox()
        self.list_all.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list_all.add_css_class("boxed-list")
        self.list_all.connect("row-activated", self.on_row_all)
        scroll_all.set_child(self.list_all)
        self.notebook.append_page(scroll_all, Gtk.Label(label="Wszystkie"))

        scroll_sel = Gtk.ScrolledWindow()
        scroll_sel.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll_sel.set_hexpand(True)
        scroll_sel.set_vexpand(True)
        scroll_sel.set_propagate_natural_height(False)
        scroll_sel.set_min_content_height(200)
        self.list_sel = Gtk.ListBox()
        self.list_sel.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list_sel.add_css_class("boxed-list")
        self.list_sel.connect("row-activated", self.on_row_sel)
        scroll_sel.set_child(self.list_sel)
        self.notebook.append_page(scroll_sel, Gtk.Label(label="Wybrane"))

        left.append(self.notebook)

        act = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        act.set_margin_start(8)
        act.set_margin_end(8)
        act.set_margin_top(6)
        act.set_margin_bottom(6)
        act.set_vexpand(False)
        self.btn_prev = Gtk.Button(label="⟨")
        self.btn_prev.connect("clicked", self.on_prev)
        self.page_lbl = Gtk.Label(label="1")
        self.btn_next = Gtk.Button(label="⟩")
        self.btn_next.connect("clicked", self.on_next)
        self.btn_more = Gtk.Button(label="Więcej")
        self.btn_more.connect("clicked", self.on_more)
        self.btn_add = Gtk.Button(label="Dodaj do wybranych")
        self.btn_add.connect("clicked", self.on_add_selected)
        for w in (self.btn_prev, self.page_lbl, self.btn_next, self.btn_more, self.btn_add):
            act.append(w)
        left.append(act)
        paned.set_start_child(left)

        # RIGHT – artist sidebar (scroll)
        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        right_scroll.set_min_content_width(260)
        right_scroll.set_max_content_width(360)
        right_scroll.set_hexpand(False)
        right_scroll.set_vexpand(True)
        right_scroll.set_propagate_natural_height(False)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right.set_margin_start(10)
        right.set_margin_end(10)
        right.set_margin_top(8)
        right.set_margin_bottom(8)

        right.append(Gtk.Label(label="Artysta", xalign=0, css_classes=["title-4"]))
        self.avatar = Gtk.Picture(content_fit=Gtk.ContentFit.COVER)
        self.avatar.set_size_request(120, 120)
        self.avatar.set_paintable(placeholder_texture())
        right.append(self.avatar)

        self.artist_name_btn = Gtk.Button(label="", css_classes=["flat", "link"])
        self.artist_name_btn.set_halign(Gtk.Align.START)
        self.artist_name_btn.connect("clicked", self._on_sidebar_artist)
        right.append(self.artist_name_btn)

        self.desc = Gtk.Label(xalign=0, wrap=True, wrap_mode=Gtk.WrapMode.WORD)
        self.desc.add_css_class("dim-label")
        self.desc.set_max_width_chars(32)
        self.desc.set_selectable(True)
        right.append(self.desc)

        right_scroll.set_child(right)
        paned.set_end_child(right_scroll)
        main.append(paned)

        # Bottom seek
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bottom.set_margin_start(12)
        bottom.set_margin_end(12)
        bottom.set_margin_top(6)
        bottom.set_margin_bottom(10)
        bottom.set_vexpand(False)
        bottom.set_hexpand(True)
        self.pos = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.pos.set_hexpand(True)
        self.pos.set_draw_value(False)
        self.pos.set_sensitive(False)
        self.pos.connect("value-changed", self.on_seek)
        gest = Gtk.GestureClick()
        gest.connect("pressed", lambda *a: setattr(self, "is_seeking", True))
        gest.connect("released", lambda *a: setattr(self, "is_seeking", False))
        self.pos.add_controller(gest)
        self.time_lbl = Gtk.Label(label="00:00 / 00:00")
        bottom.append(self.pos)
        bottom.append(self.time_lbl)
        main.append(bottom)

        self.win.set_child(main)
        self.win.present()

        self._update_user_ui()
        self._update_nav_buttons()
        self.load_genres()
        self.load_feed("popular")
        GLib.timeout_add(250, self._tick)

    # ──────────── History ────────────
    def _snapshot(self):
        return {
            "mode": self.current_mode,
            "param": self.current_param,
            "type": self.current_type,
            "page": self.page,
            "artist_username": self.artist_username,
            "view_label": self.view_label,
            "tracks": list(self.tracks),
        }

    def _push_history(self):
        if not self.tracks and self.current_mode is None:
            return
        self.history.push(self._snapshot())
        self._update_nav_buttons()

    def _restore(self, state: dict):
        if not state:
            return
        self.history._suppress = True
        try:
            self.current_mode = state.get("mode")
            self.current_param = state.get("param")
            self.current_type = state.get("type")
            self.page = state.get("page") or 1
            self.artist_username = state.get("artist_username")
            self.view_label = state.get("view_label") or ""
            self.view_title.set_text(self.view_label or "—")
            self.page_lbl.set_text(str(self.page))
            tracks = state.get("tracks") or []
            self._fill(tracks, clear=True)
            if self.artist_username:
                self._fetch_artist_info(self.artist_username)
        finally:
            self.history._suppress = False
        self._update_nav_buttons()

    def _update_nav_buttons(self):
        self.btn_undo.set_sensitive(self.history.can_undo())
        self.btn_redo.set_sensitive(self.history.can_redo())

    def on_undo(self, *_):
        st = self.history.undo()
        if st:
            self._restore(st)
            self.status.set_text("← poprzednie wyniki")

    def on_redo(self, *_):
        st = self.history.redo()
        if st:
            self._restore(st)
            self.status.set_text("→ następne wyniki")

    # ──────────── User ────────────
    def _update_user_ui(self):
        if self.cfg.has_user:
            self.user_label.set_text(self.cfg.username)
            self.btn_user.set_label("Preferencje")
            self.btn_my.set_sensitive(True)
        else:
            self.user_label.set_text("—")
            self.btn_user.set_label("Preferencje")
            self.btn_my.set_sensitive(False)

    def on_user_clicked(self, *_):
        """Preferencje: username, cache, styl okładki, spin BPM."""
        dialog = Gtk.Dialog(
            title="Preferencje",
            transient_for=self.win,
            modal=True,
        )
        dialog.add_button("Wyczyść user", Gtk.ResponseType.REJECT)
        dialog.add_button("Anuluj", Gtk.ResponseType.CANCEL)
        dialog.add_button("Zapisz", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(12)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(16)
        box.set_margin_bottom(8)

        box.append(Gtk.Label(label="Użytkownik (permalink, bez hasła)", xalign=0))
        entry = Gtk.Entry(
            placeholder_text="np. sublevelgrooves",
            hexpand=True,
            text=self.cfg.username or "",
        )
        box.append(entry)

        cache_check = Gtk.CheckButton(label="Cache lokalny (tytuł + okładka + audio)")
        cache_check.set_active(self.cfg.cache_enabled)
        box.append(cache_check)

        box.append(Gtk.Label(label="Podgląd okładki", xalign=0))
        style_model = Gtk.StringList.new(["normal", "vinyl", "cd"])
        style_dd = Gtk.DropDown(model=style_model)
        styles = ["normal", "vinyl", "cd"]
        try:
            style_dd.set_selected(styles.index(self.cfg.cover_style))
        except ValueError:
            style_dd.set_selected(1)
        box.append(style_dd)
        style_hint = Gtk.Label(
            label="normal = kwadrat · vinyl = płyta winylowa · cd = tarcza CD\n"
                  "(rysowane liniami/krzywymi + cień, bez zewnętrznych assetów)",
            xalign=0,
            wrap=True,
        )
        style_hint.add_css_class("dim-label")
        box.append(style_hint)

        spin_check = Gtk.CheckButton(label="Obrót płyty według BPM (podczas odtwarzania)")
        spin_check.set_active(self.cfg.spin_enabled)
        box.append(spin_check)

        def on_resp(dlg, resp):
            if resp == Gtk.ResponseType.OK:
                u = entry.get_text().strip().lstrip("@")
                self.cfg.username = u or None
                self.cfg.cache_enabled = cache_check.get_active()
                sel = style_dd.get_selected()
                self.cfg.cover_style = styles[sel] if 0 <= sel < len(styles) else "vinyl"
                self.cfg.spin_enabled = spin_check.get_active()
                self.cfg.save()
                self._update_user_ui()
                if getattr(self, "_cover_is_disc", False):
                    self.now_cover.set_style(self.cfg.cover_style)
                playing = self.player.state() == Gst.State.PLAYING
                self._cover_set_spinning(playing)
                self.status.set_text(
                    f"Preferencje: {self.cfg.cover_style}"
                    + (", spin" if self.cfg.spin_enabled else "")
                    + (", cache" if self.cfg.cache_enabled else ", stream")
                )
            elif resp == Gtk.ResponseType.REJECT:
                self.cfg.username = None
                self.cfg.save()
                self._update_user_ui()
                self.status.set_text("Wyczyszczono użytkownika")
            dlg.destroy()

        dialog.connect("response", on_resp)
        dialog.present()

    # ──────────── Artist nav ────────────
    def go_to_artist(self, permalink: str):
        if not permalink:
            return
        self.artist_username = permalink
        self.current_mode = "artist"
        self.current_param = permalink
        self.current_type = "tracks"
        self.page = 1
        self.view_label = f"Artysta: {permalink}"
        self.view_title.set_text(self.view_label)
        self._clear()
        self._fetch_artist_info(permalink)
        self._fetch()

    def _on_sidebar_artist(self, *_):
        if self.artist_username:
            self.go_to_artist(self.artist_username)

    def _on_now_artist(self, *_):
        if self._current_track:
            pl = artist_permalink(self._current_track)
            if pl:
                self.go_to_artist(pl)

    def on_my_uploads(self, *_):
        if not self.cfg.has_user:
            self.status.set_text("Ustaw nazwę użytkownika")
            return
        self.go_to_artist(self.cfg.username)
        self.view_label = "Moje uploady"
        self.view_title.set_text(self.view_label)

    # ──────────── API ────────────
    def load_genres(self):
        def w():
            data = fetch_json(f"{API_BASE}/categories/")
            genres = [g["id"] for g in (data or []) if isinstance(g, dict)]
            GLib.idle_add(self._set_genres, genres)

        threading.Thread(target=w, daemon=True).start()

    def _set_genres(self, genres):
        model = Gtk.StringList.new(genres or ["house"])
        self.genre_dd.set_model(model)
        if genres:
            self.genre_dd.set_selected(0)

    def load_feed(self, feed_type="popular"):
        self.current_mode = "feed"
        self.current_param = feed_type
        self.current_type = None
        self.page = 1
        self.view_label = "Popularne" if feed_type == "popular" else "Nowe"
        self.view_title.set_text(self.view_label)
        self._clear()
        self._fetch()

    def on_load_genre(self, *_):
        item = self.genre_dd.get_selected_item()
        if not item:
            return
        g = item.get_string()
        self.current_mode = "genre"
        self.current_param = g
        self.current_type = None
        self.page = 1
        self.view_label = f"Gatunek: {g}"
        self.view_title.set_text(self.view_label)
        self._clear()
        self._fetch()

    def on_search(self, *_):
        q = self.search_entry.get_text().strip()
        if q:
            self.go_to_artist(q)

    def load_artist_type(self, t):
        target = self.artist_username or (self.cfg.username if self.cfg.has_user else None)
        if not target:
            self.status.set_text("Wybierz artystę lub ustaw użytkownika")
            return
        self.artist_username = target
        self.current_mode = "artist"
        self.current_param = target
        self.current_type = t
        self.page = 1
        labels = {"tracks": "Utwory", "likes": "Lajki", "reshares": "Reshare"}
        self.view_label = f"{labels.get(t, t)}: {target}"
        self.view_title.set_text(self.view_label)
        self._clear()
        self._fetch()

    def on_prev(self, *_):
        if self.page > 1:
            self.page -= 1
            self._clear()
            self._fetch()

    def on_next(self, *_):
        self.page += 1
        self._clear()
        self._fetch()

    def on_more(self, *_):
        self.page += 1
        self._fetch(append=True)

    def _clear(self):
        self.tracks = []
        self.list_all.remove_all()

    def _fetch(self, append=False):
        mode, param, page, typ = (
            self.current_mode,
            self.current_param,
            self.page,
            self.current_type,
        )
        self._set_busy(True)

        def worker():
            if mode == "feed":
                url = f"{API_BASE}/feed/"
                params = {"page": page, "count": self.per_page, "type": param}
            elif mode == "genre":
                url = f"{API_BASE}/categories/{param}/"
                params = {"page": page, "count": self.per_page}
            elif mode == "artist":
                url = f"{API_BASE}/{param}/"
                params = {
                    "type": typ or "tracks",
                    "page": page,
                    "count": self.per_page,
                }
            else:
                GLib.idle_add(self._set_busy, False)
                return
            data = fetch_json(url, params)
            if not isinstance(data, list):
                data = []
            GLib.idle_add(self._fill, data, not append)
            GLib.idle_add(self.page_lbl.set_text, str(page))
            GLib.idle_add(self._after_fetch)

        threading.Thread(target=worker, daemon=True).start()

    def _after_fetch(self):
        self._set_busy(False)
        self._push_history()

    def _fetch_artist_info(self, username):
        def w():
            info = fetch_json(f"{API_BASE}/{username}/") or {}
            if isinstance(info, list) and info:
                info = {
                    "username": username,
                    "description": "",
                    "avatar_url": None,
                    "user": (info[0].get("user") or {}),
                }
            GLib.idle_add(self._update_artist, info, username)

        threading.Thread(target=w, daemon=True).start()

    def _update_artist(self, info, username=None):
        if isinstance(info, dict) and "user" in info and not info.get("username"):
            u = info.get("user") or {}
            name = u.get("username") or username or ""
            desc = info.get("description") or "(brak opisu)"
            av = u.get("avatar_url") or info.get("avatar_url")
        else:
            name = info.get("username") or info.get("permalink") or username or ""
            desc = info.get("description") or "(brak opisu)"
            av = info.get("avatar_url")
        self.artist_username = username or name
        self.artist_name_btn.set_label(name or username or "")
        self.desc.set_text(desc if isinstance(desc, str) else "(brak opisu)")
        if av:

            def load():
                tex = fetch_texture(av)
                GLib.idle_add(self.avatar.set_paintable, tex or placeholder_texture())

            threading.Thread(target=load, daemon=True).start()
        else:
            self.avatar.set_paintable(placeholder_texture())

    def _fill(self, tracks, clear=True):
        if clear:
            self._clear()
        for t in tracks:
            if isinstance(t, dict):
                self.tracks.append(t)
                self.list_all.append(TrackRow(t, on_artist_click=self.go_to_artist))

    # ──────────── Busy / cache UI ────────────
    def _set_busy(self, busy: bool):
        if busy:
            self.mini_icon.set_from_icon_name("content-loading-symbolic")
            self.mini_icon.set_tooltip_text("Ładowanie…")
        else:
            self.mini_icon.set_from_icon_name("media-optical-symbolic")
            self.mini_icon.set_tooltip_text("Gotowy")

    def _show_cache_progress(self, frac: float, text: str = ""):
        self.cache_bar.set_visible(True)
        self.mini_icon.set_from_icon_name("folder-download-symbolic")
        self.mini_icon.set_tooltip_text(text or "Cacheowanie…")
        if frac < 0:
            self.cache_bar.pulse()
            self.cache_bar.set_text("…")
        else:
            self.cache_bar.set_fraction(max(0.0, min(1.0, frac)))
            self.cache_bar.set_text(f"{int(frac * 100)}%")

    def _hide_cache_progress(self):
        self.cache_bar.set_visible(False)
        self.cache_bar.set_fraction(0)
        self._set_busy(False)

    def _stream_info_text(self, track: dict) -> str:
        if not track:
            return ""
        genre = track.get("genre") or track.get("category") or ""
        dur = fmt_dur(int(track.get("duration") or 0))
        plays = (track.get("counts") or {}).get("plays") or track.get("playback_count") or ""
        bits = []
        if genre:
            bits.append(str(genre))
        if dur and dur != "0:00":
            bits.append(dur)
        if plays:
            bits.append(f"{plays} plays")
        # stream hint
        if track.get("stream_url"):
            bits.append("stream")
        elif track.get("preview_url"):
            bits.append("preview")
        return " · ".join(bits)

    # ──────────── Playback ────────────
    def on_row_all(self, lb, row):
        if isinstance(row, TrackRow):
            idx = list(self.list_all).index(row)
            self._play("all", idx)

    def on_row_sel(self, lb, row):
        if isinstance(row, TrackRow):
            idx = list(self.list_sel).index(row)
            self._play("selected", idx)

    def _play(self, playlist, index):
        tracks = self.selected if playlist == "selected" else self.tracks
        if not (0 <= index < len(tracks)):
            return
        track = tracks[index]
        self.current_playlist = playlist
        self.current_index = index
        self._play_gen += 1
        gen = self._play_gen
        self._current_track = track

        title = track.get("title") or "Unknown"
        user = (track.get("user") or {}).get("username") or ""
        url = stream_url_of(track)
        known_dur = int(track.get("duration") or 0)
        bpm = track.get("bpm") or track.get("BPM") or (track.get("user") or {}).get("bpm") or 120
        local = track_cache_path(track)
        use_cache = self.cfg.cache_enabled

        def after_fade():
            if gen != self._play_gen:
                return
            self.now_title.set_text(title)
            self.now_artist_btn.set_label(user)
            self.stream_info.set_text(self._stream_info_text(track))
            self._set_now_cover(None)
            self.status.set_text("Stream…" if not (use_cache and local.exists()) else "Z cache…")
            try:
                self._cover_set_bpm(float(bpm))
            except Exception:
                self._cover_set_bpm(120)

            # okładka: lokalna z cache albo z sieci
            cpath = track_cover_path(track)
            art = track.get("artwork_url") or track.get("thumb") or track.get("artwork_url_retina")

            def load_art():
                pix = None
                if cpath.exists():
                    pix = load_pixbuf_from_url_or_file(path=str(cpath), max_side=COVER_SIZE)
                if pix is None and art:
                    pix = load_pixbuf_from_url_or_file(url=art, max_side=COVER_SIZE)
                if gen == self._play_gen:
                    GLib.idle_add(self._set_now_cover_pixbuf, pix)

            threading.Thread(target=load_art, daemon=True).start()

            # 1) lokalny cache jeśli włączony i plik jest
            if use_cache and local.exists() and local.stat().st_size > 1024:
                GLib.idle_add(self._start_playback, str(local), None, title, gen, known_dur, True)
                return

            # 2) stream od razu
            if not url:
                GLib.idle_add(self.status.set_text, "Brak URL streamu")
                GLib.idle_add(self._fade_now_in)
                return
            GLib.idle_add(self._start_playback, None, url, title, gen, known_dur, False)

            # 3) opcjonalnie cache w tle (nie blokuje odtwarzania)
            if use_cache:
                def bg_cache():
                    def prog(p):
                        if gen != self._play_gen:
                            return
                        GLib.idle_add(self._show_cache_progress, p, f"Cache: {title[:36]}")

                    with self._download_lock:
                        download_track(track, progress_cb=prog)
                    GLib.idle_add(self._hide_cache_progress)

                threading.Thread(target=bg_cache, daemon=True).start()

        Fade.out(self.now_box, 180, then=after_fade)

    def _fade_now_in(self):
        Fade.in_(self.now_box, 260)

    def _set_now_cover(self, tex):
        pix = texture_to_pixbuf(tex) if tex is not None else None
        self._set_now_cover_pixbuf(pix)

    def _set_now_cover_pixbuf(self, pix):
        if getattr(self, "_cover_is_disc", False):
            self.now_cover.set_cover_pixbuf(pix)
            self.now_cover.set_style(self.cfg.cover_style)
        else:
            if pix is not None:
                self.now_cover.set_paintable(Gdk.Texture.new_for_pixbuf(pix))
            else:
                ph = placeholder_texture()
                if ph:
                    self.now_cover.set_paintable(ph)

    def _cover_set_spinning(self, on: bool):
        if getattr(self, "_cover_is_disc", False):
            self.now_cover.set_spinning(on and self.cfg.spin_enabled)

    def _cover_set_bpm(self, bpm):
        if getattr(self, "_cover_is_disc", False):
            self.now_cover.set_bpm(bpm)

    def _start_playback(self, path, url, title, gen, known_dur=0, from_cache=False):
        if gen != self._play_gen:
            return
        try:
            if path:
                self.player.play_file(path, known_duration=known_dur)
                src = "cache"
            else:
                self.player.play_url(url, known_duration=known_dur)
                src = "stream"
        except Exception as e:
            self.status.set_text(f"Błąd odtwarzania: {e}")
            self._fade_now_in()
            return
        self.btn_pp.set_icon_name("media-playback-pause-symbolic")
        self._hide_cache_progress()
        self.status.set_text(f"▶ {title}  [{src}]")
        if known_dur > 0:
            self.pos.set_range(0, known_dur)
            self.pos.set_sensitive(True)
        if getattr(self, "_cover_is_disc", False):
            self.now_cover.set_style(self.cfg.cover_style)
        self._cover_set_spinning(True)
        self._fade_now_in()

    def on_play_pause(self, *_):
        st = self.player.state()
        if st == Gst.State.PLAYING:
            self.player.pause()
            self.btn_pp.set_icon_name("media-playback-start-symbolic")
            self._cover_set_spinning(False)
        else:
            self.player.play()
            self.btn_pp.set_icon_name("media-playback-pause-symbolic")
            self._cover_set_spinning(True)

    def on_stop(self, *_):
        self._play_gen += 1
        self.player.stop()
        self._cover_set_spinning(False)
        self.btn_pp.set_icon_name("media-playback-start-symbolic")
        self.pos.set_value(0)
        self.time_lbl.set_text("00:00 / 00:00")
        self._hide_cache_progress()
        self.status.set_text("")

    def on_volume(self, scale):
        self.player.set_volume(scale.get_value() / 100.0)

    def on_mute(self, btn):
        self.player.set_muted(btn.get_active())
        btn.set_icon_name(
            "audio-volume-muted-symbolic"
            if btn.get_active()
            else "audio-volume-high-symbolic"
        )

    def on_seek(self, scale):
        if self.is_seeking:
            self.player.seek_seconds(int(scale.get_value()))

    def _tick(self):
        try:
            pos, dur = self.player.query_pos_dur()
            if dur > 0:
                self.pos.set_range(0, dur)
                self.pos.set_sensitive(True)
                if not self.is_seeking:
                    self.pos.set_value(pos)
            else:
                self.pos.set_sensitive(False)
            self.time_lbl.set_text(
                f"{pos//60:02d}:{pos%60:02d} / {dur//60:02d}:{dur%60:02d}"
            )
        except Exception:
            pass
        return True

    def _on_eos(self):
        nxt = self.current_index + 1
        tracks = self.selected if self.current_playlist == "selected" else self.tracks
        if nxt < len(tracks):
            GLib.idle_add(self._play, self.current_playlist, nxt)
        else:
            GLib.idle_add(self.on_stop)

    def on_add_selected(self, *_):
        row = self.list_all.get_selected_row()
        if isinstance(row, TrackRow):
            self.selected.append(row.track)
            self.list_sel.append(
                TrackRow(row.track, on_artist_click=self.go_to_artist)
            )


def main():
    app = HearThisApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
