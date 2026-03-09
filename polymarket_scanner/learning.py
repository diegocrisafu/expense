"""Learning module for the trading bot.

Tracks trade outcomes and adapts strategy based on historical performance.
Implements:
1. Win rate tracking by market category
2. Kelly criterion for optimal bet sizing
3. Strategy performance scoring
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from .database import get_connection, DB_PATH

logger = logging.getLogger(__name__)


@dataclass
class StrategyStats:
    """Performance statistics for a trading strategy."""
    strategy_name: str
    total_trades: int
    wins: int
    losses: int
    pending: int
    total_profit: Decimal
    total_loss: Decimal
    
    @property
    def win_rate(self) -> float:
        """Win rate as a percentage (0-1)."""
        completed = self.wins + self.losses
        if completed == 0:
            return 0.5  # Default to 50% when no data
        return self.wins / completed
    
    @property
    def avg_win(self) -> Decimal:
        """Average profit per winning trade."""
        if self.wins == 0:
            return Decimal("0")
        return self.total_profit / self.wins
    
    @property
    def avg_loss(self) -> Decimal:
        """Average loss per losing trade."""
        if self.losses == 0:
            return Decimal("0")
        return self.total_loss / self.losses
    
    @property
    def kelly_fraction(self) -> float:
        """Kelly criterion for optimal bet sizing.
        
        Kelly = (bp - q) / b
        where:
            b = odds received on the bet (avg_win / avg_loss)
            p = probability of winning
            q = probability of losing (1 - p)
        
        Returns fraction of bankroll to bet (0-1, capped at 0.25 for safety).
        """
        if self.avg_loss == 0:
            return 0.1  # Default conservative fraction
        
        p = self.win_rate
        q = 1 - p
        b = float(self.avg_win / self.avg_loss) if self.avg_loss else 1.0
        
        if b <= 0:
            return 0
        
        kelly = (b * p - q) / b
        
        # Cap at 25% of bankroll (fractional Kelly for safety)
        return max(0, min(kelly * 0.5, 0.25))
    
    @property
    def edge(self) -> float:
        """Expected edge as a percentage.
        
        Edge = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
        """
        return float(
            self.win_rate * float(self.avg_win) - 
            (1 - self.win_rate) * float(self.avg_loss)
        )
    
    @property
    def is_profitable(self) -> bool:
        """Whether this strategy has positive expectation."""
        return self.edge > 0 and self.total_trades >= 5


class LearningEngine:
    """Tracks and learns from trade outcomes."""
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        self._ensure_tables()
    
    def _ensure_tables(self):
        """Create learning-related tables if they don't exist."""
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Enhanced trades table with outcome tracking
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    strategy TEXT,
                    market_id TEXT,
                    market_question TEXT,
                    category TEXT,
                    token_id TEXT,
                    side TEXT,
                    entry_price DECIMAL(18, 8),
                    size DECIMAL(18, 6),
                    status TEXT DEFAULT 'PENDING',
                    exit_price DECIMAL(18, 8),
                    pnl DECIMAL(18, 6),
                    resolved_at TIMESTAMP
                )
            """)
            
            # Strategy performance cache
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategy_performance (
                    strategy TEXT PRIMARY KEY,
                    total_trades INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    total_profit DECIMAL(18, 6) DEFAULT 0,
                    total_loss DECIMAL(18, 6) DEFAULT 0,
                    last_updated TIMESTAMP
                )
            """)
            
            # Category-level performance
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS category_performance (
                    category TEXT PRIMARY KEY,
                    total_trades INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    total_pnl DECIMAL(18, 6) DEFAULT 0,
                    last_updated TIMESTAMP
                )
            """)
            
            conn.commit()
    
    def record_trade(
        self,
        strategy: str,
        market_id: str,
        market_question: str,
        token_id: str,
        side: str,
        entry_price: Decimal,
        size: Decimal,
        category: str = "unknown",
    ) -> int:
        """Record a new trade entry.
        
        Returns:
            Trade ID for later updating
        """
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trade_history 
                (strategy, market_id, market_question, category, token_id, side, entry_price, size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (strategy, market_id, market_question, category, token_id, side, 
                  str(entry_price), str(size)))
            conn.commit()
            return cursor.lastrowid
    
    def resolve_trade(
        self,
        trade_id: int,
        exit_price: Decimal,
        won: bool,
    ) -> None:
        """Record the outcome of a trade."""
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Get trade details
            cursor.execute(
                "SELECT strategy, category, entry_price, size, side FROM trade_history WHERE id = ?",
                (trade_id,)
            )
            row = cursor.fetchone()
            if not row:
                logger.warning(f"Trade {trade_id} not found")
                return
            
            strategy, category, entry_price, size, side = row
            entry_price = Decimal(entry_price)
            size = Decimal(size)
            
            # Calculate P&L
            if side == "BUY":
                pnl = (exit_price - entry_price) * size
            else:
                pnl = (entry_price - exit_price) * size
            
            status = "WON" if won else "LOST"
            
            # Update trade record
            cursor.execute("""
                UPDATE trade_history 
                SET status = ?, exit_price = ?, pnl = ?, resolved_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (status, str(exit_price), str(pnl), trade_id))
            
            # Update strategy performance
            self._update_strategy_stats(cursor, strategy, won, pnl)
            
            # Update category performance  
            self._update_category_stats(cursor, category, won, pnl)
            
            conn.commit()
            logger.info(f"Trade {trade_id} resolved: {status}, PnL: ${pnl:.4f}")
    
    def _update_strategy_stats(
        self,
        cursor: sqlite3.Cursor,
        strategy: str,
        won: bool,
        pnl: Decimal,
    ):
        """Update strategy performance stats."""
        cursor.execute("""
            INSERT INTO strategy_performance (strategy, total_trades, wins, losses, total_profit, total_loss, last_updated)
            VALUES (?, 1, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(strategy) DO UPDATE SET
                total_trades = total_trades + 1,
                wins = wins + ?,
                losses = losses + ?,
                total_profit = total_profit + ?,
                total_loss = total_loss + ?,
                last_updated = CURRENT_TIMESTAMP
        """, (
            strategy,
            1 if won else 0,
            0 if won else 1,
            str(pnl) if pnl > 0 else "0",
            str(abs(pnl)) if pnl < 0 else "0",
            1 if won else 0,
            0 if won else 1,
            str(pnl) if pnl > 0 else "0",
            str(abs(pnl)) if pnl < 0 else "0",
        ))
    
    def _update_category_stats(
        self,
        cursor: sqlite3.Cursor,
        category: str,
        won: bool,
        pnl: Decimal,
    ):
        """Update category performance stats."""
        cursor.execute("""
            INSERT INTO category_performance (category, total_trades, wins, losses, total_pnl, last_updated)
            VALUES (?, 1, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(category) DO UPDATE SET
                total_trades = total_trades + 1,
                wins = wins + ?,
                losses = losses + ?,
                total_pnl = total_pnl + ?,
                last_updated = CURRENT_TIMESTAMP
        """, (
            category,
            1 if won else 0,
            0 if won else 1,
            str(pnl),
            1 if won else 0,
            0 if won else 1,
            str(pnl),
        ))
    
    def get_strategy_stats(self, strategy: str) -> StrategyStats:
        """Get performance stats for a strategy."""
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT total_trades, wins, losses, total_profit, total_loss
                FROM strategy_performance WHERE strategy = ?
            """, (strategy,))
            
            row = cursor.fetchone()
            if not row:
                return StrategyStats(
                    strategy_name=strategy,
                    total_trades=0, wins=0, losses=0, pending=0,
                    total_profit=Decimal("0"), total_loss=Decimal("0"),
                )
            
            # Count pending trades
            cursor.execute(
                "SELECT COUNT(*) FROM trade_history WHERE strategy = ? AND status = 'PENDING'",
                (strategy,)
            )
            pending = cursor.fetchone()[0]
            
            return StrategyStats(
                strategy_name=strategy,
                total_trades=row[0],
                wins=row[1],
                losses=row[2],
                pending=pending,
                total_profit=Decimal(row[3] or "0"),
                total_loss=Decimal(row[4] or "0"),
            )
    
    def get_optimal_bet_size(
        self,
        strategy: str,
        base_bet: Decimal,
        bankroll: Decimal,
    ) -> Decimal:
        """Get optimal bet size based on Kelly criterion and historical performance.
        
        Args:
            strategy: The strategy being used
            base_bet: Default bet size
            bankroll: Current available bankroll
        
        Returns:
            Recommended bet size
        """
        stats = self.get_strategy_stats(strategy)
        
        # Not enough data - use base bet
        if stats.total_trades < 10:
            logger.debug(f"Strategy {strategy}: Only {stats.total_trades} trades, using base bet ${base_bet}")
            return base_bet
        
        # Strategy is losing - reduce bet
        if not stats.is_profitable:
            reduced = base_bet * Decimal("0.5")
            logger.info(f"Strategy {strategy}: Negative edge ({stats.edge:.1%}), reducing to ${reduced}")
            return reduced
        
        # Calculate Kelly-optimal bet
        kelly = Decimal(str(stats.kelly_fraction))
        kelly_bet = bankroll * kelly
        
        # Don't exceed 2x base bet even if Kelly suggests more
        max_bet = base_bet * 2
        optimal = min(kelly_bet, max_bet, base_bet * Decimal("1.5"))
        
        logger.info(
            f"Strategy {strategy}: Win rate {stats.win_rate:.1%}, "
            f"Kelly {stats.kelly_fraction:.1%}, Bet ${optimal:.2f}"
        )
        
        return optimal
    
    def get_category_ranking(self) -> list[tuple[str, float]]:
        """Get categories ranked by profitability.
        
        Returns:
            List of (category, win_rate) tuples, best first
        """
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT category, 
                       CAST(wins AS FLOAT) / NULLIF(wins + losses, 0) as win_rate,
                       total_pnl
                FROM category_performance
                WHERE total_trades >= 5
                ORDER BY win_rate DESC, total_pnl DESC
            """)
            return [(row[0], row[1] or 0.5) for row in cursor.fetchall()]
    
    def should_trade_category(self, category: str) -> tuple[bool, str]:
        """Check if we should trade in this category based on history.
        
        Returns:
            (should_trade, reason)
        """
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT total_trades, wins, losses, total_pnl
                FROM category_performance WHERE category = ?
            """, (category,))
            
            row = cursor.fetchone()
            if not row or row[0] < 5:
                return True, "Not enough data yet"
            
            total, wins, losses, pnl = row
            win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0.5
            
            # Avoid categories with <30% win rate
            if win_rate < 0.3 and total >= 10:
                return False, f"Poor historical performance ({win_rate:.0%} win rate)"
            
            return True, f"Historical win rate: {win_rate:.0%}"
    
    def print_performance_report(self):
        """Print a summary of bot performance."""
        strategies = ["ARB", "SIGNAL", "WHALE_FOLLOW"]
        
        print("\n" + "=" * 60)
        print("📊 BOT PERFORMANCE REPORT")
        print("=" * 60)
        
        for strat in strategies:
            stats = self.get_strategy_stats(strat)
            if stats.total_trades == 0:
                continue
            
            print(f"\n{strat}:")
            print(f"  Trades: {stats.total_trades} ({stats.pending} pending)")
            print(f"  Win Rate: {stats.win_rate:.1%}")
            print(f"  Edge: {stats.edge:.2%}")
            print(f"  Net P&L: ${float(stats.total_profit - stats.total_loss):.2f}")
            print(f"  Kelly Fraction: {stats.kelly_fraction:.1%}")
        
        print("\n" + "-" * 60)
        print("Category Performance:")
        for cat, wr in self.get_category_ranking()[:5]:
            print(f"  {cat}: {wr:.0%} win rate")
        
        print("=" * 60 + "\n")
