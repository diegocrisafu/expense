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

from .config import CLOB_API_BASE, GAMMA_API_BASE
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
        """Check if a market has resolved, via the CLOB market record.

        The old gamma query (/markets?conditionId=) silently ignores the
        filter and returns an arbitrary market listing, so resolution was
        being judged against unrelated markets.  The CLOB endpoint keys on
        the condition id directly and carries per-token winner flags.

        Returns:
            Resolution data if the lookup succeeded, None on error.
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{CLOB_API_BASE}/markets/{market_id}",
                    timeout=30.0,
                )
                if response.status_code != 200:
                    return None
                market = response.json()

            if not market or not market.get("closed"):
                return {"resolved": False}

            winner = next(
                (t for t in market.get("tokens", []) if t.get("winner")), None
            )
            if winner is None:
                # Closed but not yet settled — treat as unresolved
                return {"resolved": False}

            return {
                "resolved": True,
                "winner_token_id": winner.get("token_id"),
                "winning_outcome": winner.get("outcome"),
                "resolution_price": Decimal("1"),
                "market": market,
            }

        except Exception as e:
            logger.error(f"Error checking resolution for {market_id}: {e}")
            return None
    
    def _already_closed(self, trade_id: int) -> bool:
        """True if this trade was already resolved/closed elsewhere.

        Checks both the learning ledger (trade_history) and the managed-position
        ledger, so a manager exit OR a prior resolution both count as closed.
        """
        try:
            with get_connection(self.db_path) as conn:
                cursor = conn.cursor()
                r = cursor.execute(
                    "SELECT status FROM trade_history WHERE id = ?", (trade_id,)
                ).fetchone()
                if r and r[0] and str(r[0]).upper() != "PENDING":
                    return True
                r = cursor.execute(
                    "SELECT status FROM managed_positions WHERE trade_id = ?", (trade_id,)
                ).fetchone()
                if r and str(r[0]).upper() == "CLOSED":
                    return True
        except Exception as e:
            logger.debug(f"_already_closed check failed for {trade_id}: {e}")
        return False

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

        # GUARD: never double-resolve.  If the position manager already sold
        # this position (managed exit) or it was already resolved, do NOT
        # overwrite that real outcome with the market's eventual settlement.
        # This was the root cause of trade_history showing 0 wins: cheap
        # longshots we sold at a profit were re-booked as worthless-expiry
        # losses when the market finally settled.
        if self._already_closed(position.trade_id):
            # Close out the ghost row so this position stops being re-checked
            # every cycle and stops counting as open exposure.  No P&L is
            # booked here — the ledger that settled it owns the outcome.
            with get_connection(self.db_path) as conn:
                conn.cursor().execute(
                    "UPDATE positions SET status = 'CLOSED_ELSEWHERE', "
                    "resolved_at = CURRENT_TIMESTAMP "
                    "WHERE trade_id = ? AND status = 'OPEN'",
                    (position.trade_id,),
                )
                conn.commit()
            logger.info(
                f"Closed ghost positions row for trade {position.trade_id}: "
                f"already settled by the position manager or P&L ledger."
            )
            return None

        resolution_price = resolution.get("resolution_price", Decimal("0"))
        winner_token_id = resolution.get("winner_token_id")

        # For arbitrage (BUY_BOTH), we always win $1 per unit
        if position.side == "BUY_BOTH":
            # Arbitrage: we bought both sides, one pays out $1
            pnl = position.size * (Decimal("1") - position.entry_price)
            won = True
        else:
            # Single-side bet: settle against OUR token, not the headline
            # price — judging by resolution_price alone booked WINs on
            # losing tokens.  Without winner information, do not settle.
            if winner_token_id is None:
                logger.warning(
                    f"No winner token for trade {position.trade_id}; "
                    f"leaving position open."
                )
                return None
            held_won = position.token_id == winner_token_id
            if position.side == "BUY":
                if held_won:
                    pnl = position.size * (Decimal("1") - position.entry_price)
                    won = True
                else:
                    pnl = -position.size * position.entry_price
                    won = False
            else:  # SELL
                if not held_won:
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
