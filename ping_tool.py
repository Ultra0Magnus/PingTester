#!/usr/bin/env python3
import subprocess
import datetime
import time
import re
import argparse
import csv
import statistics
import matplotlib.pyplot as plt
from pathlib import Path

LOG_FILE = "ping_log.txt"
CSV_FILE = "ping_results.csv"

# ----------------------------
# Run ping and log results
# ----------------------------
def run_ping(host="8.8.8.8", duration=60, log_file=LOG_FILE):
    """Run ping for a given duration and save raw output to log_file"""
    start_time = time.time()
    with open(log_file, "w", encoding="utf-8") as f:
        while True:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                result = subprocess.run(
                    ["ping", "-n", "1", host],  # Windows syntax
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                output = result.stdout.strip()
                f.write(f"{now} - {output}\n")
                f.flush()
                print(f"{now} - {output}")
            except Exception as e:
                f.write(f"{now} - ERROR: {e}\n")
                print(f"{now} - ERROR: {e}")
            if time.time() - start_time > duration:
                break
            time.sleep(1)

# ----------------------------
# Parse ping log (French + English)
# ----------------------------
def parse_log(log_file=LOG_FILE):
    """Parse log file and return rows [timestamp, latency, status]"""
    lines = Path(log_file).read_text(encoding="utf-8", errors="ignore").splitlines()
    rows = []

    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    # French "temps=22 ms" or English "time=22ms"
    latency_re = re.compile(r"(?:temps|time)[=<]?\s*(\d+)\s*ms", re.IGNORECASE)
    # French "Délai d'attente dépassé" or English "Request timed out"
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

# ----------------------------
# Analyze + export CSV + plots
# ----------------------------
def analyze_log(log_file=LOG_FILE, csv_file=CSV_FILE, plot_prefix="ping"):
    rows = parse_log(log_file)

    total = len(rows)
    lost = sum(1 for r in rows if r[2] == "TIMEOUT")
    latencies = [r[1] for r in rows if r[1] is not None]

    # Write CSV
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Timestamp", "Latency_ms", "Status"])
        writer.writerows(rows)
        writer.writerow([])
        writer.writerow(["=== Summary ==="])
        writer.writerow(["Total pings", total])
        writer.writerow(["Lost packets", lost])
        writer.writerow(["Packet loss (%)", (lost/total*100 if total else 0)])
        if latencies:
            writer.writerow(["Min latency (ms)", min(latencies)])
            writer.writerow(["Max latency (ms)", max(latencies)])
            writer.writerow(["Average latency (ms)", statistics.mean(latencies)])
            writer.writerow(["Median latency (ms)", statistics.median(latencies)])
            if len(latencies) > 1:
                writer.writerow(["Stdev latency (ms)", statistics.stdev(latencies)])

    print("=== Ping Analysis Report ===")
    print(f"Total pings: {total}")
    print(f"Lost packets: {lost} ({(lost/total*100 if total else 0):.2f}%)")
    if latencies:
        print(f"Min/Max/Avg latency: {min(latencies)} / {max(latencies)} / {statistics.mean(latencies):.2f} ms")
    print(f"Results saved to {csv_file}")

    # Plot 1: latency over time
    y = [r[1] if r[1] is not None else None for r in rows]
    plt.figure(figsize=(12, 5))
    plt.plot([v if v is not None else float("nan") for v in y], label="Latency (ms)", color="blue", linewidth=0.8)
    timeouts = [i for i, v in enumerate(y) if v is None]
    plt.scatter(timeouts, [0]*len(timeouts), color="red", marker="x", label="Timeouts")
    plt.xlabel("Ping index")
    plt.ylabel("Latency (ms)")
    plt.title("Latency over time")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(f"{plot_prefix}_latency.png")
    plt.close()

    # Plot 2: latency histogram
    if latencies:
        plt.figure(figsize=(7, 5))
        plt.hist(latencies, bins=50, color="green", edgecolor="black")
        plt.xlabel("Latency (ms)")
        plt.ylabel("Count")
        plt.title("Latency distribution")
        plt.tight_layout()
        plt.savefig(f"{plot_prefix}_hist.png")
        plt.close()

    print(f"Plots saved: {plot_prefix}_latency.png, {plot_prefix}_hist.png")

# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ping logger and analyzer")
    parser.add_argument("--mode", choices=["ping", "analyze"], required=True, help="Choose mode")
    parser.add_argument("--host", default="8.8.8.8", help="Host to ping")
    parser.add_argument("--duration", type=int, default=60, help="Duration in seconds (ping mode)")
    parser.add_argument("--file", default=LOG_FILE, help="Log file path")
    parser.add_argument("--csv", default=CSV_FILE, help="CSV output file")
    parser.add_argument("--plot-prefix", default="ping", help="Prefix for plot file names")
    args = parser.parse_args()

    if args.mode == "ping":
        run_ping(args.host, args.duration, args.file)
    elif args.mode == "analyze":
        analyze_log(args.file, args.csv, args.plot_prefix)
