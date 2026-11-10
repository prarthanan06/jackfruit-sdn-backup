import csv
import matplotlib.pyplot as plt

with open("results/d2-comparison.csv", newline="") as f:
    rows = list(csv.DictReader(f))

modes = [row["mode"] for row in rows]
transfer_times = [
    float(row["transfer_time_seconds"])
    for row in rows
]
recovery_times = [
    float(row["recovery_time_seconds"])
    for row in rows
]

x = range(len(modes))
width = 0.35

plt.figure(figsize=(8, 5))
plt.bar(
    [i - width / 2 for i in x],
    transfer_times,
    width,
    label="Transfer time (seconds)",
)
plt.bar(
    [i + width / 2 for i in x],
    recovery_times,
    width,
    label="Recovery time (seconds)",
)

plt.xticks(list(x), modes)
plt.ylabel("Seconds")
plt.title("Baseline vs SDN Rerouting")
plt.legend()
plt.grid(axis="y", alpha=0.3)
plt.tight_layout()
plt.savefig("results/d2-comparison.png", dpi=160)
