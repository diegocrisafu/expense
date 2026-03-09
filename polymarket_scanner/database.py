"""SQLite database operations for Polymarket Scanner."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Generator, Optional

from .config import DB_PATH
from .models import Market, Outcome, Opportunity, OpportunityType


def init_database(db_path: str = DB_PATH) -> None:
    """Initialize the SQLite database with required tables."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS markets (
                market_id TEXT PRIMARY KEY,
                event_id TEXT,
                question TEXT,
                end_time TIMESTAMP,
                resolution_source TEXT,
                active BOOLEAN,
                closed BOOLEAN,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS outcomes (
                outcome_id TEXT PRIMARY KEY,
                market_id TEXT REFERENCES markets(market_id),
                text TEXT,
                UNIQUE(market_id, text)
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                outcome_id TEXT REFERENCES outcomes(outcome_id),
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orderbook_levels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                outcome_id TEXT REFERENCES outcomes(outcome_id),
                side TEXT CHECK(side IN ('bid', 'ask')),
                price DECIMAL(10, 6),
                size DECIMAL(18, 6),
                snapshot_id INTEGER REFERENCES snapshots(id)
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT REFERENCES markets(market_id),
                opportunity_type TEXT,
                profit_bound DECIMAL(18, 6),
                confidence_score DECIMAL(3, 2),
                required_size DECIMAL(18, 6),
                liquidity_available DECIMAL(18, 6),
                rationale TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()


@contextmanager
def get_connection(db_path: str = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """Get a database connection context manager."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def upsert_market(market: Market, db_path: str = DB_PATH) -> None:
    """Insert or update a market."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO markets 
            (market_id, event_id, question, end_time, resolution_source, active, closed, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            market.market_id,
            market.event_id,
            market.question,
            market.end_time.isoformat() if market.end_time else None,
            market.resolution_source,
            market.active,
            market.closed,
            datetime.utcnow().isoformat()
        ))
        
        # Upsert outcomes
        for outcome in market.outcomes:
            cursor.execute("""
                INSERT OR REPLACE INTO outcomes (outcome_id, market_id, text)
                VALUES (?, ?, ?)
            """, (outcome.outcome_id, outcome.market_id, outcome.text))
        
        conn.commit()


def save_opportunity(opportunity: Opportunity, db_path: str = DB_PATH) -> int:
    """Save an opportunity and return its ID."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO opportunities 
            (market_id, opportunity_type, profit_bound, confidence_score, 
             required_size, liquidity_available, rationale, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            opportunity.market_id,
            opportunity.opportunity_type.value,
            str(opportunity.profit_bound),
            opportunity.confidence_score,
            str(opportunity.required_size),
            str(opportunity.liquidity_available),
            opportunity.rationale,
            opportunity.timestamp.isoformat()
        ))
        
        conn.commit()
        return cursor.lastrowid


def get_recent_opportunities(
    limit: int = 50, 
    db_path: str = DB_PATH
) -> list[dict]:
    """Get recent opportunities."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT o.*, m.question
            FROM opportunities o
            LEFT JOIN markets m ON o.market_id = m.market_id
            ORDER BY o.timestamp DESC
            LIMIT ?
        """, (limit,))
        
        return [dict(row) for row in cursor.fetchall()]


def get_market(market_id: str, db_path: str = DB_PATH) -> Optional[dict]:
    """Get a market by ID."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM markets WHERE market_id = ?", (market_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
