#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HearThis Player – GTK4 + GStreamer (flump3dec + local cache)
+ feed subskrypcji, klikalni artyści, własne uploady
"""

import sys
import json
import hashlib
import threading
from pathlib import Path

import requests
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gio, GLib, Gdk, Gst, GObject, GdkPixbuf

Gst.init(None)

API_BASE = "https://api-v2.hearthis.at"
CACHE_DIR = Path.home() / ".cache" / "hearthis_player"
AUDIO_CACHE = CACHE_DIR / "audio"
CONFIG_FILE = CACHE_DIR / "config.json"

for d in (CACHE_DIR, AUDIO_CACHE):
    d.mkdir(parents=True, exist_ok=True)

# ─────────────────────────── Config / Auth ───────────────────────────
class Config:
    def __init__(self):
        self.key = None
        self.secret = None
        self.username = None
        self.load()

    def load(self):
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                self.key = data.get("key")
                self.secret = data.get("secret")
                self.username = data.get("username")
            except Exception:
                pass

    def save(self):
        CONFIG_FILE.write_text(json.dumps({
            "key": self.key, "secret": self.secret, "username": self.username
        }, indent=2), encoding="utf-8")

    def clear(self):
        self.key = self.secret = self.username = None
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink(missing_ok=True)

    @property
    def logged_in(self):
        return bool(self.key and self.secret)

# ─────────────────────────── Helpers ───────────────────────────
def fetch_json(url, params=None, auth=None, timeout=20):
    params = dict(params or {})
    if auth and auth.key and auth.secret:
        params["key"] = auth.key
        params["secret"] = auth.secret
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"API error {url}: {e}")
        return None

def post_json(url, data, timeout=15):
    try:
        r = requests.post(url, data=data, timeout=timeout)
        return r.json()
    except Exception as e:
        print(f"POST error: {e}")
        return None

def track_cache_path(track: dict) -> Path:
    tid = str(track.get("id") or track.get("permalink") or
              hashlib.md5((track.get("stream_url") or "").encode()).hexdigest())
    return AUDIO_CACHE / f"{tid}.mp3"

def download_track(track: dict, progress_cb=None) -> Path | None:
    path = track_cache_path(track)
    if path.exists() and path.stat().st_size > 1024:
        return path
    url = track.get("stream_url") or track.get("download_url") or track.get("preview_url")
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
                            progress_cb(done / total)
        return path
    except Exception as e:
        print("Download failed:", e)
        path.unlink(missing_ok=True)
        return None

def texture_from_bytes(b: bytes):
    if not b:
        return None
    try:
        loader = GdkPixbuf.PixbufLoader.new()
        loader.write(b)
        loader.close()
        pix = loader.get_pixbuf()
        return Gdk.Texture.new_for_pixbuf(pix) if pix else None
    except Exception:
        return None

def fetch_texture(url: str):
    if not url or not str(url).startswith("http"):
        return None
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        return texture_from_bytes(r.content)
    except Exception:
        return None

def placeholder_texture():
    try:
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon = theme.lookup_icon("avatar-default-symbolic", [], 96, 1,
                                 Gtk.TextDirection.NONE, Gtk.IconLookupFlags.FORCE_SIZE)
        if icon:
            pix = icon.load_icon()
            if pix:
                return Gdk.Texture.new_for_pixbuf(pix)
    except Exception:
        pass
    return None

def artist_permalink(track: dict) -> str | None:
    """Zwraca permalink artysty z obiektu track."""
    user = track.get("user") or {}
    return user.get("permalink") or user.get("username")

# ─────────────────────────── Track Row (klikalny artysta) ───────────────────────────
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
        dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else ""

        self.pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER)
        self.pic.set_size_request(48, 48)

        title_l = Gtk.Label(label=title, xalign=0, ellipsize=3)
        title_l.add_css_class("heading")

        # Klikalny artysta
        artist_btn = Gtk.Button(label=user or "—", css_classes=["flat", "link"])
        artist_btn.set_halign(Gtk.Align.START)
        artist_btn.set_has_frame(False)
        if user and on_artist_click:
            artist_btn.connect("clicked", self._artist_clicked)

        meta = Gtk.Label(label=f"{genre}  ·  {dur_str}".strip(" ·"),
                         xalign=0, ellipsize=3)
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

        art = (track.get("artwork_url") or track.get("thumb") or
               (track.get("images") or {}).get("thumbnail"))
        if art:
            threading.Thread(target=self._load, args=(art,), daemon=True).start()

    def _artist_clicked(self, *_):
        permalink = artist_permalink(self.track)
        if permalink and self.on_artist_click:
            self.on_artist_click(permalink)

    def _load(self, url):
        tex = fetch_texture(url)
        if tex:
            GLib.idle_add(self.pic.set_paintable, tex)

# ─────────────────────────── GStreamer (local file) ───────────────────────────
class LocalGstPlayer(GObject.GObject):
    def __init__(self):
        super().__init__()
        self.pipeline = None
        self.on_eos = None
        self._volume = 1.0
        self._muted = False

    def _build(self, filepath: str):
        self.stop()
        desc = (
            f'filesrc location="{filepath}" ! '
            f'flump3dec ! audioconvert ! audioresample ! '
            f'volume name=vol ! pipewiresink'
        )
        self.pipeline = Gst.parse_launch(desc)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_msg)
        vol = self.pipeline.get_by_name("vol")
        if vol:
            vol.set_property("volume", 0.0 if self._muted else self._volume)

    def _on_msg(self, bus, msg):
        if msg.type == Gst.MessageType.EOS and self.on_eos:
            self.on_eos()
        elif msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print("GStreamer ERROR:", err, dbg)

    def play_file(self, path: str):
        self._build(path)
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
            return 0, 0
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        ok2, dur = self.pipeline.query_duration(Gst.Format.TIME)
        return (int(pos // Gst.SECOND) if ok else 0,
                int(dur // Gst.SECOND) if ok2 else 0)

    def seek_seconds(self, sec: int):
        if self.pipeline:
            self.pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                max(0, sec) * Gst.SECOND,
            )

# ─────────────────────────── Main App ───────────────────────────
class HearThisApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.hearthis.Player",
                         flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.connect("activate", self.on_activate)

        self.cfg = Config()
        self.player = LocalGstPlayer()
        self.player.on_eos = self._on_eos

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
        self._download_lock = threading.Lock()

    def on_activate(self, app):
        self.win = Gtk.ApplicationWindow(application=app, title="HearThis Player")
        self.win.set_default_size(1220, 800)

        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .title-large { font-size: 1.35em; font-weight: bold; }
            .now-playing { font-weight: 600; }
            button.link { padding: 0; min-height: 0; }
            button.link label { color: @accent_color; text-decoration: underline; }
            row:selected { background: alpha(@accent_bg_color, 0.22); }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        header = Gtk.HeaderBar()
        self.win.set_titlebar(header)

        self.search_entry = Gtk.Entry(placeholder_text="Szukaj artysty / tytułu…", width_chars=28)
        self.search_entry.connect("activate", self.on_search)
        header.pack_start(self.search_entry)
        btn_s = Gtk.Button(icon_name="system-search-symbolic")
        btn_s.connect("clicked", self.on_search)
        header.pack_start(btn_s)

        self.login_box = Gtk.Box(spacing=6)
        self.user_label = Gtk.Label(label="Gość")
        self.btn_login = Gtk.Button(label="Zaloguj")
        self.btn_login.connect("clicked", self.on_login_clicked)
        self.login_box.append(self.user_label)
        self.login_box.append(self.btn_login)
        header.pack_end(self.login_box)

        self.mute_btn = Gtk.ToggleButton(icon_name="audio-volume-high-symbolic")
        self.mute_btn.connect("toggled", self.on_mute)
        header.pack_end(self.mute_btn)
        self.vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.vol.set_value(100)
        self.vol.set_size_request(100, -1)
        self.vol.connect("value-changed", self.on_volume)
        header.pack_end(self.vol)
        self.btn_stop = Gtk.Button(icon_name="media-playback-stop-symbolic")
        self.btn_stop.connect("clicked", self.on_stop)
        header.pack_end(self.btn_stop)
        self.btn_pp = Gtk.Button(icon_name="media-playback-start-symbolic")
        self.btn_pp.connect("clicked", self.on_play_pause)
        header.pack_end(self.btn_pp)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_hexpand(True)
        paned.set_vexpand(True)
        paned.set_position(740)

        # LEFT
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.notebook = Gtk.Notebook()
        self.notebook.set_hexpand(True)
        self.notebook.set_vexpand(True)

        scroll_all = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.list_all = Gtk.ListBox()
        self.list_all.add_css_class("boxed-list")
        self.list_all.connect("row-activated", self.on_row_all)
        scroll_all.set_child(self.list_all)
        self.notebook.append_page(scroll_all, Gtk.Label(label="Wszystkie"))

        scroll_sel = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.list_sel = Gtk.ListBox()
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

        # RIGHT
        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_min_content_width(330)
        right_scroll.set_max_content_width(430)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        right.set_margin_start(12)
        right.set_margin_end(12)
        right.set_margin_top(12)
        right.set_margin_bottom(12)

        # Feed buttons
        feeds = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, mode in (("Popularne", "popular"), ("Nowe", "new")):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _, m=mode: self.load_feed(m))
            feeds.append(b)
        self.btn_subs = Gtk.Button(label="Subskrypcje")
        self.btn_subs.connect("clicked", lambda *_: self.load_feed("subscriptions"))
        self.btn_subs.set_tooltip_text("Feed subskrypcji (wymaga logowania)")
        feeds.append(self.btn_subs)
        right.append(feeds)

        # Own uploads (po zalogowaniu)
        self.btn_my = Gtk.Button(label="Moje uploady")
        self.btn_my.connect("clicked", self.on_my_uploads)
        self.btn_my.set_sensitive(False)
        right.append(self.btn_my)

        # Genre
        gbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.genre_dd = Gtk.DropDown()
        self.genre_dd.set_hexpand(True)
        btn_g = Gtk.Button(label="Gatunek")
        btn_g.connect("clicked", self.on_load_genre)
        gbox.append(self.genre_dd)
        gbox.append(btn_g)
        right.append(gbox)

        # Artist type buttons
        abox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for lab, t in (("Utwory", "tracks"), ("Lajki", "likes"), ("Reshare", "reshares")):
            b = Gtk.Button(label=lab)
            b.connect("clicked", lambda _, tt=t: self.load_artist_type(tt))
            abox.append(b)
        right.append(abox)

        right.append(Gtk.Label(label="Artysta", xalign=0, css_classes=["title-4"]))
        self.avatar = Gtk.Picture(content_fit=Gtk.ContentFit.COVER)
        self.avatar.set_size_request(140, 140)
        self.avatar.set_paintable(placeholder_texture())
        right.append(self.avatar)

        # Klikalna nazwa artysty w panelu
        self.artist_name_btn = Gtk.Button(label="", css_classes=["flat", "link"])
        self.artist_name_btn.set_halign(Gtk.Align.START)
        self.artist_name_btn.connect("clicked", self._on_sidebar_artist)
        right.append(self.artist_name_btn)

        self.desc = Gtk.Label(xalign=0, wrap=True, wrap_mode=Gtk.WrapMode.WORD)
        self.desc.add_css_class("dim-label")
        self.desc.set_max_width_chars(38)
        right.append(self.desc)

        right.append(Gtk.Label(label="Teraz gra", xalign=0, css_classes=["title-4"]))
        self.now_cover = Gtk.Picture(content_fit=Gtk.ContentFit.SCALE_DOWN)
        self.now_cover.set_size_request(220, 220)
        right.append(self.now_cover)
        self.now_title = Gtk.Label(xalign=0, wrap=True, css_classes=["now-playing"])
        # Klikalny artysta w Now Playing
        self.now_artist_btn = Gtk.Button(label="", css_classes=["flat", "link"])
        self.now_artist_btn.set_halign(Gtk.Align.START)
        self.now_artist_btn.connect("clicked", self._on_now_artist)
        right.append(self.now_title)
        right.append(self.now_artist_btn)

        self.status = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"])
        right.append(self.status)

        right_scroll.set_child(right)
        paned.set_end_child(right_scroll)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bottom.set_margin_start(12)
        bottom.set_margin_end(12)
        bottom.set_margin_top(8)
        bottom.set_margin_bottom(10)
        self.pos = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.pos.set_hexpand(True)
        self.pos.set_draw_value(False)
        self.pos.connect("value-changed", self.on_seek)
        gest = Gtk.GestureClick()
        gest.connect("pressed", lambda *a: setattr(self, "is_seeking", True))
        gest.connect("released", lambda *a: setattr(self, "is_seeking", False))
        self.pos.add_controller(gest)
        self.time_lbl = Gtk.Label(label="00:00 / 00:00")
        bottom.append(self.pos)
        bottom.append(self.time_lbl)

        main.append(paned)
        main.append(bottom)
        self.win.set_child(main)
        self.win.present()

        self._update_login_ui()
        self.load_genres()
        self.load_feed("popular")
        GLib.timeout_add(250, self._tick)

    # ──────────── Auth ────────────
    def _update_login_ui(self):
        if self.cfg.logged_in:
            self.user_label.set_text(self.cfg.username or "Zalogowany")
            self.btn_login.set_label("Wyloguj")
            self.btn_my.set_sensitive(True)
            self.btn_subs.set_sensitive(True)
        else:
            self.user_label.set_text("Gość")
            self.btn_login.set_label("Zaloguj")
            self.btn_my.set_sensitive(False)

    def on_login_clicked(self, *_):
        if self.cfg.logged_in:
            self.cfg.clear()
            self._update_login_ui()
            return
        dialog = Gtk.Dialog(title="Logowanie hearthis.at", transient_for=self.win, modal=True)
        dialog.add_button("Anuluj", Gtk.ResponseType.CANCEL)
        dialog.add_button("Zaloguj", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(10)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(16)
        box.set_margin_bottom(8)
        email = Gtk.Entry(placeholder_text="E-mail", hexpand=True)
        pwd = Gtk.Entry(placeholder_text="Hasło", visibility=False, hexpand=True)
        box.append(email)
        box.append(pwd)

        def on_resp(dlg, resp):
            if resp == Gtk.ResponseType.OK:
                e, p = email.get_text().strip(), pwd.get_text()
                if e and p:
                    self._do_login(e, p)
            dlg.destroy()
        dialog.connect("response", on_resp)
        dialog.present()

    def _do_login(self, email, password):
        def worker():
            data = post_json(f"{API_BASE}/login", {"email": email, "password": password})
            if data and data.get("success") is not False:
                payload = data.get("data") or data
                key = payload.get("key")
                secret = payload.get("secret")
                user = payload.get("username") or payload.get("user") or payload.get("permalink")
                if isinstance(user, dict):
                    user = user.get("username") or user.get("permalink")
                if key and secret:
                    self.cfg.key = key
                    self.cfg.secret = secret
                    self.cfg.username = user if isinstance(user, str) else email.split("@")[0]
                    self.cfg.save()
                    GLib.idle_add(self._update_login_ui)
                    GLib.idle_add(self.status.set_text, f"Zalogowano jako {self.cfg.username}")
                    return
            msg = (data or {}).get("message") or "Błąd logowania"
            GLib.idle_add(self.status.set_text, str(msg))
        threading.Thread(target=worker, daemon=True).start()

    # ──────────── Navigation / Artist links ────────────
    def go_to_artist(self, permalink: str):
        """Przekierowanie na profil artysty (hiperłącze)."""
        if not permalink:
            return
        self.artist_username = permalink
        self.current_mode = "artist"
        self.current_param = permalink
        self.current_type = "tracks"
        self.page = 1
        self._clear()
        self._fetch_artist_info(permalink)
        self._fetch()
        self.status.set_text(f"Artysta: {permalink}")

    def _on_sidebar_artist(self, *_):
        if self.artist_username:
            self.go_to_artist(self.artist_username)

    def _on_now_artist(self, *_):
        # z aktualnie granego tracku
        tracks = self.selected if self.current_playlist == "selected" else self.tracks
        if 0 <= self.current_index < len(tracks):
            pl = artist_permalink(tracks[self.current_index])
            if pl:
                self.go_to_artist(pl)

    def on_my_uploads(self, *_):
        if not self.cfg.logged_in or not self.cfg.username:
            self.status.set_text("Zaloguj się, aby zobaczyć własne uploady")
            return
        self.go_to_artist(self.cfg.username)
        self.status.set_text("Moje uploady")

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
        """popular | new | subscriptions (feed subskrypcji)"""
        self.current_mode = "feed"
        self.current_param = feed_type
        self.current_type = None
        self.page = 1
        self._clear()
        if feed_type == "subscriptions" and not self.cfg.logged_in:
            self.status.set_text("Subskrypcje wymagają logowania")
            return
        self._fetch()

    def on_load_genre(self, *_):
        item = self.genre_dd.get_selected_item()
        if not item:
            return
        self.current_mode = "genre"
        self.current_param = item.get_string()
        self.current_type = None
        self.page = 1
        self._clear()
        self._fetch()

    def on_search(self, *_):
        q = self.search_entry.get_text().strip()
        if not q:
            return
        # traktuj jako artystę (permalink)
        self.go_to_artist(q)

    def load_artist_type(self, t):
        if not self.artist_username:
            return
        self.current_mode = "artist"
        self.current_param = self.artist_username
        self.current_type = t
        self.page = 1
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
        mode, param, page, typ = self.current_mode, self.current_param, self.page, self.current_type

        def worker():
            if mode == "feed":
                url = f"{API_BASE}/feed/"
                params = {"page": page, "count": self.per_page}
                if param == "subscriptions":
                    # feed subskrypcji – bez type, z auth
                    pass
                elif param in ("popular", "new"):
                    params["type"] = param
            elif mode == "genre":
                url = f"{API_BASE}/categories/{param}/"
                params = {"page": page, "count": self.per_page}
            elif mode == "artist":
                url = f"{API_BASE}/{param}/"
                params = {"type": typ or "tracks", "page": page, "count": self.per_page}
            else:
                return
            data = fetch_json(url, params, self.cfg)
            if not isinstance(data, list):
                data = []
            GLib.idle_add(self._fill, data, not append)
            GLib.idle_add(self.page_lbl.set_text, str(page))
        threading.Thread(target=worker, daemon=True).start()

    def _fetch_artist_info(self, username):
        def w():
            info = fetch_json(f"{API_BASE}/{username}/", auth=self.cfg) or {}
            # czasem API zwraca listę tracków zamiast profilu – wtedy bierzemy user z pierwszego
            if isinstance(info, list) and info:
                info = {"username": username, "description": "", "avatar_url": None,
                        "user": (info[0].get("user") or {})}
            GLib.idle_add(self._update_artist, info, username)
        threading.Thread(target=w, daemon=True).start()

    def _update_artist(self, info, username=None):
        if isinstance(info, dict) and "user" in info and not info.get("username"):
            # fallback
            u = info.get("user") or {}
            name = u.get("username") or username or ""
            desc = info.get("description") or "(brak opisu)"
            av = u.get("avatar_url") or info.get("avatar_url")
        else:
            name = (info.get("username") or info.get("permalink") or username or "")
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

        title = track.get("title") or "Unknown"
        user = (track.get("user") or {}).get("username") or ""
        self.now_title.set_text(title)
        self.now_artist_btn.set_label(user)
        self.status.set_text("Pobieranie…")

        art = track.get("artwork_url") or track.get("thumb")
        if art:
            threading.Thread(target=lambda: GLib.idle_add(
                self.now_cover.set_paintable, fetch_texture(art)), daemon=True).start()

        def worker():
            with self._download_lock:
                path = download_track(track, lambda p: GLib.idle_add(
                    self.status.set_text, f"Pobieranie {int(p * 100)}%"))
            if path:
                GLib.idle_add(self._start_file, str(path), title)
            else:
                GLib.idle_add(self.status.set_text, "Błąd pobierania")
        threading.Thread(target=worker, daemon=True).start()

    def _start_file(self, path, title):
        self.player.play_file(path)
        self.btn_pp.set_icon_name("media-playback-pause-symbolic")
        self.status.set_text(f"▶ {title}")

    def on_play_pause(self, *_):
        st = self.player.state()
        if st == Gst.State.PLAYING:
            self.player.pause()
            self.btn_pp.set_icon_name("media-playback-start-symbolic")
        else:
            self.player.play()
            self.btn_pp.set_icon_name("media-playback-pause-symbolic")

    def on_stop(self, *_):
        self.player.stop()
        self.btn_pp.set_icon_name("media-playback-start-symbolic")
        self.pos.set_value(0)
        self.time_lbl.set_text("00:00 / 00:00")
        self.status.set_text("")

    def on_volume(self, scale):
        self.player.set_volume(scale.get_value() / 100.0)

    def on_mute(self, btn):
        self.player.set_muted(btn.get_active())
        btn.set_icon_name("audio-volume-muted-symbolic" if btn.get_active()
                          else "audio-volume-high-symbolic")

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
            self.time_lbl.set_text(f"{pos//60:02d}:{pos%60:02d} / {dur//60:02d}:{dur%60:02d}")
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
            self.list_sel.append(TrackRow(row.track, on_artist_click=self.go_to_artist))

def main():
    app = HearThisApp()
    return app.run(sys.argv)

if __name__ == "__main__":
    sys.exit(main())
