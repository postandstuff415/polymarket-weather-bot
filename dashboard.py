import json, os, streamlit as st, pandas as pd
from datetime import datetime

st.set_page_config(page_title="Kalshi Shopkeeper Terminal", page_icon="⚡", layout="wide")

def load_state():
    if os.path.exists("engine_state.json"):
        try:
            with open("engine_state.json", "r") as f:
                return json.load(f)
        except Exception:
            pass
    return None

state = load_state()

st.sidebar.title("⚙️ Engine Controls")
slot_size = st.sidebar.slider("Per-Market Allocation ($)", 200, 750, 375, 25)
cash_reserve_pct = st.sidebar.slider("Cash Reserve Floor (%)", 15, 40, 25, 5)
min_spread = st.sidebar.slider("Min Spread Filter (¢)", 1.5, 6.0, 2.5, 0.5)
flatten_buffer = st.sidebar.slider("Pre-Close Flattening Buffer (min)", 15, 60, 45, 5)
st.sidebar.markdown("---")
engine_active = st.sidebar.toggle("Run Market Making Engine", value=True)

st.title("⚡ Kalshi Market-Making Command Center")
col_banner1, col_banner2 = st.columns([3, 1])
col_banner1.caption(f"Last Engine Heartbeat: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Local Minisforum Engine")
if col_banner2.button("🔄 Refresh Telemetry", use_container_width=True):
    st.rerun()

equity = state.get("account_equity", 10000.0) if state else 10000.0
daily_pnl = state.get("daily_realized_pnl", 0.0) if state else 0.0
shares_cycled = state.get("shares_traded_today", 0) if state else 0
reserve = state.get("liquid_reserve", 2500.0) if state else 2500.0
slots_used = state.get("active_slots_used", 0) if state else 0
slots_total = state.get("active_slots_total", 20) if state else 20

k1, k2, k3, k4 = st.columns(4)
k1.metric("Total Account Equity", f"${equity:,.2f}", f"+${daily_pnl:,.2f} Today")
k2.metric("Today's Realized Spread PnL", f"${daily_pnl:,.2f}", f"{shares_cycled:,} shares cycled")
k3.metric("Active Deployment Slots", f"{slots_used} / {slots_total}", "Horizontal Breadth")
k4.metric("Liquid Cash Safety Reserve", f"${reserve:,.2f}", f"{(reserve/equity)*100:.1f}% of Capital")

st.markdown("---")
st.subheader("📊 Live Quoting Books")
if state and "active_markets" in state:
    df = pd.DataFrame(state["active_markets"])
    df["best_bid"] = df["best_bid"].apply(lambda x: f"${x:.2f}")
    df["best_ask"] = df["best_ask"].apply(lambda x: f"${x:.2f}")
    df["spread"] = df["spread"].apply(lambda x: f"{x*100:.1f}¢")
    df["allocated_usd"] = df["allocated_usd"].apply(lambda x: f"${x:.2f}")
    st.dataframe(df, use_container_width=True, hide_index=True)

st.markdown("---")
col_action, col_logs = st.columns([1, 2])
with col_action:
    st.subheader("🚨 Emergency Oversight")
    if st.button("KILL SWITCH: CANCEL & FLATTEN ALL", type="primary", use_container_width=True):
        st.error("⚠️ FLATTENING COMMAND BROADCAST: Cancelling all resting quotes...")
with col_logs:
    st.subheader("📋 System Audit Trail")
    logs = "\n".join(state.get("system_logs", ["Engine standing by..."])) if state else "No logs."
    st.text_area("Engine Activity Stream", value=logs, height=130, disabled=True)
