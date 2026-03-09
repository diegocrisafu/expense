"""Deep analysis of all trades to understand why the bot lost $24."""
import sqlite3
import json
from collections import defaultdict

conn = sqlite3.connect('polymarket_scanner.db')
conn.row_factory = sqlite3.Row

print("=" * 70)
print("TRADE LOSS ANALYSIS")
print("=" * 70)

# 1. Trade history breakdown
print("\n=== TRADE HISTORY BY STATUS ===")
rows = conn.execute("""
    SELECT status, COUNT(*) as cnt, 
           SUM(CAST(size AS FLOAT)) as total_size
    FROM trade_history 
    GROUP BY status
""").fetchall()
for r in rows:
    print(f"  {r['status']}: {r['cnt']} trades, total size ${r['total_size']:.2f}")

# 2. Non-cancelled trades by strategy
print("\n=== NON-CANCELLED TRADES BY STRATEGY ===")
rows = conn.execute("""
    SELECT strategy, COUNT(*) as cnt,
           SUM(CAST(size AS FLOAT)) as total_size,
           AVG(CAST(entry_price AS FLOAT)) as avg_price,
           MIN(CAST(entry_price AS FLOAT)) as min_price,
           MAX(CAST(entry_price AS FLOAT)) as max_price
    FROM trade_history 
    WHERE status != 'CANCELLED'
    GROUP BY strategy
    ORDER BY total_size DESC
""").fetchall()
for r in rows:
    print(f"  {r['strategy']}: {r['cnt']} trades, ${r['total_size']:.2f} deployed, "
          f"avg price ${r['avg_price']:.3f} (range ${r['min_price']:.3f}-${r['max_price']:.3f})")

# 3. All non-cancelled trades with details
print("\n=== ALL NON-CANCELLED TRADES (DETAIL) ===")
rows = conn.execute("""
    SELECT id, strategy, market_question, side, 
           CAST(entry_price AS FLOAT) as price,
           CAST(size AS FLOAT) as size,
           status, timestamp
    FROM trade_history 
    WHERE status != 'CANCELLED'
    ORDER BY timestamp
""").fetchall()
total_deployed = 0
for r in rows:
    cost = r['price'] * r['size']
    total_deployed += cost
    q = r['market_question'][:55] if r['market_question'] else '?'
    print(f"  #{r['id']:>4} | {r['strategy']:<12} | {r['side']:<4} @ ${r['price']:.3f} "
          f"| size=${r['size']:.2f} | cost=${cost:.2f} | {q}")

print(f"\n  Total deployed: ${total_deployed:.2f}")

# 4. Managed positions
print("\n=== MANAGED POSITIONS (ALL) ===")
rows = conn.execute("""
    SELECT status, COUNT(*) as cnt,
           SUM(CAST(cost_basis AS FLOAT)) as total_cost,
           SUM(CAST(exit_pnl AS FLOAT)) as total_pnl
    FROM managed_positions
    GROUP BY status
""").fetchall()
for r in rows:
    pnl = r['total_pnl'] or 0
    print(f"  {r['status']}: {r['cnt']} positions, cost ${r['total_cost']:.2f}, PnL ${pnl:.2f}")

# 5. Closed position details
print("\n=== CLOSED POSITIONS (WHY?) ===")
rows = conn.execute("""
    SELECT exit_reason, COUNT(*) as cnt,
           SUM(CAST(exit_pnl AS FLOAT)) as total_pnl,
           AVG(CAST(exit_pnl AS FLOAT)) as avg_pnl
    FROM managed_positions
    WHERE status = 'CLOSED'
    GROUP BY exit_reason
""").fetchall()
for r in rows:
    print(f"  {r['exit_reason']}: {r['cnt']} positions, total PnL ${r['total_pnl']:.2f}, avg ${r['avg_pnl']:.3f}")

# 6. Individual closed positions
print("\n=== INDIVIDUAL CLOSED POSITIONS ===")
rows = conn.execute("""
    SELECT id, market_question, side, 
           CAST(entry_price AS FLOAT) as entry,
           CAST(exit_price AS FLOAT) as exit_p,
           CAST(size AS FLOAT) as size,
           CAST(exit_pnl AS FLOAT) as pnl,
           exit_reason, opened_at, closed_at
    FROM managed_positions
    WHERE status = 'CLOSED'
    ORDER BY opened_at
""").fetchall()
total_pnl = 0
for r in rows:
    pnl = r['pnl'] or 0
    total_pnl += pnl
    q = r['market_question'][:45] if r['market_question'] else '?'
    emoji = '✅' if pnl > 0 else '❌'
    print(f"  {emoji} #{r['id']:>3} | {r['exit_reason']:<14} | {r['side']:<4} "
          f"entry=${r['entry']:.3f} exit=${r['exit_p']:.3f} "
          f"| shares={r['size']:.1f} | PnL=${pnl:+.3f} | {q}")

print(f"\n  Total realized PnL: ${total_pnl:.2f}")

# 7. Polymarket actual trades
print("\n=== TRADES TABLE (actual executed) ===")
rows = conn.execute("""
    SELECT trade_type, mode, COUNT(*) as cnt,
           SUM(CAST(size AS FLOAT)) as total_size,
           SUM(CAST(profit AS FLOAT)) as total_profit
    FROM trades
    GROUP BY trade_type, mode
""").fetchall()
for r in rows:
    print(f"  {r['trade_type']} ({r['mode']}): {r['cnt']} trades, "
          f"size ${r['total_size']:.2f}, profit ${r['total_profit']:.2f}")

# 8. Markets traded
print("\n=== UNIQUE MARKETS TRADED ===")
rows = conn.execute("""
    SELECT market_question, COUNT(*) as cnt,
           SUM(CAST(size AS FLOAT)) as total_size
    FROM trade_history 
    WHERE status != 'CANCELLED'
    GROUP BY market_question
    ORDER BY total_size DESC
    LIMIT 15
""").fetchall()
for r in rows:
    q = r['market_question'][:60] if r['market_question'] else '?'
    print(f"  {r['cnt']}x ${r['total_size']:.2f} | {q}")

conn.close()
