import tkinter as tk
from tkinter import messagebox, filedialog, colorchooser
import subprocess
import datetime
import time
import re
import csv
import json
import statistics
import threading
import queue
import os
import sys
import ctypes
from pathlib import Path

import customtkinter as ctk
from PIL import Image

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.backends.backend_agg import FigureCanvasAgg

try:
    import winsound
except Exception:
    winsound = None

# --- Expressions régulières partagées (français + anglais) ---
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
LATENCY_RE = re.compile(r"(?:temps|time)[=<]?\s*(\d+)\s*ms", re.IGNORECASE)
TIMEOUT_RE = re.compile(r"(D[ée]lai d|Request timed out|h[ôo]te de destination|Destination host)", re.IGNORECASE)

# --- Couleurs ---
DEFAULT_ACCENT = "#6366f1"
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

# Couleurs des courbes en mode multi-hôtes
HOST_PALETTE = ["#6366f1", "#22c55e", "#f59e0b", "#ec4899", "#06b6d4", "#a855f7", "#ef4444", "#84cc16"]

LIGHT_PLOT = {"fig": "#dbdbdb", "ax": "#ffffff", "fg": "#1f2937", "grid": "#c8c8c8"}
DARK_PLOT = {"fig": "#2b2b2b", "ax": "#1e1e1e", "fg": "#e5e7eb", "grid": "#404040"}

CONFIG_PATH = Path.home() / ".pingtester.json"
ALERT_COOLDOWN = 30.0

DEFAULT_CONFIG = {
    "hosts": "8.8.8.8", "duration": "60", "continuous": False,
    "threshold": "100", "interval": "1.0",
    "log_file": "ping_log.txt", "csv_file": "ping_results.csv", "plot_prefix": "ping",
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


def extract_latency(text):
    m = LATENCY_RE.search(text)
    return int(m.group(1)) if m else None


def is_timeout(text):
    return bool(TIMEOUT_RE.search(text))


def compute_jitter(latencies):
    if len(latencies) < 2:
        return 0.0
    diffs = [abs(latencies[i] - latencies[i - 1]) for i in range(1, len(latencies))]
    return statistics.mean(diffs)


def build_summary(records):
    """records : liste de (latence_ou_None, statut). Renvoie un dict de stats."""
    total = len(records)
    lost = sum(1 for _, st in records if st == "TIMEOUT")
    latencies = [lat for lat, st in records if lat is not None]
    s = {"total": total, "lost": lost,
         "loss_pct": (lost / total * 100) if total else 0.0,
         "min": None, "max": None, "avg": None,
         "median": None, "stdev": None, "jitter": None}
    if latencies:
        s["min"] = min(latencies)
        s["max"] = max(latencies)
        s["avg"] = statistics.mean(latencies)
        s["median"] = statistics.median(latencies)
        s["stdev"] = statistics.stdev(latencies) if len(latencies) > 1 else 0.0
        s["jitter"] = compute_jitter(latencies)
    return s


def compute_quality(summary):
    """Score de qualité 0-100 + note A-F depuis perte/latence/gigue."""
    if summary["total"] == 0:
        return None, "—"
    score = 100.0
    score -= summary["loss_pct"] * 2.5
    if summary["avg"] is not None:
        score -= max(0.0, summary["avg"] - 50) / 8.0
        score -= (summary["jitter"] or 0.0) / 4.0
    score = max(0, min(100, round(score)))
    letter = ("A" if score >= 90 else "B" if score >= 75 else
              "C" if score >= 60 else "D" if score >= 40 else "F")
    return score, letter


def parse_log_file(log_file):
    """Renvoie des lignes [timestamp, hôte, latence, statut]. Gère le format
    normalisé (TS\\tHOST\\tSTATUS\\tLAT) et l'ancien format brut FR/EN."""
    lines = Path(log_file).read_text(encoding="utf-8", errors="ignore").splitlines()
    rows = []
    current_ts = None
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= 4 and parts[2] in ("OK", "TIMEOUT"):
            ts, host, status, lat = parts[0], parts[1], parts[2], parts[3]
            rows.append([ts, host, int(lat) if lat else None, status])
            continue
        ts_match = TS_RE.match(line)
        if ts_match:
            current_ts = ts_match.group(1)
        lat = extract_latency(line)
        if lat is not None:
            rows.append([current_ts, "(défaut)", lat, "OK"])
        elif is_timeout(line):
            rows.append([current_ts, "(défaut)", None, "TIMEOUT"])
    return rows


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for k in cfg:
            if k in data:
                cfg[k] = data[k]
    except Exception:
        pass
    return cfg


class HostState:
    def __init__(self, host, color):
        self.host = host
        self.color = color
        self.gy = []
        self.gto_x = []
        self.latencies = []
        self.sent = 0
        self.lost = 0
        self.line = None
        self.to_line = None

    def add(self, lat, status):
        self.sent += 1
        if status == "OK":
            self.gy.append(lat)
            self.latencies.append(lat)
        else:
            self.lost += 1
            self.gy.append(None)
            self.gto_x.append(len(self.gy) - 1)

    def summary(self):
        return build_summary([(v, "OK" if v is not None else "TIMEOUT") for v in self.gy])

    def last_kind(self, threshold):
        if not self.gy:
            return None
        v = self.gy[-1]
        if v is None:
            return "timeout"
        if threshold > 0 and v >= threshold:
            return "high"
        return "ok"


class PingApp:
    def __init__(self, root):
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

        self.config = load_config()
        self.accent = self.config.get("accent", DEFAULT_ACCENT)
        self.accent_hover = darken(self.accent)
        self.outline_hover = ("#eef2ff", "#312e81")
        self.outline_btns = []
        self.accent_switches = []
        self.swatches = {}

        ctk.set_appearance_mode("dark" if self.config.get("dark") else "light")

        self.ping_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.ping_running = False
        self.start_time = 0.0
        self.duration = 0
        self.continuous = False
        self.interval = 1.0
        self.workers_active = 0
        self.log_lock = threading.Lock()
        self.log_fh = None

        self.hosts_state = {}
        self.host_order = []
        self.last_alert = {}
        self._min_hint_shown = False

        self._build_widgets()
        self._apply_config_to_widgets()
        self.apply_accent(self.accent)
        self._apply_theme()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------
    def _build_widgets(self):
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

        self.dark_var = tk.BooleanVar(value=bool(self.config.get("dark")))
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
        self.host_var = tk.StringVar(value="8.8.8.8")
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
        reg_tab = self.tabview.add("Réglages")

        self.fig = Figure(figsize=(8, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.threshold_line = self.ax.axhline(0, linestyle="--", linewidth=1.0, color=WARN_COLOR, visible=False)
        self.ax.set_xlabel("Index du ping")
        self.ax.set_ylabel("Latence (ms)")
        self.ax.set_title("Latence en direct")
        self.canvas = FigureCanvasTkAgg(self.fig, master=graph_tab)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=6)

        self.stats_box = ctk.CTkTextbox(stats_tab, corner_radius=8, font=self.mono_font, state="disabled")
        self.stats_box.pack(fill="both", expand=True, padx=6, pady=6)

        self.log_area = ctk.CTkTextbox(log_tab, corner_radius=8, font=self.mono_font, state="disabled")
        self.log_area.pack(fill="both", expand=True, padx=6, pady=6)

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
        self.duration_var = tk.StringVar(value="60")
        self.duration_entry = ctk.CTkEntry(pc, textvariable=self.duration_var, width=120, corner_radius=8)
        self.duration_entry.grid(row=1, column=1, sticky="w", pady=6)
        ctk.CTkLabel(pc, text="Intervalle (s)").grid(row=1, column=2, sticky="w", padx=(0, 8), pady=6)
        self.interval_var = tk.StringVar(value="1.0")
        ctk.CTkEntry(pc, textvariable=self.interval_var, width=120, corner_radius=8).grid(
            row=1, column=3, sticky="w", padx=(0, 16), pady=6)
        self.continuous_var = tk.BooleanVar(value=False)
        sw_cont = ctk.CTkSwitch(pc, text="Ping en continu", variable=self.continuous_var,
                                command=self._toggle_continuous, progress_color=self.accent)
        sw_cont.grid(row=2, column=0, columnspan=2, sticky="w", padx=16, pady=(6, 14))
        self.accent_switches.append(sw_cont)
        ctk.CTkLabel(pc, text="Seuil d'alerte (ms, 0=off)").grid(row=2, column=2, sticky="w", padx=(0, 8), pady=(6, 14))
        self.threshold_var = tk.StringVar(value="100")
        ctk.CTkEntry(pc, textvariable=self.threshold_var, width=120, corner_radius=8).grid(
            row=2, column=3, sticky="w", padx=(0, 16), pady=(6, 14))

        fc = ctk.CTkFrame(rs, corner_radius=14)
        fc.pack(fill="x", pady=6)
        ctk.CTkLabel(fc, text="Fichiers", font=self.section_font).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(12, 6))
        fc.columnconfigure(1, weight=1)
        ctk.CTkLabel(fc, text="Fichier Log").grid(row=1, column=0, sticky="w", padx=(16, 8), pady=6)
        self.log_file_var = tk.StringVar(value="ping_log.txt")
        ctk.CTkEntry(fc, textvariable=self.log_file_var, corner_radius=8).grid(row=1, column=1, sticky="ew", pady=6)
        b_log = ctk.CTkButton(fc, text="Parcourir…", width=110, corner_radius=18,
                              fg_color="transparent", border_width=2, command=self._browse_log)
        b_log.grid(row=1, column=2, padx=16, pady=6)
        ctk.CTkLabel(fc, text="Sortie CSV").grid(row=2, column=0, sticky="w", padx=(16, 8), pady=6)
        self.csv_file_var = tk.StringVar(value="ping_results.csv")
        ctk.CTkEntry(fc, textvariable=self.csv_file_var, corner_radius=8).grid(row=2, column=1, sticky="ew", pady=6)
        b_csv = ctk.CTkButton(fc, text="Parcourir…", width=110, corner_radius=18,
                              fg_color="transparent", border_width=2, command=self._browse_csv)
        b_csv.grid(row=2, column=2, padx=16, pady=6)
        ctk.CTkLabel(fc, text="Préfixe Graphiques").grid(row=3, column=0, sticky="w", padx=(16, 8), pady=(6, 14))
        self.plot_prefix_var = tk.StringVar(value="ping")
        ctk.CTkEntry(fc, textvariable=self.plot_prefix_var, corner_radius=8).grid(row=3, column=1, sticky="ew", pady=(6, 14))

        oc = ctk.CTkFrame(rs, corner_radius=14)
        oc.pack(fill="x", pady=6)
        ctk.CTkLabel(oc, text="Alertes", font=self.section_font).pack(anchor="w", padx=16, pady=(12, 6))
        orow = ctk.CTkFrame(oc, fg_color="transparent")
        orow.pack(fill="x", padx=16, pady=(0, 14))
        self.alerts_var = tk.BooleanVar(value=True)
        s_alert = ctk.CTkSwitch(orow, text="Alertes (son + clignotement barre des tâches)",
                                variable=self.alerts_var, progress_color=self.accent)
        s_alert.pack(side="left", padx=(0, 18))
        self.accent_switches.append(s_alert)

        self.outline_btns = [self.btn_analyze, self.btn_clear, b_log, b_csv]

    def _apply_config_to_widgets(self):
        c = self.config
        self.host_var.set(c.get("hosts", "8.8.8.8"))
        self.duration_var.set(str(c.get("duration", "60")))
        self.interval_var.set(str(c.get("interval", "1.0")))
        self.continuous_var.set(bool(c.get("continuous", False)))
        self.threshold_var.set(str(c.get("threshold", "100")))
        self.log_file_var.set(c.get("log_file", "ping_log.txt"))
        self.csv_file_var.set(c.get("csv_file", "ping_results.csv"))
        self.plot_prefix_var.set(c.get("plot_prefix", "ping"))
        self.alerts_var.set(bool(c.get("alerts", True)))
        self._toggle_continuous()

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
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Accent
    # ------------------------------------------------------------------
    def apply_accent(self, color):
        self.accent = color
        self.accent_hover = darken(color, 0.18)
        self.outline_hover = (_blend(color, (255, 255, 255), 0.86), _blend(color, (0, 0, 0), 0.55))
        self.btn_ping.configure(fg_color=self.accent, hover_color=self.accent_hover)
        for b in self.outline_btns:
            b.configure(border_color=self.accent, text_color=self.accent, hover_color=self.outline_hover)
        for s in self.accent_switches:
            s.configure(progress_color=self.accent)
        self.progress.configure(progress_color=self.accent)
        self.tabview.configure(segmented_button_selected_color=self.accent,
                               segmented_button_selected_hover_color=self.accent_hover)
        if len(self.host_order) == 1:
            st = self.hosts_state[self.host_order[0]]
            st.color = self.accent
            if st.line:
                st.line.set_color(self.accent)
            if st.to_line:
                st.to_line.set_color(self.accent)
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
    def _apply_theme(self):
        dark = ctk.get_appearance_mode() == "Dark"
        pal = DARK_PLOT if dark else LIGHT_PLOT
        self.fig.set_facecolor(pal["fig"])
        self.ax.set_facecolor(pal["ax"])
        for spine in self.ax.spines.values():
            spine.set_color(pal["grid"])
        self.ax.tick_params(colors=pal["fg"])
        self.ax.xaxis.label.set_color(pal["fg"])
        self.ax.yaxis.label.set_color(pal["fg"])
        self.ax.title.set_color(pal["fg"])
        self.ax.grid(True, color=pal["grid"], linewidth=0.5)
        if self.host_order:
            self.ax.legend(loc="upper right", fontsize=8, facecolor=pal["ax"],
                           edgecolor=pal["grid"], labelcolor=pal["fg"])
        self.canvas.draw_idle()
        self.log_area.tag_config("ok", foreground=pal["fg"])
        self.log_area.tag_config("warn", foreground=WARN_COLOR)
        self.log_area.tag_config("err", foreground=ERR_COLOR)
        self.log_area.tag_config("info", foreground=self.accent)
        self._highlight_swatch()

    def _toggle_theme(self):
        ctk.set_appearance_mode("dark" if self.dark_var.get() else "light")
        self._apply_theme()

    # ------------------------------------------------------------------
    # Aides UI
    # ------------------------------------------------------------------
    def log(self, message, tag="ok"):
        self.log_area.configure(state="normal")
        self.log_area.insert("end", message + "\n", tag)
        self.log_area.see("end")
        self.log_area.configure(state="disabled")

    def set_status(self, text, color=None):
        self.status_var.set(text)
        if color:
            self.status_dot.configure(fg_color=color)

    def _toggle_continuous(self):
        self.duration_entry.configure(state="disabled" if self.continuous_var.get() else "normal")

    def _browse_log(self):
        path = filedialog.asksaveasfilename(title="Fichier log", defaultextension=".txt",
                                            filetypes=[("Texte", "*.txt"), ("Tous", "*.*")],
                                            initialfile=self.log_file_var.get())
        if path:
            self.log_file_var.set(path)

    def _browse_csv(self):
        path = filedialog.asksaveasfilename(title="Sortie CSV", defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv"), ("Tous", "*.*")],
                                            initialfile=self.csv_file_var.get())
        if path:
            self.csv_file_var.set(path)

    def _get_threshold(self):
        try:
            return float(self.threshold_var.get())
        except ValueError:
            return 0.0

    def _parse_hosts(self):
        raw = self.host_var.get().replace(";", ",").replace(" ", ",")
        seen, hosts = set(), []
        for h in raw.split(","):
            h = h.strip()
            if h and h.lower() not in seen:
                seen.add(h.lower())
                hosts.append(h)
        return hosts

    def _host_color(self, i, n):
        return self.accent if n == 1 else HOST_PALETTE[i % len(HOST_PALETTE)]

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
        try:
            self.interval = max(0.2, float(self.interval_var.get()))
        except ValueError:
            self.interval = 1.0

        log_file = self.log_file_var.get().strip()
        if not log_file:
            messagebox.showerror("Erreur", "Veuillez indiquer un fichier log.")
            return
        try:
            self.log_fh = open(log_file, "w", encoding="utf-8")
        except Exception as e:
            messagebox.showerror("Erreur", f"Impossible d'ouvrir le log : {e}")
            return

        self._setup_graph(hosts)
        self.last_alert.clear()
        self.stop_event.clear()
        self.ping_running = True
        self.start_time = time.time()
        self.workers_active = len(hosts)

        thr = self._get_threshold()
        if thr > 0:
            self.threshold_line.set_ydata([thr, thr])
            self.threshold_line.set_visible(True)
        else:
            self.threshold_line.set_visible(False)

        self.btn_ping.configure(state="disabled")
        self.btn_analyze.configure(state="disabled")
        self.btn_clear.configure(state="disabled")
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
        self.root.after(150, self._process_queue)

    def stop_ping(self):
        if self.ping_running:
            self.stop_event.set()
            self.set_status("Arrêt en cours…")
            self.btn_stop.configure(state="disabled")

    def _setup_graph(self, hosts):
        self.hosts_state = {}
        self.host_order = list(hosts)
        self.ax.clear()
        n = len(hosts)
        for i, host in enumerate(hosts):
            color = self._host_color(i, n)
            st = HostState(host, color)
            st.line, = self.ax.plot([], [], linewidth=1.4, color=color, label=host)
            st.to_line, = self.ax.plot([], [], "x", markersize=6, color=color)
            self.hosts_state[host] = st
        self.threshold_line = self.ax.axhline(0, linestyle="--", linewidth=1.0, color=WARN_COLOR, visible=False)
        self.ax.set_xlabel("Index du ping")
        self.ax.set_ylabel("Latence (ms)")
        self.ax.set_title("Latence en direct")
        self._apply_theme()

    def _ping_worker(self, host):
        start = time.time()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            while not self.stop_event.is_set():
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    result = subprocess.run(["ping", "-n", "1", host], capture_output=True,
                                            text=True, timeout=5, creationflags=creationflags)
                    lat = extract_latency(result.stdout)
                    status = "OK" if lat is not None else "TIMEOUT"
                    self._write_log_line(now, host, status, lat)
                    self.ping_queue.put(("data", host, now, lat, status))
                except Exception as e:
                    self._write_log_line(now, host, "ERROR", None, extra=str(e))
                    self.ping_queue.put(("error", host, str(e)))

                if not self.continuous and time.time() - start > self.duration:
                    break
                steps = max(1, int(self.interval / 0.1))
                for _ in range(steps):
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.1)
        finally:
            self.ping_queue.put(("done", host))

    def _write_log_line(self, now, host, status, lat, extra=""):
        line = f"{now}\t{host}\t{status}\t{lat if lat is not None else ''}"
        if extra:
            line += f"\t{extra}"
        with self.log_lock:
            if self.log_fh:
                self.log_fh.write(line + "\n")
                self.log_fh.flush()

    def _process_queue(self):
        try:
            while True:
                self._handle_msg(self.ping_queue.get_nowait())
        except queue.Empty:
            pass
        if self.ping_running:
            if not self.continuous and self.duration > 0:
                self.progress.set(min(1.0, (time.time() - self.start_time) / self.duration))
            self.root.after(150, self._process_queue)

    def _handle_msg(self, msg):
        kind = msg[0]
        if kind == "data":
            _, host, now, lat, status = msg
            st = self.hosts_state.get(host)
            if st is None:
                return
            st.add(lat, status)
            thr = self._get_threshold()
            prefix = f"[{host}] " if len(self.host_order) > 1 else ""
            if status == "OK":
                if thr > 0 and lat >= thr:
                    self.log(f"{now} - {prefix}temps={lat} ms  ⚠ seuil", "warn")
                    self._maybe_alert(host, "high", f"{host}: latence élevée {lat} ms")
                else:
                    self.log(f"{now} - {prefix}temps={lat} ms", "ok")
            else:
                self.log(f"{now} - {prefix}DÉLAI DÉPASSÉ (timeout)", "err")
                self._maybe_alert(host, "outage", f"{host}: perte de paquet (timeout)")
            self._update_host_line(st)
            self._refresh_status()
            self._refresh_stats()
        elif kind == "error":
            self.log(f"ERREUR [{msg[1]}]: {msg[2]}", "err")
        elif kind == "done":
            self.workers_active -= 1
            if self.workers_active <= 0:
                self._finish_ping()

    def _finish_ping(self):
        self.ping_running = False
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(0)
        self.btn_ping.configure(state="normal")
        self.btn_analyze.configure(state="normal")
        self.btn_clear.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        with self.log_lock:
            if self.log_fh:
                self.log_fh.close()
                self.log_fh = None
        total = sum(s.sent for s in self.hosts_state.values())
        lost = sum(s.lost for s in self.hosts_state.values())
        self.set_status(f"Terminé — {total} pings, {lost} perdus.", IDLE_COLOR)
        self.log(f"--- Ping terminé. Log : {self.log_file_var.get()} ---", "info")

    # ------------------------------------------------------------------
    # Alertes (notification + son)
    # ------------------------------------------------------------------
    def _maybe_alert(self, host, kind, message):
        if not self.alerts_var.get():
            return
        now = time.time()
        key = (host, kind)
        if now - self.last_alert.get(key, 0) < ALERT_COOLDOWN:
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
    def _update_host_line(self, st):
        n = len(st.gy)
        st.line.set_data(range(n), [v if v is not None else float("nan") for v in st.gy])
        st.to_line.set_data(st.gto_x, [0] * len(st.gto_x))
        self.ax.relim()
        self.ax.autoscale_view()
        maxn = max((len(s.gy) for s in self.hosts_state.values()), default=1)
        self.ax.set_xlim(0, max(10, maxn - 1))
        self.canvas.draw_idle()

    def _refresh_status(self):
        thr = self._get_threshold()
        worst = "idle"
        rank = {"idle": 0, "ok": 1, "high": 2, "timeout": 3}
        for st in self.hosts_state.values():
            k = st.last_kind(thr) or "idle"
            if rank[k] > rank[worst]:
                worst = k
        color = {"idle": IDLE_COLOR, "ok": OK_COLOR, "high": WARN_COLOR, "timeout": ERR_COLOR}[worst]
        total = sum(s.sent for s in self.hosts_state.values())
        lost = sum(s.lost for s in self.hosts_state.values())
        pct = (lost / total * 100) if total else 0.0
        self.stats_var.set(f"Envoyés: {total}   Perdus: {lost} ({pct:.1f}%)")
        self.status_dot.configure(fg_color=color)

    def _refresh_stats(self):
        header = f"{'Hôte':<20}{'Env':>5}{'Perte':>8}{'Dern':>8}{'Moy':>8}{'Gigue':>8}{'Score':>9}\n"
        sep = "─" * 66 + "\n"
        rows = ""
        for host in self.host_order:
            st = self.hosts_state[host]
            s = st.summary()
            score, letter = compute_quality(s)
            last = "—"
            if st.gy:
                last = "perdu" if st.gy[-1] is None else f"{st.gy[-1]} ms"
            avg = f"{s['avg']:.0f} ms" if s["avg"] is not None else "—"
            jit = f"{s['jitter']:.1f}" if s["jitter"] is not None else "—"
            sc = f"{score} {letter}" if score is not None else "—"
            rows += f"{host[:20]:<20}{s['total']:>5}{s['loss_pct']:>7.1f}%{last:>8}{avg:>8}{jit:>8}{sc:>9}\n"
        self.stats_box.configure(state="normal")
        self.stats_box.delete("1.0", "end")
        self.stats_box.insert("end", header + sep + rows)
        self.stats_box.configure(state="disabled")

    def clear_all(self):
        if self.ping_running:
            return
        self.hosts_state = {}
        self.host_order = []
        self.ax.clear()
        self.threshold_line = self.ax.axhline(0, linestyle="--", linewidth=1.0, color=WARN_COLOR, visible=False)
        self.ax.set_xlabel("Index du ping")
        self.ax.set_ylabel("Latence (ms)")
        self.ax.set_title("Latence en direct")
        self._apply_theme()
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
        self.btn_analyze.configure(state="disabled")
        self.btn_ping.configure(state="disabled")
        self.set_status("Analyse en cours…", self.accent)
        self.log("--- Démarrage de l'analyse ---", "info")
        threading.Thread(target=self._analyze_worker,
                         args=(log_file, self.csv_file_var.get().strip(),
                               self.plot_prefix_var.get().strip() or "ping"), daemon=True).start()

    def _group_by_host(self, rows):
        groups = {}
        for ts, host, lat, status in rows:
            groups.setdefault(host, []).append((lat, status))
        return groups

    def _analyze_worker(self, log_file, csv_file, plot_prefix):
        try:
            rows = parse_log_file(log_file)
            groups = self._group_by_host(rows)
            self._write_csv(rows, groups, csv_file)
            self._save_plots(rows, groups, plot_prefix)
            self.root.after(0, self._show_analysis_result, rows, groups, csv_file, plot_prefix)
        except Exception as e:
            self.root.after(0, self._analysis_error, str(e))

    def _write_csv(self, rows, groups, csv_file):
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Timestamp", "Host", "Latency_ms", "Status"])
            for ts, host, lat, status in rows:
                w.writerow([ts, host, lat, status])
            for host, recs in groups.items():
                s = build_summary(recs)
                score, letter = compute_quality(s)
                w.writerow([])
                w.writerow([f"=== Résumé : {host} ==="])
                w.writerow(["Total pings", s["total"]])
                w.writerow(["Paquets perdus", s["lost"]])
                w.writerow(["Perte (%)", f"{s['loss_pct']:.2f}"])
                if s["avg"] is not None:
                    w.writerow(["Latence min/max (ms)", f"{s['min']} / {s['max']}"])
                    w.writerow(["Latence moyenne (ms)", f"{s['avg']:.2f}"])
                    w.writerow(["Latence médiane (ms)", s["median"]])
                    w.writerow(["Écart-type (ms)", f"{s['stdev']:.2f}"])
                    w.writerow(["Gigue / jitter (ms)", f"{s['jitter']:.2f}"])
                w.writerow(["Score qualité", f"{score} ({letter})"])

    def _save_plots(self, rows, groups, plot_prefix):
        fig1 = Figure(figsize=(12, 5), dpi=100)
        ax1 = fig1.add_subplot(111)
        hosts = list(groups.keys())
        for i, host in enumerate(hosts):
            recs = groups[host]
            y = [lat if lat is not None else float("nan") for lat, _ in recs]
            color = self._host_color(i, len(hosts))
            ax1.plot(range(len(y)), y, linewidth=0.9, color=color, label=host)
            tos = [j for j, (lat, _) in enumerate(recs) if lat is None]
            ax1.scatter(tos, [0] * len(tos), color=color, marker="x")
        ax1.set_xlabel("Index du ping")
        ax1.set_ylabel("Latence (ms)")
        ax1.set_title("Latence dans le temps")
        ax1.legend()
        ax1.grid(True)
        fig1.tight_layout()
        FigureCanvasAgg(fig1)
        fig1.savefig(f"{plot_prefix}_latency.png")

        all_lat = [lat for _, _, lat, st in rows if lat is not None]
        if all_lat:
            fig2 = Figure(figsize=(7, 5), dpi=100)
            ax2 = fig2.add_subplot(111)
            ax2.hist(all_lat, bins=50, color=self.accent, edgecolor="black")
            ax2.set_xlabel("Latence (ms)")
            ax2.set_ylabel("Nombre")
            ax2.set_title("Distribution de la latence")
            fig2.tight_layout()
            FigureCanvasAgg(fig2)
            fig2.savefig(f"{plot_prefix}_hist.png")

    def _show_analysis_result(self, rows, groups, csv_file, plot_prefix):
        hosts = list(groups.keys())
        self._setup_graph(hosts)
        for host in hosts:
            st = self.hosts_state[host]
            for lat, status in groups[host]:
                st.add(lat, status)
            self._update_host_line(st)
        self.ax.set_title("Latence (fichier analysé)")
        self.canvas.draw_idle()
        self._refresh_stats()
        self._refresh_status()

        self.log(f"--- Analyse : {len(rows)} mesures, {len(hosts)} hôte(s) ---", "info")
        for host in hosts:
            s = build_summary(groups[host])
            score, letter = compute_quality(s)
            if s["avg"] is not None:
                self.log(f"[{host}] {s['total']} pings, perte {s['loss_pct']:.1f}%, "
                         f"moy {s['avg']:.0f} ms, score {score} ({letter})",
                         "err" if s["lost"] else "ok")
            else:
                self.log(f"[{host}] {s['total']} pings, perte {s['loss_pct']:.1f}%", "err")
        self.log(f"CSV: {csv_file}  |  Graphiques: {plot_prefix}_latency.png, {plot_prefix}_hist.png", "info")
        self.set_status("Analyse terminée.", IDLE_COLOR)
        self.btn_analyze.configure(state="normal")
        self.btn_ping.configure(state="normal")
        messagebox.showinfo("Terminé", "Analyse terminée avec succès !")

    def _analysis_error(self, message):
        self.log(f"ERREUR pendant l'analyse: {message}", "err")
        self.set_status("Erreur lors de l'analyse", ERR_COLOR)
        self.btn_analyze.configure(state="normal")
        self.btn_ping.configure(state="normal")

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

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                            ("dwFlags", ctypes.c_uint), ("uCount", ctypes.c_uint),
                            ("dwTimeout", ctypes.c_uint)]

            FLASHW_ALL, FLASHW_TIMERNOFG = 0x3, 0xC
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd,
                              FLASHW_ALL | FLASHW_TIMERNOFG, 4, 0)
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
        self.save_config()
        self.root.destroy()


if __name__ == "__main__":
    cfg = load_config()
    ctk.set_appearance_mode("dark" if cfg.get("dark") else "light")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    try:
        root.iconbitmap(resource_path("ping_tool_ico.ico"))
    except Exception:
        pass
    app = PingApp(root)
    root.mainloop()
