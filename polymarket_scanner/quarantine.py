"""Audit and quarantine phantom exit fills.

Every settlement-priced TAKE_PROFIT in this bot's history was booked on the
LOSING side of a settled market (the book endpoint served the winning
complement's quote).  This tool sweeps closed positions whose booked exit
was settlement-priced or an outsized multiple of entry, verifies each
against the CLOB market record (token winner flags), and — on --apply —
corrects the booked P&L in place while preserving the original values in
dedicated columns.  Nothing is deleted.

Usage:
    python -m polymarket_scanner.quarantine --audit    # read-only verdicts
    python -m polymarket_scanner.quarantine --apply    # audit + write corrections
"""

import argparse
import asyncio
from decimal import Decimal
from typing import Optional

import httpx

from .config import CLOB_API_BASE, DB_PATH
from .database import get_connection

SETTLEMENT_SUSPECT = Decimal("0.99")
GAIN_MULTIPLE = Decimal("4")


def classify_fill(token_id: str, market: Optional[dict]) -> str:
    """Verdict for a suspect booked gain: 'phantom' | 'genuine' | 'unverified'.

    phantom  — market is settled and OUR token is not the winner
    genuine  — market is settled and OUR token won
    unverified — market record unavailable, token not found, or market open
    """
    if not market:
        return "unverified"
    ours = next((t for t in market.get("tokens", [])
                 if t.get("token_id") == token_id), None)
    if ours is None:
        return "unverified"
    if not market.get("closed"):
        return "unverified"
    return "genuine" if ours.get("winner") else "phantom"


async def _fetch_market(market_id: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{CLOB_API_BASE}/markets/{market_id}", timeout=20.0)
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:
        return None


def _suspect_rows(db_path: str) -> list[dict]:
    """Closed, profitable positions whose exit price is settlement-like or >=4x entry."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT id, trade_id, market_id, token_id, entry_price, exit_price,
                   exit_pnl, cost_basis, exit_reason, closed_at, market_question
            FROM managed_positions
            WHERE status = 'CLOSED' AND exit_pnl > 0
              AND (exit_price >= {SETTLEMENT_SUSPECT}
                   OR (entry_price > 0 AND exit_price >= {GAIN_MULTIPLE} * entry_price))
              AND COALESCE(quarantined, 0) = 0
            ORDER BY closed_at
        """)
        cols = ["mp_id", "trade_id", "market_id", "token_id", "entry_price",
                "exit_price", "exit_pnl", "cost_basis", "exit_reason",
                "closed_at", "question"]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]


def _ensure_columns(db_path: str) -> None:
    """Idempotently add quarantine bookkeeping columns."""
    specs = {
        "managed_positions": [
            "quarantined INTEGER DEFAULT 0", "quarantine_reason TEXT",
            "original_exit_price DECIMAL(18,8)", "original_exit_pnl DECIMAL(18,6)",
            "quarantined_at TIMESTAMP",
        ],
        "trade_history": [
            "quarantined INTEGER DEFAULT 0", "quarantine_reason TEXT",
            "original_exit_price DECIMAL(18,8)", "original_pnl DECIMAL(18,6)",
            "original_status TEXT", "quarantined_at TIMESTAMP",
        ],
    }
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        for table, cols in specs.items():
            existing = {r[1] for r in cursor.execute(f"PRAGMA table_info({table})")}
            for col in cols:
                if col.split()[0] not in existing:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        conn.commit()


def _apply_one(db_path: str, row: dict, reason: str) -> None:
    """Correct one phantom fill in place, preserving originals. True loss = -cost_basis."""
    corrected_pnl = -Decimal(str(row["cost_basis"]))
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE managed_positions
            SET quarantined = 1, quarantine_reason = ?,
                original_exit_price = exit_price, original_exit_pnl = exit_pnl,
                quarantined_at = CURRENT_TIMESTAMP,
                exit_price = 0, exit_pnl = ?
            WHERE id = ? AND COALESCE(quarantined, 0) = 0
        """, (reason, str(corrected_pnl), row["mp_id"]))
        cursor.execute("""
            UPDATE trade_history
            SET quarantined = 1, quarantine_reason = ?,
                original_exit_price = exit_price, original_pnl = pnl,
                original_status = status, quarantined_at = CURRENT_TIMESTAMP,
                exit_price = 0, pnl = ?, status = 'LOST'
            WHERE id = ? AND COALESCE(quarantined, 0) = 0
        """, (reason, str(corrected_pnl), row["trade_id"]))
        conn.commit()


async def run(db_path: str, apply: bool) -> None:
    _ensure_columns(db_path)
    suspects = _suspect_rows(db_path)
    print(f"Suspect booked gains (settlement-priced or >={GAIN_MULTIPLE}x entry): {len(suspects)}\n")

    verdicts: dict[str, list] = {"phantom": [], "genuine": [], "unverified": []}
    for row in suspects:
        market = await _fetch_market(row["market_id"])
        verdict = classify_fill(row["token_id"], market)
        verdicts[verdict].append(row)
        print(f"  [{verdict.upper():10}] trade {row['trade_id']:<5} "
              f"entry {row['entry_price']} -> booked exit {row['exit_price']} "
              f"(+${row['exit_pnl']}) {str(row['question'])[:42]}")

    if not apply:
        print("\n(read-only audit — rerun with --apply to quarantine phantom rows)")
        return

    print()
    for row in verdicts["phantom"]:
        reason = (f"phantom fill: booked exit {row['exit_price']} but CLOB says our "
                  f"token lost (verified via winner flag); true result -cost_basis")
        _apply_one(db_path, row, reason)
        print(f"  quarantined trade {row['trade_id']}: "
              f"pnl +{row['exit_pnl']} -> -{row['cost_basis']}")
    print(f"\nQuarantined {len(verdicts['phantom'])} rows "
          f"({len(verdicts['genuine'])} genuine, {len(verdicts['unverified'])} left untouched)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit/quarantine phantom exit fills")
    parser.add_argument("--audit", action="store_true", help="read-only verdicts")
    parser.add_argument("--apply", action="store_true", help="quarantine phantom rows")
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()
    if not (args.audit or args.apply):
        parser.error("choose --audit or --apply")
    asyncio.run(run(args.db, apply=args.apply))


if __name__ == "__main__":
    main()
