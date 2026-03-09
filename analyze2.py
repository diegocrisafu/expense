import sqlite3
conn = sqlite3.connect('polymarket_scanner.db')
conn.row_factory = sqlite3.Row

print('=== CLOSED POSITIONS BY REASON ===')
rows = conn.execute(
    "SELECT exit_reason, COUNT(*) as cnt, "
    "COALESCE(SUM(CAST(exit_pnl AS FLOAT)), 0) as total_pnl "
    "FROM managed_positions WHERE status = 'CLOSED' GROUP BY exit_reason"
).fetchall()
for r in rows:
    print(f"  {r['exit_reason']}: {r['cnt']} pos, total PnL ${r['total_pnl']:.2f}")

print()
print('=== INDIVIDUAL CLOSED POSITIONS ===')
rows = conn.execute(
    "SELECT market_question, side, "
    "CAST(entry_price AS FLOAT) as entry, "
    "CAST(exit_price AS FLOAT) as exit_p, "
    "CAST(size AS FLOAT) as size, "
    "COALESCE(CAST(exit_pnl AS FLOAT), 0) as pnl, "
    "exit_reason "
    "FROM managed_positions WHERE status = 'CLOSED' ORDER BY opened_at"
).fetchall()
total_pnl = 0
wins = 0
losses = 0
for r in rows:
    pnl = r['pnl']
    total_pnl += pnl
    if pnl > 0:
        wins += 1
    else:
        losses += 1
    q = (r['market_question'] or '?')[:50]
    emoji = '+' if pnl > 0 else '-'
    entry = r['entry'] or 0
    exit_p = r['exit_p'] or 0
    size = r['size'] or 0
    print(f"  {emoji} {r['exit_reason'] or 'NONE':<14} {r['side']:<4} "
          f"entry=${entry:.3f} exit=${exit_p:.3f} "
          f"shares={size:.1f} PnL=${pnl:+.4f} | {q}")

print(f"\nTotal realized PnL: ${total_pnl:.2f}")
if wins + losses > 0:
    print(f"W/L: {wins}/{losses} ({100*wins/(wins+losses):.0f}% win rate)")

print()
print('=== ACTIVE POSITIONS ===')
rows = conn.execute(
    "SELECT id, market_question, side, "
    "CAST(entry_price AS FLOAT) as entry, "
    "CAST(current_price AS FLOAT) as cur, "
    "CAST(size AS FLOAT) as size, "
    "CAST(cost_basis AS FLOAT) as cost "
    "FROM managed_positions WHERE status = 'ACTIVE'"
).fetchall()
for r in rows:
    q = (r['market_question'] or '?')[:50]
    print(f"  #{r['id']} {r['side']} entry=${r['entry']:.3f} cur=${r['cur']:.3f} "
          f"shares={r['size']:.1f} cost=${r['cost']:.2f} | {q}")

conn.close()
