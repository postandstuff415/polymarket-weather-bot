"""
scaler.py - grow (and shrink) risk ONLY when real results prove the edge.

No dollar target. Size follows evidence:
  * Step UP one level after enough days at the current level, with profit that is
    statistically meaningful (daily P&L t-stat >= 2) and mostly-green days.
  * Step DOWN one level, immediately, if the account falls a set amount below
    where it stood when the current level began.
  * Never risk more than a fixed share of the account, whatever the level.

Market maker: MarketMakerScaler (state survives restarts in mm_scaler_state.json).
Weather bot:  weather_bot.py uses daily_stats() from here for its go-live gate.
"""

import csv
import json
import math
import os
import time
from datetime import datetime, timezone, timedelta

PACIFIC = timezone(timedelta(hours=-8))  # fixed offset: day boundaries only need to be consistent


# ---- Statistics -------------------------------------------------------------
def daily_stats(daily_pnl):
    """daily_pnl: list of per-day profits. Returns (days, total, mean, t_stat, green_frac)."""
    n = len(daily_pnl)
    if n == 0:
        return 0, 0.0, 0.0, 0.0, 0.0
    total = sum(daily_pnl)
    mean = total / n
    green = sum(1 for x in daily_pnl if x > 0) / n
    if n < 2:
        return n, total, mean, 0.0, green
    var = sum((x - mean) ** 2 for x in daily_pnl) / (n - 1)
    sd = math.sqrt(var)
    t = (mean / (sd / math.sqrt(n))) if sd > 1e-12 else (99.0 if mean > 0 else 0.0)
    return n, total, mean, t, green


def day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, PACIFIC).strftime("%Y-%m-%d")


# ---- Market maker -----------------------------------------------------------
class MarketMakerScaler:
    # Multipliers on the base settings (Stage 2: 3 contracts, $450 max out).
    # 3 -> 5 -> 7 -> 10 contracts. Each step ~1.5x; never a jump like 3x -> 10x.
    LEVELS = [1.0, 1.5, 2.25, 3.4]

    MIN_DAYS_AT_LEVEL = 7          # full days of results before a step up is considered
    MIN_T_STAT = 2.0               # daily profits must be clearly above zero, not luck
    MIN_GREEN_DAY_FRACTION = 0.6   # at least 60% of days profitable
    STEP_DOWN_LOSS_FRACTION = 0.10 # drop a level if down 10% of that level's exposure cap
    MAX_EXPOSURE_FRACTION = 0.30   # never more than 30% of the account out at once
    LOG_EVERY_SECONDS = 3600       # one equity snapshot per hour

    def __init__(self, state_file="mm_scaler_state.json", equity_log="equity_log.csv", enabled=True):
        self.state_file = state_file
        self.equity_log = equity_log
        self.enabled = enabled
        self.state = self._load()
        self._last_log = 0.0

    # -- persistence --
    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self):
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.state_file)

    def _append_equity(self, ts, equity):
        new = not os.path.exists(self.equity_log)
        with open(self.equity_log, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp", "time_pacific", "equity", "level"])
            w.writerow([f"{ts:.0f}", datetime.fromtimestamp(ts, PACIFIC).strftime("%Y-%m-%d %H:%M"),
                        f"{equity:.2f}", self.state.get("level", 0)])

    def _daily_pnl_since(self, since_ts):
        """Change in end-of-day equity for each full day since since_ts."""
        if not os.path.exists(self.equity_log):
            return []
        last_by_day = {}
        with open(self.equity_log, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    ts, eq = float(row["timestamp"]), float(row["equity"])
                except (KeyError, ValueError):
                    continue
                if ts >= since_ts:
                    last_by_day[day_key(ts)] = eq
        days = sorted(last_by_day)
        today = day_key(time.time())
        closes = [last_by_day[d] for d in days if d != today]   # only completed days
        start = self.state.get("level_start_equity")
        if start is None or not closes:
            return []
        out, prev = [], start
        for c in closes:
            out.append(c - prev)
            prev = c
        return out

    # -- main entry --
    def update(self, equity: float, base_exposure: float):
        """
        Call every cycle with account value (cash + open positions at cost).
        Returns (multiplier, exposure_cap, status_line).
        """
        now = time.time()
        if not self.state:
            self.state = {"level": 0, "level_since": now, "level_start_equity": equity,
                          "history": [f"{day_key(now)} start at level 1 with ${equity:,.2f}"]}
            self._save()
        if now - self._last_log >= self.LOG_EVERY_SECONDS:
            self._append_equity(now, equity)
            self._last_log = now

        lvl = int(self.state.get("level", 0))
        if self.enabled:
            lvl = self._maybe_step_down(lvl, equity, base_exposure, now)
            lvl = self._maybe_step_up(lvl, equity, now)

        # Scaling off means base size, even if a saved state reached a higher level
        # earlier (it used to keep trading at that saved level's size).
        mult = self.LEVELS[lvl] if self.enabled else 1.0
        cap = min(base_exposure * mult, self.MAX_EXPOSURE_FRACTION * max(equity, 0.0))
        days_here = (now - self.state["level_since"]) / 86400.0
        n, total, _, t, green = daily_stats(self._daily_pnl_since(self.state["level_since"]))
        level_label = f"level {lvl+1}/{len(self.LEVELS)}" if self.enabled else "base size"
        status = (f"[*] Scaler: {level_label} ({mult:.2f}x) | {days_here:.1f} days here | "
                  f"{n} full days, net ${total:+,.2f}, t={t:.1f}, green {green:.0%}"
                  + ("" if self.enabled else " | SCALING OFF"))
        return mult, cap, status

    def _set_level(self, lvl, equity, now, why):
        self.state.update({"level": lvl, "level_since": now, "level_start_equity": equity})
        self.state.setdefault("history", []).append(f"{day_key(now)} -> level {lvl+1}: {why}")
        self._save()
        print(f"\n[!!] SCALER: {why}. Now level {lvl+1}/{len(self.LEVELS)} ({self.LEVELS[lvl]:.2f}x).\n")

    def _maybe_step_down(self, lvl, equity, base_exposure, now):
        if lvl == 0:
            return lvl
        limit = self.STEP_DOWN_LOSS_FRACTION * base_exposure * self.LEVELS[lvl]
        drop = self.state["level_start_equity"] - equity
        if drop >= limit:
            self._set_level(lvl - 1, equity, now, f"down ${drop:,.2f} at this level (limit ${limit:,.2f}), stepping DOWN")
            return lvl - 1
        return lvl

    def _maybe_step_up(self, lvl, equity, now):
        if lvl >= len(self.LEVELS) - 1:
            return lvl
        daily = self._daily_pnl_since(self.state["level_since"])
        n, total, _, t, green = daily_stats(daily)
        if (n >= self.MIN_DAYS_AT_LEVEL and total > 0 and t >= self.MIN_T_STAT
                and green >= self.MIN_GREEN_DAY_FRACTION):
            self._set_level(lvl + 1, equity, now,
                            f"{n} days, net ${total:+,.2f}, t={t:.1f}, {green:.0%} green days - stepping UP")
            return lvl + 1
        return lvl
