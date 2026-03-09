"""Gamma API client for market discovery and metadata."""

import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncGenerator, Optional

import httpx

from ..config import GAMMA_API_BASE, GAMMA_RATE_LIMIT, DEFAULT_PAGE_SIZE, MAX_PAGES
from ..models import Market, Outcome, Event

logger = logging.getLogger(__name__)


class GammaAPIClient:
    """Client for Polymarket Gamma API."""
    
    def __init__(self, base_url: str = GAMMA_API_BASE):
        self.base_url = base_url
        self._last_request_time = 0.0
        self._request_interval = 1.0 / GAMMA_RATE_LIMIT
    
    async def _rate_limit(self) -> None:
        """Enforce rate limiting."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_time
        if elapsed < self._request_interval:
            await asyncio.sleep(self._request_interval - elapsed)
        self._last_request_time = asyncio.get_event_loop().time()
    
    async def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """Make a GET request to the API."""
        await self._rate_limit()
        
        url = f"{self.base_url}{endpoint}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    
    async def get_markets(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[dict]:
        """Fetch markets from the Gamma API.
        
        Args:
            active: Filter for active markets
            closed: Filter for closed markets  
            limit: Number of markets per page
            offset: Pagination offset
            
        Returns:
            List of market dictionaries
        """
        params = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
        }
        
        try:
            data = await self._get("/markets", params)
            return data if isinstance(data, list) else []
        except httpx.HTTPError as e:
            logger.error(f"Error fetching markets: {e}")
            return []
    
    async def iter_all_markets(
        self,
        active: bool = True,
        closed: bool = False,
    ) -> AsyncGenerator[dict, None]:
        """Iterate through all markets with pagination.
        
        Yields:
            Market dictionaries
        """
        offset = 0
        page_count = 0
        
        while page_count < MAX_PAGES:
            markets = await self.get_markets(
                active=active,
                closed=closed,
                limit=DEFAULT_PAGE_SIZE,
                offset=offset,
            )
            
            if not markets:
                break
            
            for market in markets:
                yield market
            
            offset += len(markets)
            page_count += 1
            
            if len(markets) < DEFAULT_PAGE_SIZE:
                break
    
    async def get_market(self, market_id: str) -> Optional[dict]:
        """Fetch a single market by ID."""
        try:
            return await self._get(f"/markets/{market_id}")
        except httpx.HTTPError as e:
            logger.error(f"Error fetching market {market_id}: {e}")
            return None
    
    async def get_events(
        self,
        active: bool = True,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[dict]:
        """Fetch events from the Gamma API."""
        params = {
            "active": str(active).lower(),
            "limit": limit,
            "offset": offset,
        }
        
        try:
            data = await self._get("/events", params)
            return data if isinstance(data, list) else []
        except httpx.HTTPError as e:
            logger.error(f"Error fetching events: {e}")
            return []
    
    async def get_event(self, event_id: str) -> Optional[dict]:
        """Fetch a single event by ID."""
        try:
            return await self._get(f"/events/{event_id}")
        except httpx.HTTPError as e:
            logger.error(f"Error fetching event {event_id}: {e}")
            return None


def parse_market(data: dict) -> Market:
    """Parse raw API response into a Market object."""
    # Parse outcomes from clobTokenIds and outcomes arrays
    outcomes = []
    
    # Polymarket returns clobTokenIds as a JSON string (not a list!)
    # and outcomes as an array of outcome strings (e.g., ["Yes", "No"])
    clob_token_ids_raw = data.get("clobTokenIds") or "[]"
    outcome_strings_raw = data.get("outcomes") or []
    
    # Parse clobTokenIds if it's a string
    if isinstance(clob_token_ids_raw, str):
        try:
            clob_token_ids = json.loads(clob_token_ids_raw)
        except json.JSONDecodeError:
            clob_token_ids = []
    else:
        clob_token_ids = clob_token_ids_raw or []
    
    # Parse outcomes if it's a string
    if isinstance(outcome_strings_raw, str):
        try:
            outcome_strings = json.loads(outcome_strings_raw)
        except json.JSONDecodeError:
            outcome_strings = []
    else:
        outcome_strings = outcome_strings_raw or []
    
    # The market_id comes from conditionId (camelCase in API)
    market_id = data.get("conditionId", data.get("condition_id", str(data.get("id", ""))))
    
    # Match token IDs to outcome strings
    if clob_token_ids and outcome_strings:
        for i, outcome_text in enumerate(outcome_strings):
            token_id = clob_token_ids[i] if i < len(clob_token_ids) else ""
            outcomes.append(Outcome(
                outcome_id=token_id,
                market_id=market_id,
                text=outcome_text,
            ))
    elif outcome_strings:
        # No token IDs, just create outcomes with placeholder IDs
        for i, outcome_text in enumerate(outcome_strings):
            outcomes.append(Outcome(
                outcome_id=f"{market_id}_{i}",
                market_id=market_id,
                text=outcome_text,
            ))
    
    # Parse end time (API uses endDateIso or endDate)
    end_time = None
    end_date_str = data.get("endDateIso") or data.get("endDate") or data.get("end_date_iso")
    if end_date_str:
        try:
            end_time = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass
    
    # Get event ID from events array if present
    events = data.get("events") or []
    event_id = events[0].get("id", "") if events else data.get("event_id", "")
    
    return Market(
        market_id=market_id,
        event_id=str(event_id),
        question=data.get("question", ""),
        outcomes=outcomes,
        end_time=end_time,
        resolution_source=data.get("resolutionSource") or data.get("resolution_source"),
        active=data.get("active", True),
        closed=data.get("closed", False),
    )


def parse_event(data: dict) -> Event:
    """Parse raw API response into an Event object."""
    markets = []
    for market_data in data.get("markets", []):
        markets.append(parse_market(market_data))
    
    return Event(
        event_id=data.get("id", ""),
        title=data.get("title", ""),
        markets=markets,
    )
