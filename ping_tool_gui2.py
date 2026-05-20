import tkinter as tk
from tkinter import messagebox, filedialog, colorchooser
import subprocess
import datetime
import time
import re
import csv
import statistics
import threading
import queue
import os
import sys
from pathlib import Path

import customtkinter as ctk
from PIL import Image

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.backends.backend_agg import FigureCanvasAgg

# --- Expressions régulières partagées (français + anglais) ---
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
LATENCY_RE = re.compile(r"(?:temps|time)[=<]?\s*(\d+)\s*ms", re.IGNORECASE)
TIMEOUT_RE = re.compile(r"(D[ée]lai d|Request timed out|h[ôo]te de destination|Destination host)", re.IGNORECASE)

# --- Couleurs ---
DEFAULT_ACCENT = "#6366f1"  # indigo-500
DANGER = "#ef4444"
DANGER_HOVER = "#dc2626"
OK_COLOR = "#22c55e"
WARN_COLOR = "#f59e0b"
ERR_COLOR = "#ef4444"
IDLE_COLOR = "#9ca3af"

# Préréglages de couleur d'accent (nom -> hex)
PRESET_ACCENTS = {
    "Indigo": "#6366f1",
    "Bleu": "#3b82f6",
    "Cyan": "#06b6d4",
    "Vert": "#22c55e",
    "Violet": "#a855f7",
    "Rose": "#ec4899",
    "Ambre": "#f59e0b",
}

# Palettes du graphique matplotlib selon le mode
LIGHT_PLOT = {"fig": "#dbdbdb", "ax": "#ffffff", "fg": "#1f2937", "grid": "#c8c8c8"}
DARK_PLOT = {"fig": "#2b2b2b", "ax": "#1e1e1e", "fg": "#e5e7eb", "grid": "#404040"}


# --- Utilitaires couleur ---
def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb):
    return "#%02x%02x%02x" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _blend(hex_color, target_rgb, t):
    """Mélange hex_color vers target_rgb (t = proportion de target, 0..1)."""
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


def parse_log_file(log_file):
    """Relit un fichier log et renvoie des lignes [timestamp, latence, statut]."""
    lines = Path(log_file).read_text(encoding="utf-8", errors="ignore").splitlines()
    rows = []
    current_ts = None
    for line in lines:
        ts_match = TS_RE.match(line)
        if ts_match:
            current_ts = ts_match.group(1)
        lat = extract_latency(line)
        if lat is not None:
            rows.append([current_ts, lat, "OK"])
            continue
        if is_timeout(line):
            rows.append([current_ts, None, "TIMEOUT"])
    return rows


def build_summary(rows):
    """Calcule un dictionnaire de statistiques à partir des lignes parsées."""
    total = len(rows)
    lost = sum(1 for r in rows if r[2] == "TIMEOUT")
    latencies = [r[1] for r in rows if r[1] is not None]
    s = {
        "total": total,
        "lost": lost,
        "loss_pct": (lost / total * 100) if total else 0.0,
        "min": None, "max": None, "avg": None,
        "median": None, "stdev": None, "jitter": None,
    }
    if latencies:
        s["min"] = min(latencies)
        s["max"] = max(latencies)
        s["avg"] = statistics.mean(latencies)
        s["median"] = statistics.median(latencies)
        s["stdev"] = statistics.stdev(latencies) if len(latencies) > 1 else 0.0
        s["jitter"] = compute_jitter(latencies)
    return s


def resource_path(rel):
    """Chemin d'une ressource, compatible exécution normale et PyInstaller (.exe)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


class PingApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Outil de Ping & Analyse")
        self.root.geometry("960x880")
        self.root.minsize(820, 720)

        self.ping_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.ping_running = False
        self.start_time = 0.0
        self.duration = 0
        self.continuous = False

        # Accent (modifiable à chaud)
        self.accent = DEFAULT_ACCENT
        self.accent_hover = darken(self.accent)
        self.outline_hover = ("#eef2ff", "#312e81")
        self.outline_btns = []
        self.swatches = {}

        # Données du graphique en direct
        self.gy = []
        self.gto_x = []
        self.count_sent = 0
        self.count_lost = 0
        self.live_latencies = []

        self._build_widgets()
        self.apply_accent(self.accent)
        self._apply_theme()

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------
    def _build_widgets(self):
        self.title_font = ctk.CTkFont(size=20, weight="bold")
        self.section_font = ctk.CTkFont(size=14, weight="bold")
        self.mono_font = ctk.CTkFont(family="Consolas", size=12)

        outer = ctk.CTkFrame(self.root, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=16, pady=12)

        # ---- En-tête : logo + titre + sélecteur d'accent + mode sombre ----
        header = ctk.CTkFrame(outer, fg_color="transparent")
        header.pack(fill="x", pady=(0, 10))

        self.logo_img = None
        try:
            pil = Image.open(resource_path("ping_tool_ico.ico")).convert("RGBA")
            self.logo_img = ctk.CTkImage(light_image=pil, dark_image=pil, size=(34, 34))
        except Exception:
            self.logo_img = None
        ctk.CTkLabel(header, image=self.logo_img,
                     text="  Outil de Ping & Analyse",
                     compound="left", font=self.title_font).pack(side="left")

        self.dark_var = tk.BooleanVar(value=False)
        self.dark_switch = ctk.CTkSwitch(header, text="Mode sombre", variable=self.dark_var,
                                         command=self._toggle_theme, progress_color=self.accent)
        self.dark_switch.pack(side="right")

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

        # ---- Carte Paramètres ----
        p_card = ctk.CTkFrame(outer, corner_radius=14)
        p_card.pack(fill="x", pady=6)
        ctk.CTkLabel(p_card, text="Paramètres", font=self.section_font).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=16, pady=(12, 6))
        p_card.columnconfigure(1, weight=1)
        p_card.columnconfigure(3, weight=1)

        ctk.CTkLabel(p_card, text="Hôte (IP / Domaine)").grid(row=1, column=0, sticky="w", padx=(16, 8), pady=6)
        self.host_var = tk.StringVar(value="8.8.8.8")
        ctk.CTkEntry(p_card, textvariable=self.host_var, corner_radius=8).grid(
            row=1, column=1, sticky="ew", padx=(0, 16), pady=6)

        ctk.CTkLabel(p_card, text="Durée (s)").grid(row=1, column=2, sticky="w", padx=(0, 8), pady=6)
        self.duration_var = tk.StringVar(value="60")
        self.duration_entry = ctk.CTkEntry(p_card, textvariable=self.duration_var, width=110, corner_radius=8)
        self.duration_entry.grid(row=1, column=3, sticky="w", padx=(0, 16), pady=6)

        self.continuous_var = tk.BooleanVar(value=False)
        self.continuous_switch = ctk.CTkSwitch(
            p_card, text="Ping en continu", variable=self.continuous_var,
            command=self._toggle_continuous, progress_color=self.accent)
        self.continuous_switch.grid(row=2, column=0, columnspan=2, sticky="w", padx=16, pady=(6, 14))

        ctk.CTkLabel(p_card, text="Seuil d'alerte (ms, 0=off)").grid(row=2, column=2, sticky="w", padx=(0, 8), pady=(6, 14))
        self.threshold_var = tk.StringVar(value="100")
        ctk.CTkEntry(p_card, textvariable=self.threshold_var, width=110, corner_radius=8).grid(
            row=2, column=3, sticky="w", padx=(0, 16), pady=(6, 14))

        # ---- Carte Fichiers ----
        f_card = ctk.CTkFrame(outer, corner_radius=14)
        f_card.pack(fill="x", pady=6)
        ctk.CTkLabel(f_card, text="Fichiers", font=self.section_font).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(12, 6))
        f_card.columnconfigure(1, weight=1)

        ctk.CTkLabel(f_card, text="Fichier Log").grid(row=1, column=0, sticky="w", padx=(16, 8), pady=6)
        self.log_file_var = tk.StringVar(value="ping_log.txt")
        ctk.CTkEntry(f_card, textvariable=self.log_file_var, corner_radius=8).grid(
            row=1, column=1, sticky="ew", pady=6)
        b_log = ctk.CTkButton(f_card, text="Parcourir…", width=110, corner_radius=18,
                              fg_color="transparent", border_width=2, command=self._browse_log)
        b_log.grid(row=1, column=2, padx=16, pady=6)

        ctk.CTkLabel(f_card, text="Sortie CSV").grid(row=2, column=0, sticky="w", padx=(16, 8), pady=6)
        self.csv_file_var = tk.StringVar(value="ping_results.csv")
        ctk.CTkEntry(f_card, textvariable=self.csv_file_var, corner_radius=8).grid(
            row=2, column=1, sticky="ew", pady=6)
        b_csv = ctk.CTkButton(f_card, text="Parcourir…", width=110, corner_radius=18,
                              fg_color="transparent", border_width=2, command=self._browse_csv)
        b_csv.grid(row=2, column=2, padx=16, pady=6)

        ctk.CTkLabel(f_card, text="Préfixe Graphiques").grid(row=3, column=0, sticky="w", padx=(16, 8), pady=(6, 14))
        self.plot_prefix_var = tk.StringVar(value="ping")
        ctk.CTkEntry(f_card, textvariable=self.plot_prefix_var, corner_radius=8).grid(
            row=3, column=1, sticky="ew", pady=(6, 14))

        # ---- Boutons d'action ----
        btn_row = ctk.CTkFrame(outer, fg_color="transparent")
        btn_row.pack(fill="x", pady=8)
        for i in range(4):
            btn_row.columnconfigure(i, weight=1, uniform="btn")

        self.btn_ping = ctk.CTkButton(btn_row, text="▶  Lancer Ping", height=42, corner_radius=18,
                                      font=self.section_font, command=self.start_ping)
        self.btn_ping.grid(row=0, column=0, sticky="ew", padx=6)

        self.btn_stop = ctk.CTkButton(btn_row, text="■  Stop", height=42, corner_radius=18,
                                      font=self.section_font, fg_color=DANGER, hover_color=DANGER_HOVER,
                                      state="disabled", command=self.stop_ping)
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=6)

        self.btn_analyze = ctk.CTkButton(btn_row, text="📊  Analyser", height=42, corner_radius=18,
                                         font=self.section_font, fg_color="transparent", border_width=2,
                                         command=self.start_analyze)
        self.btn_analyze.grid(row=0, column=2, sticky="ew", padx=6)

        self.btn_clear = ctk.CTkButton(btn_row, text="🗑  Effacer", height=42, corner_radius=18,
                                       font=self.section_font, fg_color="transparent", border_width=2,
                                       command=self.clear_all)
        self.btn_clear.grid(row=0, column=3, sticky="ew", padx=6)

        self.outline_btns = [self.btn_analyze, self.btn_clear, b_log, b_csv]

        # ---- Barre de statut ----
        status_card = ctk.CTkFrame(outer, corner_radius=14)
        status_card.pack(fill="x", pady=6)
        self.status_dot = ctk.CTkFrame(status_card, width=14, height=14, corner_radius=7, fg_color=IDLE_COLOR)
        self.status_dot.grid(row=0, column=0, padx=(16, 8), pady=12)
        self.status_var = tk.StringVar(value="Prêt.")
        ctk.CTkLabel(status_card, textvariable=self.status_var).grid(row=0, column=1, sticky="w", pady=12)
        status_card.columnconfigure(2, weight=1)
        self.stats_var = tk.StringVar(value="Envoyés: 0   Perdus: 0 (0.0%)   Dernière: —   Moy: —")
        ctk.CTkLabel(status_card, textvariable=self.stats_var, font=self.mono_font).grid(
            row=0, column=2, sticky="e", padx=16, pady=12)

        self.progress = ctk.CTkProgressBar(outer, corner_radius=8)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=(2, 8))

        # ---- Onglets : Graphique / Journal ----
        self.tabview = ctk.CTkTabview(outer, corner_radius=14)
        self.tabview.pack(fill="both", expand=True, pady=6)
        graph_tab = self.tabview.add("Graphique")
        log_tab = self.tabview.add("Journal")

        self.fig = Figure(figsize=(8, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.line, = self.ax.plot([], [], linewidth=1.4, color=self.accent, label="Latence (ms)")
        self.to_line, = self.ax.plot([], [], "x", markersize=7, color=ERR_COLOR, label="Timeouts")
        self.threshold_line = self.ax.axhline(0, linestyle="--", linewidth=1.0, color=WARN_COLOR, visible=False)
        self.ax.set_xlabel("Index du ping")
        self.ax.set_ylabel("Latence (ms)")
        self.ax.set_title("Latence en direct")
        self.canvas = FigureCanvasTkAgg(self.fig, master=graph_tab)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=6)

        self.log_area = ctk.CTkTextbox(log_tab, corner_radius=8, font=self.mono_font, state="disabled")
        self.log_area.pack(fill="both", expand=True, padx=6, pady=6)

    # ------------------------------------------------------------------
    # Accent (changement à chaud)
    # ------------------------------------------------------------------
    def apply_accent(self, color):
        self.accent = color
        self.accent_hover = darken(color, 0.18)
        self.outline_hover = (_blend(color, (255, 255, 255), 0.86),
                              _blend(color, (0, 0, 0), 0.55))

        self.btn_ping.configure(fg_color=self.accent, hover_color=self.accent_hover)
        for b in self.outline_btns:
            b.configure(border_color=self.accent, text_color=self.accent, hover_color=self.outline_hover)
        self.continuous_switch.configure(progress_color=self.accent)
        self.dark_switch.configure(progress_color=self.accent)
        self.progress.configure(progress_color=self.accent)
        self.tabview.configure(segmented_button_selected_color=self.accent,
                               segmented_button_selected_hover_color=self.accent_hover)
        self.line.set_color(self.accent)
        self.canvas.draw_idle()
        self.log_area.tag_config("info", foreground=self.accent)
        self._highlight_swatch()

    def _highlight_swatch(self):
        border = "#ffffff" if ctk.get_appearance_mode() == "Dark" else "#1f2937"
        for color, btn in self.swatches.items():
            if color.lower() == self.accent.lower():
                btn.configure(border_width=2, border_color=border)
            else:
                btn.configure(border_width=0)

    def _pick_custom_accent(self):
        result = colorchooser.askcolor(color=self.accent, title="Couleur d'accent")
        if result and result[1]:
            self.apply_accent(result[1])

    # ------------------------------------------------------------------
    # Thème (graphique + tags du journal selon le mode clair/sombre)
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
    # Petites aides UI
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
        path = filedialog.asksaveasfilename(
            title="Fichier log", defaultextension=".txt",
            filetypes=[("Texte", "*.txt"), ("Tous", "*.*")],
            initialfile=self.log_file_var.get())
        if path:
            self.log_file_var.set(path)

    def _browse_csv(self):
        path = filedialog.asksaveasfilename(
            title="Sortie CSV", defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Tous", "*.*")],
            initialfile=self.csv_file_var.get())
        if path:
            self.csv_file_var.set(path)

    def _get_threshold(self):
        try:
            return float(self.threshold_var.get())
        except ValueError:
            return 0.0

    # ------------------------------------------------------------------
    # Lancement / arrêt du ping
    # ------------------------------------------------------------------
    def start_ping(self):
        host = self.host_var.get().strip()
        if not host:
            messagebox.showerror("Erreur", "Veuillez saisir un hôte.")
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

        log_file = self.log_file_var.get().strip()
        if not log_file:
            messagebox.showerror("Erreur", "Veuillez indiquer un fichier log.")
            return

        self._reset_live_data()
        self.stop_event.clear()
        self.ping_running = True
        self.start_time = time.time()

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
            self.set_status(f"Ping en continu vers {host}…", self.accent)
        else:
            self.progress.configure(mode="determinate")
            self.progress.set(0)
            self.set_status(f"Ping vers {host} pour {self.duration}s…", self.accent)
        self.log(f"--- Démarrage du Ping vers {host} "
                 f"({'continu' if self.continuous else str(self.duration) + 's'}) ---", "info")

        threading.Thread(target=self._ping_worker,
                         args=(host, self.duration, self.continuous, log_file),
                         daemon=True).start()
        self.root.after(120, self._process_queue)

    def stop_ping(self):
        if self.ping_running:
            self.stop_event.set()
            self.set_status("Arrêt en cours…")
            self.btn_stop.configure(state="disabled")

    def _ping_worker(self, host, duration, continuous, log_file):
        start = time.time()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            with open(log_file, "w", encoding="utf-8") as f:
                while not self.stop_event.is_set():
                    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    try:
                        result = subprocess.run(
                            ["ping", "-n", "1", host],
                            capture_output=True, text=True, timeout=5,
                            creationflags=creationflags,
                        )
                        output = result.stdout.strip()
                        f.write(f"{now} - {output}\n")
                        f.flush()
                        lat = extract_latency(output)
                        if lat is not None:
                            self.ping_queue.put(("data", now, lat, "OK"))
                        else:
                            self.ping_queue.put(("data", now, None, "TIMEOUT"))
                    except Exception as e:
                        f.write(f"{now} - ERROR: {e}\n")
                        self.ping_queue.put(("error", str(e)))

                    if not continuous and time.time() - start > duration:
                        break
                    for _ in range(10):
                        if self.stop_event.is_set():
                            break
                        time.sleep(0.1)
        finally:
            self.ping_queue.put(("done", log_file))

    def _process_queue(self):
        try:
            while True:
                msg = self.ping_queue.get_nowait()
                self._handle_msg(msg)
        except queue.Empty:
            pass

        if self.ping_running:
            if not self.continuous and self.duration > 0:
                frac = min(1.0, (time.time() - self.start_time) / self.duration)
                self.progress.set(frac)
            self.root.after(120, self._process_queue)

    def _handle_msg(self, msg):
        kind = msg[0]
        if kind == "data":
            _, now, lat, status = msg
            self.count_sent += 1
            thr = self._get_threshold()

            if status == "OK":
                self.gy.append(lat)
                self.live_latencies.append(lat)
                if thr > 0 and lat >= thr:
                    self.set_status(f"Latence élevée: {lat} ms", WARN_COLOR)
                    self.log(f"{now} - temps={lat} ms  ⚠ au-dessus du seuil", "warn")
                else:
                    self.set_status("En cours…", OK_COLOR)
                    self.log(f"{now} - temps={lat} ms", "ok")
            else:
                self.count_lost += 1
                self.gy.append(None)
                self.gto_x.append(len(self.gy) - 1)
                self.set_status("Timeout / perte de paquet", ERR_COLOR)
                self.log(f"{now} - DÉLAI DÉPASSÉ (timeout)", "err")

            self._update_stats_label()
            self._update_graph()

        elif kind == "error":
            self.log(f"ERREUR: {msg[1]}", "err")
            self.set_status("Erreur lors du ping", ERR_COLOR)

        elif kind == "done":
            self._finish_ping(msg[1])

    def _finish_ping(self, log_file):
        self.ping_running = False
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(0)
        self.btn_ping.configure(state="normal")
        self.btn_analyze.configure(state="normal")
        self.btn_clear.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        s = build_summary([[None, v, "OK" if v is not None else "TIMEOUT"] for v in self.gy])
        self.set_status(
            f"Terminé — {s['total']} pings, {s['lost']} perdus ({s['loss_pct']:.1f}%).", IDLE_COLOR)
        self.log(f"--- Ping terminé. Données : {log_file} ---", "info")

    # ------------------------------------------------------------------
    # Graphique en direct
    # ------------------------------------------------------------------
    def _reset_live_data(self):
        self.gy = []
        self.gto_x = []
        self.live_latencies = []
        self.count_sent = 0
        self.count_lost = 0
        self.line.set_data([], [])
        self.to_line.set_data([], [])
        self._update_stats_label()
        self.canvas.draw_idle()

    def _update_graph(self):
        n = len(self.gy)
        xs = list(range(n))
        ys = [v if v is not None else float("nan") for v in self.gy]
        self.line.set_data(xs, ys)
        self.to_line.set_data(self.gto_x, [0] * len(self.gto_x))
        self.ax.relim()
        self.ax.autoscale_view()
        if n > 0:
            self.ax.set_xlim(0, max(10, n - 1))
        self.canvas.draw_idle()

    def _update_stats_label(self):
        last = "—"
        if self.gy and self.gy[-1] is not None:
            last = f"{self.gy[-1]} ms"
        elif self.gy:
            last = "timeout"
        avg = f"{statistics.mean(self.live_latencies):.1f} ms" if self.live_latencies else "—"
        loss = (self.count_lost / self.count_sent * 100) if self.count_sent else 0.0
        self.stats_var.set(
            f"Envoyés: {self.count_sent}   Perdus: {self.count_lost} ({loss:.1f}%)   "
            f"Dernière: {last}   Moy: {avg}")

    def clear_all(self):
        if self.ping_running:
            return
        self._reset_live_data()
        self.log_area.configure(state="normal")
        self.log_area.delete("1.0", "end")
        self.log_area.configure(state="disabled")
        self.set_status("Prêt.", IDLE_COLOR)

    # ------------------------------------------------------------------
    # Analyse hors-ligne d'un fichier log existant
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
                               self.plot_prefix_var.get().strip() or "ping"),
                         daemon=True).start()

    def _analyze_worker(self, log_file, csv_file, plot_prefix):
        try:
            rows = parse_log_file(log_file)
            s = build_summary(rows)
            self._write_csv(rows, s, csv_file)
            self._save_plots(rows, s, plot_prefix)
            self.root.after(0, self._show_analysis_result, rows, s, csv_file, plot_prefix)
        except Exception as e:
            self.root.after(0, self._analysis_error, str(e))

    def _write_csv(self, rows, s, csv_file):
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Timestamp", "Latency_ms", "Status"])
            w.writerows(rows)
            w.writerow([])
            w.writerow(["=== Résumé ==="])
            w.writerow(["Total pings", s["total"]])
            w.writerow(["Paquets perdus", s["lost"]])
            w.writerow(["Perte (%)", f"{s['loss_pct']:.2f}"])
            if s["avg"] is not None:
                w.writerow(["Latence min (ms)", s["min"]])
                w.writerow(["Latence max (ms)", s["max"]])
                w.writerow(["Latence moyenne (ms)", f"{s['avg']:.2f}"])
                w.writerow(["Latence médiane (ms)", s["median"]])
                w.writerow(["Écart-type (ms)", f"{s['stdev']:.2f}"])
                w.writerow(["Gigue / jitter (ms)", f"{s['jitter']:.2f}"])

    def _save_plots(self, rows, s, plot_prefix):
        y = [r[1] if r[1] is not None else float("nan") for r in rows]
        timeouts = [i for i, r in enumerate(rows) if r[1] is None]

        fig1 = Figure(figsize=(12, 5), dpi=100)
        ax1 = fig1.add_subplot(111)
        ax1.plot(range(len(y)), y, color=self.accent, linewidth=0.9, label="Latence (ms)")
        ax1.scatter(timeouts, [0] * len(timeouts), color=ERR_COLOR, marker="x", label="Timeouts")
        ax1.set_xlabel("Index du ping")
        ax1.set_ylabel("Latence (ms)")
        ax1.set_title("Latence dans le temps")
        ax1.legend()
        ax1.grid(True)
        fig1.tight_layout()
        FigureCanvasAgg(fig1)
        fig1.savefig(f"{plot_prefix}_latency.png")

        latencies = [r[1] for r in rows if r[1] is not None]
        if latencies:
            fig2 = Figure(figsize=(7, 5), dpi=100)
            ax2 = fig2.add_subplot(111)
            ax2.hist(latencies, bins=50, color=self.accent, edgecolor="black")
            ax2.set_xlabel("Latence (ms)")
            ax2.set_ylabel("Nombre")
            ax2.set_title("Distribution de la latence")
            fig2.tight_layout()
            FigureCanvasAgg(fig2)
            fig2.savefig(f"{plot_prefix}_hist.png")

    def _show_analysis_result(self, rows, s, csv_file, plot_prefix):
        self.gy = [r[1] for r in rows]
        self.gto_x = [i for i, r in enumerate(rows) if r[1] is None]
        self.live_latencies = [r[1] for r in rows if r[1] is not None]
        self.count_sent = s["total"]
        self.count_lost = s["lost"]
        self.ax.set_title("Latence (fichier analysé)")
        self._update_graph()
        self._update_stats_label()

        self.log(f"Total pings: {s['total']}", "info")
        self.log(f"Paquets perdus: {s['lost']} ({s['loss_pct']:.2f}%)",
                 "err" if s["lost"] else "ok")
        if s["avg"] is not None:
            self.log(f"Min/Max: {s['min']} / {s['max']} ms", "ok")
            self.log(f"Moyenne: {s['avg']:.2f} ms   Médiane: {s['median']} ms", "ok")
            self.log(f"Écart-type: {s['stdev']:.2f} ms   Gigue: {s['jitter']:.2f} ms", "ok")
        self.log(f"CSV sauvegardé: {csv_file}", "info")
        self.log(f"Graphiques: {plot_prefix}_latency.png, {plot_prefix}_hist.png", "info")
        self.set_status(
            f"Analyse terminée — {s['total']} pings, {s['loss_pct']:.1f}% de perte.", IDLE_COLOR)
        self.btn_analyze.configure(state="normal")
        self.btn_ping.configure(state="normal")
        messagebox.showinfo("Terminé", "Analyse terminée avec succès !")

    def _analysis_error(self, message):
        self.log(f"ERREUR pendant l'analyse: {message}", "err")
        self.set_status("Erreur lors de l'analyse", ERR_COLOR)
        self.btn_analyze.configure(state="normal")
        self.btn_ping.configure(state="normal")


if __name__ == "__main__":
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    try:
        root.iconbitmap(resource_path("ping_tool_ico.ico"))
    except Exception:
        pass
    app = PingApp(root)
    root.mainloop()
