"""Main scanner orchestration."""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

from .config import MAX_CAPITAL_PER_EVENT, DB_PATH
from .database import init_database, upsert_market, save_opportunity
from .detection import scan_market_for_opportunities
from .ingestion.gamma import GammaAPIClient, parse_market
from .ingestion.clob import CLOBAPIClient
from .models import Market, Opportunity, OrderBook

logger = logging.getLogger(__name__)


class PolymarketScanner:
    """Main scanner for Polymarket arbitrage detection."""
    
    def __init__(
        self,
        dry_run: bool = True,
        db_path: str = DB_PATH,
        default_size: Decimal = Decimal("10"),
    ):
        self.dry_run = dry_run
        self.db_path = db_path
        self.default_size = default_size
        self.gamma = GammaAPIClient()
        self.clob = CLOBAPIClient()
        self._error_count = 0
        self._max_consecutive_errors = 3
    
    async def initialize(self) -> None:
        """Initialize scanner (database, etc.)."""
        init_database(self.db_path)
        logger.info(f"Scanner initialized (dry_run={self.dry_run})")
    
    async def fetch_market_with_orderbooks(
        self,
        market: Market,
    ) -> dict[str, OrderBook]:
        """Fetch order books for all outcomes in a market.
        
        Args:
            market: Market with outcomes
            
        Returns:
            Mapping of outcome_id -> OrderBook
        """
        orderbooks = {}
        
        for outcome in market.outcomes:
            if not outcome.outcome_id:
                continue
                
            book = await self.clob.get_orderbook(outcome.outcome_id)
            if book:
                orderbooks[outcome.outcome_id] = book
                outcome.orderbook = book
        
        return orderbooks
    
    async def scan_single_market(self, market: Market) -> list[Opportunity]:
        """Scan a single market for opportunities.
        
        Args:
            market: Market to scan
            
        Returns:
            List of detected opportunities
        """
        try:
            # Fetch order books
            orderbooks = await self.fetch_market_with_orderbooks(market)
            
            if len(orderbooks) < len(market.outcomes):
                logger.debug(f"Incomplete orderbooks for {market.market_id}")
                return []
            
            # Scan for opportunities
            opportunities = scan_market_for_opportunities(
                market, orderbooks, self.default_size
            )
            
            # Log and save opportunities
            for opp in opportunities:
                self._log_opportunity(opp, market)
                if not self.dry_run:
                    save_opportunity(opp, self.db_path)
                else:
                    # Still save in dry run for analysis
                    save_opportunity(opp, self.db_path)
            
            self._error_count = 0  # Reset on success
            return opportunities
            
        except Exception as e:
            logger.error(f"Error scanning market {market.market_id}: {e}")
            self._error_count += 1
            if self._error_count >= self._max_consecutive_errors:
                logger.warning("Too many consecutive errors, pausing...")
            return []
    
    def _log_opportunity(self, opp: Opportunity, market: Market) -> None:
        """Log a detected opportunity."""
        prefix = "[DRY RUN] " if self.dry_run else ""
        print(f"\n{prefix}🎯 OPPORTUNITY DETECTED")
        print(f"  Market: {market.question[:60]}...")
        print(f"  Type: {opp.opportunity_type.value}")
        print(f"  Profit Bound: ${opp.profit_bound:.4f}")
        print(f"  Required Size: {opp.required_size}")
        print(f"  Liquidity: ${opp.liquidity_available:.2f}")
        print(f"  Rationale: {opp.rationale}")
        print(f"  Timestamp: {opp.timestamp.isoformat()}")

    async def scan_all_markets(
        self,
        limit: Optional[int] = None,
        active_only: bool = True,
    ) -> list[Opportunity]:
        """Scan all markets for opportunities.
        
        Args:
            limit: Maximum number of markets to scan
            active_only: Only scan active, non-closed markets
            
        Returns:
            All detected opportunities
        """
        all_opportunities = []
        market_count = 0
        
        print(f"\n📊 Starting market scan...")
        print(f"  Dry run: {self.dry_run}")
        print(f"  Default size: {self.default_size}")
        if limit:
            print(f"  Limit: {limit} markets")
        
        async for market_data in self.gamma.iter_all_markets(active=active_only):
            if limit and market_count >= limit:
                break
            
            market = parse_market(market_data)
            
            # Skip markets without outcomes
            if not market.outcomes:
                continue
            
            # Store market in DB
            upsert_market(market, self.db_path)
            
            # Scan for opportunities
            opps = await self.scan_single_market(market)
            all_opportunities.extend(opps)
            
            market_count += 1
            
            if market_count % 10 == 0:
                print(f"  Scanned {market_count} markets, found {len(all_opportunities)} opportunities...")
        
        print(f"\n✅ Scan complete!")
        print(f"  Markets scanned: {market_count}")
        print(f"  Opportunities found: {len(all_opportunities)}")
        
        return all_opportunities
    
    async def watch(
        self,
        market_ids: Optional[list[str]] = None,
        interval_seconds: int = 60,
    ) -> None:
        """Continuously watch markets for opportunities.
        
        Args:
            market_ids: Specific markets to watch (None = all active)
            interval_seconds: Time between scans
        """
        print(f"\n👁️ Starting continuous watch (interval={interval_seconds}s)...")
        print("Press Ctrl+C to stop\n")
        
        try:
            while True:
                if market_ids:
                    for mid in market_ids:
                        market_data = await self.gamma.get_market(mid)
                        if market_data:
                            market = parse_market(market_data)
                            await self.scan_single_market(market)
                else:
                    await self.scan_all_markets(limit=50)  # Reasonable batch size
                
                await asyncio.sleep(interval_seconds)
                
        except KeyboardInterrupt:
            print("\n\n🛑 Watch stopped by user")
