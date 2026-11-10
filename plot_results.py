#!/usr/bin/env python3
"""Turn results/results.csv into report graphs.  Usage: python3 plot_results.py"""
import os
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

here = os.path.dirname(os.path.abspath(__file__))
df = pd.read_csv(f"{here}/results/results.csv")

tp = df.pivot_table(index="size_mb", columns="scenario",
                    values="avg_throughput_mbps", aggfunc="mean")
ax = tp.plot(kind="bar", rot=0)
ax.set_xlabel("File size (MB)"); ax.set_ylabel("Avg throughput per server (Mbit/s)")
ax.set_title("Throughput: normal vs. link failure vs. corruption")
plt.tight_layout(); plt.savefig(f"{here}/results/throughput.png", dpi=150); plt.close()

fail = df[df.scenario == "failure"].dropna(subset=["recovery_s"])
if not fail.empty:
    rec = fail.groupby("size_mb")["recovery_s"].agg(["mean", "std"]).fillna(0)
    ax = rec["mean"].plot(kind="bar", yerr=rec["std"], rot=0, capsize=4)
    ax.set_xlabel("File size (MB)"); ax.set_ylabel("Recovery time (s)")
    ax.set_title("Recovery time after link failure")
    plt.tight_layout(); plt.savefig(f"{here}/results/recovery.png", dpi=150); plt.close()

print(df.groupby(["size_mb", "scenario"])[
    ["duration_s", "avg_throughput_mbps", "recovery_s", "nacks", "reconnects"]].mean())
print("graphs saved in results/")
