import tkinter as tk
from tkinter import messagebox, filedialog
import tkinter.font as tkfont
import ctypes
import datetime
import ipaddress
import json
import math
import os
import queue
import sys
import threading
import time
from collections import deque
from itertools import groupby
from operator import itemgetter
from pathlib import Path

import customtkinter as ctk
from PIL import Image

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from ping_core import (CSV_FILE, LOG_FILE, RunningStats, compute_quality, format_log_line,
                       group_by_host, measure_download, measure_latency, measure_upload,
                       now_str, parse_log_file, ping_once, save_plots, summarize, write_csv)

try:
    import winsound
except ImportError:
    winsound = None

# --- Palette « Nuit » ---
BG = "#0a1122"            # fond de la fenêtre
SURFACE = "#111a2e"       # cartes
SURFACE_2 = "#0f1a30"     # bandeau de diagnostic
BORDER = "#1f2d4a"
FIELD_BORDER = "#25344f"
HOVER = "#1a2742"
TEXT = "#e6edf8"
TEXT_2 = "#9fb0cc"        # texte secondaire
TEXT_3 = "#7d8fb0"        # heures, axes
LINK = "#93c5fd"
GRID = "#18243c"
AXIS = "#2a3a58"
ON_ACCENT = "#06101f"     # texte sur un bouton d'accent
PILL_BG, PILL_BORDER, PILL_TEXT = "#10233f", "#1f3a66", "#bfdbfe"
LIVE_DOT = "#4ade80"
WARN_COLOR = "#f59e0b"    # ligne de seuil
# (texte, fond) des pastilles d'état
CHIP_OK = ("#86efac", "#0f2e22")
CHIP_WARN = ("#fdba74", "#3a2410")
CHIP_ERR = ("#fca5a5", "#3b1418")
CHIP_IDLE = (TEXT_2, "#18243c")
LOG_COLORS = {"ok": TEXT, "warn": CHIP_WARN[0], "err": CHIP_ERR[0]}

ACCENTS = {"Bleu": "#3b82f6", "Ciel": "#60a5fa", "Azur": "#38bdf8", "Pervenche": "#6d8cff"}
DEFAULT_ACCENT = ACCENTS["Bleu"]
# Courbes : la 1re adresse prend l'accent, les suivantes ces couleurs (luminosités distinctes).
HOST_COLORS = ["#fb923c", "#94a3b8", "#f472b6", "#2dd4bf", "#a78bfa", "#facc15", "#e2e8f0"]

CONFIG_PATH = Path.home() / ".pingtester.json"
ALERT_COOLDOWN = 30.0

UI_TICK_MS = 150        # rafraîchissement groupé de l'interface
GRAPH_MIN_PERIOD = 0.5  # s entre deux rendus du graphe (le rendu est le poste le plus coûteux)
LIVE_WINDOW = 1800      # points affichés par hôte sur le graphe en direct (30 min à 1 s)
SPARK_POINTS = 60       # points des mini-graphes des cartes
MAX_LOG_LINES = 5000    # lignes conservées dans le journal
MAX_EVENTS = 200        # événements conservés dans le fil
MAX_CARD_COLUMNS = 4
MIN_INTERVAL = 0.2      # s

DEFAULT_CONFIG = {
    "hosts": "192.168.1.1, 1.1.1.1, 8.8.8.8", "duration": "60", "continuous": True,
    "threshold": "100", "interval": "1.0",
    "log_file": LOG_FILE, "csv_file": CSV_FILE, "plot_prefix": "ping",
    "accent": DEFAULT_ACCENT, "alerts": True,
}

# Noms lisibles des résolveurs publics courants
KNOWN_HOSTS = {
    "1.1.1.1": "Cloudflare DNS", "1.0.0.1": "Cloudflare DNS",
    "8.8.8.8": "Google DNS", "8.8.4.4": "Google DNS",
    "9.9.9.9": "Quad9 DNS", "208.67.222.222": "OpenDNS", "208.67.220.220": "OpenDNS",
}


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb):
    return "#%02x%02x%02x" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _blend(hex_color, target_rgb, t):
    base = _hex_to_rgb(hex_color)
    return _rgb_to_hex(tuple(base[i] + (target_rgb[i] - base[i]) * t for i in range(3)))


def darken(hex_color, t=0.18):
    return _blend(hex_color, (0, 0, 0), t)


def _to_float(text, default):
    """float(text), ou `default` si la saisie est invalide ou non finie."""
    try:
        value = float(text)
    except ValueError:
        return default
    return value if math.isfinite(value) else default


def fr_num(value, digits=1):
    """Nombre à la française : 0,5 plutôt que 0.5."""
    return f"{value:.{digits}f}".replace(".", ",")


def fr_int(value):
    """Entier avec espace fine comme séparateur de milliers : 2 160."""
    return f"{value:,}".replace(",", " ")


def fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d}"


def fmt_clock(seconds):
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60:02d}:{rest % 60:02d}"


def describe_host(host):
    """(nom lisible, précision) affichés sur la carte d'une adresse."""
    if host in KNOWN_HOSTS:
        return KNOWN_HOSTS[host], host
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host, "site web"
    if ip.is_loopback:
        return "Cet ordinateur", host
    if ip.is_private:
        if ip.version == 4 and host.endswith(".1"):
            return "Box / routeur", host
        return "Réseau local", host
    return "Serveur internet", host


def _ellipsize(text, n):
    return text if len(text) <= n else text[:n - 1] + "…"


def score_letter(score):
    return ("A" if score >= 90 else "B" if score >= 75 else
            "C" if score >= 60 else "D" if score >= 40 else "F")


def host_color(i, accent):
    return accent if i == 0 else HOST_COLORS[(i - 1) % len(HOST_COLORS)]


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def load_config():
    """Préférences sauvegardées, valeur par défaut pour toute clé absente ou invalide."""
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    for key, default in DEFAULT_CONFIG.items():
        value = data.get(key)
        if isinstance(default, bool):
            if isinstance(value, bool):
                cfg[key] = value
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            cfg[key] = str(value)
    if cfg["accent"].lower() not in ACCENTS.values():   # accents d'avant la refonte : retour au bleu
        cfg["accent"] = DEFAULT_ACCENT
    return cfg


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint), ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_uint)]


FLASHW_ALL, FLASHW_TIMERNOFG = 0x3, 0xC


class HostState:
    """Mesures d'un hôte : statistiques cumulées (O(1) par ping) et fenêtre
    glissante des derniers points pour le graphe (window=None : tout garder)."""

    def __init__(self, host, color, window=LIVE_WINDOW):
        self.host = host
        self.color = color
        self.stats = RunningStats()
        self.last = None                  # dernière latence (None = perdu)
        self.outages = 0                  # coupures = séries de pertes consécutives
        self.lost_streak = 0
        self.xs = deque(maxlen=window)    # index du ping
        self.ys = deque(maxlen=window)    # latence, NaN si perdu
        self.lost_x = deque()             # index des pertes encore dans la fenêtre
        self.line = None
        self.lost_line = None
        self.card = None

    def add(self, lat):
        i = self.stats.total
        if lat is None:
            if not self.lost_streak:
                self.outages += 1
            self.lost_streak += 1
        else:
            self.lost_streak = 0
        self.stats.add(lat)
        self.last = lat
        self.xs.append(i)
        self.ys.append(math.nan if lat is None else lat)
        if lat is None:
            self.lost_x.append(i)
        while self.lost_x and self.lost_x[0] < self.xs[0]:
            self.lost_x.popleft()

    def last_kind(self, threshold):
        if not self.stats.total:
            return None
        if self.last is None:
            return "timeout"
        if threshold > 0 and self.last >= threshold:
            return "high"
        return "ok"


class HostCard(ctk.CTkFrame):
    """Carte d'une adresse : nom, état, dernière latence, mini-graphe, résumé."""

    def __init__(self, master, app, st):
        super().__init__(master, fg_color=SURFACE, border_color=BORDER, border_width=1, corner_radius=14)
        self.app = app
        self.st = st
        name, detail = describe_host(st.host)

        ctk.CTkLabel(self, text=_ellipsize(name, 24), font=app.f_card_title, text_color=TEXT,
                     height=20, anchor="w").pack(fill="x", padx=16, pady=(14, 0))
        ctk.CTkLabel(self, text=_ellipsize(detail, 30), font=app.f_mono_small, text_color=TEXT_2,
                     height=16, anchor="w").pack(fill="x", padx=16)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(6, 0))
        self.value = ctk.CTkLabel(row, text="—", font=app.f_big, text_color=TEXT, height=40)
        self.value.pack(side="left")
        self.unit = ctk.CTkLabel(row, text="", font=app.f_unit, text_color=TEXT_2)
        self.unit.pack(side="left", anchor="s", padx=(4, 0), pady=(0, 6))
        self.chip = ctk.CTkLabel(row, text="En attente", font=app.f_chip, height=24, corner_radius=12,
                                 fg_color=CHIP_IDLE[1], text_color=CHIP_IDLE[0], padx=10)
        self.chip.pack(side="right")

        self.spark = tk.Canvas(self, height=int(52 * app.scale), bg=SURFACE, highlightthickness=0, bd=0)
        self.spark.pack(fill="x", padx=16, pady=(6, 0))
        self.spark.bind("<Configure>", lambda _e: self.draw_spark())

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=16, pady=(6, 14))
        self.metrics = {}
        for col, key in enumerate(("moy.", "perte", "gigue", "note")):
            foot.columnconfigure(col, weight=1, uniform="m")
            ctk.CTkLabel(foot, text=key, font=app.f_small, text_color=TEXT_3, height=14,
                         anchor="w").grid(row=0, column=col, sticky="w")
            self.metrics[key] = ctk.CTkLabel(foot, text="—", font=app.f_mono_small, text_color=TEXT_2,
                                             height=16, anchor="w")
            self.metrics[key].grid(row=1, column=col, sticky="w")

    def refresh(self, threshold):
        st, s = self.st, self.st.stats
        if not s.total:
            chip, colors = "En attente", CHIP_IDLE
        elif st.last is None:
            chip, colors = "Ne répond pas", CHIP_ERR
        elif threshold > 0 and st.last >= threshold:
            chip, colors = "Lent", CHIP_WARN
        elif st.outages:
            chip, colors = f"{st.outages} coupure{'s' if st.outages > 1 else ''}", CHIP_WARN
        else:
            chip, colors = "Répond", CHIP_OK
        self.chip.configure(text=chip, text_color=colors[0], fg_color=colors[1])
        if st.last is None:
            self.value.configure(text="—")
            self.unit.configure(text="")
        else:
            self.value.configure(text=str(st.last))
            self.unit.configure(text="ms")
        summary = s.summary()
        _, letter = compute_quality(summary)
        has_lat = summary["avg"] is not None
        self.metrics["moy."].configure(text=f"{summary['avg']:.0f} ms" if has_lat else "—")
        self.metrics["perte"].configure(text=f"{fr_num(summary['loss_pct'])} %" if s.total else "—")
        self.metrics["gigue"].configure(text=f"{fr_num(summary['jitter'])} ms" if has_lat else "—")
        self.metrics["note"].configure(text=letter)

    def draw_spark(self):
        c = self.spark
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        vals = list(self.st.ys)[-SPARK_POINTS:]
        if w < 10 or h < 10 or not vals:
            return
        finite = [v for v in vals if v == v]
        lo, hi = (min(finite), max(finite)) if finite else (0.0, 1.0)
        span = max(hi - lo, 1.0)
        pad = 4
        step = (w - 2 * pad) / (SPARK_POINTS - 1)
        x0 = w - pad - (len(vals) - 1) * step      # aligné à droite : le plus récent au bord
        width = max(1, round(1.8 * self.app.scale))
        seg = []

        def flush():
            if len(seg) >= 4:
                c.create_line(*seg, fill=self.st.color, width=width, joinstyle="round")
            elif seg:
                c.create_oval(seg[0] - 1.5, seg[1] - 1.5, seg[0] + 1.5, seg[1] + 1.5,
                              fill=self.st.color, outline="")

        for i, v in enumerate(vals):
            x = x0 + i * step
            if v != v:          # perte : trou dans la courbe + repère pointillé
                flush()
                seg = []
                c.create_line(x, pad, x, h - pad, fill=CHIP_WARN[0], dash=(3, 3))
            else:
                seg += [x, h - pad - (v - lo) / span * (h - 2 * pad)]
        flush()


class PingApp:
    def __init__(self, root, config=None):
        self.root = root
        self.root.title("PingTester")
        # CTk applique un facteur DPI à la géométrie ; winfo_screen* renvoie des
        # pixels physiques. On divise par le scaling pour ne pas déborder l'écran.
        try:
            self.scale = ctk.ScalingTracker.get_window_scaling(self.root)
        except Exception:
            self.scale = 1.0
        try:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        except Exception:
            sw, sh = 1536, 864
        w = int(min(1120, (sw - 70) / self.scale))
        h = int(min(780, (sh - 100) / self.scale))
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(1000, 660)
        self.root.configure(fg_color=BG)

        self.config = config or load_config()
        self.accent = self.config["accent"]
        self.accent_hover = darken(self.accent)
        self.accent_widgets = []   # (widget, option) repeints par apply_accent
        self.swatches = {}

        ctk.set_appearance_mode("dark")

        # Les threads ne touchent jamais Tk : ils déposent (fonction, args) dans
        # ui_queue, vidée par _pump sur le thread principal. Journal, graphe et
        # cartes sont ensuite rafraîchis une seule fois par tick.
        self.ui_queue = queue.Queue()
        self._pending_log = []
        self._graph_dirty = False
        self._stats_dirty = False
        self._next_graph_draw = 0.0

        self.stop_event = threading.Event()
        self.ping_running = False
        self.stopping = False
        self.analyzing = False
        self.mode = "idle"          # idle | live | done | analysis
        self.start_time = self.end_time = 0.0
        self.duration = 0
        self.continuous = False
        self.interval = 1.0
        self.threshold = 0.0
        self.workers_active = 0
        self.log_lock = threading.Lock()
        self.log_fh = None

        self.hosts_state = {}
        self.host_order = []
        self.last_alert = {}
        self._min_hint_shown = False
        self._pill_text = None
        self.view = None

        # Test de débit
        self.speed_running = False
        self.speed_stop = threading.Event()

        self._make_fonts()
        self._build_widgets()
        self._toggle_continuous()
        self.threshold_var.trace_add("write", self._on_threshold_change)
        self._on_threshold_change()
        self.apply_accent(self.accent)
        self._rebuild_cards()
        self._refresh_overview()
        self._update_controls()
        self.show_view("dashboard")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(UI_TICK_MS, self._pump)

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------
    def _make_fonts(self):
        families = set(tkfont.families(self.root))
        ui = next((f for f in ("Segoe UI", "Helvetica Neue", "DejaVu Sans") if f in families), None)
        mono = next((f for f in ("Cascadia Mono", "Consolas", "DejaVu Sans Mono") if f in families), "Courier")
        self.ui_family, self.mono_family = ui, mono

        def font(size, weight="normal", family=ui):
            return ctk.CTkFont(family=family, size=size, weight=weight) if family else ctk.CTkFont(size=size, weight=weight)

        self.f_title = font(20, "bold")
        self.f_verdict = font(20, "bold")
        self.f_section = font(15, "bold")
        self.f_card_title = font(15, "bold")
        self.f_body = font(13)
        self.f_small = font(12)
        self.f_chip = font(12, "bold")
        self.f_button = font(14, "bold")
        self.f_mono = font(13, family=mono)
        self.f_mono_small = font(12, family=mono)
        self.f_pill = font(12, family=mono)
        self.f_stat = font(18, "bold", family=mono)
        self.f_big = font(38, "bold", family=mono)
        self.f_huge = font(40, "bold", family=mono)
        self.f_unit = font(16, family=mono)

    def _button(self, master, text, command, width=0, accent=False, danger=False, **kw):
        """Bouton de la charte : plein d'accent, ou sombre bordé."""
        opts = dict(text=text, command=command, height=44, corner_radius=10, font=self.f_button)
        if width:
            opts["width"] = width
        if accent:
            opts.update(fg_color=self.accent, hover_color=self.accent_hover, text_color=ON_ACCENT,
                        text_color_disabled="#3d5a86")
        else:
            opts.update(fg_color=SURFACE, hover_color=HOVER, border_width=1, border_color=FIELD_BORDER,
                        text_color=CHIP_ERR[0] if danger else TEXT, text_color_disabled=TEXT_3)
        opts.update(kw)
        b = ctk.CTkButton(master, **opts)
        if accent:
            self.accent_widgets.append((b, "accent_button"))
        return b

    def _entry(self, master, var, width=120, mono=True):
        return ctk.CTkEntry(master, textvariable=var, width=width, height=44, corner_radius=10,
                            fg_color=SURFACE, border_color=FIELD_BORDER, text_color=TEXT,
                            font=self.f_mono if mono else self.f_body)

    def _card(self, master, **kw):
        return ctk.CTkFrame(master, fg_color=SURFACE, border_color=BORDER, border_width=1, corner_radius=14, **kw)

    def _switch(self, master, text, var, command=None):
        s = ctk.CTkSwitch(master, text=text, variable=var, command=command, font=self.f_body,
                          text_color=TEXT, fg_color="#25344f", progress_color=self.accent,
                          button_color="#e6edf8", button_hover_color="#ffffff")
        self.accent_widgets.append((s, "progress_color"))
        return s

    def _build_widgets(self):
        c = self.config
        self.outer = ctk.CTkFrame(self.root, fg_color=BG, corner_radius=0)
        self.outer.pack(fill="both", expand=True, padx=24, pady=(18, 20))

        # ---- Barre du haut (toujours visible) ----
        top = ctk.CTkFrame(self.outer, fg_color="transparent")
        top.pack(fill="x", pady=(0, 16))
        try:
            pil = Image.open(resource_path("ping_tool_ico.ico")).convert("RGBA")
            self.logo_img = ctk.CTkImage(light_image=pil, dark_image=pil, size=(34, 34))
        except Exception:
            self.logo_img = None
        ctk.CTkLabel(top, image=self.logo_img, text="  PingTester", compound="left",
                     font=self.f_title, text_color=TEXT).pack(side="left")
        self.pill = ctk.CTkFrame(top, fg_color=PILL_BG, border_color=PILL_BORDER, border_width=1,
                                 corner_radius=14, height=28)
        self.pill.pack(side="left", padx=(14, 0))
        self.pill_dot = ctk.CTkFrame(self.pill, width=8, height=8, corner_radius=4, fg_color=TEXT_3)
        self.pill_dot.pack(side="left", padx=(12, 8), pady=10)
        self.pill_label = ctk.CTkLabel(self.pill, text="PRÊT", font=self.f_pill, text_color=PILL_TEXT, height=26)
        self.pill_label.pack(side="left", padx=(0, 12))

        # Ordre de pack : à l'étroit, c'est l'étiquette « Adresses » (packée en dernier) qui cède.
        self.btn_quit = self._button(top, "Quitter", self._real_quit, width=86, danger=True)
        self.btn_quit.pack(side="right")
        self.btn_settings = self._button(top, "Réglages", lambda: self.show_view("settings"), width=96)
        self.btn_settings.pack(side="right", padx=8)
        self.btn_speed_view = self._button(top, "Débit", lambda: self.show_view("speed"), width=70)
        self.btn_speed_view.pack(side="right")
        self.btn_start = self._button(top, "▶  Démarrer", self._toggle_ping, width=122, accent=True)
        self.btn_start.pack(side="right", padx=8)
        self.host_var = tk.StringVar(value=c["hosts"])
        self.host_entry = self._entry(top, self.host_var, width=210)
        self.host_entry.pack(side="right")
        self.host_entry.bind("<Return>", lambda _e: None if self.ping_running else self.start_ping())
        ctk.CTkLabel(top, text="Adresses", font=self.f_body, text_color=TEXT_2).pack(side="right", padx=(0, 8))

        self.body = ctk.CTkFrame(self.outer, fg_color="transparent")
        self.body.pack(fill="both", expand=True)
        self.views = {
            "dashboard": self._build_dashboard(self.body),
            "speed": self._build_speed_view(self.body),
            "log": self._build_log_view(self.body),
            "settings": self._build_settings_view(self.body),
        }

    def _view_header(self, master, title, subtitle):
        head = ctk.CTkFrame(master, fg_color="transparent")
        head.pack(fill="x", pady=(0, 16))
        self._button(head, "←  Tableau de bord", lambda: self.show_view("dashboard"), width=170).pack(side="left")
        texts = ctk.CTkFrame(head, fg_color="transparent")
        texts.pack(side="left", padx=16)
        ctk.CTkLabel(texts, text=title, font=self.f_verdict, text_color=TEXT, height=26).pack(anchor="w")
        sub = ctk.CTkLabel(texts, text=subtitle, font=self.f_body, text_color=TEXT_2, height=18)
        sub.pack(anchor="w")
        return head, sub

    # ---- Tableau de bord ----
    def _build_dashboard(self, master):
        view = ctk.CTkFrame(master, fg_color="transparent")

        strip = ctk.CTkFrame(view, fg_color=SURFACE_2, border_color=BORDER, border_width=1, corner_radius=14)
        strip.pack(fill="x", pady=(0, 16))
        ring_size = int(48 * self.scale)
        self.ring = tk.Canvas(strip, width=ring_size, height=ring_size, bg=SURFACE_2, highlightthickness=0, bd=0)
        self.ring.pack(side="left", padx=(20, 14), pady=14)
        texts = ctk.CTkFrame(strip, fg_color="transparent")
        texts.pack(side="left", fill="x", expand=True)
        self.verdict_title = ctk.CTkLabel(texts, text="", font=self.f_verdict, text_color=TEXT, height=26, anchor="w")
        self.verdict_title.pack(fill="x")
        self.verdict_sub = ctk.CTkLabel(texts, text="", font=self.f_body, text_color=TEXT_2, height=18, anchor="w")
        self.verdict_sub.pack(fill="x")
        self.total_lost_var = tk.StringVar(value="0")
        self.total_sent_var = tk.StringVar(value="0")
        for label, var in (("Perdus", self.total_lost_var), ("Pings envoyés", self.total_sent_var)):
            box = ctk.CTkFrame(strip, fg_color="transparent")
            box.pack(side="right", padx=(0, 20) if label == "Perdus" else (0, 28))
            ctk.CTkLabel(box, text=label, font=self.f_small, text_color=TEXT_2, height=16).pack(anchor="e")
            ctk.CTkLabel(box, textvariable=var, font=self.f_stat, text_color=TEXT, height=24).pack(anchor="e")

        self.cards_frame = ctk.CTkFrame(view, fg_color="transparent")
        self.cards_frame.pack(fill="x", pady=(0, 16))

        bottom = ctk.CTkFrame(view, fg_color="transparent")
        bottom.pack(fill="both", expand=True)
        bottom.columnconfigure(0, weight=2, uniform="bottom")
        bottom.columnconfigure(1, weight=1, uniform="bottom")
        bottom.rowconfigure(0, weight=1)

        chart = self._card(bottom)
        chart.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        head = ctk.CTkFrame(chart, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(12, 0))
        self.chart_title = ctk.CTkLabel(head, text="Toutes les adresses", font=self.f_section, text_color=TEXT)
        self.chart_title.pack(side="left")
        ctk.CTkLabel(head, text="  · temps de réponse (ms)", font=self.f_body, text_color=TEXT_2).pack(side="left")
        self.legend = ctk.CTkFrame(chart, fg_color="transparent")
        self.legend.pack(fill="x", padx=4, pady=(2, 0))

        self.fig = Figure(figsize=(7, 3), dpi=100, facecolor=SURFACE)
        self.ax = self.fig.add_subplot(111)
        self.fig.subplots_adjust(left=0.06, right=0.99, top=0.95, bottom=0.1)
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart)
        self.canvas.get_tk_widget().configure(bg=SURFACE, highlightthickness=0)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self._reset_axes()

        events = self._card(bottom)
        events.grid(row=0, column=1, sticky="nsew")
        ehead = ctk.CTkFrame(events, fg_color="transparent")
        ehead.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(ehead, text="Événements", font=self.f_section, text_color=TEXT).pack(side="left")
        self.btn_clear = ctk.CTkButton(ehead, text="Effacer", command=self.clear_all, width=64, height=28,
                                       fg_color="transparent", hover_color=HOVER, text_color=TEXT_2,
                                       text_color_disabled=TEXT_3, font=self.f_small)
        self.btn_clear.pack(side="right")
        self.events_box = tk.Text(events, bg=SURFACE, fg=TEXT, relief="flat", bd=0, highlightthickness=0,
                                  wrap="word", cursor="arrow", padx=0, pady=0, spacing1=1, spacing3=1,
                                  font=(self.ui_family or "TkDefaultFont", 10), state="disabled")
        self.events_box.pack(fill="both", expand=True, padx=16)
        ui = self.ui_family or "TkDefaultFont"
        self.events_box.tag_config("time", foreground=TEXT_3, font=(self.mono_family, 9))
        self.events_box.tag_config("detail", foreground=TEXT_2, lmargin1=0, lmargin2=0)
        for kind, color in (("ok", CHIP_OK[0]), ("warn", CHIP_WARN[0]), ("err", CHIP_ERR[0]), ("neutral", TEXT)):
            self.events_box.tag_config(kind, foreground=color, font=(ui, 10, "bold"))
        self.events_box.tag_config("info", font=(ui, 10, "bold"))
        self._events_hint()
        ctk.CTkButton(events, text="Voir le journal complet", command=lambda: self.show_view("log"),
                      fg_color="transparent", hover_color=HOVER, text_color=LINK, font=self.f_chip,
                      height=30, anchor="w").pack(fill="x", padx=10, pady=(4, 10))
        return view

    # ---- Débit ----
    def _build_speed_view(self, master):
        view = ctk.CTkFrame(master, fg_color="transparent")
        self._view_header(view, "Test de débit",
                          "Mesure via Cloudflare : réception, envoi et latence du serveur (environ 25 s).")
        panel = self._card(view)
        panel.pack(fill="x")
        inner = ctk.CTkFrame(panel, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=20)
        self.btn_speed = self._button(inner, "▶  Tester le débit", self._toggle_speedtest, accent=True, height=48)
        self.btn_speed.pack(fill="x")
        self.speed_progress = ctk.CTkProgressBar(inner, height=8, corner_radius=4, fg_color="#1a2742",
                                                 progress_color=self.accent)
        self.accent_widgets.append((self.speed_progress, "progress_color"))
        self.speed_progress.set(0)
        self.speed_progress.pack(fill="x", pady=(14, 6))
        self.speed_phase_var = tk.StringVar(value="Prêt à tester.")
        ctk.CTkLabel(inner, textvariable=self.speed_phase_var, font=self.f_body, text_color=TEXT_2,
                     anchor="w").pack(fill="x")

        cards = ctk.CTkFrame(view, fg_color="transparent")
        cards.pack(fill="x", pady=(16, 0))
        for i in range(3):
            cards.columnconfigure(i, weight=1, uniform="spd")
        self.dl_var = tk.StringVar(value="—")
        self.ul_var = tk.StringVar(value="—")
        self.lat_var = tk.StringVar(value="—")
        for col, (title, unit, var) in enumerate((("Réception", "Mbit/s", self.dl_var),
                                                  ("Envoi", "Mbit/s", self.ul_var),
                                                  ("Latence du serveur", "ms", self.lat_var))):
            card = self._card(cards)
            card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 8, 0 if col == 2 else 8))
            ctk.CTkLabel(card, text=title, font=self.f_card_title, text_color=TEXT).pack(anchor="w", padx=18, pady=(16, 0))
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(anchor="w", padx=18, pady=(4, 18))
            ctk.CTkLabel(row, textvariable=var, font=self.f_huge, text_color=TEXT).pack(side="left")
            ctk.CTkLabel(row, text=unit, font=self.f_unit, text_color=TEXT_2).pack(side="left", anchor="s",
                                                                                 padx=(6, 0), pady=(0, 8))
        return view

    # ---- Journal ----
    def _build_log_view(self, master):
        view = ctk.CTkFrame(master, fg_color="transparent")
        _, self.log_view_sub = self._view_header(view, "Journal des pings", "")
        self.log_area = ctk.CTkTextbox(view, corner_radius=14, fg_color=SURFACE, border_color=BORDER,
                                       border_width=1, text_color=TEXT, font=self.f_mono, state="disabled")
        self.log_area.pack(fill="both", expand=True)
        for tag, color in LOG_COLORS.items():
            self.log_area.tag_config(tag, foreground=color)
        return view

    # ---- Réglages ----
    def _build_settings_view(self, master):
        c = self.config
        view = ctk.CTkFrame(master, fg_color="transparent")
        self._view_header(view, "Réglages", "Enregistrés automatiquement à la fermeture.")
        scroll = ctk.CTkScrollableFrame(view, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        def section(title):
            card = self._card(scroll)
            card.pack(fill="x", pady=(0, 14))
            ctk.CTkLabel(card, text=title, font=self.f_section, text_color=TEXT).pack(anchor="w", padx=18, pady=(14, 8))
            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.pack(fill="x", padx=18, pady=(0, 16))
            return inner

        def field(parent, row, col, label, var, hint, width=120):
            ctk.CTkLabel(parent, text=label, font=self.f_body, text_color=TEXT).grid(
                row=row, column=col, sticky="w", pady=(0, 4), padx=(0, 24))
            entry = self._entry(parent, var, width=width)
            entry.grid(row=row + 1, column=col, sticky="w", padx=(0, 24))
            ctk.CTkLabel(parent, text=hint, font=self.f_small, text_color=TEXT_2).grid(
                row=row + 2, column=col, sticky="w", padx=(0, 24))
            return entry

        s = section("Surveillance")
        self.continuous_var = tk.BooleanVar(value=c["continuous"])
        self._switch(s, "En continu (jusqu'à « Arrêter »)", self.continuous_var,
                     self._toggle_continuous).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        self.duration_var = tk.StringVar(value=c["duration"])
        self.duration_entry = field(s, 1, 0, "Durée (s)", self.duration_var, "si le mode continu est coupé")
        self.interval_var = tk.StringVar(value=c["interval"])
        field(s, 1, 1, "Intervalle (s)", self.interval_var, "temps entre deux pings")
        self.threshold_var = tk.StringVar(value=c["threshold"])
        field(s, 1, 2, "Seuil d'alerte (ms)", self.threshold_var, "au-delà : « lent » (0 = désactivé)")

        s = section("Alertes")
        self.alerts_var = tk.BooleanVar(value=c["alerts"])
        self._switch(s, "Son et clignotement de la barre des tâches en cas de coupure ou de lenteur",
                     self.alerts_var).pack(anchor="w")

        s = section("Couleur d'accent")
        row = ctk.CTkFrame(s, fg_color="transparent")
        row.pack(anchor="w")
        for name, color in ACCENTS.items():
            b = ctk.CTkButton(row, text=name, width=110, height=44, corner_radius=10, font=self.f_chip,
                              fg_color=color, hover_color=darken(color), text_color=ON_ACCENT,
                              border_color=TEXT, command=lambda col=color: self.apply_accent(col))
            b.pack(side="left", padx=(0, 10))
            self.swatches[color] = b

        s = section("Fichiers et analyse")
        s.columnconfigure(1, weight=1)
        self.log_file_var = tk.StringVar(value=c["log_file"])
        self.csv_file_var = tk.StringVar(value=c["csv_file"])
        self.plot_prefix_var = tk.StringVar(value=c["plot_prefix"])
        rows = (("Fichier journal", self.log_file_var, ("Fichier journal", ".txt", "Texte")),
                ("Export CSV", self.csv_file_var, ("Export CSV", ".csv", "CSV")),
                ("Préfixe des graphiques PNG", self.plot_prefix_var, None))
        for r, (label, var, browse) in enumerate(rows):
            ctk.CTkLabel(s, text=label, font=self.f_body, text_color=TEXT).grid(row=r, column=0, sticky="w",
                                                                              padx=(0, 16), pady=5)
            self._entry(s, var, width=200).grid(row=r, column=1, sticky="ew", pady=5)
            if browse:
                self._button(s, "Parcourir…", lambda v=var, b=browse: self._browse(v, *b), width=120).grid(
                    row=r, column=2, padx=(10, 0), pady=5)
        self.btn_analyze = self._button(s, "Analyser le fichier journal", self.start_analyze, accent=True)
        self.btn_analyze.grid(row=3, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ctk.CTkLabel(s, text="Relit le journal, affiche les courbes et exporte le CSV et les graphiques PNG.",
                     font=self.f_small, text_color=TEXT_2).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        s = section("Application")
        self._button(s, "Quitter PingTester", self._real_quit, danger=True, width=200).pack(anchor="w")
        return view

    def show_view(self, name):
        if self.view == name and name != "dashboard":
            name = "dashboard"            # re-cliquer sur Débit / Réglages ramène au tableau de bord
        if self.view:
            self.views[self.view].pack_forget()
        self.view = name
        self.views[name].pack(fill="both", expand=True)
        if name == "log":
            self.log_view_sub.configure(text=f"Chaque ping, enregistré aussi dans {self.log_file_var.get()}")

    def save_config(self):
        cfg = {
            "hosts": self.host_var.get(), "duration": self.duration_var.get(),
            "continuous": self.continuous_var.get(), "threshold": self.threshold_var.get(),
            "interval": self.interval_var.get(), "log_file": self.log_file_var.get(),
            "csv_file": self.csv_file_var.get(), "plot_prefix": self.plot_prefix_var.get(),
            "accent": self.accent, "alerts": self.alerts_var.get(),
        }
        try:
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Accent
    # ------------------------------------------------------------------
    def apply_accent(self, color):
        self.accent = color
        self.accent_hover = darken(color, 0.18)
        for widget, option in self.accent_widgets:
            if option == "accent_button":
                if widget is self.btn_speed and self.speed_running:
                    continue          # pendant le test, le bouton reste « Annuler »
                widget.configure(fg_color=self.accent, hover_color=self.accent_hover)
            else:
                widget.configure(**{option: self.accent})
        for c, btn in self.swatches.items():
            btn.configure(border_width=3 if c == color else 0)
        if self.host_order:
            st = self.hosts_state[self.host_order[0]]
            st.color = color
            st.line.set_color(color)
            st.lost_line.set_color(color)
            if st.card:
                st.card.draw_spark()
            self._rebuild_legend()
            self.canvas.draw_idle()
        self.log_area.tag_config("info", foreground=color)
        self.events_box.tag_config("info", foreground=color)
        self._draw_ring()

    # ------------------------------------------------------------------
    # File d'attente threads -> interface, rafraîchissement groupé
    # ------------------------------------------------------------------
    def _post(self, fn, *args):
        """Appelable depuis n'importe quel thread : exécute fn(*args) sur le thread Tk."""
        self.ui_queue.put((fn, args))

    def _pump(self):
        try:
            while True:
                try:
                    fn, args = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                fn(*args)
            self._flush_ui()
        finally:
            self.root.after(UI_TICK_MS, self._pump)

    def _flush_ui(self):
        if self._pending_log:
            self._append_log(self._pending_log)
            self._pending_log = []
        now = time.monotonic()
        if self._graph_dirty and now >= self._next_graph_draw:
            self._graph_dirty = False
            self._next_graph_draw = now + GRAPH_MIN_PERIOD
            self._redraw_graph()
        if self._stats_dirty:
            self._stats_dirty = False
            self._refresh_overview()
        self._refresh_pill(now)

    # ------------------------------------------------------------------
    # Aides UI
    # ------------------------------------------------------------------
    def log(self, message, tag="ok"):
        """Ajoute une ligne au journal (affichée au prochain tick)."""
        self._pending_log.append((message, tag))

    def _append_log(self, entries):
        box = self.log_area
        box.configure(state="normal")
        for tag, group in groupby(entries, key=itemgetter(1)):
            box.insert("end", "".join(text + "\n" for text, _ in group), tag)
        excess = int(box.index("end-1c").split(".")[0]) - 1 - MAX_LOG_LINES
        if excess > 0:
            box.delete("1.0", f"{excess + 1}.0")
        box.see("end")
        box.configure(state="disabled")

    def _events_hint(self):
        box = self.events_box
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", "Les coupures, ralentissements et retours à la normale s'afficheront ici.", ("detail",))
        box.configure(state="disabled")
        self._events_empty = True

    def event(self, title, detail="", kind="neutral"):
        """Ajoute un événement en tête du fil (kind : ok, warn, err, info, neutral)."""
        box = self.events_box
        box.configure(state="normal")
        if self._events_empty:
            box.delete("1.0", "end")
            self._events_empty = False
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        box.insert("1.0", stamp + "  ", ("time",), title + "\n", (kind,),
                   (detail + "\n" if detail else "") + "\n", ("detail",))
        lines = int(box.index("end-1c").split(".")[0])
        if lines > MAX_EVENTS * 3:
            box.delete(f"{MAX_EVENTS * 3}.0", "end")
        box.configure(state="disabled")

    def _update_controls(self):
        if self.ping_running:
            self.btn_start.configure(text="Arrêt…" if self.stopping else "■  Arrêter",
                                     state="disabled" if self.stopping else "normal")
        else:
            self.btn_start.configure(text="▶  Démarrer", state="disabled" if self.analyzing else "normal")
        busy = "disabled" if self.ping_running or self.analyzing else "normal"
        self.btn_analyze.configure(state=busy)
        self.btn_clear.configure(state=busy)

    def _toggle_continuous(self):
        self.duration_entry.configure(state="disabled" if self.continuous_var.get() else "normal",
                                      text_color=TEXT_3 if self.continuous_var.get() else TEXT)

    def _browse(self, var, title, ext, label):
        path = filedialog.asksaveasfilename(title=title, defaultextension=ext,
                                            filetypes=[(label, "*" + ext), ("Tous", "*.*")],
                                            initialfile=var.get())
        if path:
            var.set(path)

    def _on_threshold_change(self, *_):
        self.threshold = _to_float(self.threshold_var.get(), 0.0)
        self._update_threshold_line()
        self._stats_dirty = True

    def _parse_hosts(self):
        raw = self.host_var.get().replace(";", ",").replace(" ", ",")
        seen, hosts = set(), []
        for h in raw.split(","):
            h = h.strip()
            if h and h.lower() not in seen:
                seen.add(h.lower())
                hosts.append(h)
        return hosts

    # ------------------------------------------------------------------
    # Lancement / arrêt
    # ------------------------------------------------------------------
    def _toggle_ping(self):
        if self.ping_running:
            self.stop_ping()
        else:
            self.start_ping()

    def start_ping(self):
        hosts = self._parse_hosts()
        if not hosts:
            messagebox.showerror("Erreur", "Veuillez saisir au moins une adresse.")
            return

        self.continuous = self.continuous_var.get()
        if not self.continuous:
            try:
                self.duration = int(self.duration_var.get())
                if self.duration <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Erreur", "La durée doit être un entier positif (Réglages).")
                return
        else:
            self.duration = 0
        self.interval = max(MIN_INTERVAL, _to_float(self.interval_var.get(), 1.0))

        log_file = self.log_file_var.get().strip()
        if not log_file:
            messagebox.showerror("Erreur", "Veuillez indiquer un fichier journal (Réglages).")
            return
        try:
            self.log_fh = open(log_file, "w", encoding="utf-8", buffering=1)   # vidé à chaque ligne
        except Exception as e:
            messagebox.showerror("Erreur", f"Impossible d'ouvrir le journal : {e}")
            return

        self._setup_graph(hosts)
        self.last_alert.clear()
        self.stop_event.clear()
        self.ping_running = True
        self.stopping = False
        self.mode = "live"
        self.start_time = time.monotonic()
        self.workers_active = len(hosts)
        self._update_controls()
        self.show_view("dashboard")

        rate = "1 ping/s" if self.interval == 1 else f"toutes les {fr_num(self.interval).rstrip('0').rstrip(',')} s"
        span = "en continu" if self.continuous else f"pendant {fmt_duration(self.duration)}"
        self.event("Surveillance démarrée", f"{len(hosts)} adresse{'s' if len(hosts) > 1 else ''}, {rate}, {span}",
                   "info")
        self.log(f"--- Démarrage : {', '.join(hosts)} "
                 f"({'continu' if self.continuous else str(self.duration) + 's'}, "
                 f"intervalle {self.interval}s) ---", "info")

        for host in hosts:
            threading.Thread(target=self._ping_worker, args=(host,), daemon=True).start()

    def stop_ping(self):
        if self.ping_running and not self.stopping:
            self.stop_event.set()
            self.stopping = True
            self._update_controls()

    def _ping_worker(self, host):
        # Cadence fixe (horloge monotone) : un ping toutes les `interval` s, quelle
        # que soit sa durée ; Event.wait rend l'arrêt immédiat entre deux pings.
        next_at = time.monotonic()
        deadline = next_at + self.duration - 1e-6   # tolérance aux arrondis de next_at += interval
        try:
            while not self.stop_event.is_set():
                ts = now_str()
                try:
                    lat, _ = ping_once(host)
                    self._write_log(format_log_line(ts, host, "OK" if lat is not None else "TIMEOUT", lat))
                    self._post(self._on_ping_result, host, ts, lat)
                except Exception as e:
                    self._write_log(format_log_line(ts, host, "ERROR", extra=e))
                    self._post(self._on_ping_error, host, str(e))

                # Ping plus long que l'intervalle : on repart de maintenant, sans rattrapage.
                next_at = max(next_at + self.interval, time.monotonic())
                if not self.continuous and next_at >= deadline:
                    break
                self.stop_event.wait(next_at - time.monotonic())
        finally:
            self._post(self._on_worker_done)

    def _write_log(self, line):
        with self.log_lock:
            if self.log_fh:
                self.log_fh.write(line + "\n")

    def _close_log(self):
        with self.log_lock:
            if self.log_fh:
                self.log_fh.close()
                self.log_fh = None

    def _on_ping_result(self, host, ts, lat):
        st = self.hosts_state.get(host)
        if st is None:
            return
        prev_last, prev_total, prev_streak = st.last, st.stats.total, st.lost_streak
        st.add(lat)
        prefix = f"[{host}] " if len(self.host_order) > 1 else ""
        thr = self.threshold
        if prev_streak and lat is not None:
            self.event(f"{host} répond à nouveau",
                       f"après {prev_streak} ping{'s' if prev_streak > 1 else ''} perdu{'s' if prev_streak > 1 else ''}",
                       "ok")
        if lat is None:
            self.log(f"{ts} - {prefix}DÉLAI DÉPASSÉ (timeout)", "err")
            self._maybe_alert(host, "outage")
            if not prev_streak:
                self.event(f"{host} ne répond pas", "ping sans réponse", "err")
        elif thr > 0 and lat >= thr:
            self.log(f"{ts} - {prefix}temps={lat} ms  ⚠ seuil", "warn")
            self._maybe_alert(host, "high")
            if not (prev_total and prev_last is not None and prev_last >= thr):
                avg = st.stats.summary()["avg"]
                ratio = f" · {lat / avg:.0f}× la moyenne" if avg and lat / avg >= 2 else ""
                self.event(f"{host} lent · {lat} ms", f"au-dessus du seuil de {thr:g} ms{ratio}", "warn")
        else:
            self.log(f"{ts} - {prefix}temps={lat} ms", "ok")
        self._graph_dirty = self._stats_dirty = True

    def _on_ping_error(self, host, message):
        self.log(f"ERREUR [{host}]: {message}", "err")
        self.event(f"Ping impossible vers {host}", message, "err")

    def _on_worker_done(self):
        self.workers_active -= 1
        if self.workers_active <= 0:
            self._finish_ping()

    def _finish_ping(self):
        self.ping_running = False
        self.stopping = False
        self.mode = "done"
        self.end_time = time.monotonic()
        self._close_log()
        self._update_controls()
        total = sum(s.stats.total for s in self.hosts_state.values())
        lost = sum(s.stats.lost for s in self.hosts_state.values())
        self.event("Surveillance terminée",
                   f"{fr_int(total)} pings en {fmt_duration(self.end_time - self.start_time)}, "
                   f"{fr_int(lost)} perdu{'s' if lost > 1 else ''}", "info")
        self.log(f"--- Ping terminé. Log : {self.log_file_var.get()} ---", "info")
        self._stats_dirty = True

    # ------------------------------------------------------------------
    # Alertes (son + clignotement)
    # ------------------------------------------------------------------
    def _maybe_alert(self, host, kind):
        if not self.alerts_var.get():
            return
        now = time.monotonic()
        key = (host, kind)
        if key in self.last_alert and now - self.last_alert[key] < ALERT_COOLDOWN:
            return
        self.last_alert[key] = now
        if winsound:
            try:
                flag = winsound.MB_ICONHAND if kind == "outage" else winsound.MB_ICONEXCLAMATION
                winsound.MessageBeep(flag)
            except Exception:
                pass
        self._flash_taskbar()

    # ------------------------------------------------------------------
    # Graphe, cartes, diagnostic
    # ------------------------------------------------------------------
    def _reset_axes(self):
        ax = self.ax
        ax.clear()
        ax.set_facecolor(SURFACE)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(AXIS)
        ax.tick_params(colors=TEXT_3, labelsize=9, length=0, pad=6)
        ax.grid(True, axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        self.threshold_line = ax.axhline(0, linestyle="--", linewidth=1.0, color=WARN_COLOR,
                                         alpha=0.8, visible=False)
        self._update_threshold_line()

    def _update_threshold_line(self):
        if self.threshold > 0:
            self.threshold_line.set_ydata([self.threshold, self.threshold])
        self.threshold_line.set_visible(self.threshold > 0)
        self._graph_dirty = True

    def _setup_graph(self, hosts, window=LIVE_WINDOW):
        self.hosts_state = {}
        self.host_order = list(hosts)
        self._reset_axes()
        for i, host in enumerate(hosts):
            color = host_color(i, self.accent)
            st = HostState(host, color, window)
            st.line, = self.ax.plot([], [], linewidth=1.6, color=color, label=host)
            st.lost_line, = self.ax.plot([], [], "x", markersize=6, markeredgewidth=1.6, color=color)
            self.hosts_state[host] = st
        self._rebuild_legend()
        self._rebuild_cards()
        self.canvas.draw_idle()
        self._stats_dirty = self._graph_dirty = True

    def _rebuild_legend(self):
        for child in self.legend.winfo_children():
            child.destroy()
        for host in self.host_order[:6]:
            st = self.hosts_state[host]
            ctk.CTkFrame(self.legend, width=12, height=3, corner_radius=1, fg_color=st.color).pack(side="left", padx=(12, 6))
            ctk.CTkLabel(self.legend, text=_ellipsize(host, 18), font=self.f_small, text_color="#c9d6ec",
                         height=18).pack(side="left")
        if len(self.host_order) > 6:
            ctk.CTkLabel(self.legend, text=f"  +{len(self.host_order) - 6}", font=self.f_small,
                         text_color=TEXT_2, height=18).pack(side="left")

    def _rebuild_cards(self):
        for child in self.cards_frame.winfo_children():
            child.destroy()
        for i in range(MAX_CARD_COLUMNS):
            self.cards_frame.columnconfigure(i, weight=0, uniform="")
        if not self.host_order:
            card = self._card(self.cards_frame)
            card.grid(row=0, column=0, sticky="ew")
            self.cards_frame.columnconfigure(0, weight=1)
            ctk.CTkLabel(card, text="Aucune adresse surveillée", font=self.f_card_title,
                         text_color=TEXT).pack(anchor="w", padx=18, pady=(16, 2))
            ctk.CTkLabel(card, text="Exemple : votre box (192.168.1.1), un DNS (1.1.1.1) et un site (8.8.8.8). "
                                    "Comparer les trois montre d'où vient un problème.",
                         font=self.f_body, text_color=TEXT_2).pack(anchor="w", padx=18, pady=(0, 16))
            return
        cols = min(len(self.host_order), MAX_CARD_COLUMNS)
        for i in range(cols):
            self.cards_frame.columnconfigure(i, weight=1, uniform="card")
        for i, host in enumerate(self.host_order):
            st = self.hosts_state[host]
            st.card = HostCard(self.cards_frame, self, st)
            r, col = divmod(i, cols)
            st.card.grid(row=r, column=col, sticky="nsew",
                         padx=(0 if col == 0 else 8, 0 if col == cols - 1 else 8), pady=(0 if r == 0 else 16, 0))

    def _redraw_graph(self):
        lo = hi = 0
        shown = [st for st in self.hosts_state.values() if st.xs]
        for st in self.hosts_state.values():
            st.line.set_data(st.xs, st.ys)
            st.lost_line.set_data(st.lost_x, [0] * len(st.lost_x))
            if st.card:
                st.card.draw_spark()
        if shown:
            lo = min(st.xs[0] for st in shown)
            hi = max(st.xs[-1] for st in shown)
        if shown:
            self.ax.relim(visible_only=True)
            self.ax.autoscale_view(scalex=False)
        else:   # rien à tracer : une échelle neutre plutôt qu'un zoom sur la ligne de seuil
            self.ax.set_ylim(0, max(50, self.threshold * 1.25), auto=None)
        self.ax.set_xlim(lo, max(lo + 10, hi))
        self.canvas.draw_idle()

    def _refresh_overview(self):
        for st in self.hosts_state.values():
            if st.card:
                st.card.refresh(self.threshold)
        self._refresh_verdict()

    def _overall_score(self):
        scores = [compute_quality(st.stats.summary())[0] for st in self.hosts_state.values() if st.stats.total]
        return min(scores) if scores else None       # l'adresse la plus faible donne le ton

    def _refresh_verdict(self):
        states = list(self.hosts_state.values())
        total = sum(st.stats.total for st in states)
        lost = sum(st.stats.lost for st in states)
        self.total_sent_var.set(fr_int(total))
        self.total_lost_var.set(fr_int(lost))
        n = len(states)
        if not total:
            if self.ping_running:
                title, sub = "Démarrage…", f"Premier ping vers {n} adresse{'s' if n > 1 else ''}…"
            else:
                title, sub = "Prêt", "Saisissez une ou plusieurs adresses en haut, puis cliquez sur Démarrer."
            self.verdict_title.configure(text=title)
            self.verdict_sub.configure(text=sub)
            self._draw_ring()
            return
        score = self._overall_score()
        letter = score_letter(score)
        down = [st for st in states if st.stats.total and st.last is None]
        slow = [st for st in states if st.last_kind(self.threshold) == "high"]
        if down and self.mode == "live":
            title = ("Coupure en cours" if len(down) == n else
                     f"{down[0].host} ne répond pas" if len(down) == 1 else
                     f"{len(down)} adresses ne répondent pas")
        elif slow and self.mode == "live":
            title = "Connexion ralentie"
        else:
            title = {"A": "Tout va bien", "B": "Connexion correcte", "C": "Connexion moyenne"}.get(
                letter, "Connexion dégradée")
        outages = sum(st.outages for st in states)
        answering = n - len(down)
        if self.mode == "analysis":
            where = "dans le fichier"
        else:
            end = time.monotonic() if self.ping_running else self.end_time
            where = f"en {fmt_duration(end - self.start_time)}"
        self.verdict_title.configure(text=title)
        self.verdict_sub.configure(
            text=f"{answering} adresse{'s' if answering > 1 else ''} sur {n} "
                 f"{'répondent' if answering > 1 else 'répond'} · "
                 f"{outages} coupure{'s' if outages > 1 else ''} {where} · qualité {score}/100")
        self._draw_ring(score, letter)

    def _draw_ring(self, score=None, letter="—"):
        c = self.ring
        c.delete("all")
        size = int(c.cget("width"))
        pad, width = 4 * self.scale, 5 * self.scale
        c.create_oval(pad, pad, size - pad, size - pad, outline=BORDER, width=width)
        if score is not None:
            color = self.accent if score >= 75 else CHIP_WARN[0] if score >= 40 else CHIP_ERR[0]
            if score >= 100:
                c.create_oval(pad, pad, size - pad, size - pad, outline=color, width=width)
            elif score > 0:
                c.create_arc(pad, pad, size - pad, size - pad, start=90, extent=-score * 3.6,
                             style="arc", outline=color, width=width)
        c.create_text(size / 2, size / 2, text=letter, fill=TEXT,
                      font=(self.ui_family or "TkDefaultFont", 13, "bold"))

    def _refresh_pill(self, now):
        if self.mode == "live":
            elapsed = now - self.start_time
            text = f"EN DIRECT · {fmt_clock(elapsed)}"
            if not self.continuous:
                text += f" / {fmt_clock(self.duration)}"
            dot = LIVE_DOT
        elif self.mode == "done":
            text, dot = f"TERMINÉ · {fmt_clock(self.end_time - self.start_time)}", TEXT_3
        elif self.mode == "analysis":
            text, dot = "FICHIER ANALYSÉ", self.accent
        else:
            text, dot = "PRÊT", TEXT_3
        if (text, dot) != self._pill_text:
            self._pill_text = (text, dot)
            self.pill_label.configure(text=text)
            self.pill_dot.configure(fg_color=dot)

    def clear_all(self):
        if self.ping_running or self.analyzing:
            return
        self.hosts_state = {}
        self.host_order = []
        self.mode = "idle"
        self._reset_axes()
        self._rebuild_legend()
        self._rebuild_cards()
        self.canvas.draw_idle()
        self._pending_log = []
        self.log_area.configure(state="normal")
        self.log_area.delete("1.0", "end")
        self.log_area.configure(state="disabled")
        self._events_hint()
        self._refresh_verdict()

    # ------------------------------------------------------------------
    # Analyse hors-ligne
    # ------------------------------------------------------------------
    def start_analyze(self):
        log_file = self.log_file_var.get().strip()
        if not Path(log_file).exists():
            messagebox.showerror("Erreur", f"Le fichier {log_file} n'existe pas.")
            return
        self.analyzing = True
        self._update_controls()
        self.event("Analyse du fichier en cours…", log_file, "info")
        self.log("--- Démarrage de l'analyse ---", "info")
        threading.Thread(target=self._analyze_worker,
                         args=(log_file, self.csv_file_var.get().strip(),
                               self.plot_prefix_var.get().strip() or "ping", self.accent),
                         daemon=True).start()

    def _analyze_worker(self, log_file, csv_file, plot_prefix, accent):
        try:
            rows = parse_log_file(log_file)
            groups = group_by_host(rows)
            summaries = {host: summarize(lats) for host, lats in groups.items()}
            write_csv(rows, summaries, csv_file)
            plots = save_plots(groups, plot_prefix, accent)
            self._post(self._show_analysis_result, len(rows), groups, summaries, csv_file, plots)
        except Exception as e:
            self._post(self._analysis_error, str(e))

    def _show_analysis_result(self, n_rows, groups, summaries, csv_file, plots):
        self.analyzing = False
        self.mode = "analysis"
        self._setup_graph(list(groups), window=None)
        for host, lats in groups.items():
            st = self.hosts_state[host]
            for lat in lats:
                st.add(lat)
        self._update_controls()
        self.show_view("dashboard")

        self.log(f"--- Analyse : {n_rows} mesures, {len(groups)} hôte(s) ---", "info")
        for host, s in summaries.items():
            score, letter = compute_quality(s)
            if s["avg"] is not None:
                self.log(f"[{host}] {s['total']} pings, perte {s['loss_pct']:.1f}%, "
                         f"moy {s['avg']:.0f} ms, score {score} ({letter})",
                         "err" if s["lost"] else "ok")
            else:
                self.log(f"[{host}] {s['total']} pings, perte {s['loss_pct']:.1f}%", "err")
        self.log(f"CSV: {csv_file}  |  Graphiques: {', '.join(plots)}", "info")
        self.event("Fichier analysé",
                   f"{fr_int(n_rows)} mesures, {len(groups)} adresse{'s' if len(groups) > 1 else ''}", "info")
        self.event("Exports enregistrés", f"{csv_file}, {', '.join(plots)}", "neutral")
        # Après ce tick, pour que le tableau de bord soit à jour derrière la boîte.
        self.root.after_idle(messagebox.showinfo, "Terminé", "Analyse terminée avec succès !")

    def _analysis_error(self, message):
        self.analyzing = False
        self._update_controls()
        self.log(f"ERREUR pendant l'analyse: {message}", "err")
        self.event("Échec de l'analyse", message, "err")

    # ------------------------------------------------------------------
    # Test de débit (Cloudflare)
    # ------------------------------------------------------------------
    def _toggle_speedtest(self):
        if self.speed_running:
            self.speed_stop.set()
            self.btn_speed.configure(state="disabled")
            self.speed_phase_var.set("Annulation…")
            return
        self.speed_stop.clear()
        self.speed_running = True
        self.dl_var.set("…")
        self.ul_var.set("—")
        self.lat_var.set("—")
        self.speed_progress.set(0)
        self.speed_phase_var.set("Mesure de la latence…")
        self.btn_speed.configure(text="■  Annuler", fg_color=SURFACE, hover_color=HOVER,
                                 text_color=CHIP_ERR[0], border_width=1, border_color=FIELD_BORDER)
        self.log("--- Test de débit ---", "info")
        threading.Thread(target=self._speedtest_worker, daemon=True).start()

    def _speedtest_worker(self):
        stop = self.speed_stop
        dl = ul = lat = None
        error = None

        def progress(phase, which):
            return lambda frac, mbps: self._post(self._speed_ui, phase, frac, mbps, which)

        try:
            lat = measure_latency(stop)
            if not stop.is_set():
                self._post(self._speed_ui, "Réception…", 0.0, None, "dl")
                dl = measure_download(stop, progress("Réception…", "dl"))
            if not stop.is_set():
                self._post(self._speed_ui, "Envoi…", 0.0, None, "ul")
                ul = measure_upload(stop, progress("Envoi…", "ul"))
        except Exception as e:
            error = str(e) or type(e).__name__
        # Les mesures déjà obtenues sont conservées même en cas d'erreur ou d'annulation.
        self._post(self._speed_done, dl, ul, lat, stop.is_set(), error)

    def _speed_ui(self, phase, frac, mbps, which):
        self.speed_phase_var.set(phase)
        self.speed_progress.set(frac)
        if mbps is not None:
            (self.dl_var if which == "dl" else self.ul_var).set(f"{mbps:.0f}" if mbps >= 100 else fr_num(mbps))

    def _speed_done(self, dl, ul, lat, cancelled, error):
        self.speed_running = False
        self.speed_progress.set(0)
        self.btn_speed.configure(text="▶  Tester le débit", state="normal", fg_color=self.accent,
                                 hover_color=self.accent_hover, text_color=ON_ACCENT, border_width=0)

        def mbps(v):
            return "—" if v is None else f"{v:.0f}" if v >= 100 else fr_num(v)

        self.dl_var.set(mbps(dl))
        self.ul_var.set(mbps(ul))
        self.lat_var.set(f"{lat:.0f}" if lat is not None else "—")
        if error:
            self.speed_phase_var.set(f"Échec du test : {error}")
            self.log(f"ERREUR test de débit : {error}", "err")
            self.event("Échec du test de débit", error, "err")
        elif cancelled:
            self.speed_phase_var.set("Test annulé.")
            self.log("Test de débit annulé.", "warn")
            self.event("Test de débit annulé", "", "warn")
        else:
            stamp = datetime.datetime.now().strftime("%H:%M")
            self.speed_phase_var.set(f"Terminé à {stamp}.")
            self.log(f"Débit : ↓ {dl:.1f} Mbps   ↑ {ul:.1f} Mbps"
                     + (f"   (latence {lat:.0f} ms)" if lat is not None else ""), "info")
            self.event("Débit mesuré", f"réception {mbps(dl)} Mbit/s · envoi {mbps(ul)} Mbit/s", "info")

    # ------------------------------------------------------------------
    # Réduction (barre des tâches) + fermeture
    # ------------------------------------------------------------------
    def _flash_taskbar(self):
        """Fait clignoter le bouton dans la barre des tâches (visible si réduit)."""
        try:
            user32 = ctypes.windll.user32
            user32.GetParent.restype = ctypes.c_void_p
            user32.GetParent.argtypes = [ctypes.c_void_p]
            hwnd = user32.GetParent(self.root.winfo_id())
            info = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG, 4, 0)
            user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def _on_close(self):
        # La croix réduit dans la barre des tâches ; l'app continue de tourner.
        try:
            self.root.iconify()
            if not self._min_hint_shown:
                self._min_hint_shown = True
                self.event("Fenêtre réduite", "La surveillance continue. Le bouton Quitter ferme l'application.", "info")
            return
        except Exception:
            pass
        self._real_quit()

    def _real_quit(self):
        self.stop_event.set()
        self.speed_stop.set()
        self._close_log()
        self.save_config()
        self.root.destroy()


if __name__ == "__main__":
    cfg = load_config()
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    try:
        root.iconbitmap(resource_path("ping_tool_ico.ico"))
    except Exception:
        pass
    app = PingApp(root, cfg)
    root.mainloop()
