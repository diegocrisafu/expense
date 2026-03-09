"""Configuration constants for Polymarket Scanner."""

from decimal import Decimal

# API Base URLs
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/"

# Rate Limiting
GAMMA_RATE_LIMIT = 10  # requests per second
CLOB_RATE_LIMIT = 20   # requests per second

# Fee Configuration (hooks for adjustment)
# Set to 0 by default, adjust based on current Polymarket fee structure
MAKER_FEE = Decimal("0.00")  # e.g., 0.01 = 1%
TAKER_FEE = Decimal("0.00")  # e.g., 0.02 = 2%

# Risk Controls
MAX_CAPITAL_PER_EVENT = Decimal("100.00")  # USD
MAX_OPEN_POSITIONS = 10
SLIPPAGE_BUFFER = Decimal("0.01")  # 1% buffer on costs

# Detection Thresholds
MIN_PROFIT_THRESHOLD = Decimal("0.001")  # Minimum profit to flag
MIN_LIQUIDITY_USD = Decimal("10.00")     # Minimum liquidity to consider

# Database
DB_PATH = "polymarket_scanner.db"

# API Pagination
DEFAULT_PAGE_SIZE = 100
MAX_PAGES = 50
