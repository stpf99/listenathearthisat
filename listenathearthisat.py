#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HearThis Player – GTK4 (PyGObject) + GStreamer
- Scalable window (no fixed size) - GUI adapts to window size
- Auto-play next track in active playlist (All / Selected)
- Cover art from ID3 (GStreamer TAG image/preview-image), fallback to artwork_url from API
- Horizontal and vertical scrolling in GUI areas
- Improved layout with balanced proportions using Gtk.Paned and constrained sizes
- Fixed seek bar synchronization with streamed tracks using Gtk.GestureClick for interaction
- Improved artist info layout with proper avatar placeholder (themed icon via lookup_icon) and readable description
- Added pagination for artist tracks, likes, reshares, and search results using prev/next/load more buttons

Requirements (Debian/Ubuntu example):
  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-gst-plugins-base-1.0 gir1.2-gstreamer-1.0 \
                   gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav \
                   python3-gi-cairo gir1.2-gdkpixbuf-2.0

Run:
  python3 listenhearthisat.py
"""

import sys
import json
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

# ------------- Helper cache -------------
class Cache:
    def __init__(self, cache_dir=".cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace(" ", "_")
        return self.cache_dir / f"{safe}.json"

    def get(self, key: str):
        p = self._path(key)
        if p.exists():
            try:
                with p.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None
        return None

    def set(self, key: str, data):
        p = self._path(key)
        try:
            with p.open("w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

# ---------- Image utilities ----------
def fetch_image_bytes(url: str, timeout=15):
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.content
    except Exception:
        return None

def texture_from_bytes(b: bytes):
    if not b:
        return None
    try:
        loader = GdkPixbuf.PixbufLoader.new()
        loader.write(b)
        loader.close()
        pixbuf = loader.get_pixbuf()
        if not pixbuf:
            return None
        tex = Gdk.Texture.new_for_pixbuf(pixbuf)
        return tex
    except Exception:
        return None

def get_placeholder_texture():
    try:
        # Load themed icon as fallback
        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon_info = icon_theme.lookup_icon(
            "avatar-default-symbolic",
            [],
            96,
            1,
            Gtk.TextDirection.NONE,
            Gtk.IconLookupFlags.FORCE_SIZE
        )
        if icon_info:
            pixbuf = icon_info.load_icon()
            if pixbuf:
                return Gdk.Texture.new_for_pixbuf(pixbuf)
    except Exception as e:
        print("Placeholder icon error:", e)
    return None

# ---------- Track row with cover ----------
class TrackRow(Gtk.ListBoxRow):
    __gtype_name__ = "TrackRow"

    def __init__(self, track: dict):
        super().__init__()
        self.track = track
        self.set_selectable(True)

        title = track.get("title") or "Unknown"
        user = track.get("user", {}).get("username") or ""
        genre = track.get("genre") or ""

        # Cover (thumbnail)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER)
        self.picture.set_size_request(56, 56)

        # Text
        title_lbl = Gtk.Label(label=title, xalign=0)
        title_lbl.add_css_class("title-3")

        subtitle_text = user if user else genre
        subtitle_lbl = Gtk.Label(label=subtitle_text, xalign=0)
        subtitle_lbl.add_css_class("dim-label")

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.append(title_lbl)
        vbox.append(subtitle_lbl)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        hbox.set_margin_top(6)
        hbox.set_margin_bottom(6)
        hbox.set_margin_start(6)
        hbox.set_margin_end(6)
        hbox.append(self.picture)
        hbox.append(vbox)
        self.set_child(hbox)

        # Load cover from API (fallback - ID3 during playback)
        art_url = (
            track.get("artwork_url") or
            track.get("thumb") or
            track.get("images", {}).get("thumbnail") or
            track.get("background") or
            track.get("waveform")
        )
        if art_url and art_url.startswith("http"):
            threading.Thread(target=self._load_cover, args=(art_url,), daemon=True).start()
        else:
            print(f"No valid artwork URL for track: {title}")

    def _load_cover(self, url):
        b = fetch_image_bytes(url)
        tex = texture_from_bytes(b)
        if tex:
            GLib.idle_add(self.picture.set_paintable, tex)

# ---------- GStreamer adapter ----------
class GstPlayer(GObject.GObject):
    __gtype_name__ = "GstPlayer"

    def __init__(self):
        super().__init__()
        self.playbin = Gst.ElementFactory.make("playbin", "player")
        # Signals from bus (EOS, TAG, etc.)
        self.bus = self.playbin.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self._on_bus_message)
        # about-to-finish for smooth transition
        self.playbin.connect("about-to-finish", self._on_about_to_finish)

        # External callbacks
        self.on_eos = None
        self.on_tags = None
        self.on_about_to_finish = None

        # Current queue for auto-play
        self.next_uri_provider = None

    def set_uri(self, uri: str):
        self.playbin.set_property("uri", uri)

    def play(self):
        self.playbin.set_state(Gst.State.PLAYING)

    def pause(self):
        self.playbin.set_state(Gst.State.PAUSED)

    def stop(self):
        self.playbin.set_state(Gst.State.NULL)

    def state(self):
        return self.playbin.get_state(Gst.StateChangeReturn.SUCCESS).state

    def set_volume(self, v: float):
        v = max(0.0, min(1.0, v))
        self.playbin.set_property("volume", v)

    def get_volume(self) -> float:
        try:
            return float(self.playbin.get_property("volume"))
        except Exception:
            return 1.0

    def set_muted(self, muted: bool):
        try:
            self.playbin.set_property("mute", muted)
        except Exception:
            pass

    def query_pos_dur(self):
        pos_ok, pos = self.playbin.query_position(Gst.Format.TIME)
        dur_ok, dur = self.playbin.query_duration(Gst.Format.TIME)
        if not pos_ok:
            pos = 0
        if not dur_ok:
            dur = 0
        return int(pos // Gst.SECOND), int(dur // Gst.SECOND)

    def seek_seconds(self, sec: int):
        sec = max(0, sec)
        self.playbin.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            sec * Gst.SECOND,
        )

    # --- BUS & TAGS ---
    def _on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            if self.on_eos:
                self.on_eos()
        elif t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print("GStreamer ERROR:", err, dbg)
        elif t == Gst.MessageType.TAG:
            taglist = message.parse_tag()
            if self.on_tags:
                self.on_tags(taglist)

    def _on_about_to_finish(self, playbin):
        if self.on_about_to_finish and callable(self.on_about_to_finish):
            self.on_about_to_finish(self)

    # Assign function that returns next URI
    def set_next_uri_provider(self, fn):
        self.next_uri_provider = fn

# ---------- Main window ----------
class HearThisApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.example.HearThisGTK4",
                         flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.connect("activate", self.on_activate)

        # State
        self.current_mode = ''
        self.current_param = ''
        self.current_type = None
        self.current_page = 1
        self.tracks_per_page = 20
        self.is_seeking = False  # Track user interaction with seek bar

        self.cache = Cache()
        self.player = GstPlayer()

        # Lists and auto-next
        self.local_tracks = []    # All
        self.selected_tracks = [] # Selected
        self.current_playlist = "all"   # "all" | "selected"
        self.current_index = -1

        # Cover from tags (last)
        self.last_stream_texture = None

        # Assign GStreamer callbacks
        self.player.on_eos = self._on_eos
        self.player.on_tags = self._on_gst_tags
        self.player.on_about_to_finish = self._on_about_to_finish

    def on_activate(self, app):
        # Main window
        self.win = Gtk.ApplicationWindow(application=app)
        self.win.set_title("HearThis (GTK4)")
        self.win.set_default_size(1100, 720)
        self.win.set_resizable(True)

        # Headerbar
        header = Gtk.HeaderBar()
        self.win.set_titlebar(header)

        # Left side of header
        self.search_entry = Gtk.Entry(placeholder_text="Search Artist (username)")
        self.search_entry.set_width_chars(22)
        header.pack_start(self.search_entry)

        btn_search_artist = Gtk.Button(icon_name="system-search-symbolic", tooltip_text="Search Artist")
        btn_search_artist.connect("clicked", self.on_search_artist)
        header.pack_start(btn_search_artist)

        self.search_on_entry = Gtk.Entry(placeholder_text="Search on hearthis.at (title/tags)")
        self.search_on_entry.set_width_chars(26)
        header.pack_start(self.search_on_entry)

        btn_search_on = Gtk.Button(icon_name="edit-find-symbolic", tooltip_text="Search on hearthis.at")
        btn_search_on.connect("clicked", self.on_search_on_platform)
        header.pack_start(btn_search_on)

        # Right side of header
        self.mute_btn = Gtk.ToggleButton(icon_name="audio-volume-high-symbolic", tooltip_text="Mute")
        self.mute_btn.connect("toggled", self.on_toggle_mute)
        header.pack_end(self.mute_btn)

        self.volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume_scale.set_value(100)
        self.volume_scale.set_size_request(120, -1)
        self.volume_scale.connect("value-changed", self.on_volume_changed)
        header.pack_end(self.volume_scale)

        self.stop_btn = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Stop")
        self.stop_btn.connect("clicked", self.on_stop)
        header.pack_end(self.stop_btn)

        self.playpause_btn = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play/Pause")
        self.playpause_btn.connect("clicked", self.on_play_pause)
        header.pack_end(self.playpause_btn)

        # --- Main layout ---
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main.set_margin_top(12)
        main.set_margin_bottom(12)
        main.set_margin_start(12)
        main.set_margin_end(12)

        # Content area with Paned for resizable split
        content_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        content_paned.set_hexpand(True)
        content_paned.set_vexpand(True)
        content_paned.set_position(600)  # Initial split: 60% for tracks, 40% for sidebar

        # Left side: Track lists
        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_hexpand(True)
        left_scroll.set_vexpand(True)

        self.notebook = Gtk.Notebook()

        # ScrolledWindow for "All Tracks"
        scroll_all = Gtk.ScrolledWindow()
        scroll_all.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll_all.set_hexpand(True)
        scroll_all.set_vexpand(True)

        self.list_all = Gtk.ListBox()
        self.list_all.add_css_class("boxed-list")
        self.list_all.connect("row-activated", self.on_row_activated)
        scroll_all.set_child(self.list_all)

        # ScrolledWindow for "Selected Tracks"
        scroll_selected = Gtk.ScrolledWindow()
        scroll_selected.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll_selected.set_hexpand(True)
        scroll_selected.set_vexpand(True)

        self.list_selected = Gtk.ListBox()
        self.list_selected.add_css_class("boxed-list")
        self.list_selected.connect("row-activated", self.on_row_activated_selected)
        scroll_selected.set_child(self.list_selected)

        self.notebook.append_page(scroll_all, Gtk.Label(label="All Tracks"))
        self.notebook.append_page(scroll_selected, Gtk.Label(label="Selected Tracks"))

        left_scroll.set_child(self.notebook)
        content_paned.set_start_child(left_scroll)

        # Right side: Sidebar
        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        right_scroll.set_min_content_width(300)
        right_scroll.set_max_content_width(400)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        right.set_margin_start(8)
        right.set_margin_end(8)
        right.set_margin_top(8)
        right.set_margin_bottom(8)

        # Artist info
        info_title = Gtk.Label(label="Artist Info", xalign=0)
        info_title.add_css_class("title-4")

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.artist_avatar = Gtk.Picture(content_fit=Gtk.ContentFit.COVER)
        self.artist_avatar.set_size_request(120, 120)
        self.artist_avatar.set_paintable(get_placeholder_texture())  # Set placeholder
        self.artist_desc = Gtk.Label(xalign=0)
        self.artist_desc.set_wrap(True)
        self.artist_desc.set_wrap_mode(Gtk.WrapMode.WORD)
        self.artist_desc.set_width_chars(35)
        self.artist_desc.set_max_width_chars(35)
        self.artist_desc.add_css_class("dim-label")

        scroll_artist = Gtk.ScrolledWindow()
        scroll_artist.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll_artist.set_min_content_height(120)
        scroll_artist.set_max_content_height(200)
        scroll_artist.set_hexpand(True)
        scroll_artist.set_child(self.artist_desc)

        info_box.append(self.artist_avatar)
        info_box.append(scroll_artist)

        # Load buttons for artist
        load_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_tracks = Gtk.Button(label="Tracks")
        btn_tracks.connect("clicked", lambda *_: self.load_artist_tracks("tracks"))
        btn_likes = Gtk.Button(label="Likes")
        btn_likes.connect("clicked", lambda *_: self.load_artist_tracks("likes"))
        btn_reshares = Gtk.Button(label="Reshares")
        btn_reshares.connect("clicked", lambda *_: self.load_artist_tracks("reshares"))
        load_row.append(btn_tracks)
        load_row.append(btn_likes)
        load_row.append(btn_reshares)

        # Genres + pagination
        genre_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        genre_row.append(Gtk.Label(label="Genre:", xalign=0))
        self.genre_dropdown = Gtk.DropDown()
        self.genre_dropdown.set_hexpand(True)
        genre_row.append(self.genre_dropdown)

        btn_load_genre = Gtk.Button(label="Load Genre")
        btn_load_genre.connect("clicked", self.on_load_genre)

        page_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btn_prev = Gtk.Button(label="⟨ Prev")
        self.btn_prev.connect("clicked", self.on_prev_page)
        self.page_label = Gtk.Label(label="Page: 1")
        self.btn_next = Gtk.Button(label="Next ⟩")
        self.btn_next.connect("clicked", self.on_next_page)
        self.btn_more = Gtk.Button(label="Load More")
        self.btn_more.connect("clicked", self.on_load_more)
        page_row.append(self.btn_prev)
        page_row.append(self.page_label)
        page_row.append(self.btn_next)
        page_row.append(self.btn_more)

        # Add to selected
        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btn_add_selected = Gtk.Button(label="Add to Selected")
        self.btn_add_selected.connect("clicked", self.on_add_selected)
        add_row.append(self.btn_add_selected)

        # Now playing (with cover)
        now_title = Gtk.Label(label="Now Playing", xalign=0)
        now_title.add_css_class("title-4")
        self.now_cover = Gtk.Picture(content_fit=Gtk.ContentFit.SCALE_DOWN)
        self.now_cover.set_size_request(200, 200)

        self.now_title = Gtk.Label(xalign=0)
        self.now_title.add_css_class("title-4")
        self.now_meta = Gtk.Label(xalign=0)
        self.now_meta.add_css_class("dim-label")

        # Compose right panel
        for w in (
            info_title, info_box, load_row, genre_row, btn_load_genre,
            page_row, add_row, now_title, self.now_cover, self.now_title, self.now_meta
        ):
            right.append(w)

        right_scroll.set_child(right)
        content_paned.set_end_child(right_scroll)

        # Bottom - time and seek
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.pos_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.pos_scale.set_hexpand(True)
        self.pos_scale.set_draw_value(False)
        self.pos_scale.connect("value-changed", self.on_seek)

        # Add gesture controller for seek bar interaction
        gesture = Gtk.GestureClick()
        gesture.connect("pressed", self._on_seek_start)
        gesture.connect("released", self._on_seek_end)
        self.pos_scale.add_controller(gesture)

        self.time_label = Gtk.Label(label="00:00 / 00:00")
        bottom.append(self.pos_scale)
        bottom.append(self.time_label)

        main.append(content_paned)
        main.append(bottom)

        # Place main layout in window
        self.win.set_child(main)
        self.win.present()

        # Initial data
        self.load_genres()
        GLib.timeout_add(100, self.update_position)
        self.player.set_volume(1.0)

    # -------- Seek bar interaction --------
    def _on_seek_start(self, gesture, n_press, x, y):
        self.is_seeking = True
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_seek_end(self, gesture, n_press, x, y):
        self.is_seeking = False
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    # -------- API / Loading --------
    def _threaded(self, fn, *a, **kw):
        threading.Thread(target=fn, args=a, kwargs=kw, daemon=True).start()

    def fetch_artist_info(self, username):
        try:
            info = requests.get(f"{API_BASE}/{username}/", timeout=15).json()
        except Exception as e:
            info = {}
            print("Artist info error:", e)
        GLib.idle_add(self.update_artist_info, info)

    def on_search_artist(self, *_):
        username = self.search_entry.get_text().strip()
        if not username:
            return
        self.artist_username = username
        self.current_mode = 'artist'
        self.current_param = username
        self.current_type = 'tracks'
        self.current_page = 1
        self._clear_all_tracks()
        self._threaded(self.fetch_artist_info, username)
        self._threaded(self._fetch_page, 'artist', username, 1, 'tracks')

    def on_search_on_platform(self, *_):
        q = self.search_on_entry.get_text().strip()
        if not q:
            return
        self.current_mode = 'search'
        self.current_param = q
        self.current_type = None
        self.current_page = 1
        self._clear_all_tracks()
        GLib.idle_add(self.update_artist_info, {})
        self._threaded(self._fetch_page, 'search', q, 1)

    def load_artist_tracks(self, track_type="tracks"):
        if not self.artist_username:
            return
        self.current_mode = 'artist'
        self.current_param = self.artist_username
        self.current_type = track_type
        self.current_page = 1
        self._clear_all_tracks()
        self._threaded(self.fetch_artist_info, self.artist_username)
        self._threaded(self._fetch_page, 'artist', self.artist_username, 1, track_type)

    def load_genres(self):
        def worker():
            try:
                data = requests.get(f"{API_BASE}/categories/", timeout=15).json()
                genres = [g["id"] for g in data]
            except Exception as e:
                print("Genres error:", e)
                genres = []
            GLib.idle_add(self._set_genres, genres)
        self._threaded(worker)

    def _set_genres(self, genres):
        model = Gtk.StringList.new(genres)
        self.genre_dropdown.set_model(model)
        if genres:
            self.genre_dropdown.set_selected(0)
            self.selected_genre = genres[0]

    def on_load_genre(self, *_):
        si = self.genre_dropdown.get_selected_item()
        if not si:
            return
        self.selected_genre = si.get_string()
        self.current_mode = 'genre'
        self.current_param = self.selected_genre
        self.current_type = None
        self.current_page = 1
        self._clear_all_tracks()
        GLib.idle_add(self.update_artist_info, {})
        self._threaded(self._fetch_page, 'genre', self.selected_genre, 1)

    def on_prev_page(self, *_):
        if self.current_mode and self.current_page > 1:
            self.current_page -= 1
            self._clear_all_tracks()
            self._threaded(self._fetch_page, self.current_mode, self.current_param, self.current_page, self.current_type)

    def on_next_page(self, *_):
        if self.current_mode:
            self.current_page += 1
            self._clear_all_tracks()
            self._threaded(self._fetch_page, self.current_mode, self.current_param, self.current_page, self.current_type)

    def on_load_more(self, *_):
        if self.current_mode:
            self.current_page += 1
            self._threaded(self._fetch_page, self.current_mode, self.current_param, self.current_page, self.current_type, True)
            GLib.idle_add(self.page_label.set_text, f"Page: {self.current_page}")

    def _fetch_page(self, mode, param, page, type_=None, append=False):
        if mode == 'genre':
            url = f"{API_BASE}/categories/{param}/"
            params = {"page": page, "count": self.tracks_per_page}
            key = f"{mode}_{param}_page{page}"
        elif mode == 'artist':
            url = f"{API_BASE}/{param}/"
            params = {"type": type_, "page": page, "count": self.tracks_per_page}
            key = f"{mode}_{param}_{type_}_page{page}"
        elif mode == 'search':
            url = f"{API_BASE}/search"
            params = {"t": param, "page": page, "count": self.tracks_per_page}
            key = f"{mode}_{param.replace(' ','_')}_page{page}"
        else:
            return

        cached = self.cache.get(key)
        if cached:
            GLib.idle_add(self.fill_track_list, cached, not append, append)
            GLib.idle_add(self.page_label.set_text, f"Page: {page}")
            return
        try:
            data = requests.get(url, params=params, timeout=20).json()
            self.cache.set(key, data)
        except Exception as e:
            data = []
            print(f"{mode.capitalize()} page error:", e)
        GLib.idle_add(self.fill_track_list, data, not append, append)
        GLib.idle_add(self.page_label.set_text, f"Page: {page}")

    # ---- Lists / UI ----
    def _clear_all_tracks(self):
        self.local_tracks = []
        self.list_all.remove_all()

    def fill_track_list(self, tracks, clear_first=False, append=False):
        if clear_first:
            self._clear_all_tracks()
        if not isinstance(tracks, list):
            return
        for t in tracks:
            if not isinstance(t, dict):
                continue
            self.local_tracks.append(t)
            self.list_all.append(TrackRow(t))
        if not append:
            self.list_all.select_row(None)

    def update_artist_info(self, info: dict):
        avatar_url = info.get("avatar_url")
        desc = info.get("description") or "(No description available)"
        self.artist_desc.set_text(desc)
        if avatar_url and avatar_url.startswith("http"):
            def worker():
                tex = texture_from_bytes(fetch_image_bytes(avatar_url))
                if tex:
                    GLib.idle_add(self.artist_avatar.set_paintable, tex)
                else:
                    GLib.idle_add(self.artist_avatar.set_paintable, get_placeholder_texture())
            threading.Thread(target=worker, daemon=True).start()
        else:
            self.artist_avatar.set_paintable(get_placeholder_texture())

    # ---- Playback / Auto-play ----
    def _play_track_from(self, playlist_name: str, index: int):
        # Set playlist and index
        if playlist_name == "selected":
            if not (0 <= index < len(self.selected_tracks)):
                return
            t = self.selected_tracks[index]
        else:
            if not (0 <= index < len(self.local_tracks)):
                return
            t = self.local_tracks[index]
            playlist_name = "all"

        stream = t.get("stream_url")
        if not stream:
            return

        self.current_playlist = playlist_name
        self.current_index = index

        # Set source
        self.player.set_uri(stream)
        self.player.play()
        self.playpause_btn.set_icon_name("media-playback-pause-symbolic")

        # "Now playing" section
        title = t.get("title") or "Unknown"
        user = t.get("user", {}).get("username") or ""
        genre = t.get("genre") or (t.get("category") or "")
        self.now_title.set_text(title)
        meta_line = user if user else (f"genre: {genre}" if genre else "")
        self.now_meta.set_text(meta_line)

        # Cover from API (initially), then ID3 if available from TAG
        cover = t.get("artwork_url") or t.get("thumb") or t.get("images", {}).get("thumbnail")
        if cover and cover.startswith("http"):
            def worker():
                tex = texture_from_bytes(fetch_image_bytes(cover))
                if tex:
                    GLib.idle_add(self.now_cover.set_paintable, tex)
            threading.Thread(target=worker, daemon=True).start()
        else:
            self.now_cover.set_paintable(None)

    def on_row_activated(self, listbox, row: TrackRow):
        if not row or not isinstance(row, TrackRow):
            return
        idx = list(self.list_all).index(row)
        self._play_track_from("all", idx)

    def on_row_activated_selected(self, listbox, row: TrackRow):
        if not row or not isinstance(row, TrackRow):
            return
        idx = list(self.list_selected).index(row)
        self._play_track_from("selected", idx)

    def on_play_pause(self, *_):
        st = self.player.state()
        if st == Gst.State.PLAYING:
            self.player.pause()
            self.playpause_btn.set_icon_name("media-playback-start-symbolic")
        else:
            self.player.play()
            self.playpause_btn.set_icon_name("media-playback-pause-symbolic")

    def on_stop(self, *_):
        self.player.stop()
        self.playpause_btn.set_icon_name("media-playback-start-symbolic")
        self.pos_scale.set_value(0)
        self.time_label.set_text("00:00 / 00:00")
        self.pos_scale.set_sensitive(False)

    def on_volume_changed(self, scale: Gtk.Scale):
        v = scale.get_value() / 100.0
        self.player.set_volume(v)
        if v == 0:
            self.mute_btn.set_active(True)

    def on_toggle_mute(self, btn: Gtk.ToggleButton):
        muted = btn.get_active()
        self.player.set_muted(muted)
        btn.set_icon_name("audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic")

    def on_seek(self, scale: Gtk.Scale):
        if not self.is_seeking:
            return
        sec = int(scale.get_value())
        self.player.seek_seconds(sec)

    def update_position(self):
        try:
            pos, dur = self.player.query_pos_dur()
            if dur <= 0:
                dur = 100  # Fallback for unknown duration
                self.pos_scale.set_sensitive(False)
            else:
                self.pos_scale.set_sensitive(True)
            self.pos_scale.set_range(0, dur)
            if not self.is_seeking:
                self.pos_scale.set_value(pos)
            self.time_label.set_text(f"{pos//60:02}:{pos%60:02} / {dur//60:02}:{dur%60:02}")
        except Exception as e:
            print("Position update error:", e)
            self.pos_scale.set_sensitive(False)
            self.time_label.set_text("00:00 / 00:00")
        return True

    # --- Auto next ---
    def _advance_index(self):
        if self.current_playlist == "selected":
            total = len(self.selected_tracks)
        else:
            total = len(self.local_tracks)
        if total == 0:
            return -1
        nxt = self.current_index + 1
        if nxt >= total:
            return -1  # No next track (could loop -> nxt = 0)
        return nxt

    def _on_about_to_finish(self, player: GstPlayer):
        # Set next URI immediately (gapless)
        nxt = self._advance_index()
        if nxt < 0:
            return
        if self.current_playlist == "selected":
            t = self.selected_tracks[nxt]
        else:
            t = self.local_tracks[nxt]
        stream = t.get("stream_url")
        if stream:
            # Set next without starting; playbin will transition
            player.playbin.set_property("uri", stream)
            # Update index immediately
            self.current_index = nxt
            GLib.idle_add(self._update_now_playing_labels, t)

    def _on_eos(self):
        # Fallback if about-to-finish didn't work
        nxt = self._advance_index()
        if nxt >= 0:
            self._play_track_from(self.current_playlist, nxt)

    def _update_now_playing_labels(self, t: dict):
        title = t.get("title") or "Unknown"
        user = t.get("user", {}).get("username") or ""
        genre = t.get("genre") or (t.get("category") or "")
        self.now_title.set_text(title)
        meta_line = user if user else (f"genre: {genre}" if genre else "")
        self.now_meta.set_text(meta_line)

    # --- Receive GStreamer tags (ID3 with cover) ---
    def _on_gst_tags(self, taglist: Gst.TagList):
        # Title / artist / genre
        title = None
        artist = None
        genre = None
        if taglist.get_string(Gst.TAG_TITLE)[0]:
            title = taglist.get_string(Gst.TAG_TITLE)[1]
        if taglist.get_string(Gst.TAG_ARTIST)[0]:
            artist = taglist.get_string(Gst.TAG_ARTIST)[1]
        if taglist.get_string(Gst.TAG_GENRE)[0]:
            genre = taglist.get_string(Gst.TAG_GENRE)[1]
        if title or artist or genre:
            GLib.idle_add(self._update_now_playing_from_tags, title, artist, genre)

        # Image: image or preview-image
        img = None
        if taglist.get_sample(Gst.TAG_IMAGE)[0]:
            img = taglist.get_sample(Gst.TAG_IMAGE)[1]
        elif taglist.get_sample(Gst.TAG_PREVIEW_IMAGE)[0]:
            img = taglist.get_sample(Gst.TAG_PREVIEW_IMAGE)[1]

        if img:
            sample = img
            buf = sample.get_buffer()
            if not buf:
                return
            success, mapinfo = buf.map(Gst.MapFlags.READ)
            if not success:
                return
            try:
                data = mapinfo.data  # Already bytes
            finally:
                buf.unmap(mapinfo)

            tex = texture_from_bytes(data)
            if tex:
                self.last_stream_texture = tex
                GLib.idle_add(self.now_cover.set_paintable, tex)

    def _update_now_playing_from_tags(self, title, artist, genre):
        if title:
            self.now_title.set_text(title)
        if artist or genre:
            meta_line = artist if artist else (f"genre: {genre}" if genre else "")
            self.now_meta.set_text(meta_line)

    # ---- Selected list management ----
    def on_add_selected(self, *_):
        row = self.list_all.get_selected_row()
        if not row or not isinstance(row, TrackRow):
            return
        t = row.track
        self.selected_tracks.append(t)
        self.list_selected.append(TrackRow(t))

def main(argv):
    app = HearThisApp()
    return app.run(argv)

if __name__ == "__main__":
    sys.exit(main(sys.argv))
