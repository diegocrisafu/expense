"""Trading configuration and risk controls.

RISK LIMITS - These are hard-coded for safety.
"""

from decimal import Decimal

# === ACCOUNT SETTINGS ===
STARTING_BALANCE = Decimal("25.00")  # Actual USDC cash as of Feb 22
STOP_LOSS_THRESHOLD = Decimal("5.00")  # Stop if balance drops below this (protect 80% of capital)

# === TRADE SIZING ===
# Polymarket requires minimum 5 shares per order.
# At price 0.30, that's 5 × $0.30 = $1.50 per order minimum.
# HARD_MAX_COST_PER_TRADE is the absolute dollar cap — if an order would
# cost more than this, we REJECT it entirely (don't inflate the bet).
ARB_BET_SIZE = Decimal("2.00")    # Bigger arb bets — arb is risk-free
SIGNAL_BET_SIZE = Decimal("2.00") # Bigger signal bets — deploy more capital
HARD_MAX_COST_PER_TRADE = Decimal("5.00")  # Raised from $2 — allow meaningful positions

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
