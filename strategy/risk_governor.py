class RiskGovernor:
    def __init__(
        self,
        max_contracts_per_market: int = 5,
        max_dollar_exposure_per_market: float = 15.0,
        max_total_open_exposure: float = 250.0,
        min_balance_threshold: float = 2500.0
    ):
        self.max_contracts_per_market = max_contracts_per_market
        self.max_dollar_exposure_per_market = max_dollar_exposure_per_market
        self.max_total_open_exposure = max_total_open_exposure
        self.min_balance_threshold = min_balance_threshold

    def evaluate_order(
        self,
        ticker: str,
        price_dollars: float,
        count: int,
        current_positions: dict,
        resting_orders: list,
        current_balance: float = 2960.0
    ) -> tuple[bool, str]:
        """
        Evaluates whether an order complies with all risk parameters.
        Returns: (allowed: bool, reason: str)
        """
        # 1. Global Balance Circuit Breaker
        if current_balance < self.min_balance_threshold:
            return False, f"Balance (${current_balance:.2f}) below threshold (${self.min_balance_threshold:.2f})"

        # 2. Calculate existing filled exposure on this ticker
        pos_data = current_positions.get(ticker, {})
        existing_contracts = abs(pos_data.get("position", 0.0))
        existing_dollar_exposure = pos_data.get("market_exposure", 0.0)

        # 3. Calculate pending exposure from existing resting orders on this ticker
        pending_contracts = 0
        pending_dollars = 0.0
        for ro in resting_orders:
            if ro.get("ticker") == ticker:
                r_count = float(ro.get("remaining_count") or ro.get("count") or 0.0)
                r_price = float(ro.get("yes_price") or ro.get("price") or 0.50)
                if r_price > 1.0:
                    r_price = r_price / 100.0
                pending_contracts += r_count
                pending_dollars += (r_count * r_price)

        total_projected_contracts = existing_contracts + pending_contracts + count
        total_projected_dollars = existing_dollar_exposure + pending_dollars + (count * price_dollars)

        # 4. Check Per-Market Contract Limit
        if total_projected_contracts > self.max_contracts_per_market:
            return False, f"Contract limit reached ({total_projected_contracts} > {self.max_contracts_per_market})"

        # 5. Check Per-Market Dollar Risk Limit
        if total_projected_dollars > self.max_dollar_exposure_per_market:
            return False, f"Dollar exposure limit reached (${total_projected_dollars:.2f} > ${self.max_dollar_exposure_per_market:.2f})"

        # 6. Check Global Portfolio Exposure Limit
        total_portfolio_exposure = sum(p.get("market_exposure", 0.0) for p in current_positions.values())
        if total_portfolio_exposure + (count * price_dollars) > self.max_total_open_exposure:
            return False, f"Global exposure limit reached (${total_portfolio_exposure:.2f} > ${self.max_total_open_exposure:.2f})"

        return True, "Approved"