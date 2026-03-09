"""Pricing engine for probability and cost calculations."""

from decimal import Decimal
from typing import Optional

from .config import MAKER_FEE, TAKER_FEE, SLIPPAGE_BUFFER
from .models import OrderBook, OrderBookLevel


def calculate_midpoint_probability(orderbook: OrderBook) -> Optional[Decimal]:
    """Calculate implied probability from midpoint of best bid and ask.
    
    This is a simple estimate, not suitable for actual execution.
    
    Args:
        orderbook: The order book for an outcome
        
    Returns:
        Midpoint probability as Decimal (0-1), or None if no book
    """
    return orderbook.midpoint


def calculate_executable_cost(
    levels: list[OrderBookLevel],
    size: Decimal,
) -> Optional[Decimal]:
    """Calculate volume-weighted average price to fill a given size.
    
    Walks through order book levels until the target size is filled.
    
    Args:
        levels: List of order book levels (asks for buy, bids for sell)
        size: Target size to fill
        
    Returns:
        VWAP as Decimal (0-1), or None if insufficient liquidity
    """
    if not levels or size <= 0:
        return None
    
    remaining = size
    total_cost = Decimal(0)
    
    for level in levels:
        fill = min(remaining, level.size)
        total_cost += fill * level.price
        remaining -= fill
        
        if remaining <= 0:
            break
    
    if remaining > 0:
        return None  # Insufficient liquidity
    
    return total_cost / size  # VWAP


def effective_cost_buy(
    orderbook: OrderBook,
    size: Decimal,
    include_fees: bool = True,
    include_slippage: bool = True,
) -> Optional[Decimal]:
    """Calculate effective cost to buy shares including fees and slippage.
    
    Args:
        orderbook: Order book for the outcome
        size: Number of shares to buy
        include_fees: Whether to include taker fees
        include_slippage: Whether to add slippage buffer
        
    Returns:
        Effective cost per share as Decimal (0-1), or None if insufficient liquidity
    """
    if not orderbook.asks:
        return None
    
    base_cost = calculate_executable_cost(orderbook.asks, size)
    if base_cost is None:
        return None
    
    cost = base_cost
    
    if include_fees:
        cost = cost * (1 + TAKER_FEE)
    
    if include_slippage:
        cost = cost * (1 + SLIPPAGE_BUFFER)
    
    return cost


def effective_cost_sell(
    orderbook: OrderBook,
    size: Decimal,
    include_fees: bool = True,
    include_slippage: bool = True,
) -> Optional[Decimal]:
    """Calculate effective proceeds from selling shares.
    
    Args:
        orderbook: Order book for the outcome
        size: Number of shares to sell
        include_fees: Whether to include maker fees
        include_slippage: Whether to subtract slippage buffer
        
    Returns:
        Effective proceeds per share as Decimal (0-1), or None
    """
    if not orderbook.bids:
        return None
    
    base_price = calculate_executable_cost(orderbook.bids, size)
    if base_price is None:
        return None
    
    proceeds = base_price
    
    if include_fees:
        proceeds = proceeds * (1 - MAKER_FEE)
    
    if include_slippage:
        proceeds = proceeds * (1 - SLIPPAGE_BUFFER)
    
    return proceeds


def get_available_liquidity(levels: list[OrderBookLevel]) -> Decimal:
    """Calculate total liquidity available in order book levels.
    
    Args:
        levels: List of order book levels
        
    Returns:
        Total size available
    """
    return sum((level.size for level in levels), Decimal(0))


def calculate_spread(orderbook: OrderBook) -> Optional[Decimal]:
    """Calculate bid-ask spread.
    
    Args:
        orderbook: Order book for an outcome
        
    Returns:
        Spread as Decimal, or None if no book
    """
    if orderbook.best_bid is None or orderbook.best_ask is None:
        return None
    
    return orderbook.best_ask - orderbook.best_bid
