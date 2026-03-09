"""Execution layer for Polymarket trading.

Uses the official py-clob-client SDK for order placement.
"""

import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import Optional

from dotenv import load_dotenv

from .trading_config import (
    CHAIN_ID,
    CLOB_HOST,
    SIGNATURE_TYPE,
    STOP_LOSS_THRESHOLD,
    ARB_BET_SIZE,
    SIGNAL_BET_SIZE,
    HARD_MAX_COST_PER_TRADE,
    MAX_OPEN_POSITIONS,
    MIN_PROFIT_FOR_ARB,
)
from .models import Opportunity, OpportunityType
from .database import get_connection, DB_PATH

logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()


class TradingExecutor:
    """Handles order execution on Polymarket CLOB."""
    
    def __init__(self, paper_trading: bool = True):
        """Initialize the trading executor.
        
        Args:
            paper_trading: If True, simulate trades without real execution
        """
        self.paper_trading = paper_trading
        self.client = None
        self.balance = Decimal("0")
        self.open_positions = 0
        self.trades_this_hour = 0
        self._initialized = False
    
    def initialize(self) -> bool:
        """Initialize the CLOB client with credentials.
        
        Returns:
            True if successfully initialized, False otherwise
        """
        if self.paper_trading:
            logger.info("Paper trading mode - no credentials required")
            self._initialized = True
            return True
        
        private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
        funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
        
        if not private_key:
            logger.error("POLYMARKET_PRIVATE_KEY not set in .env file")
            return False
        
        try:
            from py_clob_client.client import ClobClient
            
            self.client = ClobClient(
                host=CLOB_HOST,
                key=private_key,
                chain_id=CHAIN_ID,
                signature_type=SIGNATURE_TYPE,  # 0=MetaMask/EOA, 1=Magic.Link
                funder=funder if funder else None,
            )
            
            # Derive API credentials (or create if first time)
            try:
                creds = self.client.create_or_derive_api_creds()
            except Exception:
                creds = self.client.create_api_key()
            self.client.set_api_creds(creds)
            
            # Verify connection
            self.client.get_orders()
            logger.info(f"CLOB client initialized — live trading enabled")
            self._initialized = True
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize CLOB client: {e}")
            return False
    
    def get_balance(self) -> Decimal:
        """Get current USDC balance.
        
        For paper trading, returns simulated balance.
        For live trading, queries the internal tracker (synced on startup).
        """
        if self.paper_trading:
            return self.balance
        return self.balance
    
    def sync_balance_from_api(self) -> Optional[Decimal]:
        """Sync balance from Polymarket API.
        
        Queries open orders and recent trades to estimate current balance.
        Returns the updated balance or None on failure.
        """
        if self.paper_trading or not self.client:
            return None
        
        try:
            # Count actual open orders on Polymarket
            orders = self.client.get_orders()
            open_count = 0
            if isinstance(orders, list):
                open_count = len(orders)
            elif hasattr(orders, 'data'):
                open_count = len(orders.data)
            
            self.open_positions = open_count
            logger.info(f"[SYNC] Open orders on Polymarket: {open_count}")
            return self.balance
        except Exception as e:
            logger.warning(f"Balance sync failed: {e}")
            return None
    
    def check_risk_limits(self, bet_size: Decimal) -> tuple[bool, str]:
        """Check if trade is within risk limits.
        
        Returns:
            (allowed, reason) tuple
        """
        current_balance = self.get_balance()
        
        # Check stop loss
        if current_balance <= STOP_LOSS_THRESHOLD:
            return False, f"Stop loss triggered: balance ${current_balance} <= ${STOP_LOSS_THRESHOLD}"
        
        # Check if bet would put us below stop loss
        if current_balance - bet_size < STOP_LOSS_THRESHOLD:
            return False, f"Trade would breach stop loss"
        
        # Check open positions
        if self.open_positions >= MAX_OPEN_POSITIONS:
            return False, f"Max open positions reached: {MAX_OPEN_POSITIONS}"
        
        # Check sufficient balance
        if bet_size > current_balance:
            return False, f"Insufficient balance: ${current_balance} < ${bet_size}"
        
        return True, "OK"
    
    def execute_arbitrage(self, opportunity: Opportunity) -> bool:
        """Execute an arbitrage trade.
        
        For complement arb, this means buying both Yes and No shares.
        
        Returns:
            True if trade executed successfully
        """
        if not self._initialized:
            logger.error("Executor not initialized")
            return False
        
        # Check profit threshold
        if opportunity.profit_bound < MIN_PROFIT_FOR_ARB:
            logger.info(f"Arb profit ${opportunity.profit_bound} below minimum ${MIN_PROFIT_FOR_ARB}")
            return False
        
        # Check risk limits
        allowed, reason = self.check_risk_limits(ARB_BET_SIZE)
        if not allowed:
            logger.warning(f"Trade blocked: {reason}")
            return False
        
        if self.paper_trading:
            return self._paper_trade_arb(opportunity)
        else:
            return self._live_trade_arb(opportunity)
    
    def execute_signal_trade(
        self,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        price: Decimal,
        signal_source: str,
    ) -> bool:
        """Execute a signal-based trade.
        
        Returns:
            True if trade executed successfully
        """
        if not self._initialized:
            logger.error("Executor not initialized")
            return False
        
        # Check risk limits
        allowed, reason = self.check_risk_limits(SIGNAL_BET_SIZE)
        if not allowed:
            logger.warning(f"Trade blocked: {reason}")
            return False
        
        if self.paper_trading:
            return self._paper_trade_signal(token_id, side, price, signal_source)
        else:
            return self._live_trade_signal(token_id, side, price, signal_source)
    
    def _paper_trade_arb(self, opportunity: Opportunity) -> bool:
        """Simulate an arbitrage trade."""
        profit = opportunity.profit_bound * ARB_BET_SIZE
        
        logger.info(f"[PAPER] ARB TRADE: {opportunity.rationale}")
        logger.info(f"[PAPER] Bet: ${ARB_BET_SIZE}, Expected profit: ${profit:.4f}")
        
        # Update paper balance (assume arb succeeds)
        self.balance += profit
        self._log_trade("ARB", opportunity.market_id, ARB_BET_SIZE, profit, "PAPER")
        
        return True
    
    def _paper_trade_signal(
        self,
        token_id: str,
        side: str,
        price: Decimal,
        signal_source: str,
    ) -> bool:
        """Simulate a signal trade.
        
        NOTE: Balance deduction is handled by the caller (trading_bot.py).
        Do NOT deduct here — that caused a double-deduction bug.
        """
        logger.info(f"[PAPER] SIGNAL TRADE: {side} {token_id[:20]}... @ ${price:.4f}")
        logger.info(f"[PAPER] Source: {signal_source}, Size: ${SIGNAL_BET_SIZE}")
        
        self.open_positions += 1
        self._log_trade("SIGNAL", token_id, SIGNAL_BET_SIZE, Decimal("0"), "PAPER")
        
        return True
    
    def _live_trade_arb(self, opportunity: Opportunity) -> bool:
        """Execute a real arbitrage trade on Polymarket.
        
        For a complement arb, we buy both Yes and No sides.
        The opportunity.rationale contains the token info.
        """
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            
            # Extract token IDs from the opportunity's legs
            if not hasattr(opportunity, 'legs') or not opportunity.legs:
                logger.warning("[LIVE] ARB opportunity missing leg data — skipping")
                return False
            
            bet_size = float(ARB_BET_SIZE)
            logger.info(f"[LIVE] Executing arbitrage on {opportunity.market_id[:20]}... (${ARB_BET_SIZE})")
            
            placed_orders = []
            for leg in opportunity.legs:
                token_id = leg.get('token_id', '')
                price = float(leg.get('price', 0))
                if not token_id or price <= 0:
                    continue
                
                shares = bet_size / price
                if shares < 5.0:
                    shares = 5.0
                
                order_args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=round(shares, 2),
                    side="BUY",
                    fee_rate_bps=200,  # 2% Polymarket fee
                )
                
                signed_order = self.client.create_order(order_args)
                result = self.client.post_order(signed_order, OrderType.GTC)
                logger.info(f"[LIVE] ARB leg placed: BUY {shares:.1f} @ ${price:.3f} → {result}")
                placed_orders.append(result)
            
            if len(placed_orders) >= 2:
                self.open_positions += 1
                self._log_trade("ARB", opportunity.market_id, ARB_BET_SIZE,
                               opportunity.profit_bound * ARB_BET_SIZE, "LIVE")
                return True
            else:
                logger.warning(f"[LIVE] Only placed {len(placed_orders)}/2 arb legs — cancelling")
                # Cancel any partially placed orders
                self.client.cancel_all()
                return False
            
        except Exception as e:
            logger.error(f"[LIVE] Failed to execute arbitrage: {e}")
            return False
    
    def _live_trade_signal(
        self,
        token_id: str,
        side: str,
        price: Decimal,
        signal_source: str,
    ) -> bool:
        """Execute a real signal trade on Polymarket."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            
            # Polymarket requires minimum 5 shares per order
            shares = float(SIGNAL_BET_SIZE / price) if price > 0 else 5.0
            if shares < 5.0:
                shares = 5.0
            
            total_cost = shares * float(price)
            
            # HARD COST CAP: reject if minimum order exceeds our max
            if total_cost > float(HARD_MAX_COST_PER_TRADE):
                logger.warning(
                    f"[LIVE] REJECTED: {shares:.1f} shares @ ${price:.4f} = ${total_cost:.2f} "
                    f"exceeds max ${HARD_MAX_COST_PER_TRADE} per trade"
                )
                return False
            
            order_args = OrderArgs(
                token_id=token_id,
                price=float(price),
                size=round(shares, 2),
                side=side,
                fee_rate_bps=200,  # 2% Polymarket fee
            )
            
            logger.info(f"[LIVE] Placing: {side} {shares:.1f} shares @ ${price:.4f} (${total_cost:.2f}) [{signal_source}]")
            
            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC)
            
            # Check if order was accepted
            if isinstance(result, dict) and result.get('errorMsg'):
                logger.error(f"[LIVE] Order rejected: {result['errorMsg']}")
                return False
            
            # Check order status — accept both 'matched' (instant fill) and 'live' (on book)
            # Previously we cancelled 'live' orders immediately, but they can fill async.
            # Now we accept 'live' as a valid order and let it sit (GTC = Good Till Cancel).
            status = result.get('status', '') if isinstance(result, dict) else ''
            if status in ('matched', 'live', 'delayed'):
                fill_type = "FILLED" if status == 'matched' else "PLACED (pending fill)"
                logger.info(f"[LIVE] ✅ Order {fill_type}: {result}")
                self.open_positions += 1
                self._log_trade("SIGNAL", token_id, Decimal(str(round(total_cost, 4))), Decimal("0"), "LIVE")
                return True
            else:
                logger.info(f"[LIVE] ⚠️ Unexpected order status ({status}): {result}")
                return False
            
        except Exception as e:
            logger.error(f"[LIVE] Failed to place order: {e}")
            return False
    
    def _log_trade(
        self,
        trade_type: str,
        market_or_token: str,
        size: Decimal,
        profit: Decimal,
        mode: str,
    ) -> None:
        """Log trade to database."""
        with get_connection(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    trade_type TEXT,
                    market_or_token TEXT,
                    size DECIMAL(18, 6),
                    profit DECIMAL(18, 6),
                    mode TEXT
                )
            """)
            cursor.execute("""
                INSERT INTO trades (trade_type, market_or_token, size, profit, mode)
                VALUES (?, ?, ?, ?, ?)
            """, (trade_type, market_or_token, str(size), str(profit), mode))
            conn.commit()
