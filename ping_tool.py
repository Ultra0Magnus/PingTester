#!/usr/bin/env python3
"""PingTester en ligne de commande : --mode ping (journal) ou --mode analyze (CSV + PNG)."""
import argparse
import time

from ping_core import (CSV_FILE, LOG_FILE, compute_quality, format_log_line, group_by_host, now_str,
                       parse_log_file, ping_once, save_plots, summarize, write_csv)


# ----------------------------
# Run ping and log results
# ----------------------------
def run_ping(host="8.8.8.8", duration=60, log_file=LOG_FILE, interval=1.0):
    """Ping `host` every `interval` s for `duration` s, logging in the normalized
    format shared with the GUI (TS\\tHOST\\tSTATUS\\tLAT)."""
    next_at = time.monotonic()
    deadline = next_at + duration - 1e-6   # tolerance for rounding in next_at += interval
    with open(log_file, "w", encoding="utf-8", buffering=1) as f:
        while True:
            ts = now_str()
            try:
                lat, _ = ping_once(host)
                status = "OK" if lat is not None else "TIMEOUT"
                f.write(format_log_line(ts, host, status, lat) + "\n")
                print(f"{ts} - {host}: " + (f"time={lat} ms" if lat is not None else "timeout"))
            except Exception as e:
                f.write(format_log_line(ts, host, "ERROR", extra=e) + "\n")
                print(f"{ts} - ERROR: {e}")
            next_at = max(next_at + interval, time.monotonic())   # no catch-up after a slow ping
            if next_at >= deadline:
                break
            time.sleep(max(0.0, next_at - time.monotonic()))


# ----------------------------
# Analyze + export CSV + plots
# ----------------------------
def analyze_log(log_file=LOG_FILE, csv_file=CSV_FILE, plot_prefix="ping"):
    rows = parse_log_file(log_file)
    groups = group_by_host(rows)
    summaries = {host: summarize(lats) for host, lats in groups.items()}
    write_csv(rows, summaries, csv_file)
    plots = save_plots(groups, plot_prefix)

    print("=== Ping Analysis Report ===")
    if not summaries:
        print("No ping results found.")
    for host, s in summaries.items():
        score, letter = compute_quality(s)
        print(f"[{host}] Total pings: {s['total']}  Lost: {s['lost']} ({s['loss_pct']:.2f}%)")
        if s["avg"] is not None:
            print(f"[{host}] Min/Max/Avg latency: {s['min']} / {s['max']} / {s['avg']:.2f} ms"
                  f"  Jitter: {s['jitter']:.2f} ms  Score: {score} ({letter})")
    print(f"Results saved to {csv_file}")
    print(f"Plots saved: {', '.join(plots)}")


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

    try:
        if args.mode == "ping":
            run_ping(args.host, args.duration, args.file)
        else:
            analyze_log(args.file, args.csv, args.plot_prefix)
    except KeyboardInterrupt:
        print("\nInterrupted.")
