"""
weather_calibration.py - learn the NWS forecast bias from settled markets (read-only).

    python weather_calibration.py      recalibrate now and print the result

The bot's model centres each next-day high on the NWS official daily-max forecast plus
a bias (lows: the NWS forecast low plus a separate bias). This finds each bias from the
bot's own hourly snapshot logs:

  residual = actual high/low (from the winning Kalshi bracket) - NWS forecast

using only snapshots taken BEFORE the climate day started (the next-day forecasts the
bot actually trades on). Each settled city-day counts once. The estimate is shrunk
toward 0 while data is scarce: bias = sum(residuals) / (city-days + PRIOR_CITY_DAYS).

weather_bot.py runs this at startup and every few hours, and uses the result.
Results are cached in weather_calibration.json, so settled events are fetched once.
"""

import csv
import json
import os
import statistics
from datetime import datetime, timezone

import weather_model as wm

CALIBRATION_JSON = "weather_calibration.json"
SNAPSHOT_FILES = ["weather_model_log_v3.csv", "weather_model_log_v4.csv"]  # v3 next-day rows are raw NWS too
PRIOR_CITY_DAYS = 10     # shrink toward 0: with 10 city-days of data the estimate counts half
MAX_BIAS_F = 3.0         # a larger "bias" means bad data, not a forecast problem


def _parse(raw):
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def load():
    """Last saved calibration, or {} if there is none."""
    try:
        with open(CALIBRATION_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def next_day_forecasts(files):
    """event -> [NWS forecast max, ...] from snapshots taken before the event's climate day began."""
    out = {}
    for path in files:
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("obs_max_f") or not r.get("future_max_f"):
                    continue
                event = r["ticker"].rsplit("-", 1)[0]
                city = wm.city_for(event.split("-")[0])
                day = wm.event_date(event)
                t = _parse(r.get("time_utc"))
                if city is None or day is None or t is None:
                    continue
                if t >= wm.climate_day_window_utc(day, city[1])[0]:
                    continue
                out.setdefault(event, set()).add((r["time_utc"], float(r["future_max_f"])))
    # one value per snapshot time (each hourly snapshot logs every bracket of the event)
    return {e: [v for _, v in sorted(s)] for e, s in out.items()}


def settled_high(event, kalshi_get):
    """Actual high (or low, for KXLOW events) from the winning bracket: a number, 'tail' (open-ended bracket won), or None (not settled)."""
    ms = kalshi_get(f"/events/{event}", {"with_nested_markets": "true"})
    ms = (ms.get("event") or {}).get("markets") or ms.get("markets") or []
    win = [m for m in ms if str(m.get("result") or "").lower() == "yes"]
    if not win:
        return None
    m = win[0]
    if str(m.get("strike_type", "")).lower() != "between":
        return "tail"
    return (float(m["floor_strike"]) + float(m["cap_strike"])) / 2.0


def _stats(residuals):
    """Shrunk bias and summary for one kind (highs or lows)."""
    n = len(residuals)
    total = sum(residuals)
    return {
        "bias_f": round(max(-MAX_BIAS_F, min(MAX_BIAS_F, total / (n + PRIOR_CITY_DAYS))), 2),
        "city_days": n,
        "mean_residual_f": round(total / n, 2) if n else None,
        "residual_sd_f": round(statistics.stdev(residuals), 2) if n >= 2 else None,
    }


def calibrate(kalshi_get, files=SNAPSHOT_FILES):
    """Recompute the bias, save it, and return the calibration dict."""
    cal = load()
    actuals = cal.get("actuals", {})
    forecasts = next_day_forecasts(files)
    today = datetime.now(timezone.utc).date()
    for event in sorted(forecasts):
        day = wm.event_date(event)
        if event in actuals or day is None or day >= today:
            continue  # cached, or can't have settled yet (markets settle the morning after)
        try:
            high = settled_high(event, kalshi_get)
        except Exception:
            continue
        if high is not None:
            actuals[event] = high

    residuals = {e: actuals[e] - statistics.mean(forecasts[e])
                 for e in forecasts if isinstance(actuals.get(e), float)}
    cal = _stats([r for e, r in residuals.items() if not wm.is_low(e)])     # highs (top level)
    cal["low"] = _stats([r for e, r in residuals.items() if wm.is_low(e)])
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


def describe(cal):
    if not cal:
        return "no calibration yet; NWS forecast used as-is (bias +0.0°F)"
    if not cal.get("city_days"):
        return "no settled next-day forecasts yet; NWS forecast used as-is (bias +0.0°F)"
    sd = cal.get("residual_sd_f")
    return (f"bias {cal['bias_f']:+.1f}°F from {cal['city_days']} settled city-days "
            f"(raw mean {cal['mean_residual_f']:+.1f}°F" + (f", spread {sd:.1f}°F" if sd is not None else "") + ")")


if __name__ == "__main__":
    import weather_bot
    c = calibrate(weather_bot.kalshi_get)
    print("highs:", describe(c))
    print("lows: ", describe(c["low"]))
