"""Market resolution tracking and trade outcome management.

Monitors markets for resolution and updates trade records with outcomes.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

import httpx

from .config import GAMMA_API_BASE
from .database import get_connection, DB_PATH
from .learning import LearningEngine

logger = logging.getLogger(__name__)


@dataclass
class PendingPosition:
    """A position waiting for market resolution."""
    trade_id: int
    market_id: str
    token_id: str
    side: str
    entry_price: Decimal
    size: Decimal
    market_question: str


class ResolutionTracker:
    """Tracks market resolutions and updates trade outcomes."""
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        self.learning = LearningEngine(db_path=self.db_path)
        self._ensure_tables()
    
    def _ensure_tables(self):
        """Ensure resolution tracking tables exist."""
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Position tracking with market resolution status
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id INTEGER,
                    market_id TEXT,
                    token_id TEXT,
                    side TEXT,
                    entry_price DECIMAL(18, 8),
                    size DECIMAL(18, 6),
                    status TEXT DEFAULT 'OPEN',
                    resolution_price DECIMAL(18, 8),
                    pnl DECIMAL(18, 6),
                    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP,
                    FOREIGN KEY (trade_id) REFERENCES trade_history(id)
                )
            """)
            
            # Market resolution cache
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS market_resolutions (
                    market_id TEXT PRIMARY KEY,
                    resolved BOOLEAN DEFAULT FALSE,
                    winning_outcome TEXT,
                    resolution_price DECIMAL(18, 8),
                    resolved_at TIMESTAMP,
                    last_checked TIMESTAMP
                )
            """)
            
            conn.commit()
    
    def record_position(
        self,
        trade_id: int,
        market_id: str,
        token_id: str,
        side: str,
        entry_price: Decimal,
        size: Decimal,
    ) -> int:
        """Record a new open position.
        
        Returns:
            Position ID
        """
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO positions 
                (trade_id, market_id, token_id, side, entry_price, size)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (trade_id, market_id, token_id, side, str(entry_price), str(size)))
            conn.commit()
            
            logger.info(f"Recorded position for market {market_id[:20]}...")
            return cursor.lastrowid
    
    def get_open_positions(self) -> list[PendingPosition]:
        """Get all positions waiting for resolution."""
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT p.trade_id, p.market_id, p.token_id, p.side, 
                       p.entry_price, p.size, th.market_question
                FROM positions p
                LEFT JOIN trade_history th ON p.trade_id = th.id
                WHERE p.status = 'OPEN'
            """)
            
            return [
                PendingPosition(
                    trade_id=row[0],
                    market_id=row[1],
                    token_id=row[2],
                    side=row[3],
                    entry_price=Decimal(row[4]),
                    size=Decimal(row[5]),
                    market_question=row[6] or "",
                )
                for row in cursor.fetchall()
            ]
    
    async def check_market_resolution(self, market_id: str) -> Optional[dict]:
        """Check if a market has resolved via Gamma API.
        
        Returns:
            Resolution data if resolved, None otherwise
        """
        try:
            async with httpx.AsyncClient() as client:
                # Query the market by condition ID
                response = await client.get(
                    f"{GAMMA_API_BASE}/markets",
                    params={"conditionId": market_id},
                    timeout=30.0,
                )
                response.raise_for_status()
                
                markets = response.json()
                if not markets:
                    # Try by slug or ID
                    response = await client.get(
                        f"{GAMMA_API_BASE}/markets/{market_id}",
                        timeout=30.0,
                    )
                    if response.status_code == 200:
                        markets = [response.json()]
                    else:
                        return None
                
                market = markets[0] if markets else None
                if not market:
                    return None
                
                # Check if resolved
                if market.get("closed"):
                    # Get resolution outcome
                    outcomes = market.get("outcomes", [])
                    outcome_prices = market.get("outcomePrices", "")
                    
                    # Parse outcome prices (JSON string)
                    import json
                    try:
                        prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                    except:
                        prices = []
                    
                    # Find winning outcome (price = 1.0)
                    winning_outcome = None
                    winning_price = Decimal("0")
                    
                    for i, price in enumerate(prices):
                        p = Decimal(str(price))
                        if p > Decimal("0.99"):  # Winner
                            if i < len(outcomes):
                                winning_outcome = outcomes[i]
                            winning_price = p
                            break
                    
                    return {
                        "resolved": True,
                        "winning_outcome": winning_outcome,
                        "resolution_price": winning_price,
                        "market": market,
                    }
                
                return {"resolved": False}
                
        except Exception as e:
            logger.error(f"Error checking resolution for {market_id}: {e}")
            return None
    
    async def resolve_position(
        self,
        position: PendingPosition,
        resolution: dict,
    ) -> Optional[Decimal]:
        """Resolve a position based on market resolution.
        
        Returns:
            PnL from the position, or None if failed
        """
        if not resolution.get("resolved"):
            return None
        
        winning_outcome = resolution.get("winning_outcome")
        resolution_price = resolution.get("resolution_price", Decimal("0"))
        
        # For arbitrage (BUY_BOTH), we always win $1 per unit
        if position.side == "BUY_BOTH":
            # Arbitrage: we bought both sides, one pays out $1
            pnl = position.size * (Decimal("1") - position.entry_price)
            won = True
        else:
            # Single-side bet
            # If we bought the winning side, we get $1 per share
            # Our cost was entry_price * size
            if position.side == "BUY":
                # Check if our token won
                # This is simplified - in reality we'd check token_id against winning token
                if resolution_price > Decimal("0.99"):
                    pnl = position.size * (Decimal("1") - position.entry_price)
                    won = True
                else:
                    pnl = -position.size * position.entry_price
                    won = False
            else:  # SELL
                if resolution_price < Decimal("0.01"):
                    pnl = position.size * position.entry_price
                    won = True
                else:
                    pnl = -position.size * (Decimal("1") - position.entry_price)
                    won = False
        
        # Update position in database
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE positions
                SET status = ?, resolution_price = ?, pnl = ?, resolved_at = CURRENT_TIMESTAMP
                WHERE trade_id = ?
            """, (
                "WON" if won else "LOST",
                str(resolution_price),
                str(pnl),
                position.trade_id,
            ))
            conn.commit()
        
        # Update learning engine
        self.learning.resolve_trade(
            position.trade_id,
            exit_price=resolution_price,
            won=won,
        )
        
        logger.info(
            f"Position resolved: {position.market_question[:30]}... "
            f"{'WON' if won else 'LOST'} ${pnl:.2f}"
        )
        
        return pnl
    
    async def check_all_positions(self) -> tuple[int, Decimal]:
        """Check and resolve all open positions.
        
        Returns:
            (positions_resolved, total_pnl)
        """
        positions = self.get_open_positions()
        
        if not positions:
            logger.debug("No open positions to check")
            return 0, Decimal("0")
        
        resolved_count = 0
        total_pnl = Decimal("0")
        
        for position in positions:
            resolution = await self.check_market_resolution(position.market_id)
            
            if resolution and resolution.get("resolved"):
                pnl = await self.resolve_position(position, resolution)
                if pnl is not None:
                    resolved_count += 1
                    total_pnl += pnl
        
        if resolved_count > 0:
            logger.info(f"Resolved {resolved_count} positions, total PnL: ${total_pnl:.2f}")
        
        return resolved_count, total_pnl
    
    def get_position_summary(self) -> dict:
        """Get summary of all positions."""
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT status, COUNT(*), SUM(CAST(pnl AS FLOAT))
                FROM positions
                GROUP BY status
            """)
            
            summary = {"OPEN": 0, "WON": 0, "LOST": 0, "total_pnl": Decimal("0")}
            for row in cursor.fetchall():
                status, count, pnl = row
                summary[status] = count
                if pnl:
                    summary["total_pnl"] += Decimal(str(pnl))
            
            return summary
    
    def print_position_report(self):
        """Print current position status."""
        summary = self.get_position_summary()
        
        print("\n" + "-" * 40)
        print("📈 POSITION STATUS")
        print("-" * 40)
        print(f"  Open: {summary.get('OPEN', 0)}")
        print(f"  Won: {summary.get('WON', 0)}")
        print(f"  Lost: {summary.get('LOST', 0)}")
        print(f"  Total PnL: ${summary['total_pnl']:.2f}")
        print("-" * 40 + "\n")
