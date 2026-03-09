"""CLOB API client for order books and prices."""

import asyncio
import logging
import random
from decimal import Decimal
from typing import Optional

import httpx

from ..config import CLOB_API_BASE, CLOB_RATE_LIMIT
from ..models import OrderBook, OrderBookLevel

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 0.5  # seconds
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class CLOBAPIClient:
    """Client for Polymarket CLOB API.

    Uses a persistent httpx.AsyncClient with connection pooling for
    efficient HTTP/2 multiplexing across requests.
    """

    def __init__(self, base_url: str = CLOB_API_BASE):
        self.base_url = base_url
        self._last_request_time = 0.0
        self._request_interval = 1.0 / CLOB_RATE_LIMIT
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazily create and return a persistent HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(30.0, connect=10.0),
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    keepalive_expiry=30.0,
                ),
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client. Call on shutdown."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _rate_limit(self) -> None:
        """Enforce rate limiting."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_time
        if elapsed < self._request_interval:
            await asyncio.sleep(self._request_interval - elapsed)
        self._last_request_time = asyncio.get_event_loop().time()

    async def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """Make a GET request with retry and exponential backoff."""
        client = self._get_client()
        last_exc: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            await self._rate_limit()
            try:
                response = await client.get(endpoint, params=params)

                # Retry on transient server errors
                if response.status_code in RETRYABLE_STATUS_CODES:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
                    logger.warning(
                        f"CLOB {endpoint} returned {response.status_code}, "
                        f"retry {attempt}/{MAX_RETRIES} in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue

                response.raise_for_status()
                return response.json()

            except httpx.TimeoutException as e:
                last_exc = e
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
                logger.warning(
                    f"CLOB {endpoint} timeout, retry {attempt}/{MAX_RETRIES} in {delay:.1f}s"
                )
                await asyncio.sleep(delay)

            except httpx.HTTPStatusError:
                raise  # Non-retryable HTTP errors propagate immediately

        # All retries exhausted
        raise last_exc or httpx.TimeoutException(
            f"All {MAX_RETRIES} retries exhausted for {endpoint}"
        )

    async def get_orderbook(self, token_id: str) -> Optional[OrderBook]:
        """Fetch order book for a token.

        Args:
            token_id: The outcome token ID

        Returns:
            OrderBook object or None if error
        """
        try:
            data = await self._get("/book", params={"token_id": token_id})
            return parse_orderbook(token_id, data)
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.error(f"Error fetching orderbook for {token_id}: {e}")
            return None

    async def get_price(self, token_id: str) -> Optional[Decimal]:
        """Fetch current price for a token."""
        try:
            data = await self._get("/price", params={"token_id": token_id})
            price = data.get("price")
            return Decimal(str(price)) if price is not None else None
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.error(f"Error fetching price for {token_id}: {e}")
            return None

    async def get_midpoint(self, token_id: str) -> Optional[Decimal]:
        """Fetch midpoint price for a token."""
        try:
            data = await self._get("/midpoint", params={"token_id": token_id})
            mid = data.get("mid")
            return Decimal(str(mid)) if mid is not None else None
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.error(f"Error fetching midpoint for {token_id}: {e}")
            return None

    async def get_spread(self, token_id: str) -> Optional[Decimal]:
        """Fetch bid-ask spread for a token."""
        try:
            data = await self._get("/spread", params={"token_id": token_id})
            spread = data.get("spread")
            return Decimal(str(spread)) if spread is not None else None
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.error(f"Error fetching spread for {token_id}: {e}")
            return None


def parse_orderbook(token_id: str, data: dict) -> OrderBook:
    """Parse raw API response into an OrderBook object."""
    bids = []
    asks = []

    # Parse bids (descending by price)
    for bid in data.get("bids", []):
        bids.append(OrderBookLevel(
            price=Decimal(str(bid.get("price", 0))),
            size=Decimal(str(bid.get("size", 0))),
        ))

    # Sort bids descending
    bids.sort(key=lambda x: x.price, reverse=True)

    # Parse asks (ascending by price)
    for ask in data.get("asks", []):
        asks.append(OrderBookLevel(
            price=Decimal(str(ask.get("price", 0))),
            size=Decimal(str(ask.get("size", 0))),
        ))

    # Sort asks ascending
    asks.sort(key=lambda x: x.price)

    return OrderBook(
        outcome_id=token_id,
        bids=bids,
        asks=asks,
    )
