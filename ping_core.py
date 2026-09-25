"""Cœur de PingTester, sans interface graphique.

Exécution du ping, lecture du journal, statistiques, export CSV/PNG et test de
débit. Partagé par l'interface (``ping_tool_gui2.py``) et la ligne de commande
(``ping_tool.py``).
"""
import base64
import csv
import datetime
import http.client
import math
import re
import ssl
import statistics
import subprocess
import time
import urllib.parse
import urllib.request

LOG_FILE = "ping_log.txt"
CSV_FILE = "ping_results.csv"
DEFAULT_HOST = "(défaut)"      # hôte attribué aux lignes de l'ancien format brut
PING_TIMEOUT = 5               # s, garde-fou autour de « ping -n 1 » (4 s par défaut sous Windows)

# Couleurs des courbes en mode multi-hôtes
HOST_PALETTE = ["#6366f1", "#22c55e", "#f59e0b", "#ec4899", "#06b6d4", "#a855f7", "#ef4444", "#84cc16"]
DEFAULT_ACCENT = HOST_PALETTE[0]

# --- Expressions régulières partagées (français + anglais) ---
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
LATENCY_RE = re.compile(r"(?:temps|time)[=<]?\s*(\d+)\s*ms", re.IGNORECASE)
TIMEOUT_RE = re.compile(r"(D[ée]lai d|Request timed out|h[ôo]te de destination|Destination host)", re.IGNORECASE)

# Masque la console de ping.exe (sans effet hors Windows)
_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def extract_latency(text):
    m = LATENCY_RE.search(text)
    return int(m.group(1)) if m else None


def is_timeout(text):
    return bool(TIMEOUT_RE.search(text))


def ping_once(host, timeout=PING_TIMEOUT):
    """Envoie un ping (syntaxe Windows). Renvoie (latence en ms ou None, sortie brute).

    stdin est redirigé pour l'exe PyInstaller sans console, et les octets non
    décodables (page de code OEM de ping.exe) sont remplacés plutôt que de lever.
    """
    result = subprocess.run(["ping", "-n", "1", host], capture_output=True, text=True,
                            errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
                            creationflags=_CREATION_FLAGS)
    return extract_latency(result.stdout), result.stdout


def host_color(i, n, accent=DEFAULT_ACCENT):
    return accent if n == 1 else HOST_PALETTE[i % len(HOST_PALETTE)]


# ---------------------------------------------------------------------------
# Statistiques
# ---------------------------------------------------------------------------
class RunningStats:
    """Statistiques incrémentales, en O(1) par mesure : perte, min/max, moyenne,
    écart-type et gigue (moyenne des écarts entre deux réponses successives)."""

    __slots__ = ("total", "lost", "count", "min", "max", "_last", "_sum", "_sumsq", "_jitter_sum")

    def __init__(self):
        self.total = self.lost = self.count = 0
        self.min = self.max = self._last = None
        self._sum = self._sumsq = self._jitter_sum = 0

    def add(self, lat):
        """lat : latence en ms, ou None pour un paquet perdu."""
        self.total += 1
        if lat is None:
            self.lost += 1
            return
        if self.count:
            self._jitter_sum += abs(lat - self._last)
            self.min = min(self.min, lat)
            self.max = max(self.max, lat)
        else:
            self.min = self.max = lat
        self.count += 1
        self._last = lat
        self._sum += lat
        self._sumsq += lat * lat

    def summary(self):
        n = self.count
        s = {"total": self.total, "lost": self.lost,
             "loss_pct": (self.lost / self.total * 100) if self.total else 0.0,
             "min": self.min, "max": self.max, "avg": None,
             "median": None, "stdev": None, "jitter": None}
        if n:
            s["avg"] = self._sum / n
            # Variance d'échantillon ; numérateur entier exact pour des latences entières.
            s["stdev"] = math.sqrt(max(0, n * self._sumsq - self._sum ** 2) / (n * (n - 1))) if n > 1 else 0.0
            s["jitter"] = self._jitter_sum / (n - 1) if n > 1 else 0.0
        return s


def summarize(latencies):
    """Résumé complet (médiane comprise) d'une série de latences (None = perdu)."""
    rs = RunningStats()
    for lat in latencies:
        rs.add(lat)
    s = rs.summary()
    if rs.count:
        s["median"] = statistics.median(lat for lat in latencies if lat is not None)
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


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------
def format_log_line(ts, host, status, lat=None, extra=""):
    """Ligne du format normalisé : TS\\tHÔTE\\tSTATUT\\tLATENCE[\\tDÉTAIL]."""
    line = f"{ts}\t{host}\t{status}\t{'' if lat is None else lat}"
    if extra:
        line += "\t" + " ".join(str(extra).split())   # ni tabulation ni saut de ligne
    return line


def parse_log_file(log_file):
    """Renvoie des lignes [timestamp, hôte, latence, statut] (statut OK ou TIMEOUT).

    Gère le format normalisé (TS\\tHOST\\tSTATUS\\tLAT) et l'ancien format brut
    FR/EN. Les lignes ERROR (ping non exécuté) sont ignorées, comme en direct.
    """
    rows = []
    current_ts = None
    with open(log_file, encoding="utf-8", errors="ignore") as f:
        for line in f:
            parts = line.rstrip("\r\n").split("\t")
            if len(parts) >= 4 and parts[2] in ("OK", "TIMEOUT", "ERROR"):
                ts, host, status, lat = parts[:4]
                if status == "TIMEOUT":
                    rows.append([ts, host, None, status])
                elif status == "OK" and lat.isdigit():
                    rows.append([ts, host, int(lat), status])
                continue
            ts_match = TS_RE.match(line)
            if ts_match:
                current_ts = ts_match.group(1)
            lat = extract_latency(line)
            if lat is not None:
                rows.append([current_ts, DEFAULT_HOST, lat, "OK"])
            elif is_timeout(line):
                rows.append([current_ts, DEFAULT_HOST, None, "TIMEOUT"])
    return rows


def group_by_host(rows):
    """{hôte: [latence ou None, ...]} dans l'ordre d'apparition."""
    groups = {}
    for _, host, lat, _ in rows:
        groups.setdefault(host, []).append(lat)
    return groups


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------
def write_csv(rows, summaries, csv_file):
    """rows : sortie de parse_log_file ; summaries : {hôte: summarize(...)}."""
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp", "Host", "Latency_ms", "Status"])
        w.writerows(rows)
        for host, s in summaries.items():
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


def save_plots(groups, plot_prefix, accent=DEFAULT_ACCENT):
    """Enregistre la courbe de latence (+ l'histogramme s'il y a des réponses).

    API objet de matplotlib (sans pyplot) : utilisable depuis un thread et sans
    charger matplotlib tant qu'on n'exporte pas. Renvoie les fichiers écrits.
    """
    from matplotlib.figure import Figure

    files = []
    fig = Figure(figsize=(12, 5), dpi=100)
    ax = fig.add_subplot(111)
    for i, (host, lats) in enumerate(groups.items()):
        color = host_color(i, len(groups), accent)
        ax.plot(range(len(lats)), [math.nan if v is None else v for v in lats],
                linewidth=0.9, color=color, label=host)
        lost = [j for j, v in enumerate(lats) if v is None]
        ax.scatter(lost, [0] * len(lost), color=color, marker="x")
    ax.set_xlabel("Index du ping")
    ax.set_ylabel("Latence (ms)")
    ax.set_title("Latence dans le temps")
    if groups:
        ax.legend()
    ax.grid(True)
    fig.tight_layout()
    files.append(f"{plot_prefix}_latency.png")
    fig.savefig(files[-1])

    all_lat = [v for lats in groups.values() for v in lats if v is not None]
    if all_lat:
        fig = Figure(figsize=(7, 5), dpi=100)
        ax = fig.add_subplot(111)
        ax.hist(all_lat, bins=50, color=accent, edgecolor="black")
        ax.set_xlabel("Latence (ms)")
        ax.set_ylabel("Nombre")
        ax.set_title("Distribution de la latence")
        fig.tight_layout()
        files.append(f"{plot_prefix}_hist.png")
        fig.savefig(files[-1])
    return files


# ---------------------------------------------------------------------------
# Test de débit (Cloudflare)
#
# Une connexion HTTPS persistante par phase (http.client) : la latence mesure un
# aller-retour HTTP et non DNS + TCP + TLS à chaque essai, et les débits ne
# paient pas une poignée de main par requête. Chaque phase dure au plus
# SPEED_SECONDS et reste annulable via l'Event `stop`.
# ---------------------------------------------------------------------------
SPEED_HOST = "speed.cloudflare.com"
SPEED_HEADERS = {"User-Agent": "PingTester"}
SPEED_TIMEOUT = 15             # s, par opération réseau
SPEED_SECONDS = 10.0           # durée max de chaque phase de débit
SPEED_DL_REQUEST = 25_000_000  # octets par requête de téléchargement (répétée)
SPEED_UL_FIRST = 2_000_000     # 1re requête d'envoi ; les suivantes visent ~1 s de données
SPEED_UL_REQUEST = 25_000_000  # octets max par requête d'envoi
SPEED_BLOCK = 65536
SPEED_LAT_SAMPLES = 5
SPEED_PROGRESS_PERIOD = 0.12   # s entre deux rappels de progression


def _mbps(nbytes, seconds):
    return nbytes * 8 / seconds / 1e6 if seconds > 0 else 0.0


def _speed_connection():
    """Connexion HTTPS vers Cloudflare, via le proxy système s'il y en a un
    (même détection qu'urllib : variables d'environnement / registre Windows)."""
    ctx = ssl.create_default_context()
    proxy = urllib.request.getproxies().get("https")
    if proxy and not urllib.request.proxy_bypass(SPEED_HOST):
        p = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        headers = {}
        if p.username:
            cred = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
            headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        conn = http.client.HTTPSConnection(p.hostname, p.port, timeout=SPEED_TIMEOUT, context=ctx)
        conn.set_tunnel(SPEED_HOST, 443, headers=headers)
    else:
        conn = http.client.HTTPSConnection(SPEED_HOST, timeout=SPEED_TIMEOUT, context=ctx)
    conn.connect()   # établie avant de lancer les chronos
    return conn


def _check(resp):
    if resp.status == 429:
        raise OSError("trop de tests récents, le serveur limite les mesures : réessayez dans quelques minutes")
    if resp.status != 200:
        raise OSError(f"HTTP {resp.status} {resp.reason}")


def measure_latency(stop, samples=SPEED_LAT_SAMPLES):
    """Latence HTTP (ms) : minimum de plusieurs allers-retours sur une connexion
    chaude. Renvoie None si annulé avant la première mesure."""
    conn = _speed_connection()
    try:
        best = None
        for i in range(samples + 1):          # la 1re requête sert d'échauffement
            if stop.is_set():
                break
            t = time.perf_counter()
            conn.request("GET", "/__down?bytes=0", headers=SPEED_HEADERS)
            resp = conn.getresponse()
            resp.read()
            _check(resp)
            ms = (time.perf_counter() - t) * 1000
            if i and (best is None or ms < best):
                best = ms
        return best
    finally:
        conn.close()


def measure_download(stop, progress=None, seconds=SPEED_SECONDS):
    """Débit descendant (Mbps). Enchaîne les requêtes jusqu'à `seconds` pour que
    la mesure dure assez longtemps même sur un lien rapide.
    progress(fraction, mbps) est appelé au plus toutes les SPEED_PROGRESS_PERIOD s."""
    conn = _speed_connection()
    received = 0
    start = time.perf_counter()
    next_report = start + SPEED_PROGRESS_PERIOD
    try:
        while True:
            conn.request("GET", f"/__down?bytes={SPEED_DL_REQUEST}", headers=SPEED_HEADERS)
            resp = conn.getresponse()
            _check(resp)
            while True:
                block = resp.read(SPEED_BLOCK)
                if not block:
                    break
                received += len(block)
                now = time.perf_counter()
                if now - start >= seconds or stop.is_set():
                    return _mbps(received, now - start)
                if progress and now >= next_report:
                    next_report = now + SPEED_PROGRESS_PERIOD
                    progress(min(1.0, (now - start) / seconds), _mbps(received, now - start))
    finally:
        conn.close()


def measure_upload(stop, progress=None, seconds=SPEED_SECONDS):
    """Débit montant (Mbps). Requêtes en flux (chunked) enchaînées sur la même
    connexion jusqu'à `seconds`, chacune d'environ 1 s de données au débit mesuré
    (entre SPEED_UL_FIRST et SPEED_UL_REQUEST octets) : durée bornée, annulation
    rapide, et attente de la réponse courte même derrière un proxy qui bufferise."""
    conn = _speed_connection()
    block = bytes(SPEED_BLOCK)
    sent = 0
    start = time.perf_counter()
    next_report = start + SPEED_PROGRESS_PERIOD

    def body(limit):
        nonlocal sent, next_report
        in_request = 0
        while in_request < limit and not stop.is_set():
            now = time.perf_counter()
            if now - start >= seconds:
                return
            if progress and now >= next_report:
                next_report = now + SPEED_PROGRESS_PERIOD
                progress(min(1.0, (now - start) / seconds), _mbps(sent, now - start))
            yield block
            in_request += len(block)
            sent += len(block)   # compté une fois remis au socket

    size = SPEED_UL_FIRST
    try:
        while not stop.is_set() and time.perf_counter() - start < seconds:
            req_start, req_sent = time.perf_counter(), sent
            conn.request("POST", "/__up", body=body(size),
                         headers={**SPEED_HEADERS, "Content-Type": "application/octet-stream"})
            if stop.is_set():      # annulé : inutile d'attendre la réponse, valeur indicative
                break
            resp = conn.getresponse()
            resp.read()
            _check(resp)
            rate = (sent - req_sent) / max(time.perf_counter() - req_start, 1e-3)   # octets/s
            size = int(min(SPEED_UL_REQUEST, max(SPEED_UL_FIRST, rate)))
        return _mbps(sent, time.perf_counter() - start)   # chrono arrêté à la dernière réponse
    finally:
        conn.close()
