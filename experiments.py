#!/usr/bin/env python3
"""Automated experiments: normal / link-failure / checksum-corruption runs for
several file sizes.  Writes results/results.csv.

Run (controller already running):  sudo python3 experiments.py --sizes 5 20 --reps 3
"""
import argparse
import csv
import json
import os
import shutil
import statistics
import subprocess
import time

from mininet.log import setLogLevel

from common import sha256_file
from topology import SERVER_IPS, build_net, prepare

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = "/tmp/backup_src"
SERVERS = [("h3", "/tmp/backup_h3"), ("h4", "/tmp/backup_h4")]
UPLINKS = {"s2": "s1-eth2", "s3": "s1-eth3"}      # s1's two core links


def make_file(path, mb):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        for _ in range(mb):
            f.write(os.urandom(1 << 20))


def tx_bytes(iface):
    with open(f"/sys/class/net/{iface}/statistics/tx_bytes") as f:
        return int(f.read())


def active_uplink():
    """Which of s1's core links is carrying the transfer right now?"""
    before = {k: tx_bytes(v) for k, v in UPLINKS.items()}
    time.sleep(0.5)
    delta = {k: tx_bytes(v) - before[k] for k, v in UPLINKS.items()}
    return max(delta, key=delta.get)


def run_once(net, size_mb, scenario, args):
    src = f"{SRC_DIR}/test_{size_mb}MB.bin"
    if not os.path.exists(src):
        make_file(src, size_mb)
    for _, d in SERVERS:
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    result_file = "/tmp/client_result.json"
    cmd = ["python3", f"{HERE}/client.py", "--servers", *SERVER_IPS, "--port", "5000",
           "--file", src, "--chunk-size", str(args.chunk_size),
           "--timeout", str(args.timeout), "--result", result_file]
    if scenario == "corrupt":
        cmd += ["--corrupt-chunks", "1,3,5"]
    h1 = net.get("h1")
    proc = h1.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    universal_newlines=True)
    t_fail, failed = None, None
    if scenario == "failure":
        time.sleep(args.fail_after)
        if proc.poll() is None:
            failed = active_uplink()
            t_fail = time.time()
            net.configLinkStatus("s1", failed, "down")
        else:
            print("  (transfer finished before failure injection - use a bigger file)")
    out, _ = proc.communicate(timeout=args.max_time)
    if failed:
        net.configLinkStatus("s1", failed, "up")
        time.sleep(3)
    results = json.load(open(result_file))

    ok = all(sha256_file(f"{d}/{os.path.basename(src)}") == sha256_file(src)
             if os.path.exists(f"{d}/{os.path.basename(src)}") else False
             for _, d in SERVERS)
    dur = max(r["duration_s"] for r in results)
    row = {
        "size_mb": size_mb, "scenario": scenario,
        "duration_s": round(dur, 3),
        "avg_throughput_mbps": round(statistics.mean(r["throughput_mbps"] for r in results), 3),
        "recovery_s": "", "max_ack_gap_s": "",
        "nacks": sum(r["nacks"] for r in results),
        "reconnects": sum(r["reconnects"] for r in results),
        "integrity_ok": ok, "failed_link": f"s1-{failed}" if failed else "",
    }
    gaps = []
    for r in results:
        t = [r["start"]] + r["ack_times"]
        gaps.append(max(b - a for a, b in zip(t, t[1:])) if len(t) > 1 else 0)
    row["max_ack_gap_s"] = round(max(gaps), 3)
    if t_fail:
        rec = []
        for r in results:
            after = [x for x in r["ack_times"] if x > t_fail]
            if after:
                rec.append(after[0] - t_fail)
        if rec:
            row["recovery_s"] = round(max(rec), 3)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[5, 20], help="file sizes in MB")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--scenarios", nargs="+", default=["normal", "failure", "corrupt"])
    ap.add_argument("--chunk-size", type=int, default=256 * 1024)
    ap.add_argument("--timeout", type=float, default=5)
    ap.add_argument("--fail-after", type=float, default=1.5, help="s before link is cut")
    ap.add_argument("--max-time", type=float, default=300)
    ap.add_argument("--bw", type=float, default=20)
    ap.add_argument("--out", default=f"{HERE}/results/results.csv")
    args = ap.parse_args()

    setLogLevel("warning")
    net = build_net(args.bw)
    net.start()
    if not prepare(net):
        print("Connectivity check failed - is the Ryu controller running?")
        net.stop()
        return
    subprocess.call(["pkill", "-f", "server.py"])
    for h, d in SERVERS:
        net.get(h).cmd(f"python3 {HERE}/server.py --port 5000 --dir {d} "
                       f"> /tmp/server_{h}.log 2>&1 &")
    time.sleep(1)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    rows = []
    for size in args.sizes:
        for sc in args.scenarios:
            for rep in range(1, args.reps + 1):
                row = run_once(net, size, sc, args)
                row["rep"] = rep
                rows.append(row)
                print(row)
    subprocess.call(["pkill", "-f", "server.py"])
    net.stop()
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("saved", args.out)


if __name__ == "__main__":
    main()
