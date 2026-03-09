"""CLI entry point for Polymarket Scanner."""

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

from .scanner import PolymarketScanner
from .database import get_recent_opportunities


def setup_logging(verbose: bool = False) -> None:
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def run_scan(args: argparse.Namespace) -> int:
    """Run the scanner."""
    scanner = PolymarketScanner(
        dry_run=args.dry_run,
        default_size=Decimal(str(args.size)),
    )
    
    await scanner.initialize()
    
    if args.watch:
        await scanner.watch(
            interval_seconds=args.interval,
        )
    else:
        opportunities = await scanner.scan_all_markets(
            limit=args.limit,
            active_only=not args.include_closed,
        )
        
        if not opportunities:
            print("\n📭 No opportunities detected in this scan.")
    
    return 0


def show_recent(args: argparse.Namespace) -> int:
    """Show recent opportunities from database."""
    opportunities = get_recent_opportunities(limit=args.limit)
    
    if not opportunities:
        print("No recent opportunities in database.")
        return 0
    
    print(f"\n📜 Recent {len(opportunities)} opportunities:\n")
    
    for opp in opportunities:
        print(f"  [{opp['timestamp'][:19]}] {opp['opportunity_type']}")
        print(f"    Profit: ${opp['profit_bound']}")
        if opp.get('question'):
            print(f"    Market: {opp['question'][:50]}...")
        print(f"    {opp['rationale']}")
        print()
    
    return 0


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Polymarket Arbitrage Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick dry-run scan of 10 markets
  python -m polymarket_scanner.main --dry-run --limit 10
  
  # Continuous monitoring
  python -m polymarket_scanner.main --watch --interval 60
  
  # Show recent opportunities
  python -m polymarket_scanner.main --recent
        """
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Run in dry-run mode (no trading, default: True)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of markets to scan",
    )
    parser.add_argument(
        "--size",
        type=float,
        default=10.0,
        help="Default position size in USD (default: 10)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously watch for opportunities",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Seconds between scans in watch mode (default: 60)",
    )
    parser.add_argument(
        "--include-closed",
        action="store_true",
        help="Include closed markets in scan",
    )
    parser.add_argument(
        "--recent",
        action="store_true",
        help="Show recent opportunities from database",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    
    if args.recent:
        return show_recent(args)
    
    try:
        return asyncio.run(run_scan(args))
    except KeyboardInterrupt:
        print("\n\n🛑 Interrupted by user")
        return 0
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
