# 🤖 Roger — Polymarket Trading Bot

An autonomous trading bot that monitors Polymarket prediction markets for profitable trading opportunities using edge analysis, momentum detection, and swing trading.

---

## 🚀 Quick Start / Stop Guide

### Starting the Bot

```bash
# 1. Make sure you're in the project directory
cd /Users/diegocrisafulli/Documents/expense

# 2. Start Roger in the background (live trading)
./run_roger.sh start

# That's it! Roger is now running in the background.
# It will keep running even if you close the terminal.
```

### Stopping the Bot

```bash
./run_roger.sh stop
```

### Other Commands

| Command | What it does |
|---------|-------------|
| `./run_roger.sh start` | Launch Roger in background (live trading) |
| `./run_roger.sh stop` | Stop Roger gracefully |
| `./run_roger.sh status` | Check if Roger is running + uptime |
| `./run_roger.sh logs` | Tail live logs (Ctrl+C to exit) |
| `./run_roger.sh restart` | Stop + Start |

### Dashboard

Once running, open **http://localhost:8080** in your browser.  
Password: `diegoiscool`

---

## 📊 How It Works

Roger scans Polymarket every 60 seconds and runs through 4 phases:

### Phase 1: SELL — Manage Existing Positions
- Checks all open positions for Take Profit, Stop Loss, or Trailing Stop triggers
- Sells positions that hit their targets
- Recycles freed capital back into the trading pool

### Phase 2: BUY — Arbitrage Scan
- Looks for markets where buying both YES + NO costs less than $1.00
- These are risk-free guaranteed profits (very rare)

### Phase 3: BUY — Swing / Scalp Trading
- Finds markets with strong momentum (3%+ price change in 1 hour)
- Requires $10k+ daily volume (enough liquidity to exit)
- Buys the trending side, sells for +8% profit target

### Phase 4: BUY — Signal Strategies
- **Momentum**: Follows strong price trends with edge validation
- **Correlated markets**: Finds mispricings between related markets  
- **Smart strategies**: Mean reversion, volume spikes, contrarian
- **Whale following**: Copies large trades ($1k+)

---

## 🧠 Edge Engine — How Accuracy Works

Every trade goes through the **edge engine** before execution:

1. **True probability estimation** — Computes midpoint of bid/ask, then calibrates for favourite-longshot bias (markets over-price favourites by ~3%)
2. **Momentum adjustment** — If price is moving, shifts the probability estimate in that direction
3. **Both sides compared** — Calculates edge for YES *and* NO, picks the better one
4. **Minimum edge gate** — Requires 2.5%+ edge after spread (since Polymarket charges 2% taker fee)
5. **Spread check** — Won't scalp when spread > 8% (can't exit profitably)
6. **Kelly sizing** — Uses quarter-Kelly criterion to size bets proportionally to edge

### What the bot does NOT do (limitations)
- Does **not** predict market outcomes — it trades on price movement and mispricing
- Does **not** use news, AI predictions, or sentiment analysis
- Does **not** guarantee profits — even with edge, individual trades can lose
- The calibration model is statistical, not market-specific — it works better on liquid markets

---

## 💰 Does It Track Existing Bets?

**Partially.** Here's how it works:

### What it DOES track:
- ✅ All positions **it creates** — stored in SQLite database with entry price, TP/SL targets, and timestamps
- ✅ On startup, it **syncs open orders** from the Polymarket API to know how many positions are active
- ✅ It monitors live prices for all its positions and auto-sells when targets are hit
- ✅ Only counts **actually filled** orders (status=`matched`) — unfilled limit orders are cancelled

### What it does NOT track:
- ❌ Bets you placed **manually on the Polymarket website** — the bot only manages positions it created
- ❌ Your actual USDC wallet balance on Polygon — it uses an internal balance tracker starting from `$13.02`
- ❌ Positions from previous bot runs that weren't in the database

### What this means:
If you manually buy something on Polymarket, Roger won't know about it. It won't manage it, sell it, or factor it into risk calculations. Roger only manages positions it opens itself.

---

## 🛡️ Risk Controls

| Parameter | Value | Description |
|-----------|-------|-------------|
| Starting Balance | $13.02 | Declared USDC balance |
| Stop Loss Floor | $1.00 | Bot stops all trading if balance drops here |
| Max Trades/Hour | 6 | Prevents over-trading |
| Max Open Positions | 8 | Across all strategies |
| Max Per-Trade | 10% of balance | Hard cap per individual trade |
| Minimum Edge | 2.5% | Won't trade below this edge after spread |
| Scan Interval | 60 seconds | Time between market scans |

### Per-Strategy Budgets

| Strategy | Budget % | Max/Trade | TP | SL | Max Positions |
|----------|----------|-----------|----|----|---------------|
| Arbitrage | 30% | 10% | +5% | -3% | 3 |
| Swing/Scalp | 30% | 8% | +8% | -5% | 4 |
| Momentum | 15% | 7% | +15% | -8% | 2 |
| Correlated | 10% | 6% | +12% | -7% | 2 |
| Whale Follow | 10% | 5% | +15% | -10% | 1 |

---

## 🏗️ Architecture

```
polymarket_scanner/
├── trading_bot.py       # Main bot loop — runs all 4 phases
├── executor.py          # Places real orders via py-clob-client SDK
├── edge.py              # Probability engine — compares YES vs NO
├── position_manager.py  # TP/SL/trailing stop management
├── risk_manager.py      # Per-strategy capital budgets
├── swing_trader.py      # Momentum scalp, dip scalp, range scalp
├── aggressive.py        # Momentum + mispriced market strategies
├── smart_strategy.py    # Mean reversion, correlation, volume spike
├── signals.py           # Whale / smart money detection
├── trading_config.py    # All configurable parameters
├── config.py            # API endpoints
├── database.py          # SQLite storage
├── models.py            # Data structures
├── learning.py          # Trade history + category learning
├── resolution.py        # Market resolution tracking
├── scanner.py           # Opportunity detection
├── detection.py         # Arbitrage math
├── relationships.py     # Multi-outcome constraints
├── pricing.py           # VWAP, midpoint calculations
├── dashboard.py         # Web dashboard on :8080
├── ingestion/
│   ├── gamma.py         # Market discovery API
│   ├── clob.py          # Order book API
│   └── websocket.py     # Real-time updates
└── tests/
    ├── test_detection.py
    └── test_pricing.py
```

---

## ⚙️ Configuration

All settings are in `polymarket_scanner/trading_config.py`.

### Environment Variables (`.env` file)

```bash
POLYMARKET_PRIVATE_KEY=0xYourPrivateKeyHere
POLYMARKET_FUNDER_ADDRESS=0xYourProxyAddressHere
```

**Never commit your `.env` file.** It's in `.gitignore`.

### Changing the Starting Balance

Edit `STARTING_BALANCE` in `trading_config.py` to match your actual USDC balance.

### Adjusting Aggressiveness

To trade **more conservatively** (fewer bets, higher quality):
- Increase `MIN_EDGE` in `edge.py` (currently 2.5%)
- Decrease `MAX_TRADES_PER_HOUR` in `trading_config.py` (currently 6)
- Increase minimum volume in `swing_trader.py` (currently $10k)

To trade **more aggressively** (more bets, lower threshold):
- Decrease `MIN_EDGE` (minimum 2% to cover fees)
- Increase `MAX_TRADES_PER_HOUR`
- Lower volume thresholds (risk: harder to exit positions)

---

## 🧪 Testing

```bash
# Run all 26 tests
.venv/bin/python -m pytest polymarket_scanner/tests/ -v

# Run edge engine tests
.venv/bin/python test_edge.py
```

---

## 📡 APIs Used

| API | Purpose | Auth Required |
|-----|---------|---------------|
| Gamma API (`gamma-api.polymarket.com`) | Market discovery, prices, volume | No |
| CLOB API (`clob.polymarket.com`) | Order books, order placement | Yes (API key derived from private key) |

---

## ⚠️ Disclaimers

1. **This is experimental software.** Use at your own risk.
2. **Trading involves financial risk.** You can lose your entire balance.
3. **Markets are efficient.** True mispricings get corrected within minutes.
4. **Your private key = your money.** Never share it or commit it to git.
5. **Past performance doesn't guarantee future results.** Edge estimates are probabilistic, not certain.

---

## 📜 License

MIT License — Use freely, no warranty provided.
