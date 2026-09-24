import tkinter as tk
from tkinter import messagebox, filedialog, colorchooser
import ctypes
import json
import math
import os
import queue
import re
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

from ping_core import (CSV_FILE, DEFAULT_ACCENT, LOG_FILE, RunningStats, compute_quality, format_log_line,
                       group_by_host, host_color, measure_download, measure_latency, measure_upload,
                       now_str, parse_log_file, ping_once, save_plots, summarize, write_csv)

try:
    import winsound
except ImportError:
    winsound = None

# --- Couleurs ---
DANGER = "#ef4444"
DANGER_HOVER = "#dc2626"
OK_COLOR = "#22c55e"
WARN_COLOR = "#f59e0b"
ERR_COLOR = "#ef4444"
IDLE_COLOR = "#9ca3af"

PRESET_ACCENTS = {
    "Indigo": "#6366f1", "Bleu": "#3b82f6", "Cyan": "#06b6d4", "Vert": "#22c55e",
    "Violet": "#a855f7", "Rose": "#ec4899", "Ambre": "#f59e0b",
}

LIGHT_PLOT = {"fig": "#dbdbdb", "ax": "#ffffff", "fg": "#1f2937", "grid": "#c8c8c8"}
DARK_PLOT = {"fig": "#2b2b2b", "ax": "#1e1e1e", "fg": "#e5e7eb", "grid": "#404040"}

CONFIG_PATH = Path.home() / ".pingtester.json"
ALERT_COOLDOWN = 30.0

UI_TICK_MS = 150        # rafraîchissement groupé de l'interface
GRAPH_MIN_PERIOD = 0.5  # s entre deux rendus du graphe (le rendu est le poste le plus coûteux)
LIVE_WINDOW = 1800      # points affichés par hôte sur le graphe en direct (30 min à 1 s)
MAX_LOG_LINES = 5000    # lignes conservées dans l'onglet Journal
MIN_INTERVAL = 0.2      # s

STATUS_RANK = {"idle": 0, "ok": 1, "high": 2, "timeout": 3}
STATUS_COLOR = {"idle": IDLE_COLOR, "ok": OK_COLOR, "high": WARN_COLOR, "timeout": ERR_COLOR}
HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")

DEFAULT_CONFIG = {
    "hosts": "8.8.8.8", "duration": "60", "continuous": False,
    "threshold": "100", "interval": "1.0",
    "log_file": LOG_FILE, "csv_file": CSV_FILE, "plot_prefix": "ping",
    "dark": False, "accent": DEFAULT_ACCENT,
    "alerts": True,
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
    if not HEX_COLOR_RE.fullmatch(cfg["accent"]):
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
        self.xs = deque(maxlen=window)    # index du ping
        self.ys = deque(maxlen=window)    # latence, NaN si perdu
        self.lost_x = deque()             # index des pertes encore dans la fenêtre
        self.line = None
        self.lost_line = None

    def add(self, lat):
        i = self.stats.total
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


class PingApp:
    def __init__(self, root, config=None):
        self.root = root
        self.root.title("Outil de Ping & Analyse")
        # CTk applique un facteur DPI à la géométrie ; winfo_screen* renvoie des
        # pixels physiques. On divise par le scaling pour ne pas déborder l'écran.
        try:
            scale = ctk.ScalingTracker.get_window_scaling(self.root)
        except Exception:
            scale = 1.0
        try:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        except Exception:
            sw, sh = 1536, 864
        w = int(min(1040, (sw - 70) / scale))
        h = int(min(820, (sh - 100) / scale))
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(760, 520)

        self.config = config or load_config()
        self.accent = self.config["accent"]
        self.accent_hover = darken(self.accent)
        self.outline_hover = ("#eef2ff", "#312e81")
        self.outline_btns = []
        self.accent_switches = []
        self.swatches = {}

        ctk.set_appearance_mode("dark" if self.config["dark"] else "light")

        # Les threads ne touchent jamais Tk : ils déposent (fonction, args) dans
        # ui_queue, vidée par _pump sur le thread principal. Journal, graphe et
        # stats sont ensuite rafraîchis une seule fois par tick.
        self.ui_queue = queue.Queue()
        self._pending_log = []
        self._graph_dirty = False
        self._stats_dirty = False
        self._next_graph_draw = 0.0

        self.stop_event = threading.Event()
        self.ping_running = False
        self.start_time = 0.0
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

        # Test de débit
        self.speed_running = False
        self.speed_stop = threading.Event()

        self._build_widgets()
        self._toggle_continuous()
        self.threshold_var.trace_add("write", self._on_threshold_change)
        self._on_threshold_change()
        self.apply_accent(self.accent)
        self._apply_theme()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(UI_TICK_MS, self._pump)

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------
    def _build_widgets(self):
        c = self.config
        self.title_font = ctk.CTkFont(size=20, weight="bold")
        self.section_font = ctk.CTkFont(size=14, weight="bold")
        self.mono_font = ctk.CTkFont(family="Consolas", size=12)

        outer = ctk.CTkFrame(self.root, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=14, pady=8)

        # ---- En-tête ----
        header = ctk.CTkFrame(outer, fg_color="transparent")
        header.pack(fill="x", pady=(0, 6))
        try:
            pil = Image.open(resource_path("ping_tool_ico.ico")).convert("RGBA")
            self.logo_img = ctk.CTkImage(light_image=pil, dark_image=pil, size=(32, 32))
        except Exception:
            self.logo_img = None
        ctk.CTkLabel(header, image=self.logo_img, text="  Outil de Ping & Analyse",
                     compound="left", font=self.title_font).pack(side="left")

        self.btn_quit = ctk.CTkButton(header, text="Quitter", width=84, height=28, corner_radius=14,
                                      fg_color=DANGER, hover_color=DANGER_HOVER, command=self._real_quit)
        self.btn_quit.pack(side="right", padx=(10, 0))

        self.dark_var = tk.BooleanVar(value=c["dark"])
        self.dark_switch = ctk.CTkSwitch(header, text="Mode sombre", variable=self.dark_var,
                                         command=self._toggle_theme, progress_color=self.accent)
        self.dark_switch.pack(side="right")
        self.accent_switches.append(self.dark_switch)

        accent_frame = ctk.CTkFrame(header, fg_color="transparent")
        accent_frame.pack(side="right", padx=(0, 16))
        ctk.CTkLabel(accent_frame, text="Accent").pack(side="left", padx=(0, 8))
        for name, color in PRESET_ACCENTS.items():
            b = ctk.CTkButton(accent_frame, text="", width=22, height=22, corner_radius=11,
                              fg_color=color, hover_color=color,
                              command=lambda c=color: self.apply_accent(c))
            b.pack(side="left", padx=2)
            self.swatches[color] = b
        ctk.CTkButton(accent_frame, text="🎨", width=28, height=22, corner_radius=11,
                      fg_color="transparent", hover_color=self.outline_hover,
                      command=self._pick_custom_accent).pack(side="left", padx=(6, 0))

        # ---- Barre principale : hôtes + actions ----
        top = ctk.CTkFrame(outer, corner_radius=14)
        top.pack(fill="x", pady=6)
        top.columnconfigure(1, weight=1)
        ctk.CTkLabel(top, text="Hôtes (séparés par virgule)").grid(
            row=0, column=0, sticky="w", padx=(16, 8), pady=(12, 6))
        self.host_var = tk.StringVar(value=c["hosts"])
        ctk.CTkEntry(top, textvariable=self.host_var, corner_radius=8).grid(
            row=0, column=1, sticky="ew", padx=(0, 16), pady=(12, 6))
        btnf = ctk.CTkFrame(top, fg_color="transparent")
        btnf.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
        for i in range(4):
            btnf.columnconfigure(i, weight=1, uniform="btn")
        self.btn_ping = ctk.CTkButton(btnf, text="▶  Lancer Ping", height=40, corner_radius=18,
                                      font=self.section_font, command=self.start_ping)
        self.btn_ping.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.btn_stop = ctk.CTkButton(btnf, text="■  Stop", height=40, corner_radius=18,
                                      font=self.section_font, fg_color=DANGER, hover_color=DANGER_HOVER,
                                      state="disabled", command=self.stop_ping)
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=6)
        self.btn_analyze = ctk.CTkButton(btnf, text="📊  Analyser", height=40, corner_radius=18,
                                         font=self.section_font, fg_color="transparent", border_width=2,
                                         command=self.start_analyze)
        self.btn_analyze.grid(row=0, column=2, sticky="ew", padx=6)
        self.btn_clear = ctk.CTkButton(btnf, text="🗑  Effacer", height=40, corner_radius=18,
                                       font=self.section_font, fg_color="transparent", border_width=2,
                                       command=self.clear_all)
        self.btn_clear.grid(row=0, column=3, sticky="ew", padx=(6, 0))

        # ---- Barre de statut ----
        status_card = ctk.CTkFrame(outer, corner_radius=14)
        status_card.pack(fill="x", pady=6)
        self.status_dot = ctk.CTkFrame(status_card, width=14, height=14, corner_radius=7, fg_color=IDLE_COLOR)
        self.status_dot.grid(row=0, column=0, padx=(16, 8), pady=10)
        self.status_var = tk.StringVar(value="Prêt.")
        ctk.CTkLabel(status_card, textvariable=self.status_var).grid(row=0, column=1, sticky="w", pady=10)
        status_card.columnconfigure(2, weight=1)
        self.stats_var = tk.StringVar(value="Envoyés: 0   Perdus: 0 (0.0%)")
        ctk.CTkLabel(status_card, textvariable=self.stats_var, font=self.mono_font).grid(
            row=0, column=2, sticky="e", padx=16, pady=10)

        self.progress = ctk.CTkProgressBar(outer, corner_radius=8)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=(2, 6))

        # ---- Onglets ----
        self.tabview = ctk.CTkTabview(outer, corner_radius=14)
        self.tabview.pack(fill="both", expand=True, pady=6)
        graph_tab = self.tabview.add("Graphique")
        stats_tab = self.tabview.add("Stats")
        log_tab = self.tabview.add("Journal")
        deb_tab = self.tabview.add("Débit")
        reg_tab = self.tabview.add("Réglages")

        self.fig = Figure(figsize=(8, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=graph_tab)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=6)
        self._reset_axes()

        self.stats_box = ctk.CTkTextbox(stats_tab, corner_radius=8, font=self.mono_font, state="disabled")
        self.stats_box.pack(fill="both", expand=True, padx=6, pady=6)

        self.log_area = ctk.CTkTextbox(log_tab, corner_radius=8, font=self.mono_font, state="disabled")
        self.log_area.pack(fill="both", expand=True, padx=6, pady=6)

        # ---- Onglet Débit ----
        self.speed_big_font = ctk.CTkFont(size=30, weight="bold")
        dframe = ctk.CTkFrame(deb_tab, fg_color="transparent")
        dframe.pack(fill="both", expand=True, padx=10, pady=10)
        ctk.CTkLabel(dframe, text="Test de débit (via Cloudflare)", font=self.section_font).pack(anchor="w")
        self.btn_speed = ctk.CTkButton(dframe, text="▶  Tester le débit", height=42, corner_radius=18,
                                       font=self.section_font, command=self._toggle_speedtest)
        self.btn_speed.pack(fill="x", pady=(10, 6))
        self.speed_progress = ctk.CTkProgressBar(dframe, corner_radius=8)
        self.speed_progress.set(0)
        self.speed_progress.pack(fill="x", pady=(0, 4))
        self.speed_phase_var = tk.StringVar(value="Prêt à tester.")
        ctk.CTkLabel(dframe, textvariable=self.speed_phase_var).pack(anchor="w", pady=(0, 10))

        cards = ctk.CTkFrame(dframe, fg_color="transparent")
        cards.pack(fill="x")
        for i in range(3):
            cards.columnconfigure(i, weight=1, uniform="spd")
        self.dl_var = tk.StringVar(value="—")
        self.ul_var = tk.StringVar(value="—")
        self.lat_var = tk.StringVar(value="—")
        for col, (title, var) in enumerate((("Download (Mbps)", self.dl_var),
                                            ("Upload (Mbps)", self.ul_var),
                                            ("Latence serveur (ms)", self.lat_var))):
            card = ctk.CTkFrame(cards, corner_radius=14)
            card.grid(row=0, column=col, sticky="ew", padx=6)
            ctk.CTkLabel(card, text=title).pack(pady=(14, 2))
            ctk.CTkLabel(card, textvariable=var, font=self.speed_big_font).pack(pady=(0, 14))

        # ---- Onglet Réglages (défilable) ----
        rs = ctk.CTkScrollableFrame(reg_tab, fg_color="transparent")
        rs.pack(fill="both", expand=True, padx=4, pady=4)

        pc = ctk.CTkFrame(rs, corner_radius=14)
        pc.pack(fill="x", pady=6)
        ctk.CTkLabel(pc, text="Paramètres", font=self.section_font).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=16, pady=(12, 6))
        pc.columnconfigure(1, weight=1)
        pc.columnconfigure(3, weight=1)
        ctk.CTkLabel(pc, text="Durée (s)").grid(row=1, column=0, sticky="w", padx=(16, 8), pady=6)
        self.duration_var = tk.StringVar(value=c["duration"])
        self.duration_entry = ctk.CTkEntry(pc, textvariable=self.duration_var, width=120, corner_radius=8)
        self.duration_entry.grid(row=1, column=1, sticky="w", pady=6)
        ctk.CTkLabel(pc, text="Intervalle (s)").grid(row=1, column=2, sticky="w", padx=(0, 8), pady=6)
        self.interval_var = tk.StringVar(value=c["interval"])
        ctk.CTkEntry(pc, textvariable=self.interval_var, width=120, corner_radius=8).grid(
            row=1, column=3, sticky="w", padx=(0, 16), pady=6)
        self.continuous_var = tk.BooleanVar(value=c["continuous"])
        sw_cont = ctk.CTkSwitch(pc, text="Ping en continu", variable=self.continuous_var,
                                command=self._toggle_continuous, progress_color=self.accent)
        sw_cont.grid(row=2, column=0, columnspan=2, sticky="w", padx=16, pady=(6, 14))
        self.accent_switches.append(sw_cont)
        ctk.CTkLabel(pc, text="Seuil d'alerte (ms, 0=off)").grid(row=2, column=2, sticky="w", padx=(0, 8), pady=(6, 14))
        self.threshold_var = tk.StringVar(value=c["threshold"])
        ctk.CTkEntry(pc, textvariable=self.threshold_var, width=120, corner_radius=8).grid(
            row=2, column=3, sticky="w", padx=(0, 16), pady=(6, 14))

        fc = ctk.CTkFrame(rs, corner_radius=14)
        fc.pack(fill="x", pady=6)
        ctk.CTkLabel(fc, text="Fichiers", font=self.section_font).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(12, 6))
        fc.columnconfigure(1, weight=1)
        ctk.CTkLabel(fc, text="Fichier Log").grid(row=1, column=0, sticky="w", padx=(16, 8), pady=6)
        self.log_file_var = tk.StringVar(value=c["log_file"])
        ctk.CTkEntry(fc, textvariable=self.log_file_var, corner_radius=8).grid(row=1, column=1, sticky="ew", pady=6)
        b_log = ctk.CTkButton(fc, text="Parcourir…", width=110, corner_radius=18,
                              fg_color="transparent", border_width=2,
                              command=lambda: self._browse(self.log_file_var, "Fichier log", ".txt", "Texte"))
        b_log.grid(row=1, column=2, padx=16, pady=6)
        ctk.CTkLabel(fc, text="Sortie CSV").grid(row=2, column=0, sticky="w", padx=(16, 8), pady=6)
        self.csv_file_var = tk.StringVar(value=c["csv_file"])
        ctk.CTkEntry(fc, textvariable=self.csv_file_var, corner_radius=8).grid(row=2, column=1, sticky="ew", pady=6)
        b_csv = ctk.CTkButton(fc, text="Parcourir…", width=110, corner_radius=18,
                              fg_color="transparent", border_width=2,
                              command=lambda: self._browse(self.csv_file_var, "Sortie CSV", ".csv", "CSV"))
        b_csv.grid(row=2, column=2, padx=16, pady=6)
        ctk.CTkLabel(fc, text="Préfixe Graphiques").grid(row=3, column=0, sticky="w", padx=(16, 8), pady=(6, 14))
        self.plot_prefix_var = tk.StringVar(value=c["plot_prefix"])
        ctk.CTkEntry(fc, textvariable=self.plot_prefix_var, corner_radius=8).grid(row=3, column=1, sticky="ew", pady=(6, 14))

        oc = ctk.CTkFrame(rs, corner_radius=14)
        oc.pack(fill="x", pady=6)
        ctk.CTkLabel(oc, text="Alertes", font=self.section_font).pack(anchor="w", padx=16, pady=(12, 6))
        orow = ctk.CTkFrame(oc, fg_color="transparent")
        orow.pack(fill="x", padx=16, pady=(0, 14))
        self.alerts_var = tk.BooleanVar(value=c["alerts"])
        s_alert = ctk.CTkSwitch(orow, text="Alertes (son + clignotement barre des tâches)",
                                variable=self.alerts_var, progress_color=self.accent)
        s_alert.pack(side="left", padx=(0, 18))
        self.accent_switches.append(s_alert)

        self.outline_btns = [self.btn_analyze, self.btn_clear, b_log, b_csv]

    def save_config(self):
        cfg = {
            "hosts": self.host_var.get(), "duration": self.duration_var.get(),
            "continuous": self.continuous_var.get(), "threshold": self.threshold_var.get(),
            "interval": self.interval_var.get(), "log_file": self.log_file_var.get(),
            "csv_file": self.csv_file_var.get(), "plot_prefix": self.plot_prefix_var.get(),
            "dark": self.dark_var.get(), "accent": self.accent,
            "alerts": self.alerts_var.get(),
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
        self.outline_hover = (_blend(color, (255, 255, 255), 0.86), _blend(color, (0, 0, 0), 0.55))
        self.btn_ping.configure(fg_color=self.accent, hover_color=self.accent_hover)
        if not self.speed_running:   # pendant le test, le bouton est « Annuler » (rouge)
            self.btn_speed.configure(fg_color=self.accent, hover_color=self.accent_hover)
        for b in self.outline_btns:
            b.configure(border_color=self.accent, text_color=self.accent, hover_color=self.outline_hover)
        for s in self.accent_switches:
            s.configure(progress_color=self.accent)
        self.progress.configure(progress_color=self.accent)
        self.speed_progress.configure(progress_color=self.accent)
        self.tabview.configure(segmented_button_selected_color=self.accent,
                               segmented_button_selected_hover_color=self.accent_hover)
        if len(self.host_order) == 1:
            st = self.hosts_state[self.host_order[0]]
            st.color = self.accent
            st.line.set_color(self.accent)
            st.lost_line.set_color(self.accent)
            self._refresh_legend()
            self.canvas.draw_idle()
        self.log_area.tag_config("info", foreground=self.accent)
        self._highlight_swatch()

    def _highlight_swatch(self):
        border = "#ffffff" if ctk.get_appearance_mode() == "Dark" else "#1f2937"
        for color, btn in self.swatches.items():
            btn.configure(border_width=2 if color.lower() == self.accent.lower() else 0, border_color=border)

    def _pick_custom_accent(self):
        result = colorchooser.askcolor(color=self.accent, title="Couleur d'accent")
        if result and result[1]:
            self.apply_accent(result[1])

    # ------------------------------------------------------------------
    # Thème
    # ------------------------------------------------------------------
    @staticmethod
    def _plot_palette():
        return DARK_PLOT if ctk.get_appearance_mode() == "Dark" else LIGHT_PLOT

    def _apply_theme(self):
        pal = self._plot_palette()
        self.fig.set_facecolor(pal["fig"])
        self.ax.set_facecolor(pal["ax"])
        for spine in self.ax.spines.values():
            spine.set_color(pal["grid"])
        self.ax.tick_params(colors=pal["fg"])
        self.ax.xaxis.label.set_color(pal["fg"])
        self.ax.yaxis.label.set_color(pal["fg"])
        self.ax.title.set_color(pal["fg"])
        self.ax.grid(True, color=pal["grid"], linewidth=0.5)
        self._refresh_legend()
        self.canvas.draw_idle()
        self.log_area.tag_config("ok", foreground=pal["fg"])
        self.log_area.tag_config("warn", foreground=WARN_COLOR)
        self.log_area.tag_config("err", foreground=ERR_COLOR)
        self.log_area.tag_config("info", foreground=self.accent)
        self._highlight_swatch()

    def _refresh_legend(self):
        if self.host_order:
            pal = self._plot_palette()
            self.ax.legend(loc="upper right", fontsize=8, facecolor=pal["ax"],
                           edgecolor=pal["grid"], labelcolor=pal["fg"])

    def _toggle_theme(self):
        ctk.set_appearance_mode("dark" if self.dark_var.get() else "light")
        self._apply_theme()

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
            self._refresh_stats()
            self._refresh_status()
        if self.ping_running and not self.continuous:
            self.progress.set(min(1.0, (now - self.start_time) / self.duration))

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

    def set_status(self, text, color=None):
        self.status_var.set(text)
        if color:
            self.status_dot.configure(fg_color=color)

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for b in (self.btn_ping, self.btn_analyze, self.btn_clear):
            b.configure(state=state)

    def _toggle_continuous(self):
        self.duration_entry.configure(state="disabled" if self.continuous_var.get() else "normal")

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
    def start_ping(self):
        hosts = self._parse_hosts()
        if not hosts:
            messagebox.showerror("Erreur", "Veuillez saisir au moins un hôte.")
            return

        self.continuous = self.continuous_var.get()
        if not self.continuous:
            try:
                self.duration = int(self.duration_var.get())
                if self.duration <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Erreur", "La durée doit être un entier positif.")
                return
        else:
            self.duration = 0
        self.interval = max(MIN_INTERVAL, _to_float(self.interval_var.get(), 1.0))

        log_file = self.log_file_var.get().strip()
        if not log_file:
            messagebox.showerror("Erreur", "Veuillez indiquer un fichier log.")
            return
        try:
            self.log_fh = open(log_file, "w", encoding="utf-8", buffering=1)   # vidé à chaque ligne
        except Exception as e:
            messagebox.showerror("Erreur", f"Impossible d'ouvrir le log : {e}")
            return

        self._setup_graph(hosts)
        self.last_alert.clear()
        self.stop_event.clear()
        self.ping_running = True
        self.start_time = time.monotonic()
        self.workers_active = len(hosts)

        self._set_busy(True)
        self.btn_stop.configure(state="normal")
        if self.continuous:
            self.progress.configure(mode="indeterminate")
            self.progress.start()
        else:
            self.progress.configure(mode="determinate")
            self.progress.set(0)
        self.set_status(f"Ping de {len(hosts)} hôte(s)…", self.accent)
        self.log(f"--- Démarrage : {', '.join(hosts)} "
                 f"({'continu' if self.continuous else str(self.duration) + 's'}, "
                 f"intervalle {self.interval}s) ---", "info")

        for host in hosts:
            threading.Thread(target=self._ping_worker, args=(host,), daemon=True).start()

    def stop_ping(self):
        if self.ping_running:
            self.stop_event.set()
            self.set_status("Arrêt en cours…")
            self.btn_stop.configure(state="disabled")

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
                    self._post(self.log, f"ERREUR [{host}]: {e}", "err")

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
        st.add(lat)
        prefix = f"[{host}] " if len(self.host_order) > 1 else ""
        if lat is None:
            self.log(f"{ts} - {prefix}DÉLAI DÉPASSÉ (timeout)", "err")
            self._maybe_alert(host, "outage")
        elif self.threshold > 0 and lat >= self.threshold:
            self.log(f"{ts} - {prefix}temps={lat} ms  ⚠ seuil", "warn")
            self._maybe_alert(host, "high")
        else:
            self.log(f"{ts} - {prefix}temps={lat} ms", "ok")
        self._graph_dirty = self._stats_dirty = True

    def _on_worker_done(self):
        self.workers_active -= 1
        if self.workers_active <= 0:
            self._finish_ping()

    def _finish_ping(self):
        self.ping_running = False
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(0)
        self._set_busy(False)
        self.btn_stop.configure(state="disabled")
        self._close_log()
        total = sum(s.stats.total for s in self.hosts_state.values())
        lost = sum(s.stats.lost for s in self.hosts_state.values())
        self.set_status(f"Terminé — {total} pings, {lost} perdus.", IDLE_COLOR)
        self.log(f"--- Ping terminé. Log : {self.log_file_var.get()} ---", "info")

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
    # Graphe + stats
    # ------------------------------------------------------------------
    def _reset_axes(self, title="Latence en direct"):
        self.ax.clear()
        self.threshold_line = self.ax.axhline(0, linestyle="--", linewidth=1.0, color=WARN_COLOR, visible=False)
        self.ax.set_xlabel("Index du ping")
        self.ax.set_ylabel("Latence (ms)")
        self.ax.set_title(title)
        self._update_threshold_line()

    def _update_threshold_line(self):
        if self.threshold > 0:
            self.threshold_line.set_ydata([self.threshold, self.threshold])
        self.threshold_line.set_visible(self.threshold > 0)
        self._graph_dirty = True

    def _setup_graph(self, hosts, window=LIVE_WINDOW, title="Latence en direct"):
        self.hosts_state = {}
        self.host_order = list(hosts)
        self._reset_axes(title)
        n = len(hosts)
        for i, host in enumerate(hosts):
            color = host_color(i, n, self.accent)
            st = HostState(host, color, window)
            st.line, = self.ax.plot([], [], linewidth=1.4, color=color, label=host)
            st.lost_line, = self.ax.plot([], [], "x", markersize=6, color=color)
            self.hosts_state[host] = st
        self._apply_theme()
        self._stats_dirty = True

    def _redraw_graph(self):
        lo = hi = 0
        shown = [st for st in self.hosts_state.values() if st.xs]
        for st in self.hosts_state.values():
            st.line.set_data(st.xs, st.ys)
            st.lost_line.set_data(st.lost_x, [0] * len(st.lost_x))
        if shown:
            lo = min(st.xs[0] for st in shown)
            hi = max(st.xs[-1] for st in shown)
        self.ax.relim(visible_only=True)
        self.ax.autoscale_view(scalex=False)
        self.ax.set_xlim(lo, max(lo + 10, hi))
        self.canvas.draw_idle()

    def _refresh_status(self):
        worst = max((st.last_kind(self.threshold) or "idle" for st in self.hosts_state.values()),
                    key=STATUS_RANK.__getitem__, default="idle")
        total = sum(s.stats.total for s in self.hosts_state.values())
        lost = sum(s.stats.lost for s in self.hosts_state.values())
        pct = (lost / total * 100) if total else 0.0
        self.stats_var.set(f"Envoyés: {total}   Perdus: {lost} ({pct:.1f}%)")
        if self.ping_running:   # sinon la pastille garde la couleur posée par set_status
            self.status_dot.configure(fg_color=STATUS_COLOR[worst])

    def _refresh_stats(self):
        lines = [f"{'Hôte':<20}{'Env':>5}{'Perte':>8}{'Dern':>8}{'Moy':>8}{'Gigue':>8}{'Score':>9}",
                 "─" * 66]
        for host in self.host_order:
            st = self.hosts_state[host]
            s = st.stats.summary()
            score, letter = compute_quality(s)
            last = "—" if not s["total"] else "perdu" if st.last is None else f"{st.last} ms"
            avg = f"{s['avg']:.0f} ms" if s["avg"] is not None else "—"
            jit = f"{s['jitter']:.1f}" if s["jitter"] is not None else "—"
            sc = f"{score} {letter}" if score is not None else "—"
            lines.append(f"{host[:20]:<20}{s['total']:>5}{s['loss_pct']:>7.1f}%{last:>8}{avg:>8}{jit:>8}{sc:>9}")
        self.stats_box.configure(state="normal")
        self.stats_box.delete("1.0", "end")
        self.stats_box.insert("end", "\n".join(lines) + "\n")
        self.stats_box.configure(state="disabled")

    def clear_all(self):
        if self.ping_running:
            return
        self.hosts_state = {}
        self.host_order = []
        self._reset_axes()
        self._apply_theme()
        self._pending_log = []
        for box in (self.log_area, self.stats_box):
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.configure(state="disabled")
        self.stats_var.set("Envoyés: 0   Perdus: 0 (0.0%)")
        self.set_status("Prêt.", IDLE_COLOR)

    # ------------------------------------------------------------------
    # Analyse hors-ligne
    # ------------------------------------------------------------------
    def start_analyze(self):
        log_file = self.log_file_var.get().strip()
        if not Path(log_file).exists():
            messagebox.showerror("Erreur", f"Le fichier {log_file} n'existe pas.")
            return
        self._set_busy(True)
        self.set_status("Analyse en cours…", self.accent)
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
        self._setup_graph(list(groups), window=None, title="Latence (fichier analysé)")
        for host, lats in groups.items():
            st = self.hosts_state[host]
            for lat in lats:
                st.add(lat)
        self._graph_dirty = True

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
        self.set_status("Analyse terminée.", IDLE_COLOR)
        self._set_busy(False)
        # Après ce tick, pour que journal et graphe soient à jour derrière la boîte.
        self.root.after_idle(messagebox.showinfo, "Terminé", "Analyse terminée avec succès !")

    def _analysis_error(self, message):
        self.log(f"ERREUR pendant l'analyse: {message}", "err")
        self.set_status("Erreur lors de l'analyse", ERR_COLOR)
        self._set_busy(False)

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
        self.btn_speed.configure(text="■  Annuler", fg_color=DANGER, hover_color=DANGER_HOVER)
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
                self._post(self._speed_ui, "Téléchargement…", 0.0, None, "dl")
                dl = measure_download(stop, progress("Téléchargement…", "dl"))
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
            (self.dl_var if which == "dl" else self.ul_var).set(f"{mbps:.1f}")

    def _speed_done(self, dl, ul, lat, cancelled, error):
        self.speed_running = False
        self.speed_progress.set(0)
        self.btn_speed.configure(text="▶  Tester le débit", state="normal",
                                 fg_color=self.accent, hover_color=self.accent_hover)
        self.dl_var.set(f"{dl:.1f}" if dl is not None else "—")
        self.ul_var.set(f"{ul:.1f}" if ul is not None else "—")
        self.lat_var.set(f"{lat:.0f}" if lat is not None else "—")
        if error:
            self.speed_phase_var.set("Échec du test.")
            self.log(f"ERREUR test de débit : {error}", "err")
            self.set_status("Échec du test de débit", ERR_COLOR)
        elif cancelled:
            self.speed_phase_var.set("Test annulé.")
            self.log("Test de débit annulé.", "warn")
            self.set_status("Test de débit annulé.", IDLE_COLOR)
        else:
            self.speed_phase_var.set("Terminé.")
            self.log(f"Débit : ↓ {dl:.1f} Mbps   ↑ {ul:.1f} Mbps"
                     + (f"   (latence {lat:.0f} ms)" if lat is not None else ""), "info")
            self.set_status(f"Débit : ↓ {dl:.1f} / ↑ {ul:.1f} Mbps", OK_COLOR)

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
                self.log("ℹ Fenêtre réduite dans la barre des tâches (le ping continue). "
                         "Utilise le bouton « Quitter » pour fermer l'application.", "info")
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
    ctk.set_appearance_mode("dark" if cfg["dark"] else "light")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    try:
        root.iconbitmap(resource_path("ping_tool_ico.ico"))
    except Exception:
        pass
    app = PingApp(root, cfg)
    root.mainloop()
