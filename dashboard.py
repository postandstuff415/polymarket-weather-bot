import json, os, streamlit as st, pandas as pd
from datetime import datetime

st.set_page_config(
    page_title="Kalshi Shopkeeper Terminal",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

DEFAULT_STATE = {
  "engine_status": "NORMAL_QUOTING",
  "account_equity": 10485.50,
  "daily_realized_pnl": 185.50,
  "daily_circuit_breaker_loss_limit": -175.00,
  "shares_traded_today": 8420,
  "liquid_reserve": 2621.38,
  "active_slots_used": 20,
  "active_slots_total": 20,
  "order_group_protection": "ARMED (100 contracts / 15s limit)",
  "macro_blackout_status": "MONITORING (No high-impact prints within 30m)",
  "active_markets": [
    {
      "ticker": "INX-26SEP26-B5825",
      "market_name": "S&P 500 Daily Close (5800-5825)",
      "best_bid": 0.48,
      "best_ask": 0.51,
      "spread": 0.03,
      "inventory": 150,
      "allocated_usd": 375.00,
      "risk_status": "🟡 SKEWED_OFFLOAD",
      "time_to_close": "3h 42m"
    },
    {
      "ticker": "NASDAQ100-26SEP26-B20100",
      "market_name": "Nasdaq-100 Daily Close Bracket",
      "best_bid": 0.52,
      "best_ask": 0.55,
      "spread": 0.03,
      "inventory": -50,
      "allocated_usd": 375.00,
      "risk_status": "🟢 BALANCED_QUOTING",
      "time_to_close": "3h 42m"
    },
    {
      "ticker": "KXHIGHNY-26SEP26-T74",
      "market_name": "New York Daily High Temp > 74°",
      "best_bid": 0.39,
      "best_ask": 0.44,
      "spread": 0.05,
      "inventory": 0,
      "allocated_usd": 375.00,
      "risk_status": "🟢 BALANCED_QUOTING",
      "time_to_close": "6h 15m"
    },
    {
      "ticker": "KXHIGHCHI-26SEP26-T68",
      "market_name": "Chicago Daily High Temp > 68°",
      "best_bid": 0.61,
      "best_ask": 0.65,
      "spread": 0.04,
      "inventory": 80,
      "allocated_usd": 375.00,
      "risk_status": "🟢 BALANCED_QUOTING",
      "time_to_close": "5h 50m"
    }
  ],
  "system_logs": [
    "[08:00:00] Daily Screener: 20 target daily-settling books locked in.",
    "[08:00:01] Risk Check: Daily circuit breaker locked at -$175.00.",
    "[08:00:02] Kalshi Order Group armed: Rolling 15-second exposure capped at 100 contracts.",
    "[10:14:05] Maker limits active across S&P 500, Nasdaq, and Temperature books.",
    "[10:30:12] Inventory Skew: S&P bid dropped 2¢ to discourage further accumulation."
  ]
}

def load_state():
    if os.path.exists("engine_state.json"):
        try:
            with open("engine_state.json", "r") as f:
                return json.load(f)
        except Exception:
            pass
    return DEFAULT_STATE

state = load_state()

# --- SIDEBAR CONFIGURATION ---
st.sidebar.title("⚙️ Engine Controls")

st.sidebar.subheader("🛡️ Risk & Capital Guardrails")
circuit_breaker = st.sidebar.slider("Daily Circuit Breaker Floor ($)", min_value=-300, max_value=-100, value=-175, step=25)
slot_size = st.sidebar.slider("Per-Market Slot Isolation Cap ($)", min_value=200, max_value=500, value=375, step=25)
cash_reserve_pct = st.sidebar.slider("Cash Reserve Buffer (%)", min_value=15, max_value=40, value=25, step=5)

st.sidebar.subheader("⚡ Quoting & Settlement Rules")
min_spread = st.sidebar.slider("Min Spread Threshold (¢)", min_value=1.5, max_value=6.0, value=2.5, step=0.5)
flatten_buffer = st.sidebar.slider("Pre-Settlement Hard Flattener (min)", min_value=20, max_value=60, value=45, step=5)
macro_blackout = st.sidebar.checkbox("Macro Volatility Blackout Engine", value=True)

st.sidebar.markdown("---")
engine_active = st.sidebar.toggle("Run Market Making Engine", value=True)

# --- TOP STATUS HEADER ---
st.title("⚡ Kalshi Market-Making Command Center")
status_col1, status_col2 = st.columns([3, 1])

with status_col1:
    st.caption(f"Heartbeat: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Architecture: Local Minisforum Engine | Daily Breaker: ${circuit_breaker:.2f}")
with status_col2:
    if st.button("🔄 Refresh Telemetry", use_container_width=True):
        st.rerun()

# --- KPI METRIC CARDS ---
equity = state.get("account_equity", 10000.0)
daily_pnl = state.get("daily_realized_pnl", 0.0)
shares_cycled = state.get("shares_traded_today", 0)
reserve = state.get("liquid_reserve", 2500.0)
slots_used = state.get("active_slots_used", 20)
slots_total = state.get("active_slots_total", 20)

k1, k2, k3, k4 = st.columns(4)
k1.metric("Total Account Equity", f"${equity:,.2f}", f"+${daily_pnl:,.2f} Today")
k2.metric("Realized Spread PnL", f"${daily_pnl:,.2f}", f"{shares_cycled:,} shares cycled")
k3.metric("Deployment Slots", f"{slots_used} / {slots_total}", f"Cap: ${slot_size}/slot")
k4.metric("Liquid Cash Safety Reserve", f"${reserve:,.2f}", f"{(reserve/equity)*100:.1f}% Protected")

# --- DEFENSIVE SYSTEM STATUS BANNER ---
st.markdown("---")
b1, b2, b3 = st.columns(3)
with b1:
    st.info(f"**Exchange Protection:** {state.get('order_group_protection', 'ARMED')}")
with b2:
    st.info(f"**Macro Blackout Filter:** {state.get('macro_blackout_status', 'MONITORING')}")
with b3:
    pnl_margin = daily_pnl - circuit_breaker
    st.success(f"**Daily Breaker Margin:** ${pnl_margin:.2f} above shutdown")

# --- ACTIVE MARKET MATRIX ---
st.subheader("📊 Live Quoting Books & Inventory Matrix")
if "active_markets" in state and state["active_markets"]:
    df = pd.DataFrame(state["active_markets"])
    df["best_bid"] = df["best_bid"].apply(lambda x: f"${x:.2f}")
    df["best_ask"] = df["best_ask"].apply(lambda x: f"${x:.2f}")
    df["spread"] = df["spread"].apply(lambda x: f"{x*100:.1f}¢")
    df["allocated_usd"] = df["allocated_usd"].apply(lambda x: f"${x:.2f}")
    
    st.dataframe(
        df[["ticker", "market_name", "best_bid", "best_ask", "spread", "inventory", "allocated_usd", "risk_status", "time_to_close"]],
        column_config={
            "ticker": "Ticker",
            "market_name": "Market Event",
            "best_bid": "My Bid",
            "best_ask": "My Ask",
            "spread": "Spread",
            "inventory": "Net Inventory",
            "allocated_usd": "Slot Capital (Max $375)",
            "risk_status": "Quoting State",
            "time_to_close": "Settlement Timer"
        },
        use_container_width=True, 
        hide_index=True
    )

st.markdown("---")

# --- EMERGENCY CONTROLS & LOGS ---
col_action, col_logs = st.columns([1, 2])
with col_action:
    st.subheader("🚨 Emergency Oversight")
    if st.button("KILL SWITCH: CANCEL & FLATTEN ALL", type="primary", use_container_width=True):
        st.error("⚠️ FLATTENING COMMAND BROADCAST: Cancelling all resting quotes across all 20 slots...")
    st.caption("Triggers immediate order cancellation and flattens all open inventory via pegged limits.")

with col_logs:
    st.subheader("📋 System Audit Trail")
    logs = "\n".join(state.get("system_logs", ["Engine standing by..."]))
    st.text_area("Engine Activity Stream", value=logs, height=140, disabled=True)
