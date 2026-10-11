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

Seeded from Kalshi: the Kalshi weather bot learns the same quantity (NWS next-day forecast vs
the settled high) for 24 cities, incl. these 5 stations. As of 2026-10-11 it had 40 settled
city-days with NWS running +0.52°F cold. That is used as a prior worth SEED_CITY_DAYS city-days:
    bias = (sum of own residuals + SEED_CITY_DAYS x SEED_BIAS_F) / (own city-days + SEED_CITY_DAYS)
and the seed counts toward the bot's calibration gate, so it trades from the start while its own
settled data takes over. Set SEED_CITY_DAYS = 0 to go back to own-data-only.

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
SEED_BIAS_F = 0.5        # Kalshi weather_calibration.json, 2026-10-11: 40 next-day city-days, raw mean +0.52°F
SEED_CITY_DAYS = 15      # the seed's weight, in settled city-days
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


def seeded_stats(residuals):
    """wcal._stats, but shrunk toward the Kalshi seed instead of 0; the seed counts as city-days."""
    cal = wcal._stats(residuals)
    n = len(residuals)
    bias = (sum(residuals) + SEED_CITY_DAYS * SEED_BIAS_F) / max(1, n + SEED_CITY_DAYS)
    cal.update(bias_f=round(max(-wcal.MAX_BIAS_F, min(wcal.MAX_BIAS_F, bias)), 2),
               city_days=n + SEED_CITY_DAYS, own_city_days=n, seed_bias_f=SEED_BIAS_F, seed_city_days=SEED_CITY_DAYS)
    return cal


def describe(cal):
    if not cal.get("seed_city_days"):
        return wcal.describe(cal)
    own = cal.get("own_city_days", 0)
    text = f"bias {cal['bias_f']:+.1f}°F = Kalshi seed {cal['seed_bias_f']:+.1f}°F (worth {cal['seed_city_days']} city-days)"
    if own:
        sd = cal.get("residual_sd_f")
        text += (f" + {own} own settled city-days (raw mean {cal['mean_residual_f']:+.1f}°F"
                 + (f", spread {sd:.1f}°F" if sd is not None else "") + ")")
    else:
        text += " + no own settled city-days yet"
    return text


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
    cal = seeded_stats(list(residuals.values()))
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



if __name__ == "__main__":
    import polymarket_weather_bot
    print("highs:", describe(calibrate(polymarket_weather_bot.winning_bracket)))
