"""
weather_model.py - the math behind weather_bot.py (no network calls here).

How the daily high is modeled
-----------------------------
The day's official high = max(high already observed today, high still to come).
Daily LOWS (LowModel) mirror this: min(low observed so far, low still to come).

  * Already observed: the max temperature reported by the settlement station since
    the climate day began. The true high is at least this (a little higher on
    average, because the station's official max is taken between hourly reports).
  * Still to come: the max of the NWS hourly forecast for the rest of the climate
    day, with forecast error that grows with lead time, shifted by FORECAST_BIAS_F
    (learned from settled markets by weather_calibration.py).

Treating the two parts as independent, P(high <= x) is the product of their CDFs.
Kalshi settles on the whole-degree reported high, so a bracket "65° to 66°"
wins when 64.5 <= true high < 66.5.

All settings here are deliberately simple starting guesses. The paper log exists to
check them: weather_report.py shows whether the model's probabilities are calibrated.
"""

import math
from datetime import datetime, timedelta, timezone

# ---- Model settings (calibrate these from the paper log) --------------------
OBS_BIAS_F = 0.3          # official max tends to be a bit above the max hourly reading
OBS_SIGMA_F = 0.8         # uncertainty of the observed part (rounding, C->F conversion)
FORECAST_BIAS_F = 0.0     # added ONCE to the forecast part; weather_bot.py sets it from
                          # weather_calibration.py (NWS forecast vs settled highs)
FORECAST_BIAS_LOW_F = 0.0 # same for daily lows (NWS forecast low vs settled lows)
FORECAST_SIGMA_BASE_F = 1.2
FORECAST_SIGMA_PER_HOUR_F = 0.05
FORECAST_SIGMA_MAX_F = 4.0
ERROR_DECAY_HOURS = 6.0   # how fast today's 'running hot/cold vs forecast' correction fades

# ---- Kalshi fees ------------------------------------------------------------
TAKER_FEE_RATE = 0.07

# City table: series ticker -> (settlement station, standard-time UTC offset, name)
# The climate day runs midnight-to-midnight LOCAL STANDARD TIME all year, so a
# fixed offset is exactly right (no daylight-saving handling needed).
CITIES = {
    "KXHIGHNY":    ("KNYC", -5, "New York (Central Park)"),
    "KXHIGHCHI":   ("KMDW", -6, "Chicago (Midway)"),
    "KXHIGHMIA":   ("KMIA", -5, "Miami"),
    "KXHIGHAUS":   ("KAUS", -6, "Austin"),
    "KXHIGHLAX":   ("KLAX", -8, "Los Angeles (LAX)"),
    "KXHIGHDEN":   ("KDEN", -7, "Denver"),
    "KXHIGHPHIL":  ("KPHL", -5, "Philadelphia"),
    "KXHIGHTPHX":  ("KPHX", -7, "Phoenix"),
    "KXHIGHTHOU":  ("KHOU", -6, "Houston (Hobby)"),
    "KXHIGHTMIN":  ("KMSP", -6, "Minneapolis"),
    "KXHIGHTOKC":  ("KOKC", -6, "Oklahoma City"),
    "KXHIGHTSFO":  ("KSFO", -8, "San Francisco (SFO)"),
    "KXHIGHTDC":   ("KDCA", -5, "Washington DC (National)"),
    "KXHIGHTBOS":  ("KBOS", -5, "Boston"),
    "KXHIGHTDAL":  ("KDFW", -6, "Dallas (DFW)"),
    "KXHIGHTSEA":  ("KSEA", -8, "Seattle"),
    "KXHIGHTLV":   ("KLAS", -8, "Las Vegas"),
    "KXHIGHTATL":  ("KATL", -5, "Atlanta"),
    "KXHIGHTSATX": ("KSAT", -6, "San Antonio"),
    "KXHIGHTNOLA": ("KMSY", -6, "New Orleans"),
    "KXHIGHTSAN":  ("KSAN", -8, "San Diego"),
    "KXHIGHTSDF":  ("KSDF", -5, "Louisville"),
    "KXHIGHTEWR":  ("KEWR", -5, "Newark"),
    "KXHIGHTTTN":  ("KTTN", -5, "Trenton"),
}

# Daily-LOW series -> the high series for the same city. Same station, same NWS climate
# report (e.g. CLINYC) and same climate day, per Kalshi's market rules.
LOW_SERIES = {
    "KXLOWTNYC": "KXHIGHNY", "KXLOWTCHI": "KXHIGHCHI", "KXLOWTMIA": "KXHIGHMIA", "KXLOWTAUS": "KXHIGHAUS",
    "KXLOWTLAX": "KXHIGHLAX", "KXLOWTDEN": "KXHIGHDEN", "KXLOWTPHIL": "KXHIGHPHIL", "KXLOWTPHX": "KXHIGHTPHX",
    "KXLOWTHOU": "KXHIGHTHOU", "KXLOWTMIN": "KXHIGHTMIN", "KXLOWTOKC": "KXHIGHTOKC", "KXLOWTSFO": "KXHIGHTSFO",
    "KXLOWTDC": "KXHIGHTDC", "KXLOWTBOS": "KXHIGHTBOS", "KXLOWTDAL": "KXHIGHTDAL", "KXLOWTSEA": "KXHIGHTSEA",
    "KXLOWTLV": "KXHIGHTLV", "KXLOWTATL": "KXHIGHTATL", "KXLOWTSATX": "KXHIGHTSATX", "KXLOWTNOLA": "KXHIGHTNOLA",
    "KXLOWTSAN": "KXHIGHTSAN", "KXLOWTSDF": "KXHIGHTSDF", "KXLOWTEWR": "KXHIGHTEWR", "KXLOWTTTN": "KXHIGHTTTN",
}


def city_for(series: str):
    """(station, offset, name) for a high OR low series, or None."""
    return CITIES.get(series) or CITIES.get(LOW_SERIES.get(series, ""))


MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


# ---- Dates ------------------------------------------------------------------
def event_date(event_ticker: str):
    """'KXHIGHNY-26OCT09' -> date(2026, 10, 9), or None."""
    try:
        part = event_ticker.split("-")[1]
        yy, mon, dd = int(part[:2]), part[2:5].upper(), int(part[5:7])
        return datetime(2000 + yy, MONTHS[mon], dd).date()
    except Exception:
        return None


def climate_day_window_utc(day, lst_offset_hours: int):
    """UTC start/end of the climate day (midnight to midnight local standard time)."""
    start_local = datetime(day.year, day.month, day.day, tzinfo=timezone(timedelta(hours=lst_offset_hours)))
    start = start_local.astimezone(timezone.utc)
    return start, start + timedelta(hours=24)


# ---- Probability ------------------------------------------------------------
def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def forecast_sigma(lead_hours: float) -> float:
    s = FORECAST_SIGMA_BASE_F + FORECAST_SIGMA_PER_HOUR_F * max(0.0, lead_hours)
    return min(FORECAST_SIGMA_MAX_F, s)


class HighModel:
    """Distribution of the day's true high temperature (°F)."""

    def __init__(self, obs_max_f=None, future_max_f=None, future_lead_hours=0.0):
        if obs_max_f is None and future_max_f is None:
            raise ValueError("need an observation or a forecast")
        self.obs_mu = None if obs_max_f is None else obs_max_f + OBS_BIAS_F
        self.obs_sigma = OBS_SIGMA_F
        self.fc_mu = None if future_max_f is None else future_max_f + FORECAST_BIAS_F
        self.fc_sigma = forecast_sigma(future_lead_hours)

    def cdf(self, x: float) -> float:
        p = 1.0
        if self.obs_mu is not None:
            p *= _phi((x - self.obs_mu) / self.obs_sigma)
        if self.fc_mu is not None:
            p *= _phi((x - self.fc_mu) / self.fc_sigma)
        return p

    def center(self) -> float:
        """Rough median, for display."""
        lo, hi = -60.0, 140.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if self.cdf(mid) < 0.5:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    def bracket_prob(self, strike_type: str, floor_strike, cap_strike, inclusive_tails=False):
        """
        P(market resolves YES). Kalshi strike fields:
          between: floor..cap inclusive, whole degrees      -> floor-0.5 <= T < cap+0.5
          greater: reported high > floor_strike               -> T >= floor+0.5
          less:    reported high < cap_strike                 -> T <  cap-0.5
        inclusive_tails=True reads the tails as ">= floor" / "<= cap" instead. The bot
        tries both and keeps whichever makes the event's brackets add up to 100%.
        Returns None if the fields don't make sense.
        """
        st = (strike_type or "").lower()
        shift = -1.0 if inclusive_tails else 0.0
        try:
            if st == "between":
                a, b = float(floor_strike), float(cap_strike)
                return max(0.0, self.cdf(b + 0.5) - self.cdf(a - 0.5))
            if st == "greater":
                return max(0.0, 1.0 - self.cdf(float(floor_strike) + 0.5 + shift))
            if st == "less":
                return max(0.0, self.cdf(float(cap_strike) - 0.5 - shift))
        except (TypeError, ValueError):
            return None
        return None


class LowModel(HighModel):
    """Distribution of the day's true LOW (°F): min(low observed so far, low still to come)."""

    def __init__(self, obs_min_f=None, future_min_f=None, future_lead_hours=0.0):
        if obs_min_f is None and future_min_f is None:
            raise ValueError("need an observation or a forecast")
        # official min tends to be a bit BELOW the min hourly reading
        self.obs_mu = None if obs_min_f is None else obs_min_f - OBS_BIAS_F
        self.obs_sigma = OBS_SIGMA_F
        self.fc_mu = None if future_min_f is None else future_min_f + FORECAST_BIAS_LOW_F
        self.fc_sigma = forecast_sigma(future_lead_hours)

    def cdf(self, x: float) -> float:
        # P(low <= x) = 1 - P(both parts stay above x)
        above = 1.0
        if self.obs_mu is not None:
            above *= 1.0 - _phi((x - self.obs_mu) / self.obs_sigma)
        if self.fc_mu is not None:
            above *= 1.0 - _phi((x - self.fc_mu) / self.fc_sigma)
        return 1.0 - above


def is_low(ticker: str) -> bool:
    """True for daily-LOW markets/events (KXLOW...)."""
    return str(ticker).startswith("KXLOW")


# ---- Fees and edge ----------------------------------------------------------
def taker_fee(price: float, count: int = 1) -> float:
    """Kalshi taker fee: 7% x C x P x (1-P), rounded up to the cent."""
    raw = TAKER_FEE_RATE * count * price * (1.0 - price)
    return math.ceil(raw * 100 - 1e-9) / 100.0


def best_trade(p_yes: float, yes_bid: float, yes_ask: float):
    """
    Best of: buy YES at the ask, or buy NO (= sell YES at the bid).
    Returns (side, cost_per_contract, fee, win_prob, edge) or None.
    Edge = expected profit per contract after the fee.
    """
    options = []
    if 0.01 <= yes_ask <= 0.99:
        fee = taker_fee(yes_ask)
        options.append(("YES", yes_ask, fee, p_yes, p_yes - yes_ask - fee))
    if 0.01 <= yes_bid <= 0.99:
        cost = round(1.0 - yes_bid, 4)
        fee = taker_fee(yes_bid)
        options.append(("NO", cost, fee, 1.0 - p_yes, (1.0 - p_yes) - cost - fee))
    if not options:
        return None
    return max(options, key=lambda o: o[4])
