"""Performance Dashboard — CLI & Web.

Provides real-time visibility into:
- Current balance and P&L
- Active positions with live unrealized P&L
- Trade history with win/loss breakdown
- Strategy performance comparison
- Capital utilization metrics

Two modes:
1. CLI: Rich terminal display (runs with --dashboard flag)
2. Web: Lightweight HTTP server at localhost:8080
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from typing import Optional

from .database import get_connection, DB_PATH
from .trading_config import STARTING_BALANCE, STOP_LOSS_THRESHOLD

logger = logging.getLogger(__name__)


# =====================================================================
# Data collection
# =====================================================================
class DashboardData:
    """Collects all data needed for the dashboard from the DB."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH

    def _safe_query(self, query: str, params: tuple = ()) -> list:
        """Run a query safely, returning empty list on error."""
        try:
            with get_connection(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.debug(f"Dashboard query error: {e}")
            return []

    def _safe_query_one(self, query: str, params: tuple = ()) -> Optional[dict]:
        rows = self._safe_query(query, params)
        return rows[0] if rows else None

    # --- Balance ---
    def get_current_balance(self) -> Decimal:
        """Approximate balance from trade history."""
        row = self._safe_query_one("""
            SELECT 
                COALESCE(SUM(CASE WHEN mode = 'PAPER' THEN CAST(profit AS FLOAT) ELSE 0 END), 0) as paper_profit,
                COALESCE(SUM(CASE WHEN mode != 'PAPER' THEN CAST(profit AS FLOAT) ELSE 0 END), 0) as live_profit
            FROM trades
        """)
        if row:
            return STARTING_BALANCE + Decimal(str(row["paper_profit"])) + Decimal(str(row["live_profit"]))
        return STARTING_BALANCE

    # --- Trades ---
    def get_trade_summary(self) -> dict:
        """Overall trade statistics."""
        row = self._safe_query_one("""
            SELECT 
                COUNT(*) as total_trades,
                COALESCE(SUM(CAST(profit AS FLOAT)), 0) as total_profit,
                COALESCE(SUM(CAST(size AS FLOAT)), 0) as total_volume,
                COALESCE(SUM(CASE WHEN CAST(profit AS FLOAT) > 0 THEN 1 ELSE 0 END), 0) as wins,
                COALESCE(SUM(CASE WHEN CAST(profit AS FLOAT) < 0 THEN 1 ELSE 0 END), 0) as losses,
                COALESCE(SUM(CASE WHEN CAST(profit AS FLOAT) = 0 THEN 1 ELSE 0 END), 0) as pending,
                MIN(timestamp) as first_trade,
                MAX(timestamp) as last_trade
            FROM trades
        """)
        return row or {}

    def get_recent_trades(self, limit: int = 20) -> list[dict]:
        """Last N trades."""
        return self._safe_query("""
            SELECT timestamp, trade_type, market_or_token, 
                   CAST(size AS FLOAT) as size, 
                   CAST(profit AS FLOAT) as profit, mode
            FROM trades ORDER BY timestamp DESC LIMIT ?
        """, (limit,))

    # --- Positions (managed) ---
    def get_active_positions(self) -> list[dict]:
        return self._safe_query("""
            SELECT market_question, side,
                   CAST(entry_price AS FLOAT) as entry_price,
                   CAST(current_price AS FLOAT) as current_price,
                   CAST(size AS FLOAT) as size,
                   CAST(cost_basis AS FLOAT) as cost_basis,
                   CAST(take_profit_price AS FLOAT) as tp,
                   CAST(stop_loss_price AS FLOAT) as sl,
                   opened_at,
                   (CAST(current_price AS FLOAT) - CAST(entry_price AS FLOAT)) 
                     * CAST(size AS FLOAT) as unrealized_pnl
            FROM managed_positions WHERE status = 'ACTIVE'
            ORDER BY opened_at DESC
        """)

    def get_closed_positions(self, limit: int = 20) -> list[dict]:
        return self._safe_query("""
            SELECT market_question, side, exit_reason,
                   CAST(entry_price AS FLOAT) as entry_price,
                   CAST(exit_price AS FLOAT) as exit_price,
                   CAST(size AS FLOAT) as size,
                   CAST(exit_pnl AS FLOAT) as pnl,
                   opened_at, closed_at
            FROM managed_positions WHERE status = 'CLOSED'
            ORDER BY closed_at DESC LIMIT ?
        """, (limit,))

    # --- Strategy breakdown ---
    def get_strategy_performance(self) -> list[dict]:
        return self._safe_query("""
            SELECT strategy,
                   total_trades, wins, losses,
                   CAST(total_profit AS FLOAT) as total_profit,
                   CAST(total_loss AS FLOAT) as total_loss,
                   last_updated
            FROM strategy_performance
            ORDER BY total_trades DESC
        """)

    # --- Trade history (learning engine) ---
    def get_trade_history(self, limit: int = 30) -> list[dict]:
        return self._safe_query("""
            SELECT timestamp, strategy, market_question, side,
                   CAST(entry_price AS FLOAT) as entry_price,
                   CAST(exit_price AS FLOAT) as exit_price,
                   CAST(size AS FLOAT) as size,
                   CAST(pnl AS FLOAT) as pnl,
                   status
            FROM trade_history
            ORDER BY timestamp DESC LIMIT ?
        """, (limit,))

    # --- Today's P&L ---
    def get_today_pnl(self) -> dict:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        row = self._safe_query_one("""
            SELECT 
                COALESCE(SUM(CAST(exit_pnl AS FLOAT)), 0) as realized,
                COUNT(*) as trades_closed
            FROM managed_positions
            WHERE status = 'CLOSED' AND DATE(closed_at) = ?
        """, (today,))

        active_row = self._safe_query_one("""
            SELECT COALESCE(SUM(
                (CAST(current_price AS FLOAT) - CAST(entry_price AS FLOAT)) 
                * CAST(size AS FLOAT)
            ), 0) as unrealized
            FROM managed_positions WHERE status = 'ACTIVE'
        """)

        return {
            "realized": row.get("realized", 0) if row else 0,
            "unrealized": active_row.get("unrealized", 0) if active_row else 0,
            "trades_closed": row.get("trades_closed", 0) if row else 0,
        }

    # --- All bets with rationale ---
    def get_all_bets(self, limit: int = 100) -> list[dict]:
        """Get all bets with strategy info for the bet log."""
        return self._safe_query("""
            SELECT th.id, th.timestamp, th.strategy, th.market_question,
                   th.category, th.side,
                   CAST(th.entry_price AS FLOAT) as entry_price,
                   CAST(th.exit_price AS FLOAT) as exit_price,
                   CAST(th.size AS FLOAT) as size,
                   CAST(th.pnl AS FLOAT) as pnl,
                   th.status,
                   mp.market_question as mp_question,
                   CAST(mp.current_price AS FLOAT) as current_price,
                   CAST(mp.take_profit_price AS FLOAT) as tp,
                   CAST(mp.stop_loss_price AS FLOAT) as sl,
                   mp.exit_reason,
                   mp.status as position_status
            FROM trade_history th
            LEFT JOIN managed_positions mp ON th.id = mp.trade_id
            ORDER BY th.timestamp DESC LIMIT ?
        """, (limit,))

    # --- Full snapshot for web dashboard ---
    def get_full_snapshot(self) -> dict:
        """Everything the dashboard needs in one shot."""
        today = self.get_today_pnl()
        summary = self.get_trade_summary()
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "balance": float(self.get_current_balance()),
            "starting_balance": float(STARTING_BALANCE),
            "stop_loss": float(STOP_LOSS_THRESHOLD),
            "today": today,
            "summary": summary,
            "active_positions": self.get_active_positions(),
            "closed_positions": self.get_closed_positions(20),
            "strategies": self.get_strategy_performance(),
            "recent_trades": self.get_recent_trades(20),
            "trade_history": self.get_trade_history(50),
            "all_bets": self.get_all_bets(100),
        }


# =====================================================================
# CLI Dashboard
# =====================================================================
def print_cli_dashboard(db_path: str = None):
    """Print a rich CLI dashboard to the terminal."""
    data = DashboardData(db_path)

    today = data.get_today_pnl()
    summary = data.get_trade_summary()
    balance = data.get_current_balance()
    active = data.get_active_positions()
    strategies = data.get_strategy_performance()

    total_pnl = today["realized"] + today["unrealized"]
    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"

    print("\033[2J\033[H")  # clear screen
    print("=" * 70)
    print("  🤖 POLYMARKET TRADING BOT — PERFORMANCE DASHBOARD")
    print("=" * 70)
    print(f"  ⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print()

    # --- Balance ---
    print(f"  💰 Balance:       ${balance:.2f}  (started at ${STARTING_BALANCE})")
    all_time_pnl = float(balance - STARTING_BALANCE)
    print(f"  📊 All-Time P&L:  ${all_time_pnl:+.2f}")
    print(f"  {pnl_emoji} Today's P&L:    ${total_pnl:+.2f}  "
          f"(realized ${today['realized']:+.2f} | unrealized ${today['unrealized']:+.2f})")
    print(f"  🛡️  Stop Loss:     ${STOP_LOSS_THRESHOLD}")
    print()

    # --- Trade Stats ---
    total = summary.get("total_trades", 0)
    wins = summary.get("wins", 0)
    losses = summary.get("losses", 0)
    win_rate = wins / max(wins + losses, 1) * 100
    print("  ─── TRADE STATS ───")
    print(f"  Total: {total}  |  Wins: {wins}  |  Losses: {losses}  |  Win Rate: {win_rate:.0f}%")
    print(f"  Volume: ${summary.get('total_volume', 0):.2f}")
    print()

    # --- Active Positions ---
    print(f"  ─── ACTIVE POSITIONS ({len(active)}) ───")
    if active:
        for p in active:
            upnl = p.get("unrealized_pnl", 0)
            emoji = "📈" if upnl >= 0 else "📉"
            q = (p.get("market_question") or "?")[:42]
            print(
                f"  {emoji} {q:<42} "
                f"Entry=${p['entry_price']:.3f} Now=${p['current_price']:.3f} "
                f"PnL=${upnl:+.3f} | TP=${p['tp']:.3f} SL=${p['sl']:.3f}"
            )
    else:
        print("  (none)")
    print()

    # --- Strategy Performance ---
    print("  ─── STRATEGY PERFORMANCE ───")
    if strategies:
        print(f"  {'Strategy':<15} {'Trades':>7} {'Wins':>5} {'Losses':>6} {'Profit':>9} {'Loss':>9}")
        for s in strategies:
            print(
                f"  {s.get('strategy', '?'):<15} "
                f"{s.get('total_trades', 0):>7} "
                f"{s.get('wins', 0):>5} "
                f"{s.get('losses', 0):>6} "
                f"${s.get('total_profit', 0):>8.2f} "
                f"${s.get('total_loss', 0):>8.2f}"
            )
    else:
        print("  (no data yet)")
    print()
    print("=" * 70)
    print("  Press Ctrl+C to stop  |  Refreshes every 30s")
    print("=" * 70)


async def run_cli_dashboard(db_path: str = None, interval: int = 30):
    """Run the CLI dashboard in a loop."""
    print("Starting dashboard... (Ctrl+C to stop)")
    try:
        while True:
            print_cli_dashboard(db_path)
            await asyncio.sleep(interval)
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


# =====================================================================
# Web Dashboard — "Roger the Polymarket Bot"
# =====================================================================
_roger_path = os.path.join(os.path.dirname(__file__), '..', 'roger.html')
ROGER_HTML = "<h1>roger.html not found — run from project root</h1>"


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the web dashboard."""

    db_path = DB_PATH  # class-level so it can be set before starting

    def log_message(self, format, *args):
        pass  # silence default logging

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html" or self.path == "/roger":
            # Serve the Roger website
            try:
                html_path = os.path.join(os.path.dirname(__file__), '..', 'roger.html')
                with open(html_path, 'r') as f:
                    html = f.read()
            except Exception:
                html = ROGER_HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        elif self.path == "/api/data":
            data = DashboardData(self.db_path)
            snapshot = data.get_full_snapshot()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(snapshot, default=str).encode())

        else:
            self.send_response(404)
            self.end_headers()


def start_web_dashboard(port: int = 8080, db_path: str = None):
    """Start the web dashboard in a background thread.

    Returns the thread so it can be joined if needed.
    """
    DashboardHandler.db_path = db_path or DB_PATH
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"  🌐 Dashboard running at http://localhost:{port}")
    logger.info(f"Web dashboard started on port {port}")
    return thread
