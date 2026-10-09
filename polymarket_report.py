"""
polymarket_report.py - Is the Polymarket weather bot's edge real? (read-only)

    python polymarket_report.py

Reads polymarket_paper_trades.csv and shows:
  * net profit after fees on settled paper trades
  * calibration: when the model said 70%, did those trades win ~70% of the time?
  * results by city and by side (YES / NO)
"""

import csv
import os
from collections import defaultdict

from polymarket_weather_bot import TRADES_CSV


def main():
    if not os.path.exists(TRADES_CSV):
        print("No paper trades yet. Leave polymarket_weather_bot.py running for a day or two.")
        return
    with open(TRADES_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    settled = [r for r in rows if r.get("status") == "settled"]
    open_rows = [r for r in rows if r.get("status") == "open"]

    print("=" * 60)
    print("  POLYMARKET WEATHER BOT - PAPER RESULTS")
    print("=" * 60)
    print(f"  Trades recorded: {len(rows)}  |  settled: {len(settled)}  |  waiting: {len(open_rows)}")
    if not settled:
        print("  No paper trades have settled yet. Daily markets settle the morning after.")
        return

    pnl = [float(r["pnl"]) for r in settled]
    wins = sum(1 for p in pnl if p > 0)
    cost = sum(float(r["price"]) * float(r.get("count") or 1) for r in settled)
    exp = sum(float(r["edge"]) for r in settled)
    print(f"  Net profit after fees:  ${sum(pnl):+.2f}   (expected by model ${exp:+.2f})")
    print(f"  Win rate:               {wins}/{len(settled)} = {wins/len(settled):.0%}")
    if cost:
        print(f"  Return on money risked: {sum(pnl)/cost:+.1%}")
    print(f"  Average per trade:      {sum(pnl)/len(settled)*100:+.1f}¢")

    print("\n  Calibration (model's win probability vs what actually happened):")
    buckets = defaultdict(list)
    for r in settled:
        buckets[min(9, int(float(r["model_win_prob"]) * 10))].append(1 if float(r["pnl"]) > 0 else 0)
    for b in sorted(buckets):
        hits = buckets[b]
        print(f"    model {b*10:>3}-{b*10+10}%: won {sum(hits)}/{len(hits)} = {sum(hits)/len(hits):.0%}")

    for label, key in (("city", "city"), ("side", "side")):
        groups = defaultdict(list)
        for r in settled:
            groups[r.get(key, "?")].append(float(r["pnl"]))
        print(f"\n  By {label}:")
        for k, v in sorted(groups.items(), key=lambda kv: sum(kv[1])):
            print(f"    {k:<28} {len(v):>4} trades   ${sum(v):+8.2f}")


if __name__ == "__main__":
    main()
