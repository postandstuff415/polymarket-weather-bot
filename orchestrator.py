"""
orchestrator.py - NOT USED BY THE BOT.

The live engine is entirely in autopilot.py (discovery, quoting, exits, loss stop)
plus the strategy/ modules it imports. Nothing imports this file.

The exit_inventory() function that used to live here was never wired in, and its
checks would have been wrong for this bot:
  - it compared status != "open", but live market responses say "active",
    so it would have blocked every exit;
  - it read "symbol"/"count" fields and called place_market_order(), none of
    which exist in this codebase.

The real dust filter, market-status check and 5-second exit budget are in
run_inventory_exits() in autopilot.py.
"""

if __name__ == "__main__":
    raise SystemExit("orchestrator.py is not the bot. Run:  python autopilot.py")
