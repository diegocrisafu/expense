"""WebSocket client for live order book updates."""

import asyncio
import json
import logging
from typing import Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from ..config import CLOB_WS_URL
from .clob import parse_orderbook

logger = logging.getLogger(__name__)


class WebSocketClient:
    """WebSocket client for CLOB live updates."""
    
    def __init__(self, url: str = CLOB_WS_URL):
        self.url = url
        self.connection = None
        self._running = False
        self._subscriptions: set[str] = set()
    
    async def connect(self) -> None:
        """Establish WebSocket connection."""
        try:
            self.connection = await websockets.connect(self.url)
            logger.info(f"Connected to WebSocket: {self.url}")
        except Exception as e:
            logger.error(f"Failed to connect to WebSocket: {e}")
            raise
    
    async def disconnect(self) -> None:
        """Close WebSocket connection."""
        self._running = False
        if self.connection:
            await self.connection.close()
            self.connection = None
            logger.info("Disconnected from WebSocket")
    
    async def subscribe(self, token_ids: list[str]) -> None:
        """Subscribe to order book updates for tokens."""
        if not self.connection:
            await self.connect()
        
        for token_id in token_ids:
            if token_id not in self._subscriptions:
                message = {
                    "type": "subscribe",
                    "channel": "market",
                    "assets_ids": [token_id],
                }
                await self.connection.send(json.dumps(message))
                self._subscriptions.add(token_id)
                logger.debug(f"Subscribed to {token_id}")
    
    async def unsubscribe(self, token_ids: list[str]) -> None:
        """Unsubscribe from order book updates."""
        if not self.connection:
            return
        
        for token_id in token_ids:
            if token_id in self._subscriptions:
                message = {
                    "type": "unsubscribe",
                    "channel": "market",
                    "assets_ids": [token_id],
                }
                await self.connection.send(json.dumps(message))
                self._subscriptions.discard(token_id)
                logger.debug(f"Unsubscribed from {token_id}")
    
    async def listen(
        self,
        on_message: Callable[[dict], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        """Listen for incoming messages.
        
        Args:
            on_message: Callback for each message received
            on_error: Optional callback for errors
        """
        if not self.connection:
            await self.connect()
        
        self._running = True
        
        while self._running:
            try:
                message = await self.connection.recv()
                data = json.loads(message)
                on_message(data)
            except ConnectionClosed:
                logger.warning("WebSocket connection closed, reconnecting...")
                await asyncio.sleep(1)
                try:
                    await self.connect()
                    # Resubscribe to all tokens
                    for token_id in list(self._subscriptions):
                        self._subscriptions.discard(token_id)
                    await self.subscribe(list(self._subscriptions))
                except Exception as e:
                    if on_error:
                        on_error(e)
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                if on_error:
                    on_error(e)


async def run_websocket_listener(
    token_ids: list[str],
    on_update: Callable[[dict], None],
    duration_seconds: Optional[int] = None,
) -> None:
    """Run WebSocket listener for a set of tokens.
    
    Args:
        token_ids: List of token IDs to subscribe to
        on_update: Callback for order book updates
        duration_seconds: Optional duration to run, None for indefinite
    """
    client = WebSocketClient()
    
    try:
        await client.connect()
        await client.subscribe(token_ids)
        
        if duration_seconds:
            # Run for specified duration
            async def timed_listen():
                await asyncio.sleep(duration_seconds)
                await client.disconnect()
            
            listen_task = asyncio.create_task(client.listen(on_update))
            timer_task = asyncio.create_task(timed_listen())
            
            await asyncio.wait(
                [listen_task, timer_task],
                return_when=asyncio.FIRST_COMPLETED
            )
        else:
            await client.listen(on_update)
    finally:
        await client.disconnect()
