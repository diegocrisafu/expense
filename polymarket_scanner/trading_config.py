"""Trading configuration and risk controls.

RISK LIMITS - These are hard-coded for safety.
"""

from decimal import Decimal

# === ACCOUNT SETTINGS ===
STARTING_BALANCE = Decimal("25.00")  # Actual USDC cash as of Feb 22
STOP_LOSS_THRESHOLD = Decimal("5.00")  # Stop if balance drops below this (protect 80% of capital)

# === RISK RULE (SINGLE SOURCE OF TRUTH) ===
# HARD non-negotiable: no single trade may cost more than this fraction of the
# CURRENT balance.  Every sizing path funnels through risk_manager.check_trade,
# which enforces this even against the Polymarket 5-share order minimum.
MAX_TRADE_FRACTION = Decimal("0.05")   # 5% of balance, absolute ceiling per trade
MIN_ORDER_SHARES = Decimal("5")        # Polymarket minimum shares per order

# === TRADE SIZING ===
# Polymarket requires a minimum of 5 shares per order.  At price 0.30 that is
# 5 × $0.30 = $1.50 minimum.  Because that floor can push an order ABOVE the 5%
# cap on a small balance, the risk manager REJECTS such trades instead of
# silently inflating them (the old behaviour blew past the cap).
ARB_BET_SIZE = Decimal("1.00")    # Base arb bet (still capped at 5% by risk mgr)
SIGNAL_BET_SIZE = Decimal("1.00") # Base signal bet (still capped at 5% by risk mgr)
# Absolute dollar cap == 5% of starting balance; recomputed live from balance.
HARD_MAX_COST_PER_TRADE = (STARTING_BALANCE * MAX_TRADE_FRACTION).quantize(Decimal("0.01"))

# === SAFETY LIMITS ===
MAX_TRADES_PER_HOUR = 12  # 3x more trades/hour — seize more opportunities
MAX_OPEN_POSITIONS = 15   # 3x more positions — diversify across markets
MIN_PROFIT_FOR_ARB = Decimal("0.01")  # 1% min arb profit (was 2% — too picky)

# === SIGNAL THRESHOLDS ===
# Minimum whale trade size to follow (in USD)
MIN_WHALE_TRADE_SIZE = Decimal("500.00")  # Lowered — follow smaller whales too

# Minimum expected edge for signal trades
MIN_SIGNAL_EDGE = Decimal("0.05")  # 5% edge (was 8% — too restrictive)

# === HIGH-CONFIDENCE FILTER ===
# Global minimum confidence for any strategy to place a trade.
# Strategy confidence formulas cap around 0.70-0.80, so setting this
# above 0.75 effectively blocks ALL trades.  Per-strategy is_actionable
# checks provide finer control.
MIN_GLOBAL_CONFIDENCE = 0.50  # 50% floor (was 65% — blocked too many good trades)
MAX_ENTRY_PRICE = Decimal("0.55")  # Raised from $0.40 — more markets in play

# === EXIT STRATEGY (Position Manager) ===
# ⚠️  These are DEFAULT fallbacks.  Each strategy now has its own
# TP/SL/trailing profile defined in risk_manager.py → STRATEGY_PROFILES.
# We buy cheap long-shots, so TP is very wide (we want 3-5x returns).
TAKE_PROFIT_PCT = Decimal("0.40")  # +40% profit target (take wins quicker to recycle capital)
STOP_LOSS_PCT = Decimal("0.25")    # -25% stop (was 30% — cut losers faster)
TRAILING_STOP_PCT = Decimal("0.12")  # 12% trailing from high (tighter — lock in gains)
MAX_HOLD_HOURS = 48  # 2 days max (was 3 — recycle capital faster)
MIN_EXIT_SHARES = Decimal("5.0")

# === SMART EXIT ENGINE ===
# Enable the intelligent position reassessment system.
# When enabled, every cycle re-evaluates each position's health using
# live market data (edge, momentum, volume, spread) and exits dynamically.
SMART_EXIT_ENABLED = True

# Minimum health score to hold a losing position (0–1).
# Below this, the engine will cut losses even if the fixed SL hasn't triggered.
SMART_EXIT_MIN_HEALTH_LOSS = 0.25

# Minimum health score to hold a profitable position.
# Below this, the engine takes profit even if TP hasn't triggered.
SMART_EXIT_MIN_HEALTH_PROFIT = 0.40

# How often to run full smart exit analysis (seconds).
# Lightweight price checks happen every cycle; full analysis runs at this interval.
SMART_EXIT_INTERVAL = 60

# === COST-EDGE GATE ===
# When True, the risk manager rejects any trade whose estimated edge cannot beat
# round-trip costs (fees + slippage) by costs.MIN_NET_EDGE, and sizes accepted
# trades by quarter-Kelly on the net-of-cost edge.  This is the principled filter
# that stops the bot paying fees to take coin-flips.  Expect it to block most
# weak signals — that is correct: those trades were -EV.  Tune via costs.py.
ENFORCE_COST_EDGE_GATE = True

# === DATA QUARANTINE ===
# All trade history before this date was recorded by buggy accounting:
#   • placeholder exit prices (0.5) → hallucinated take-profits
#   • three uncoordinated ledgers double-counting the same position
#   • trade_history booking sold-at-profit winners as worthless-expiry losses
# It is NOT trustworthy and must be excluded from the performance scorecard and
# go-live decisions.  The bot must re-accumulate CLEAN data (paper mode) after
# the ledger fixes before any real-money allocation.  Set to the fix date.
CLEAN_DATA_SINCE = "2026-07-03"

# === DASHBOARD ===
DASHBOARD_PORT = 8080

# === API SETTINGS ===
CHAIN_ID = 137  # Polygon mainnet

# Polymarket CLOB endpoints — imported from config.py (single source of truth)
from .config import CLOB_API_BASE as CLOB_HOST  # noqa: E402
from .config import GAMMA_API_BASE as GAMMA_HOST  # noqa: E402

# Signature type (0 = EOA like MetaMask, 1 = Magic/Email, 2 = Proxy)
# Polymarket routes all users through proxy wallets, even MetaMask
SIGNATURE_TYPE = 1
