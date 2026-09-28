"""
Pricing & Evaluation Brain (Core-Satellite Architecture)
1. Core: Standard settlement & maker quote pricing
2. Satellite: Outlier detection & strictly capped budget sizing ($10 max)
"""

def evaluate_contract(category: str, actual_value: float, strike_value: float, contract_type: str = "above") -> dict:
    """Core Market Making Evaluation."""
    settles_yes = False
    
    if contract_type == "above":
        settles_yes = actual_value > strike_value
    elif contract_type == "below":
        settles_yes = actual_value < strike_value
    elif contract_type == "exact":
        settles_yes = round(actual_value, 2) == round(strike_value, 2)
        
    target_side = "yes" if settles_yes else "no"
    max_taker_price = 0.88
    maker_bid_price = 0.82
    
    return {
        "category": category,
        "actual_value": actual_value,
        "strike_value": strike_value,
        "settles_yes": settles_yes,
        "target_side": target_side,
        "max_taker_price": max_taker_price,
        "maker_bid_price": maker_bid_price
    }

def evaluate_satellite_edge(eval_result: dict, current_market_ask: float, satellite_budget_usd: float = 10.00) -> dict | None:
    """
    Satellite Outlier Filter:
    Evaluates if an opportunity has an outsized edge (e.g., buying guaranteed/favored 
    contracts trading at steep discounts). Capped strictly to a flat dollar budget.
    """
    target_side = eval_result["target_side"]
    max_acceptable_price = eval_result["max_taker_price"]
    
    # Check if market ask offers a genuine mispricing edge
    if current_market_ask <= max_acceptable_price:
        edge_cents = round((1.00 - current_market_ask) * 100, 1)
        
        # Calculate maximum contracts within the strict dollar budget
        price_per_contract = current_market_ask
        if price_per_contract <= 0:
            return None
            
        allowed_contracts = int(satellite_budget_usd // price_per_contract)
        
        if allowed_contracts >= 1:
            total_risk = round(allowed_contracts * price_per_contract, 2)
            max_profit = round(allowed_contracts * (1.00 - price_per_contract), 2)
            
            return {
                "qualifies": True,
                "target_side": target_side,
                "entry_price": current_market_ask,
                "contract_count": allowed_contracts,
                "capital_at_risk": total_risk,
                "projected_profit": max_profit,
                "edge_cents": edge_cents
            }
            
    return None

def main():
    print("--- [TESTING CORE + SATELLITE PRICING LOGIC] ---\n")
    # US Gas: AAA printed 4.4768, strike is 4.4600 -> Settles YES
    core_eval = evaluate_contract("GAS", actual_value=4.4768, strike_value=4.4600, contract_type="above")
    print(f"Core Target: BUY {core_eval['target_side'].upper()} | Rest Maker Bid @ ${core_eval['maker_bid_price']}")
    
    # Simulate an Outlier Ask available on the book at 42¢ (like the 40% contract on your screen)
    satellite_res = evaluate_satellite_edge(core_eval, current_market_ask=0.42, satellite_budget_usd=10.00)
    if satellite_res:
        print("\n[!] >>> SATELLITE OUTLIER TRIGGERED <<<")
        print(f"  Target Side    : {satellite_res['target_side'].upper()} @ {int(satellite_res['entry_price']*100)}¢")
        print(f"  Contracts Sized: {satellite_res['contract_count']} contracts")
        print(f"  Total Capital  : ${satellite_res['capital_at_risk']} (Strictly capped <= $10.00)")
        print(f"  Max Potential  : +${satellite_res['projected_profit']} (+{round((satellite_res['projected_profit']/satellite_res['capital_at_risk'])*100, 1)}% ROI)")
    print("-------------------------------------------------")

if __name__ == "__main__":
    main()
