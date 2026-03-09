"""Arbitrage and opportunity detection algorithms."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from .config import MIN_PROFIT_THRESHOLD, MIN_LIQUIDITY_USD
from .models import Market, Outcome, Opportunity, OpportunityType, OrderBook
from .pricing import effective_cost_buy, get_available_liquidity
from .relationships import (
    ComplementConstraint,
    MutuallyExclusiveConstraint,
    detect_complement_relationship,
    detect_mutually_exclusive_relationship,
)


def check_complement_arbitrage(
    market: Market,
    orderbooks: dict[str, OrderBook],
    size: Decimal,
) -> Optional[Opportunity]:
    """Check for complement arbitrage in a binary market.
    
    Arbitrage exists if: effective_cost(Yes) + effective_cost(No) < 1.0
    
    Args:
        market: The binary market to check
        orderbooks: Mapping of outcome_id -> OrderBook
        size: Target position size
        
    Returns:
        Opportunity if arb exists, None otherwise
    """
    constraint = detect_complement_relationship(market)
    if constraint is None:
        return None
    
    # Get orderbooks for both outcomes
    book_yes = orderbooks.get(constraint.yes_id)
    book_no = orderbooks.get(constraint.no_id)
    
    if book_yes is None or book_no is None:
        return None
    
    # Calculate effective costs
    cost_yes = effective_cost_buy(book_yes, size)
    cost_no = effective_cost_buy(book_no, size)
    
    if cost_yes is None or cost_no is None:
        return None  # Insufficient liquidity
    
    total_cost = cost_yes + cost_no
    
    if total_cost < Decimal(1):
        profit = Decimal(1) - total_cost
        
        if profit < MIN_PROFIT_THRESHOLD:
            return None  # Below threshold
        
        # Calculate available liquidity
        yes_liquidity = get_available_liquidity(book_yes.asks)
        no_liquidity = get_available_liquidity(book_no.asks)
        min_liquidity = min(yes_liquidity, no_liquidity)
        
        if min_liquidity < MIN_LIQUIDITY_USD:
            return None  # Too thin
        
        return Opportunity(
            market_id=market.market_id,
            opportunity_type=OpportunityType.COMPLEMENT_ARB,
            profit_bound=profit * size,
            confidence_score=1.0,  # Mathematical guarantee
            required_size=size,
            liquidity_available=min_liquidity,
            rationale=f"Buy Yes@{cost_yes:.4f} + No@{cost_no:.4f} = {total_cost:.4f} < 1.0, profit={profit:.4f}/share",
        )
    
    return None


def check_multi_outcome_arbitrage(
    market: Market,
    orderbooks: dict[str, OrderBook],
    size: Decimal,
) -> Optional[Opportunity]:
    """Check for arbitrage in a mutually exclusive multi-outcome market.
    
    Arbitrage exists if: sum(effective_cost(Oi)) < 1.0 for all outcomes
    
    Args:
        market: The multi-outcome market to check
        orderbooks: Mapping of outcome_id -> OrderBook
        size: Target position size
        
    Returns:
        Opportunity if arb exists, None otherwise
    """
    constraint = detect_mutually_exclusive_relationship(market)
    if constraint is None:
        return None
    
    total_cost = Decimal(0)
    min_liquidity = None
    costs_detail = []
    
    for outcome_id in constraint.outcome_ids:
        book = orderbooks.get(outcome_id)
        if book is None:
            return None
        
        cost = effective_cost_buy(book, size)
        if cost is None:
            return None
        
        total_cost += cost
        liquidity = get_available_liquidity(book.asks)
        
        if min_liquidity is None or liquidity < min_liquidity:
            min_liquidity = liquidity
        
        # Find outcome text
        outcome = next((o for o in market.outcomes if o.outcome_id == outcome_id), None)
        text = outcome.text if outcome else outcome_id[:8]
        costs_detail.append(f"{text}@{cost:.4f}")
    
    if total_cost < Decimal(1):
        profit = Decimal(1) - total_cost
        
        if profit < MIN_PROFIT_THRESHOLD:
            return None
        
        if min_liquidity and min_liquidity < MIN_LIQUIDITY_USD:
            return None
        
        return Opportunity(
            market_id=market.market_id,
            opportunity_type=OpportunityType.MULTI_OUTCOME_ARB,
            profit_bound=profit * size,
            confidence_score=1.0,
            required_size=size,
            liquidity_available=min_liquidity or Decimal(0),
            rationale=f"Buy all: {' + '.join(costs_detail)} = {total_cost:.4f} < 1.0",
        )
    
    return None


def check_positive_ev(
    outcome: Outcome,
    orderbook: OrderBook,
    external_prob: Decimal,
    size: Decimal,
) -> Optional[Opportunity]:
    """Check for positive expected value opportunity.
    
    EV = p_external - effective_cost
    
    Args:
        outcome: The outcome to check
        orderbook: Order book for the outcome
        external_prob: External probability estimate (0-1)
        size: Target position size
        
    Returns:
        Opportunity if +EV, None otherwise
    """
    cost = effective_cost_buy(orderbook, size)
    if cost is None:
        return None
    
    ev = external_prob - cost
    
    if ev > Decimal(0):
        # Kelly criterion for optimal sizing
        if cost > 0:
            odds = (Decimal(1) / cost) - Decimal(1)
            kelly = ev / odds if odds > 0 else Decimal(0)
        else:
            kelly = Decimal(0)
        
        return Opportunity(
            market_id=outcome.market_id,
            opportunity_type=OpportunityType.POSITIVE_EV,
            profit_bound=ev * size,  # Expected, not guaranteed
            confidence_score=0.5,  # Depends on external prob quality
            required_size=size,
            liquidity_available=get_available_liquidity(orderbook.asks),
            rationale=f"p_ext={external_prob:.2%}, cost={cost:.4f}, EV={ev:.4f}, kelly={kelly:.2%}",
        )
    
    return None


def scan_market_for_opportunities(
    market: Market,
    orderbooks: dict[str, OrderBook],
    size: Decimal = Decimal("10"),
) -> list[Opportunity]:
    """Scan a single market for all types of opportunities.
    
    Args:
        market: Market to scan
        orderbooks: Order books for all outcomes
        size: Default position size
        
    Returns:
        List of detected opportunities
    """
    opportunities = []
    
    # Check for complement arbitrage (binary markets)
    if market.is_binary:
        opp = check_complement_arbitrage(market, orderbooks, size)
        if opp:
            opportunities.append(opp)
    else:
        # Check multi-outcome arbitrage
        opp = check_multi_outcome_arbitrage(market, orderbooks, size)
        if opp:
            opportunities.append(opp)
    
    return opportunities
