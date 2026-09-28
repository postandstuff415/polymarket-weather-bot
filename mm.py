import time
import requests

# ==========================================
# 1. RISK & INVENTORY CONFIGURATION
# ==========================================
TICKER = "INXD-26SEP27-T5900"        # Target Kalshi market ticker
SPREAD_WIDTH = 4                     # Target bid/ask spread (in cents)
ORDER_SIZE = 5                       # Standard quote lot size (contracts)

# Risk Thresholds
MAX_NET_POSITION = 25                # Max contracts allowed long or short before pausing
SKEW_PER_CONTRACT = 0.2              # Cents to shift quotes per contract in inventory
MAX_LOSS_LIMIT = -150.00             # Static dollar stop-loss (Kill switch)

# ==========================================
# 2. INVENTORY & PRICING ENGINE
# ==========================================
def calculate_skewed_quotes(mid_price: float, net_inventory: int):
    """
    Adjusts bid and ask quotes based on inventory imbalance.
    Positive inventory (holding YES) -> skews quotes DOWN to deter buyers and encourage sellers.
    Negative inventory (holding NO)  -> skews quotes UP to deter sellers and encourage buyers.
    """
    half_spread = SPREAD_WIDTH / 2.0
    inventory_skew = net_inventory * SKEW_PER_CONTRACT
    
    # Skewed target center price
    adjusted_center = mid_price - inventory_skew
    
    calc_bid = round(adjusted_center - half_spread)
    calc_ask = round(adjusted_center + half_spread)
    
    # Ensure quotes stay within valid Kalshi bounds (1¢ to 99¢) and maintain spread
    bid = max(1, min(97, calc_bid))
    ask = max(bid + 1, min(99, calc_ask))
    
    return bid, ask

def evaluate_quote_eligibility(net_inventory: int, current_pnl: float):
    """
    Determines whether the market maker should quote both sides, 
    quote one side only, or execute emergency circuit breakers.
    """
    # 1. Static Dollar Stop-Loss (Emergency Kill Switch)
    if current_pnl <= MAX_LOSS_LIMIT:
        return {
            "can_quote_bid": False, 
            "can_quote_ask": False, 
            "kill_switch": True, 
            "reason": f"CRITICAL: Dollar stop-loss hit (${current_pnl:.2f} <= ${MAX_LOSS_LIMIT:.2f})"
        }
    
    can_bid = True
    can_ask = True
    reason = "Normal two-sided quoting"
    
    # 2. Hard Position Ceilings
    if net_inventory >= MAX_NET_POSITION:
        can_bid = False
        reason = f"Inventory Ceiling: Long {net_inventory} YES. Bid quoting paused."
    elif net_inventory <= -MAX_NET_POSITION:
        can_ask = False
        reason = f"Inventory Ceiling: Long {-net_inventory} NO. Ask quoting paused."
        
    return {
        "can_quote_bid": can_bid, 
        "can_quote_ask": can_ask, 
        "kill_switch": False, 
        "reason": reason
    }

# ==========================================
# 3. EXECUTION LOOP
# ==========================================
def run_market_maker():
    print(f"[*] Starting Kalshi Market Maker Engine on {TICKER}")
    print(f"[*] Max Position: ±{MAX_NET_POSITION} | Stop Loss: ${MAX_LOSS_LIMIT:.2f}\n")
    
    # Simulated initial account state
    net_inventory = 18       # Holding +18 YES contracts
    current_pnl = -45.50      # Running PnL down -$45.50
    market_mid_price = 50.0   # Current mid-price 50¢

    # Step A: Evaluate Risk Gates
    risk_status = evaluate_quote_eligibility(net_inventory, current_pnl)
    
    if risk_status["kill_switch"]:
        print(f"[!] {risk_status['reason']}")
        print("[!] CANCELING ALL RESTING ORDERS. SCRIPT HALTED.")
        return

    print(f"[STATUS] {risk_status['reason']}")

    # Step B: Compute Skewed Quotes
    bid, ask = calculate_skewed_quotes(market_mid_price, net_inventory)
    print(f"[PRICING] Mid: {market_mid_price}¢ | Net Inventory: {net_inventory}")
    print(f"[PRICING] Skewed Target -> Bid: {bid}¢ | Ask: {ask}¢")

    # Step C: Dispatch Orders Based on Inventory Gates
    if risk_status["can_quote_bid"]:
        print(f" -> Resting BUY order placed: {ORDER_SIZE} contracts @ {bid}¢")
    else:
        print(f" -> BUY side SUPPRESSED (Inventory too heavy)")

    if risk_status["can_quote_ask"]:
        print(f" -> Resting SELL order placed: {ORDER_SIZE} contracts @ {ask}¢")
    else:
        print(f" -> SELL side SUPPRESSED (Inventory too short)")
    print("-" * 50)

if __name__ == "__main__":
    while True:
        run_market_maker()
        time.sleep(30)
