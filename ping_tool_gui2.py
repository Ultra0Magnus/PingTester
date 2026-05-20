import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
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

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.backends.backend_agg import FigureCanvasAgg

# --- Expressions régulières partagées (français + anglais) ---
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
LATENCY_RE = re.compile(r"(?:temps|time)[=<]?\s*(\d+)\s*ms", re.IGNORECASE)
TIMEOUT_RE = re.compile(r"(D[ée]lai d|Request timed out|h[ôo]te de destination|Destination host)", re.IGNORECASE)

# --- Palettes de couleurs (thème clair / sombre) ---
THEMES = {
    "light": {
        "bg": "#f4f4f5", "fg": "#1a1a1a", "entry": "#ffffff",
        "text_bg": "#ffffff", "text_fg": "#1a1a1a", "accent": "#0078d4",
        "plot_bg": "#ffffff", "plot_fg": "#1a1a1a", "grid": "#d4d4d8",
        "ok": "#1f9d55", "warn": "#d97706", "err": "#dc2626", "line": "#2563eb",
    },
    "dark": {
        "bg": "#23272e", "fg": "#e4e4e7", "entry": "#2f343c",
        "text_bg": "#1b1e24", "text_fg": "#e4e4e7", "accent": "#4ea1ff",
        "plot_bg": "#1b1e24", "plot_fg": "#e4e4e7", "grid": "#3a3f47",
        "ok": "#34d399", "warn": "#fbbf24", "err": "#f87171", "line": "#60a5fa",
    },
}


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


class PingApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Outil de Ping & Analyse")
        self.root.geometry("920x840")
        self.root.minsize(760, 640)

        self.theme_name = "light"
        self.ping_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.ping_running = False
        self.start_time = 0.0
        self.duration = 0
        self.continuous = False

        # Données du graphique en direct
        self.gy = []          # latences (None pour timeout)
        self.gto_x = []       # index des timeouts
        self.count_sent = 0
        self.count_lost = 0
        self.live_latencies = []

        self._build_widgets()
        self.apply_theme()

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------
    def _build_widgets(self):
        # ---- Paramètres ----
        p_frame = ttk.LabelFrame(self.root, text="Paramètres", padding=10)
        p_frame.pack(fill="x", padx=10, pady=(8, 4))
        p_frame.columnconfigure(1, weight=1)
        p_frame.columnconfigure(3, weight=1)

        ttk.Label(p_frame, text="Hôte (IP/Domaine):").grid(row=0, column=0, sticky="w")
        self.host_var = tk.StringVar(value="8.8.8.8")
        ttk.Entry(p_frame, textvariable=self.host_var).grid(row=0, column=1, sticky="ew", padx=5, pady=2)

        ttk.Label(p_frame, text="Durée (secondes):").grid(row=0, column=2, sticky="w", padx=(10, 0))
        self.duration_var = tk.StringVar(value="60")
        self.duration_entry = ttk.Entry(p_frame, textvariable=self.duration_var, width=10)
        self.duration_entry.grid(row=0, column=3, sticky="w", padx=5, pady=2)

        self.continuous_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(p_frame, text="Ping en continu", variable=self.continuous_var,
                        command=self._toggle_continuous).grid(row=1, column=0, sticky="w", pady=2)

        ttk.Label(p_frame, text="Seuil d'alerte (ms, 0=off):").grid(row=1, column=2, sticky="w", padx=(10, 0))
        self.threshold_var = tk.StringVar(value="100")
        ttk.Entry(p_frame, textvariable=self.threshold_var, width=10).grid(row=1, column=3, sticky="w", padx=5, pady=2)

        # ---- Fichiers ----
        f_frame = ttk.LabelFrame(self.root, text="Fichiers", padding=10)
        f_frame.pack(fill="x", padx=10, pady=4)
        f_frame.columnconfigure(1, weight=1)

        ttk.Label(f_frame, text="Fichier Log:").grid(row=0, column=0, sticky="w")
        self.log_file_var = tk.StringVar(value="ping_log.txt")
        ttk.Entry(f_frame, textvariable=self.log_file_var).grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        ttk.Button(f_frame, text="Parcourir…", command=self._browse_log).grid(row=0, column=2, padx=2)

        ttk.Label(f_frame, text="Sortie CSV:").grid(row=1, column=0, sticky="w")
        self.csv_file_var = tk.StringVar(value="ping_results.csv")
        ttk.Entry(f_frame, textvariable=self.csv_file_var).grid(row=1, column=1, sticky="ew", padx=5, pady=2)
        ttk.Button(f_frame, text="Parcourir…", command=self._browse_csv).grid(row=1, column=2, padx=2)

        ttk.Label(f_frame, text="Préfixe Graphiques:").grid(row=2, column=0, sticky="w")
        self.plot_prefix_var = tk.StringVar(value="ping")
        ttk.Entry(f_frame, textvariable=self.plot_prefix_var).grid(row=2, column=1, sticky="ew", padx=5, pady=2)

        # ---- Actions ----
        btn_frame = ttk.Frame(self.root, padding=(10, 4))
        btn_frame.pack(fill="x", padx=10)

        self.btn_ping = ttk.Button(btn_frame, text="▶ Lancer Ping", command=self.start_ping)
        self.btn_ping.pack(side="left", fill="x", expand=True, padx=3)
        self.btn_stop = ttk.Button(btn_frame, text="■ Stop", command=self.stop_ping, state="disabled")
        self.btn_stop.pack(side="left", fill="x", expand=True, padx=3)
        self.btn_analyze = ttk.Button(btn_frame, text="📊 Analyser", command=self.start_analyze)
        self.btn_analyze.pack(side="left", fill="x", expand=True, padx=3)
        self.btn_clear = ttk.Button(btn_frame, text="🗑 Effacer", command=self.clear_all)
        self.btn_clear.pack(side="left", fill="x", expand=True, padx=3)
        self.btn_theme = ttk.Button(btn_frame, text="🌙 Thème", command=self.toggle_theme)
        self.btn_theme.pack(side="left", fill="x", expand=True, padx=3)

        # ---- Barre de statut ----
        status_frame = ttk.Frame(self.root, padding=(10, 4))
        status_frame.pack(fill="x", padx=10)

        self.status_dot = tk.Canvas(status_frame, width=16, height=16, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 6))
        self._dot = self.status_dot.create_oval(3, 3, 13, 13, fill="#9ca3af", outline="")

        self.status_var = tk.StringVar(value="Prêt.")
        ttk.Label(status_frame, textvariable=self.status_var).pack(side="left")

        self.stats_var = tk.StringVar(value="Envoyés: 0   Perdus: 0 (0.0%)   Dernière: —   Moy: —")
        ttk.Label(status_frame, textvariable=self.stats_var).pack(side="right")

        self.progress = ttk.Progressbar(self.root, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=10, pady=(0, 4))

        # ---- Onglets : Graphique / Journal ----
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        graph_tab = ttk.Frame(self.notebook)
        self.notebook.add(graph_tab, text="Graphique")

        self.fig = Figure(figsize=(8, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.line, = self.ax.plot([], [], linewidth=1.0, label="Latence (ms)")
        self.to_line, = self.ax.plot([], [], "x", markersize=7, label="Timeouts")
        self.threshold_line = self.ax.axhline(0, linestyle="--", linewidth=0.9, visible=False)
        self.ax.set_xlabel("Index du ping")
        self.ax.set_ylabel("Latence (ms)")
        self.ax.set_title("Latence en direct")
        self.ax.legend(loc="upper right", fontsize=8)
        self.canvas = FigureCanvasTkAgg(self.fig, master=graph_tab)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        log_tab = ttk.Frame(self.notebook)
        self.notebook.add(log_tab, text="Journal")
        self.log_area = scrolledtext.ScrolledText(log_tab, state="disabled", height=12)
        self.log_area.pack(fill="both", expand=True)

    # ------------------------------------------------------------------
    # Thème
    # ------------------------------------------------------------------
    def apply_theme(self):
        c = THEMES[self.theme_name]
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=c["bg"], foreground=c["fg"])
        style.configure("TFrame", background=c["bg"])
        style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        style.configure("TLabelframe", background=c["bg"], foreground=c["fg"])
        style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])
        style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"])
        style.map("TCheckbutton", background=[("active", c["bg"])])
        style.configure("TButton", background=c["entry"], foreground=c["fg"], padding=5)
        style.map("TButton",
                  background=[("active", c["accent"]), ("disabled", c["bg"])],
                  foreground=[("active", "#ffffff")])
        style.configure("TEntry", fieldbackground=c["entry"], foreground=c["fg"],
                        insertcolor=c["fg"])
        style.configure("TNotebook", background=c["bg"])
        style.configure("TNotebook.Tab", background=c["entry"], foreground=c["fg"], padding=(12, 4))
        style.map("TNotebook.Tab", background=[("selected", c["accent"])],
                  foreground=[("selected", "#ffffff")])
        style.configure("TProgressbar", background=c["accent"], troughcolor=c["entry"])

        self.root.configure(bg=c["bg"])
        self.status_dot.configure(bg=c["bg"])
        self.log_area.configure(bg=c["text_bg"], fg=c["text_fg"], insertbackground=c["fg"])
        self.log_area.tag_config("ok", foreground=c["text_fg"])
        self.log_area.tag_config("warn", foreground=c["warn"])
        self.log_area.tag_config("err", foreground=c["err"])
        self.log_area.tag_config("info", foreground=c["accent"])

        # Couleurs du graphique
        self.fig.set_facecolor(c["plot_bg"])
        self.ax.set_facecolor(c["plot_bg"])
        for spine in self.ax.spines.values():
            spine.set_color(c["plot_fg"])
        self.ax.tick_params(colors=c["plot_fg"])
        self.ax.xaxis.label.set_color(c["plot_fg"])
        self.ax.yaxis.label.set_color(c["plot_fg"])
        self.ax.title.set_color(c["plot_fg"])
        self.ax.grid(True, color=c["grid"], linewidth=0.5)
        self.line.set_color(c["line"])
        self.to_line.set_color(c["err"])
        self.threshold_line.set_color(c["warn"])
        self.canvas.draw_idle()

    def toggle_theme(self):
        self.theme_name = "dark" if self.theme_name == "light" else "light"
        self.btn_theme.config(text="☀ Thème" if self.theme_name == "dark" else "🌙 Thème")
        self.apply_theme()

    # ------------------------------------------------------------------
    # Petites aides UI
    # ------------------------------------------------------------------
    def log(self, message, tag="ok"):
        self.log_area.config(state="normal")
        self.log_area.insert(tk.END, message + "\n", tag)
        self.log_area.see(tk.END)
        self.log_area.config(state="disabled")

    def set_status(self, text, color=None):
        self.status_var.set(text)
        if color:
            self.status_dot.itemconfig(self._dot, fill=color)

    def _toggle_continuous(self):
        if self.continuous_var.get():
            self.duration_entry.config(state="disabled")
        else:
            self.duration_entry.config(state="normal")

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

        # Réinitialisation de l'état
        self._reset_live_data()
        self.stop_event.clear()
        self.ping_running = True
        self.start_time = time.time()

        # Ligne de seuil
        thr = self._get_threshold()
        if thr > 0:
            self.threshold_line.set_ydata([thr, thr])
            self.threshold_line.set_visible(True)
        else:
            self.threshold_line.set_visible(False)

        # UI
        self.btn_ping.config(state="disabled")
        self.btn_analyze.config(state="disabled")
        self.btn_clear.config(state="disabled")
        self.btn_stop.config(state="normal")
        if self.continuous:
            self.progress.config(mode="indeterminate")
            self.progress.start(15)
            self.set_status(f"Ping en continu vers {host}…", THEMES[self.theme_name]["accent"])
        else:
            self.progress.config(mode="determinate", value=0)
            self.set_status(f"Ping vers {host} pour {self.duration}s…", THEMES[self.theme_name]["accent"])
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
            self.btn_stop.config(state="disabled")

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
                    # Pause ~1s, mais réactive à l'arrêt
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
                pct = min(100, (time.time() - self.start_time) / self.duration * 100)
                self.progress.config(value=pct)
            self.root.after(120, self._process_queue)

    def _handle_msg(self, msg):
        kind = msg[0]
        if kind == "data":
            _, now, lat, status = msg
            self.count_sent += 1
            thr = self._get_threshold()
            c = THEMES[self.theme_name]

            if status == "OK":
                self.gy.append(lat)
                self.live_latencies.append(lat)
                if thr > 0 and lat >= thr:
                    self.set_status(f"Latence élevée: {lat} ms", c["warn"])
                    self.log(f"{now} - temps={lat} ms  ⚠ au-dessus du seuil", "warn")
                else:
                    self.set_status("En cours…", c["ok"])
                    self.log(f"{now} - temps={lat} ms", "ok")
            else:
                self.count_lost += 1
                self.gy.append(None)
                self.gto_x.append(len(self.gy) - 1)
                self.set_status("Timeout / perte de paquet", c["err"])
                self.log(f"{now} - DÉLAI DÉPASSÉ (timeout)", "err")

            self._update_stats_label()
            self._update_graph()

        elif kind == "error":
            self.log(f"ERREUR: {msg[1]}", "err")
            self.set_status("Erreur lors du ping", THEMES[self.theme_name]["err"])

        elif kind == "done":
            self._finish_ping(msg[1])

    def _finish_ping(self, log_file):
        self.ping_running = False
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self.btn_ping.config(state="normal")
        self.btn_analyze.config(state="normal")
        self.btn_clear.config(state="normal")
        self.btn_stop.config(state="disabled")
        s = build_summary([[None, v, "OK" if v is not None else "TIMEOUT"] for v in self.gy])
        self.set_status(
            f"Terminé — {s['total']} pings, {s['lost']} perdus ({s['loss_pct']:.1f}%).",
            "#9ca3af")
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
        self.log_area.config(state="normal")
        self.log_area.delete("1.0", tk.END)
        self.log_area.config(state="disabled")
        self.set_status("Prêt.", "#9ca3af")

    # ------------------------------------------------------------------
    # Analyse hors-ligne d'un fichier log existant
    # ------------------------------------------------------------------
    def start_analyze(self):
        log_file = self.log_file_var.get().strip()
        if not Path(log_file).exists():
            messagebox.showerror("Erreur", f"Le fichier {log_file} n'existe pas.")
            return
        self.btn_analyze.config(state="disabled")
        self.btn_ping.config(state="disabled")
        self.set_status("Analyse en cours…", THEMES[self.theme_name]["accent"])
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
        c = THEMES["light"]  # PNG toujours en clair pour la lisibilité
        y = [r[1] if r[1] is not None else float("nan") for r in rows]
        timeouts = [i for i, r in enumerate(rows) if r[1] is None]

        fig1 = Figure(figsize=(12, 5), dpi=100)
        ax1 = fig1.add_subplot(111)
        ax1.plot(range(len(y)), y, color=c["line"], linewidth=0.8, label="Latence (ms)")
        ax1.scatter(timeouts, [0] * len(timeouts), color=c["err"], marker="x", label="Timeouts")
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
            ax2.hist(latencies, bins=50, color=c["ok"], edgecolor="black")
            ax2.set_xlabel("Latence (ms)")
            ax2.set_ylabel("Nombre")
            ax2.set_title("Distribution de la latence")
            fig2.tight_layout()
            FigureCanvasAgg(fig2)
            fig2.savefig(f"{plot_prefix}_hist.png")

    def _show_analysis_result(self, rows, s, csv_file, plot_prefix):
        # Charger les données dans le graphique en direct
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
            f"Analyse terminée — {s['total']} pings, {s['loss_pct']:.1f}% de perte.", "#9ca3af")
        self.btn_analyze.config(state="normal")
        self.btn_ping.config(state="normal")
        messagebox.showinfo("Terminé", "Analyse terminée avec succès !")

    def _analysis_error(self, message):
        self.log(f"ERREUR pendant l'analyse: {message}", "err")
        self.set_status("Erreur lors de l'analyse", THEMES[self.theme_name]["err"])
        self.btn_analyze.config(state="normal")
        self.btn_ping.config(state="normal")


def resource_path(rel):
    """Chemin d'une ressource, compatible exécution normale et PyInstaller (.exe)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


if __name__ == "__main__":
    root = tk.Tk()
    try:
        root.iconbitmap(resource_path("ping_tool_ico.ico"))
    except Exception:
        pass
    app = PingApp(root)
    root.mainloop()
