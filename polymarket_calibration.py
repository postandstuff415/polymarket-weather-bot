"""
polymarket_calibration.py - learn the NWS forecast bias for Polymarket's markets (read-only).

    python polymarket_calibration.py      recalibrate now and print the result

Same method as weather_calibration.py (Kalshi), on Polymarket's own data, since Polymarket
settles on its own stations and brackets:

  residual = actual high (midpoint of the winning Polymarket bracket) - NWS forecast

using only snapshots in polymarket_model_log.csv taken BEFORE the climate day started (no
observed max yet: the next-day forecasts the bot actually trades on). Each settled city-day
counts once, and the bias is shrunk toward 0 while data is scarce (weather_calibration's
PRIOR_CITY_DAYS). City-days won by an open-ended bracket have no midpoint and are skipped.

polymarket_weather_bot.py runs this at startup and every hour, and uses the result.
Results are cached in polymarket_calibration.json, so settled events are fetched once.
"""

import csv
import json
import os
import re
import statistics
from datetime import date, datetime, timezone

import weather_calibration as wcal

CALIBRATION_JSON = "polymarket_calibration.json"
SNAPSHOT_CSV = "polymarket_model_log.csv"
MARKET_EVENT = re.compile(r"^tc-(temp-[a-z]+high-(\d{4})-(\d{2})-(\d{2}))-")   # market slug -> event slug, date


def load():
    """Last saved calibration, or {} if there is none."""
    try:
        with open(CALIBRATION_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def next_day_forecasts(path=SNAPSHOT_CSV):
    """event -> [NWS forecast max, ...] from snapshots taken before the event's climate day began."""
    out = {}
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("obs_max_f") or not r.get("future_max_f"):
                continue   # an observed max means the climate day had already started
            m = MARKET_EVENT.match(r.get("market", ""))
            if m:
                out.setdefault(m.group(1), set()).add((r["time_utc"], float(r["future_max_f"])))
    # one value per snapshot time (each hourly snapshot logs every bracket of the event)
    return {e: [v for _, v in sorted(s)] for e, s in out.items()}


def event_day(event):
    return date.fromisoformat(event[-10:])


def calibrate(winning_bracket, path=SNAPSHOT_CSV):
    """Recompute the bias, save it, and return the calibration dict.
    winning_bracket(event) -> (low_f, high_f) of the bracket that settled YES (None = open end),
    or None while the event is unsettled."""
    cal = load()
    actuals = cal.get("actuals", {})
    forecasts = next_day_forecasts(path)
    today = datetime.now(timezone.utc).date()
    for event in sorted(forecasts):
        if event in actuals or event_day(event) >= today:
            continue  # cached, or can't have settled yet (markets settle the morning after)
        try:
            won = winning_bracket(event)
        except Exception:
            continue
        if won is not None:
            lo, hi = won
            actuals[event] = (lo + hi) / 2.0 if lo is not None and hi is not None else "tail"

    residuals = {e: actuals[e] - statistics.mean(forecasts[e])
                 for e in forecasts if isinstance(actuals.get(e), float)}
    cal = wcal._stats(list(residuals.values()))
    cal.update({
        "tail_city_days_skipped": sum(1 for e in forecasts if actuals.get(e) == "tail"),
        "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "residuals": {e: round(r, 2) for e, r in sorted(residuals.items())},
        "actuals": actuals,
    })
    tmp = CALIBRATION_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cal, f, indent=1)
    os.replace(tmp, CALIBRATION_JSON)
    return cal


describe = wcal.describe


if __name__ == "__main__":
    import polymarket_weather_bot
    print("highs:", describe(calibrate(polymarket_weather_bot.winning_bracket)))
