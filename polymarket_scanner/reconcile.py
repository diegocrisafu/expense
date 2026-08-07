"""Reconcile the position ledgers without running the bot.

The bot tracks positions in three places: `positions` (resolution tracker),
`managed_positions` (exit manager), and `trade_history` (P&L ledger).  They
can disagree: a position closed by the exit manager stays OPEN in `positions`
forever ("ghost"), and nothing ever reported the true open count.

Usage:
    python -m polymarket_scanner.reconcile
"""

import argparse
from datetime import datetime, timezone

from .config import DB_PATH
from .database import get_connection

# trade_history statuses that mean "this trade is finished"
_CLOSED_TH = ("WON", "LOST", "CANCELLED", "QUARANTINED")


def classify_open_positions(db_path: str = DB_PATH) -> tuple[list[dict], list[dict]]:
    """Split `positions` rows with status='OPEN' into (truly_open, ghosts).

    A ghost is a row still OPEN in `positions` although the same trade_id is
    already settled in trade_history or closed in managed_positions.
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.trade_id, p.market_id, p.token_id, p.side, p.entry_price,
                   p.size, th.status, th.timestamp, th.market_question,
                   mp.status, mp.exit_reason, mp.closed_at
            FROM positions p
            LEFT JOIN trade_history th ON th.id = p.trade_id
            LEFT JOIN managed_positions mp ON mp.trade_id = p.trade_id
            WHERE p.status = 'OPEN'
            ORDER BY th.timestamp
        """)
        rows = cursor.fetchall()

    truly_open, ghosts = [], []
    for (trade_id, market_id, token_id, side, entry, size,
         th_status, th_ts, question, mp_status, exit_reason, closed_at) in rows:
        closed_in_th = th_status is not None and th_status.upper() in _CLOSED_TH
        closed_in_mp = mp_status is not None and mp_status.upper() == "CLOSED"
        info = {
            "trade_id": trade_id,
            "market_id": market_id,
            "token_id": token_id,
            "side": side,
            "entry_price": entry,
            "size": size,
            "opened_at": th_ts,
            "question": question or "",
            "trade_history_status": th_status,
            "managed_status": mp_status,
            "exit_reason": exit_reason,
            "closed_at": closed_at,
        }
        if closed_in_th or closed_in_mp:
            ghosts.append(info)
        else:
            truly_open.append(info)
    return truly_open, ghosts


def count_truly_open(db_path: str = DB_PATH) -> int:
    """True open-position count derived from the DB (ghosts excluded)."""
    return len(classify_open_positions(db_path)[0])


def _age_days(ts: str | None) -> str:
    if not ts:
        return "?"
    try:
        opened = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except ValueError:
        return "?"
    return f"{(datetime.now(timezone.utc) - opened).days}d"


def print_report(db_path: str = DB_PATH) -> None:
    truly_open, ghosts = classify_open_positions(db_path)

    print("=" * 78)
    print("POSITION RECONCILIATION")
    print("=" * 78)

    print(f"\nTruly open positions: {len(truly_open)}")
    for p in truly_open:
        print(f"  #{p['trade_id']:<5} {p['side']:<8} entry {p['entry_price']:<8} "
              f"age {_age_days(p['opened_at']):<5} {p['question'][:48]}")

    print(f"\nGhost OPEN rows in `positions` (already closed elsewhere): {len(ghosts)}")
    for p in ghosts:
        where = []
        if p["trade_history_status"] and p["trade_history_status"].upper() in _CLOSED_TH:
            where.append(f"trade_history={p['trade_history_status']}")
        if p["managed_status"] and p["managed_status"].upper() == "CLOSED":
            where.append(f"managed={p['exit_reason'] or 'CLOSED'}@{p['closed_at']}")
        print(f"  #{p['trade_id']:<5} {p['side']:<8} entry {p['entry_price']:<8} "
              f"age {_age_days(p['opened_at']):<5} {p['question'][:38]:<40} [{', '.join(where)}]")

    print(f"\nSummary: {len(truly_open)} truly open, {len(ghosts)} ghosts, "
          f"{len(truly_open) + len(ghosts)} total OPEN rows in `positions`")
    print("=" * 78)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile position ledgers")
    parser.add_argument("--db", default=DB_PATH, help="Path to the SQLite database")
    args = parser.parse_args()
    print_report(args.db)


if __name__ == "__main__":
    main()
