import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import subprocess
import datetime
import time
import re
import csv
import statistics
import threading
import matplotlib
from pathlib import Path

# Important : On dit à Matplotlib de ne pas utiliser d'interface graphique (évite les conflits)
matplotlib.use('Agg')
import matplotlib.pyplot as plt

class PingApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Outil de Ping & Analyse")
        self.root.geometry("600x650")

        # --- Cadre: Paramètres ---
        p_frame = ttk.LabelFrame(root, text="Paramètres", padding=10)
        p_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(p_frame, text="Hôte (IP/Domaine):").grid(row=0, column=0, sticky="w")
        self.host_var = tk.StringVar(value="8.8.8.8")
        ttk.Entry(p_frame, textvariable=self.host_var).grid(row=0, column=1, padx=5, pady=2)

        ttk.Label(p_frame, text="Durée (secondes):").grid(row=1, column=0, sticky="w")
        self.duration_var = tk.StringVar(value="60")
        ttk.Entry(p_frame, textvariable=self.duration_var).grid(row=1, column=1, padx=5, pady=2)

        # --- Cadre: Fichiers ---
        f_frame = ttk.LabelFrame(root, text="Fichiers", padding=10)
        f_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(f_frame, text="Fichier Log:").grid(row=0, column=0, sticky="w")
        self.log_file_var = tk.StringVar(value="ping_log.txt")
        ttk.Entry(f_frame, textvariable=self.log_file_var, width=40).grid(row=0, column=1, padx=5, pady=2)
        
        ttk.Label(f_frame, text="Sortie CSV:").grid(row=1, column=0, sticky="w")
        self.csv_file_var = tk.StringVar(value="ping_results.csv")
        ttk.Entry(f_frame, textvariable=self.csv_file_var, width=40).grid(row=1, column=1, padx=5, pady=2)

        ttk.Label(f_frame, text="Préfixe Graphiques:").grid(row=2, column=0, sticky="w")
        self.plot_prefix_var = tk.StringVar(value="ping")
        ttk.Entry(f_frame, textvariable=self.plot_prefix_var, width=40).grid(row=2, column=1, padx=5, pady=2)

        # --- Cadre: Actions ---
        btn_frame = ttk.Frame(root, padding=10)
        btn_frame.pack(fill="x", padx=10, pady=5)

        self.btn_ping = ttk.Button(btn_frame, text="▶ Lancer Ping", command=self.start_ping_thread)
        self.btn_ping.pack(side="left", fill="x", expand=True, padx=5)

        self.btn_analyze = ttk.Button(btn_frame, text="📊 Analyser Données", command=self.start_analyze_thread)
        self.btn_analyze.pack(side="left", fill="x", expand=True, padx=5)

        # --- Zone de Log ---
        log_frame = ttk.LabelFrame(root, text="Journal d'exécution", padding=10)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        self.log_area = scrolledtext.ScrolledText(log_frame, state='disabled', height=15)
        self.log_area.pack(fill="both", expand=True)

    def log(self, message):
        self.log_area.config(state='normal')
        self.log_area.insert(tk.END, message + "\n")
        self.log_area.see(tk.END)
        self.log_area.config(state='disabled')

    def start_ping_thread(self):
        threading.Thread(target=self.run_ping, daemon=True).start()

    def start_analyze_thread(self):
        threading.Thread(target=self.analyze_log, daemon=True).start()

    def run_ping(self):
        host = self.host_var.get()
        try:
            duration = int(self.duration_var.get())
        except ValueError:
            self.log("ERREUR: La durée doit être un nombre entier.")
            return
        log_file = self.log_file_var.get()

        self.btn_ping.config(state="disabled")
        self.log(f"--- Démarrage du Ping vers {host} pour {duration}s ---")
        
        start_time = time.time()
        
        with open(log_file, "w", encoding="utf-8") as f:
            while True:
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    creationflags = 0
                    if hasattr(subprocess, 'CREATE_NO_WINDOW'):
                        creationflags = subprocess.CREATE_NO_WINDOW
                        
                    result = subprocess.run(
                        ["ping", "-n", "1", host],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        creationflags=creationflags
                    )
                    output = result.stdout.strip()
                    f.write(f"{now} - {output}\n")
                    f.flush()
                    
                    lat_match = re.search(r"(?:temps|time)[=<]?\s*\d+\s*ms", output, re.IGNORECASE)
                    display_msg = lat_match.group(0) if lat_match else "Ping envoyé..."
                    self.log(f"{now} - {display_msg}")

                except Exception as e:
                    f.write(f"{now} - ERROR: {e}\n")
                    self.log(f"{now} - ERROR: {e}")

                if time.time() - start_time > duration:
                    break
                time.sleep(1)
        
        self.log(f"--- Ping terminé. Données sauvegardées dans {log_file} ---")
        self.btn_ping.config(state="normal")

    def parse_log(self, log_file):
        lines = Path(log_file).read_text(encoding="utf-8", errors="ignore").splitlines()
        rows = []
        ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
        latency_re = re.compile(r"(?:temps|time)[=<]?\s*(\d+)\s*ms", re.IGNORECASE)
        timeout_re = re.compile(r"(D[ée]lai d|Request timed out)", re.IGNORECASE)

        current_ts = None
        for line in lines:
            ts_match = ts_re.match(line)
            if ts_match:
                current_ts = ts_match.group(1)

            lat_match = latency_re.search(line)
            if lat_match:
                rows.append([current_ts, int(lat_match.group(1)), "OK"])
                continue

            if timeout_re.search(line):
                rows.append([current_ts, None, "TIMEOUT"])
                continue
        return rows

    def analyze_log(self):
        log_file = self.log_file_var.get()
        csv_file = self.csv_file_var.get()
        plot_prefix = self.plot_prefix_var.get()

        if not Path(log_file).exists():
            self.log(f"ERREUR: Le fichier {log_file} n'existe pas.")
            return

        self.btn_analyze.config(state="disabled")
        self.log("--- Démarrage de l'analyse ---")

        try:
            rows = self.parse_log(log_file)
            total = len(rows)
            lost = sum(1 for r in rows if r[2] == "TIMEOUT")
            latencies = [r[1] for r in rows if r[1] is not None]

            with open(csv_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Timestamp", "Latency_ms", "Status"])
                writer.writerows(rows)
            
            summary = []
            summary.append(f"Total pings: {total}")
            percentage = (lost/total*100 if total else 0)
            summary.append(f"Lost packets: {lost} ({percentage:.2f}%)")
            
            if latencies:
                summary.append(f"Min: {min(latencies)} ms")
                summary.append(f"Max: {max(latencies)} ms")
                summary.append(f"Moyenne: {statistics.mean(latencies):.2f} ms")
            
            for line in summary:
                self.log(line)
            
            self.log(f"CSV sauvegardé: {csv_file}")

            y = [r[1] if r[1] is not None else None for r in rows]
            
            plt.figure(figsize=(10, 4))
            plt.plot([v if v is not None else float("nan") for v in y], label="Latence (ms)", color="blue")
            timeouts = [i for i, v in enumerate(y) if v is None]
            plt.scatter(timeouts, [0]*len(timeouts), color="red", marker="x", label="Timeouts")
            plt.title("Latence dans le temps")
            plt.legend()
            plt.grid(True)
            plt.tight_layout()
            plt.savefig(f"{plot_prefix}_latency.png")
            plt.close()

            if latencies:
                plt.figure(figsize=(6, 4))
                plt.hist(latencies, bins=50, color="green", edgecolor="black")
                plt.title("Distribution de la latence")
                plt.tight_layout()
                plt.savefig(f"{plot_prefix}_hist.png")
                plt.close()

            self.log(f"Graphiques sauvegardés: {plot_prefix}_latency.png, {plot_prefix}_hist.png")
            messagebox.showinfo("Terminé", "Analyse terminée avec succès !")

        except Exception as e:
            self.log(f"ERREUR pendant l'analyse: {e}")
            import traceback
            traceback.print_exc()
        
        self.btn_analyze.config(state="normal")

# -------------------------------------------------------------
# MAIN : C'est ici que l'icône est gérée
# -------------------------------------------------------------
if __name__ == "__main__":
    # 1. On crée la fenêtre (root) UNE SEULE FOIS
    root = tk.Tk()
    
    # 2. On applique l'icône si elle existe
    icon_file = "ping_tool_ico.ico"
    try:
        root.iconbitmap(icon_file)
    except:
        pass # Pas grave si l'icône manque

    # 3. On lance l'application
    app = PingApp(root)
    root.mainloop()