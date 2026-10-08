"""
weather_bot.py - Kalshi daily-high temperature bot, built to PROVE an edge before
risking money.

    python weather_bot.py            paper mode (default): places NO orders
    python weather_bot.py --once     one scan, print the table, exit
    python weather_report.py         how the paper trades are doing

What it does every few minutes
------------------------------
1. Finds open Kalshi daily-high markets (KXHIGH...) for the next ~36 hours.
2. For each city: reads the settlement station's observations so far today and the
   National Weather Service hourly forecast, and turns them into a probability for
   every temperature bracket (see weather_model.py).
3. Compares that probability with the market price. If buying YES or NO is worth at
   least MIN_EDGE after Kalshi's fee, it records a PAPER trade (price, fee, model
   probability) in weather_paper_trades.csv. One trade per market, ever.
4. When markets settle, it fills in each paper trade's real result and profit.

It runs alongside autopilot.py. autopilot.py now ignores all KXHIGH/KXLOW markets,
so the two bots never touch each other's markets or orders.

Going live
----------
With AUTO_GO_LIVE on, the bot switches itself to real money ONLY when the paper
record proves an edge: 100+ settled trades over 10+ different days, net profit after
fees, and daily profits clearly above zero (t-stat >= 2). It then starts at 1 contract
($100 max open) and grows a level per 50 settled live trades that stay profitable,
steps back down if the last 30 live trades lose, and returns to paper for good if live
trading ever loses LIVE_KILL_LOSS in total.
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import weather_model as wm
from scaler import daily_stats

# Windows: keep ¢ / °F prints from raising UnicodeEncodeError when output is redirected
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# =============================================================================
# SETTINGS
# =============================================================================
SCAN_INTERVAL_SECONDS = 300      # one full scan every 5 minutes
MIN_EDGE = 0.07                  # need 7¢+ expected profit per contract after fees
MAX_HOURS_TO_CLOSE = 36          # only markets settling within ~1.5 days
MIN_MINUTES_TO_CLOSE = 15        # skip markets about to close
MIN_PRICE, MAX_PRICE = 0.03, 0.97  # skip prices where 1¢ ticks dominate
SUM_TOLERANCE = 0.04             # an event's brackets must add to ~100% or it's skipped
SNAPSHOT_EVERY_SECONDS = 3600    # log every bracket's model vs market once an hour
MAX_CENTER_GAP_F = 2.0           # model and market more than 2°F apart = likely bad data, skip
MAX_EDGE = 0.30                  # an "edge" above 30¢ is almost always a data problem, not an opportunity
TRADE_SAME_DAY = False           # same-day markets are priced off live readings we can't beat; log only

# Live trading. ON: the bot switches to REAL MONEY by itself once the paper record
# passes the gate below. Set AUTO_GO_LIVE = False to stay on paper no matter what.
AUTO_GO_LIVE = True              # go live automatically once the paper record earns it
LIVE_MIN_SETTLED = 100           # settled paper trades required...
LIVE_MIN_DAYS = 10               # ...spread over at least this many different days
LIVE_MIN_T = 2.0                 # daily profits must be clearly above zero, not luck
LIVE_LEVELS = [(1, 100.0), (2, 200.0), (3, 300.0), (5, 500.0)]  # (contracts/trade, max $ open)
LIVE_TRADES_PER_LEVEL = 50       # settled profitable live trades needed per level up
LIVE_STEP_DOWN_WINDOW = 30       # if the last 30 live trades lose money, drop a level
LIVE_KILL_LOSS = 50.00           # live trading down this much in total -> back to paper for good
LIVE_ALLOW_NO = False            # NO trades = selling YES you don't own; off until tested
# =============================================================================

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
NWS = "https://api.weather.gov"
NWS_HEADERS = {"User-Agent": "(kalshi-weather-bot, postandstuff415@gmail.com)",
               "Accept": "application/geo+json"}
TRADES_CSV = "weather_paper_trades_v3.csv"   # earlier model versions are kept in _v1/_v2 files
SNAPSHOT_CSV = "weather_model_log_v3.csv"
TRADE_FIELDS = ["time_utc", "mode", "ticker", "event", "city", "side", "price", "fee", "count",
                "model_win_prob", "edge", "yes_bid", "yes_ask", "model_center_f", "obs_max_f",
                "future_max_f", "status", "result", "pnl", "order_id"]
SNAP_FIELDS = ["time_utc", "ticker", "model_p_yes", "yes_bid", "yes_ask", "model_center_f", "obs_max_f", "future_max_f"]

_auth = None


def auth():
    global _auth
    if _auth is None:
        from auth import KalshiAuth
        _auth = KalshiAuth()
    return _auth


def now_utc():
    return datetime.now(timezone.utc)


def parse_iso(raw):
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def money(obj, key):
    """Kalshi price field in dollars: '<key>_dollars' or '<key>' (cents)."""
    v = obj.get(f"{key}_dollars")
    if v not in (None, ""):
        return float(v)
    v = obj.get(key)
    if v not in (None, ""):
        v = float(v)
        return v / 100.0 if v > 1.0 else v
    return 0.0


# ---- Kalshi -----------------------------------------------------------------
def kalshi_get(path, params=None):
    full = "/trade-api/v2" + path
    res = requests.get(KALSHI + path, headers=auth().get_headers("GET", full), params=params, timeout=15)
    if res.status_code != 200:
        raise RuntimeError(f"Kalshi GET {path} -> {res.status_code}: {res.text[:200]}")
    return res.json()


def open_markets(series):
    out, cursor = [], None
    for _ in range(10):
        params = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = kalshi_get("/markets", params)
        out.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


# ---- NWS (cached) -------------------------------------------------------------
_station_cache = {}   # station -> (lat, lon)
_urls = {}            # station -> (forecastHourly URL, forecastGridData URL)
_fc_cache = {}        # station -> (fetched_at, hourly periods)
_grid_cache = {}      # station -> (fetched_at, [(start, end, max_f)])
_obs_cache = {}       # (station, window_start) -> (fetched_at, (max_f, latest_f, latest_time))


def nws_get(url, params=None):
    res = requests.get(url, headers=NWS_HEADERS, params=params, timeout=15)
    if res.status_code != 200:
        raise RuntimeError(f"NWS {url} -> {res.status_code}")
    return res.json()


def _c_to_f(v, unit):
    v = float(v)
    return v if "degF" in str(unit) else v * 9 / 5 + 32


def station_urls(station):
    if station not in _urls:
        if station not in _station_cache:
            lon, lat = nws_get(f"{NWS}/stations/{station}")["geometry"]["coordinates"][:2]
            _station_cache[station] = (round(lat, 4), round(lon, 4))
        lat, lon = _station_cache[station]
        props = nws_get(f"{NWS}/points/{lat},{lon}")["properties"]
        _urls[station] = (props["forecastHourly"], props["forecastGridData"])
    return _urls[station]


def hourly_forecast(station):
    hit = _fc_cache.get(station)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    periods = nws_get(station_urls(station)[0])["properties"]["periods"]
    _fc_cache[station] = (time.time(), periods)
    return periods


def grid_daily_max(station):
    """NWS official daily max-temperature forecasts: [(valid_start, valid_end, max_f)]."""
    hit = _grid_cache.get(station)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    mt = nws_get(station_urls(station)[1])["properties"].get("maxTemperature") or {}
    unit = mt.get("uom", "wmoUnit:degC")
    out = []
    for v in mt.get("values", []):
        if v.get("value") is None:
            continue
        t0_raw, _, dur = str(v.get("validTime", "")).partition("/")
        t0 = parse_iso(t0_raw)
        if t0 is None:
            continue
        hours = 12
        if dur.startswith("PT") and dur.endswith("H"):
            try:
                hours = int(dur[2:-1])
            except ValueError:
                pass
        out.append((t0, t0 + timedelta(hours=hours), _c_to_f(v["value"], unit)))
    _grid_cache[station] = (time.time(), out)
    return out


def observations(station, start, end):
    """(max temp so far, latest temp, latest time) for the climate day, from the station itself."""
    key = (station, start.isoformat())
    hit = _obs_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    data = nws_get(f"{NWS}/stations/{station}/observations",
                   {"start": start.isoformat().replace("+00:00", "Z"),
                    "end": min(end, now_utc()).isoformat().replace("+00:00", "Z")})
    best = latest = latest_t = None
    for f in data.get("features", []):
        props = f.get("properties") or {}
        t = props.get("temperature") or {}
        ts = parse_iso(props.get("timestamp"))
        if t.get("value") is None or t.get("qualityControl") == "X" or ts is None or ts < start:
            continue
        val = _c_to_f(t["value"], t.get("unitCode", ""))
        best = val if best is None else max(best, val)
        if latest_t is None or ts > latest_t:
            latest, latest_t = val, ts
    result = (best, latest, latest_t)
    _obs_cache[key] = (time.time(), result)
    return result


def _period_temp(p):
    return _c_to_f(p.get("temperature"), "degC" if str(p.get("temperatureUnit", "F")).upper() == "C" else "degF")


def forecast_error_now(periods, latest, latest_t):
    """How far the station is running above (+) or below (-) the hourly forecast right now."""
    if latest is None or latest_t is None or now_utc() - latest_t > timedelta(hours=2):
        return 0.0
    for p in periods:
        t0 = parse_iso(p.get("startTime"))
        if t0 is not None and t0 <= latest_t < t0 + timedelta(hours=1):
            return latest - _period_temp(p)
    first = periods[0] if periods else None
    t0 = parse_iso(first.get("startTime")) if first else None
    if t0 is not None and abs((t0 - latest_t).total_seconds()) <= 3600:
        return latest - _period_temp(first)
    return 0.0


def build_model(station, offset, day):
    """
    Model inputs for one city-day:
      obs     max temperature already observed today at the station
      fut     forecast max for the rest of the day, corrected for how far the
              station is currently running from the forecast (that error fades
              over ~6 hours), and using the NWS official daily-max forecast
    """
    start, end = wm.climate_day_window_utc(day, offset)
    now = now_utc()
    if now >= end:
        return None, None, None
    peak = start + timedelta(hours=15)                 # ~3 PM local standard time
    lead = max(0.0, (peak - now).total_seconds() / 3600.0)

    periods = hourly_forecast(station)
    obs = latest = latest_t = None
    if now > start:
        obs, latest, latest_t = observations(station, start, end)
    err = forecast_error_now(periods, latest, latest_t)

    # Hourly path: remaining hours of the climate day, error-corrected
    hourly_max = None
    for p in periods:
        t0 = parse_iso(p.get("startTime"))
        if t0 is None or t0 < start or t0 >= end or t0 + timedelta(hours=1) <= now:
            continue
        h_ahead = max(0.0, (t0 - now).total_seconds() / 3600.0)
        temp = _period_temp(p) + err * wm.math.exp(-h_ahead / wm.ERROR_DECAY_HOURS)
        hourly_max = temp if hourly_max is None else max(hourly_max, temp)
    if hourly_max is not None:
        hourly_max += wm.FORECAST_BIAS_F

    # Official daily max for this date (its daytime window must still be ahead of us)
    grid_max = None
    for t0, t1, v in grid_daily_max(station):
        if start <= t0 < end and now < t1 - timedelta(hours=3):
            grid_max = v + err * wm.math.exp(-lead / wm.ERROR_DECAY_HOURS)
            break

    if grid_max is not None and hourly_max is not None:
        fut = grid_max if now < start else (grid_max + hourly_max) / 2.0
    else:
        fut = grid_max if grid_max is not None else hourly_max
    if obs is None and fut is None:
        return None, None, None
    return wm.HighModel(obs, fut, lead), obs, fut


def market_center_f(ms):
    """Temperature the market is centred on (price-weighted bracket midpoints)."""
    num = den = 0.0
    for m in ms:
        mid = (money(m, "yes_bid") + money(m, "yes_ask")) / 2.0
        st = str(m.get("strike_type", "")).lower()
        try:
            if st == "between":
                x = (float(m["floor_strike"]) + float(m["cap_strike"])) / 2.0
            elif st == "greater":
                x = float(m["floor_strike"]) + 2.0
            elif st == "less":
                x = float(m["cap_strike"]) - 2.0
            else:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        num += mid * x
        den += mid
    return num / den if den > 0 else None


# ---- Trade log ----------------------------------------------------------------
_unsaved_live = {}   # ticker -> live trade row placed but not yet written to TRADES_CSV


def load_trades():
    rows = []
    if os.path.exists(TRADES_CSV):
        with open(TRADES_CSV, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    # Real orders whose save failed are kept in memory so they're never placed twice
    on_file = {r.get("ticker") for r in rows}
    rows.extend(r for t, r in _unsaved_live.items() if t not in on_file)
    return rows


def save_trades(rows):
    tmp = TRADES_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in TRADE_FIELDS})
    os.replace(tmp, TRADES_CSV)
    saved = {r.get("ticker") for r in rows}
    for t in [t for t in _unsaved_live if t in saved]:
        del _unsaved_live[t]


def append_snapshots(rows):
    new = not os.path.exists(SNAPSHOT_CSV)
    with open(SNAPSHOT_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SNAP_FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def settle_trades(rows):
    """Fill in results for trades whose markets have settled."""
    changed = 0
    for r in rows:
        if r.get("status") != "open":
            continue
        try:
            m = kalshi_get(f"/markets/{r['ticker']}").get("market", {})
        except Exception:
            # Old markets move to the historical database; without this fallback their
            # trades stayed "open" forever (and kept counting against the live cap).
            try:
                m = kalshi_get(f"/historical/markets/{r['ticker']}").get("market", {})
            except Exception:
                continue
        result = str(m.get("result") or "").lower()
        if result not in ("yes", "no"):
            continue
        won = (result == "yes") == (r["side"] == "YES")
        price, fee, count = float(r["price"]), float(r["fee"]), float(r.get("count") or 1)
        r["result"] = result
        r["pnl"] = f"{((1.0 - price) if won else -price) * count - fee:.4f}"
        r["status"] = "settled"
        changed += 1
    return changed


def paper_summary(rows):
    settled = [r for r in rows if r.get("status") == "settled" and r.get("mode") == "paper"]
    net = sum(float(r["pnl"]) for r in settled)
    return len(settled), net


def _daily(rows):
    """Profit per trading day (the market's date), for the significance test."""
    by_day = {}
    for r in rows:
        day = str(r.get("event", "")).split("-")[1] if "-" in str(r.get("event", "")) else r.get("time_utc", "")[:10]
        by_day[day] = by_day.get(day, 0.0) + float(r["pnl"])
    return [by_day[d] for d in sorted(by_day)]


# ---- Live (gated, automatic) ---------------------------------------------------
def live_allowed(rows):
    """Returns (allowed, reason, (contracts, max_open_cost))."""
    sides = {"YES", "NO"} if LIVE_ALLOW_NO else {"YES"}
    if not AUTO_GO_LIVE:
        return False, "AUTO_GO_LIVE is off", None

    live = [r for r in rows if r.get("mode") == "live" and r.get("status") == "settled"]
    live_net = sum(float(r["pnl"]) for r in live)
    if live_net <= -LIVE_KILL_LOSS:
        return False, f"KILL SWITCH: live trading lost ${-live_net:.2f} (limit ${LIVE_KILL_LOSS:.2f}); paper only", None

    # Gate: only the kinds of trades that will actually be placed live count
    paper = [r for r in rows if r.get("mode") == "paper" and r.get("status") == "settled" and r.get("side") in sides]
    n_days, total, _, t, _ = daily_stats(_daily(paper))
    if len(paper) < LIVE_MIN_SETTLED:
        return False, f"{len(paper)}/{LIVE_MIN_SETTLED} settled paper trades", None
    if n_days < LIVE_MIN_DAYS:
        return False, f"paper record covers {n_days}/{LIVE_MIN_DAYS} days", None
    if total <= 0 or t < LIVE_MIN_T:
        return False, f"paper not proven yet (net ${total:+.2f}, t={t:.1f}, need >0 and t>={LIVE_MIN_T})", None

    # Size level from the live record
    level = 0
    if live:
        _, l_total, _, l_t, _ = daily_stats(_daily(live))
        if l_total > 0 and l_t >= LIVE_MIN_T:
            level = min(len(LIVE_LEVELS) - 1, len(live) // LIVE_TRADES_PER_LEVEL)
        recent = live[-LIVE_STEP_DOWN_WINDOW:]
        if len(recent) >= LIVE_STEP_DOWN_WINDOW and sum(float(r["pnl"]) for r in recent) < 0:
            level = max(0, level - 1)
    contracts, cap = LIVE_LEVELS[level]
    return True, (f"LIVE level {level+1}/{len(LIVE_LEVELS)}: {contracts} contract(s), ${cap:.0f} max open | "
                  f"paper {len(paper)} trades net ${total:+.2f} t={t:.1f} | live {len(live)} settled net ${live_net:+.2f}"), (contracts, cap)


def place_live(trade, rows, size):
    """Send a real order. Returns (order_id, filled_count) or None."""
    from strategy.order_manager import OrderManager
    contracts, cap = size
    if trade["side"] == "NO" and not LIVE_ALLOW_NO:
        return None
    open_cost = sum(float(r["price"]) * float(r.get("count") or 1)
                    for r in rows if r.get("mode") == "live" and r.get("status") == "open")
    if open_cost + float(trade["price"]) * contracts > cap:
        print(f"    [live] skipped: ${open_cost:.2f} already open (cap ${cap:.2f})")
        return None
    om = OrderManager(auth_client=auth(), live=True)
    if trade["side"] == "YES":
        order = {"ticker": trade["ticker"], "side": "bid", "price": float(trade["yes_ask"]),
                 "count": contracts, "time_in_force": "immediate_or_cancel"}
    else:
        order = {"ticker": trade["ticker"], "side": "ask", "price": float(trade["yes_bid"]),
                 "count": contracts, "time_in_force": "immediate_or_cancel"}
    res = om.dispatch_order(order)
    if not res:
        return None
    o = res.get("order", res)
    filled = o.get("fill_count_fp", o.get("fill_count"))
    try:
        filled = float(filled) if filled not in (None, "") else float(contracts)
    except (TypeError, ValueError):
        filled = float(contracts)
    if filled <= 0:
        print(f"    [live] {trade['ticker']}: order not filled (price moved); kept as paper")
        return None
    return o.get("order_id") or res.get("order_id"), filled


# ---- One scan -------------------------------------------------------------------
def scan(rows, live_ok, take_snapshot, verbose=False, live_size=None):
    traded = {r["ticker"] for r in rows}
    signals, snapshots, problems = [], [], []
    now = now_utc()

    for i, (series, (station, offset, city)) in enumerate(wm.CITIES.items(), 1):
        if verbose:
            print(f"  [{i:>2}/{len(wm.CITIES)}] {city:<26}", end=" ", flush=True)
        try:
            markets = open_markets(series)
        except Exception as e:
            problems.append(f"{series}: {e}")
            if verbose:
                print("Kalshi error", flush=True)
            continue
        if verbose:
            print(f"{len(markets)} open markets", flush=True)
        events = {}
        for m in markets:
            events.setdefault(m.get("event_ticker", ""), []).append(m)

        for event_ticker, ms in events.items():
            day = wm.event_date(event_ticker)
            close = parse_iso(ms[0].get("close_time"))
            if day is None or close is None:
                continue
            hours_left = (close - now).total_seconds() / 3600.0
            if hours_left > MAX_HOURS_TO_CLOSE or hours_left * 60 < MIN_MINUTES_TO_CLOSE:
                continue
            try:
                model, obs, fut = build_model(station, offset, day)
            except Exception as e:
                problems.append(f"{city}: weather data unavailable ({e})")
                continue
            if model is None:
                continue

            # Read the tail brackets both ways; keep the reading where the brackets tile to 100%
            candidates = []
            for incl in (False, True):
                pr = {m["ticker"]: model.bracket_prob(m.get("strike_type"), m.get("floor_strike"),
                                                      m.get("cap_strike"), inclusive_tails=incl) for m in ms}
                if all(p is not None for p in pr.values()):
                    candidates.append(pr)
            if not candidates:
                problems.append(f"{event_ticker}: unreadable bracket definitions, skipped")
                continue
            probs = min(candidates, key=lambda pr: abs(sum(pr.values()) - 1.0))
            total = sum(probs.values())
            if abs(total - 1.0) > SUM_TOLERANCE:
                problems.append(f"{event_ticker}: brackets add to {total:.0%}, not 100% - skipped (event may be incomplete)")
                continue
            mkt_c = market_center_f(ms)
            gap_ok = mkt_c is None or abs(model.center() - mkt_c) <= MAX_CENTER_GAP_F
            if not gap_ok:
                problems.append(f"{event_ticker}: model {model.center():.1f}°F vs market {mkt_c:.1f}°F - too far apart, no trades")
            day_start, _ = wm.climate_day_window_utc(day, offset)
            tradable_day = TRADE_SAME_DAY or now < day_start

            for m in ms:
                tkr, p = m["ticker"], probs[m["ticker"]]
                bid, ask = money(m, "yes_bid"), money(m, "yes_ask")
                if take_snapshot:
                    snapshots.append({"time_utc": now.isoformat(), "ticker": tkr, "model_p_yes": f"{p:.4f}",
                                      "yes_bid": bid, "yes_ask": ask, "model_center_f": f"{model.center():.1f}",
                                      "obs_max_f": "" if obs is None else f"{obs:.1f}",
                                      "future_max_f": "" if fut is None else f"{fut:.1f}"})
                if tkr in traded or not gap_ok or not tradable_day:
                    continue
                best = wm.best_trade(p, bid, ask)
                if not best:
                    continue
                side, cost, fee, win_p, edge = best
                trade_price = ask if side == "YES" else bid
                if edge < MIN_EDGE or edge > MAX_EDGE or not (MIN_PRICE <= trade_price <= MAX_PRICE):
                    continue
                signals.append({
                    "time_utc": now.isoformat(), "mode": "paper", "ticker": tkr, "event": event_ticker,
                    "city": city, "side": side, "price": f"{cost:.4f}", "fee": f"{fee:.2f}", "count": 1,
                    "model_win_prob": f"{win_p:.4f}", "edge": f"{edge:.4f}", "yes_bid": bid, "yes_ask": ask,
                    "model_center_f": f"{model.center():.1f}",
                    "obs_max_f": "" if obs is None else f"{obs:.1f}",
                    "future_max_f": "" if fut is None else f"{fut:.1f}",
                    "status": "open", "result": "", "pnl": "", "order_id": ""})

    for s in signals:
        if live_ok and live_size:
            placed = place_live(s, rows, live_size)
            if placed:
                oid, filled = placed
                trade_px = float(s["yes_ask"]) if s["side"] == "YES" else float(s["yes_bid"])
                s["mode"], s["count"], s["order_id"] = "live", f"{filled:g}", oid
                s["fee"] = f"{wm.taker_fee(trade_px, max(1, int(round(filled)))):.2f}"
                rows.append(s)
                _unsaved_live[s["ticker"]] = s
                # Record every real order the moment it fills. If the file can't be
                # written (e.g. open in Excel) the order was forgotten and re-placed every
                # scan. Now: stop placing live orders until the record is safe.
                try:
                    save_trades(rows)
                except Exception as e:
                    print(f"[!!] LIVE ORDER {oid} on {s['ticker']} could not be saved to {TRADES_CSV} ({e}). "
                          f"Live trading paused for this scan. Close the file if it is open in Excel.")
                    live_ok = False
                continue
        rows.append(s)
    return signals, snapshots, problems


def print_cycle(signals, problems, rows):
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"\n==================== WEATHER SCAN {stamp} ====================")
    for s in signals:
        print(f"  [{s['mode'].upper()}] {s['side']:<3} {s['ticker']:<32} @ ${float(s['price']):.2f}"
              f"  model {float(s['model_win_prob']):.0%}  edge {float(s['edge'])*100:+.1f}¢"
              f"  (model high {s['model_center_f']}°F)")
    if not signals:
        print("  No new trades this scan (nothing beat the edge threshold).")
    for p in problems[:15]:
        print(f"  [!] {p}")
    n, net = paper_summary(rows)
    open_n = sum(1 for r in rows if r.get("status") == "open")
    print(f"  Paper record: {n} settled, net ${net:+.2f} | {open_n} open")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one scan and exit")
    args = ap.parse_args()

    print("=" * 64)
    print("  KALSHI WEATHER BOT  -  " + ("PAPER, goes live automatically once proven" if AUTO_GO_LIVE
                                           else "PAPER MODE ONLY: no orders are placed"))
    print("=" * 64)

    last_snapshot = 0.0
    first_scan = True
    while True:
        try:
            # Inside the try: a locked/unreadable CSV used to crash the bot for good
            # (run_weather.bat does not restart it).
            rows = load_trades()
            if settle_trades(rows):
                save_trades(rows)
            live_ok, why, live_size = live_allowed(rows)
            if live_ok:
                # Never place real orders unless the trade record can be written.
                try:
                    save_trades(rows)
                except Exception as e:
                    live_ok, why = False, f"live paused: cannot write {TRADES_CSV} ({e}); close it if open in Excel"
            print(f"[*] {'LIVE' if live_ok else 'Paper mode'}: {why}")
            take_snap = time.time() - last_snapshot >= SNAPSHOT_EVERY_SECONDS
            print("[*] Scanning cities...", flush=True)
            signals, snaps, problems = scan(rows, live_ok, take_snap, verbose=first_scan, live_size=live_size)
            first_scan = False
            if signals:
                save_trades(rows)
            if snaps:
                append_snapshots(snaps)
                last_snapshot = time.time()
            print_cycle(signals, problems, rows)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[!] Scan failed: {e}. Retrying next cycle.")
        if args.once:
            return
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[*] Weather bot stopped.")
        sys.exit(0)
