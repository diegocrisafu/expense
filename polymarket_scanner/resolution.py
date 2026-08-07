"""Market resolution tracking and trade outcome management.

Monitors markets for resolution and updates trade records with outcomes.
"""

import asyncio
import json
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


def _parse_json_list(raw) -> list:
    """Gamma returns outcomes/prices/token-ids as JSON-encoded strings."""
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw) if isinstance(raw, str) else []
    except Exception:
        return []


def settlement_from_market(market: dict) -> Optional[dict]:
    """Pure: turn a Gamma market dict into resolution info, or None if open.

    The key output is ``settlement_by_token`` — a map from CLOB token id to its
    settled price (≈1 for the winner, ≈0 for losers).  Settling the token we
    ACTUALLY hold is what stops "did any outcome win" from booking every
    resolved bet as a jackpot.
    """
    if not market or not market.get("closed"):
        return None

    outcomes = _parse_json_list(market.get("outcomes", []))
    prices = _parse_json_list(market.get("outcomePrices", ""))
    token_ids = _parse_json_list(market.get("clobTokenIds", ""))

    settlement_by_token: dict[str, Decimal] = {}
    for i, tid in enumerate(token_ids):
        if i < len(prices):
            try:
                settlement_by_token[str(tid)] = Decimal(str(prices[i]))
            except Exception:
                continue

    winning_outcome = None
    winning_price = Decimal("0")
    for i, price in enumerate(prices):
        try:
            p = Decimal(str(price))
        except Exception:
            continue
        if p > Decimal("0.99"):
            if i < len(outcomes):
                winning_outcome = outcomes[i]
            winning_price = p
            break

    return {
        "resolved": True,
        "winning_outcome": winning_outcome,
        "resolution_price": winning_price,
        "settlement_by_token": settlement_by_token,
        "market": market,
    }


async def fetch_market_resolution(market_id: str) -> Optional[dict]:
    """Query the Gamma API and return resolution info for a market.

    Returns a dict with ``resolved`` True/False (and settlement details when
    resolved), or None on network/parse failure.  Shared by the resolution
    tracker and the position manager so both settle a closed market the same
    way.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{GAMMA_API_BASE}/markets",
                params={"conditionId": market_id},
                timeout=30.0,
            )
            response.raise_for_status()
            markets = response.json()
            if not markets:
                response = await client.get(
                    f"{GAMMA_API_BASE}/markets/{market_id}", timeout=30.0,
                )
                if response.status_code == 200:
                    markets = [response.json()]
                else:
                    return None
            market = markets[0] if markets else None
            if not market:
                return None
            settled = settlement_from_market(market)
            return settled if settled is not None else {"resolved": False}
    except Exception as e:
        logger.error(f"Error checking resolution for {market_id}: {e}")
        return None


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

        Thin wrapper around the shared ``fetch_market_resolution`` so the
        position manager and resolution tracker settle markets identically.
        """
        return await fetch_market_resolution(market_id)

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
            logger.debug(
                f"Skipping resolution for trade {position.trade_id}: "
                f"already closed by the position manager."
            )
            return None

        resolution_price = resolution.get("resolution_price", Decimal("0"))
        settlement_by_token = resolution.get("settlement_by_token", {}) or {}

        # For arbitrage (BUY_BOTH), we always win $1 per unit
        if position.side == "BUY_BOTH":
            # Arbitrage: we bought both sides, one pays out $1
            pnl = position.size * (Decimal("1") - position.entry_price)
            won = True
        else:
            # Settle the token we ACTUALLY hold — not "did any outcome win".
            # Earlier this used `resolution_price > 0.99`, which is true for
            # EVERY resolved market (resolution_price is always the winner's
            # ~1.0 price), so every resolved BUY was booked as a jackpot even
            # when our token settled to $0.  We now look up our token_id in the
            # market's settlement map.
            token_settle = settlement_by_token.get(str(position.token_id))
            if token_settle is None:
                # Can't prove which side we hold → refuse to fabricate an
                # outcome.  Leave the position OPEN for the next check/manual
                # review rather than booking a phantom win or an unfair loss.
                logger.warning(
                    f"Trade {position.trade_id}: token {str(position.token_id)[:16]}… not "
                    f"found in resolved market {position.market_id[:16]}… — leaving OPEN "
                    f"(winning_outcome={resolution.get('winning_outcome')})"
                )
                return None

            # token_settle is ~1 (our token won) or ~0 (it lost).
            our_side_won = token_settle > Decimal("0.5")
            if position.side == "BUY":
                # Bought the token: payout = settlement per share, cost = entry.
                pnl = position.size * (token_settle - position.entry_price)
                won = our_side_won
            else:  # SELL / short the token: profit if it settles to 0.
                pnl = position.size * (position.entry_price - token_settle)
                won = not our_side_won
            # Record the token's own settlement, not the market winner's price.
            resolution_price = token_settle
        
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
