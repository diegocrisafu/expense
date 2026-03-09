"""Unit tests for pricing module."""

import pytest
from decimal import Decimal

from polymarket_scanner.models import OrderBook, OrderBookLevel
from polymarket_scanner.pricing import (
    calculate_midpoint_probability,
    calculate_executable_cost,
    effective_cost_buy,
    effective_cost_sell,
    get_available_liquidity,
    calculate_spread,
)


class TestMidpointCalculation:
    """Tests for midpoint probability calculation."""
    
    def test_midpoint_basic(self):
        """Midpoint of 0.60 bid / 0.65 ask should be 0.625."""
        book = OrderBook(
            outcome_id="token_1",
            bids=[OrderBookLevel(Decimal("0.60"), Decimal("100"))],
            asks=[OrderBookLevel(Decimal("0.65"), Decimal("100"))],
        )
        
        result = calculate_midpoint_probability(book)
        
        assert result == Decimal("0.625")
    
    def test_midpoint_empty_book(self):
        """Empty book should return None."""
        book = OrderBook(outcome_id="token_1", bids=[], asks=[])
        
        result = calculate_midpoint_probability(book)
        
        assert result is None
    
    def test_midpoint_only_bids(self):
        """Book with only bids returns None."""
        book = OrderBook(
            outcome_id="token_1",
            bids=[OrderBookLevel(Decimal("0.60"), Decimal("100"))],
            asks=[],
        )
        
        result = calculate_midpoint_probability(book)
        
        assert result is None


class TestExecutableCost:
    """Tests for order book depth walking."""
    
    def test_single_level_fill(self):
        """Filling from a single level returns that level's price."""
        levels = [
            OrderBookLevel(Decimal("0.65"), Decimal("100")),
        ]
        
        result = calculate_executable_cost(levels, Decimal("50"))
        
        assert result == Decimal("0.65")
    
    def test_multi_level_fill(self):
        """Walking multiple levels returns VWAP."""
        levels = [
            OrderBookLevel(Decimal("0.60"), Decimal("10")),  # Fill 10 @ 0.60
            OrderBookLevel(Decimal("0.62"), Decimal("10")),  # Fill 10 @ 0.62
        ]
        
        result = calculate_executable_cost(levels, Decimal("20"))
        
        # VWAP = (10*0.60 + 10*0.62) / 20 = 12.2 / 20 = 0.61
        assert result == Decimal("0.61")
    
    def test_partial_level_fill(self):
        """Partial fill of a level."""
        levels = [
            OrderBookLevel(Decimal("0.60"), Decimal("100")),
        ]
        
        result = calculate_executable_cost(levels, Decimal("50"))
        
        assert result == Decimal("0.60")
    
    def test_insufficient_liquidity(self):
        """Returns None when book is too thin."""
        levels = [
            OrderBookLevel(Decimal("0.60"), Decimal("10")),
        ]
        
        result = calculate_executable_cost(levels, Decimal("100"))
        
        assert result is None
    
    def test_empty_levels(self):
        """Empty levels returns None."""
        result = calculate_executable_cost([], Decimal("10"))
        
        assert result is None
    
    def test_zero_size(self):
        """Zero size returns None."""
        levels = [OrderBookLevel(Decimal("0.60"), Decimal("100"))]
        
        result = calculate_executable_cost(levels, Decimal("0"))
        
        assert result is None


class TestEffectiveCost:
    """Tests for effective cost with fees and slippage."""
    
    def test_effective_buy_cost(self):
        """Effective buy cost should be >= raw cost."""
        book = OrderBook(
            outcome_id="token_1",
            bids=[],
            asks=[OrderBookLevel(Decimal("0.60"), Decimal("100"))],
        )
        
        result = effective_cost_buy(book, Decimal("10"))
        
        # With default 0% fees and 1% slippage
        # 0.60 * 1.01 = 0.606
        assert result is not None
        assert result >= Decimal("0.60")
    
    def test_effective_buy_no_liquidity(self):
        """No asks means no buy possible."""
        book = OrderBook(outcome_id="token_1", bids=[], asks=[])
        
        result = effective_cost_buy(book, Decimal("10"))
        
        assert result is None
    
    def test_effective_sell_proceeds(self):
        """Effective sell proceeds should be <= raw price."""
        book = OrderBook(
            outcome_id="token_1",
            bids=[OrderBookLevel(Decimal("0.58"), Decimal("100"))],
            asks=[],
        )
        
        result = effective_cost_sell(book, Decimal("10"))
        
        # With default 0% fees and 1% slippage
        # 0.58 * 0.99 = 0.5742
        assert result is not None
        assert result <= Decimal("0.58")


class TestLiquidityAndSpread:
    """Tests for liquidity and spread calculations."""
    
    def test_available_liquidity(self):
        """Sum of sizes across levels."""
        levels = [
            OrderBookLevel(Decimal("0.60"), Decimal("50")),
            OrderBookLevel(Decimal("0.62"), Decimal("30")),
            OrderBookLevel(Decimal("0.65"), Decimal("20")),
        ]
        
        result = get_available_liquidity(levels)
        
        assert result == Decimal("100")
    
    def test_spread_calculation(self):
        """Spread is best_ask - best_bid."""
        book = OrderBook(
            outcome_id="token_1",
            bids=[OrderBookLevel(Decimal("0.58"), Decimal("100"))],
            asks=[OrderBookLevel(Decimal("0.62"), Decimal("100"))],
        )
        
        result = calculate_spread(book)
        
        assert result == Decimal("0.04")
    
    def test_spread_no_book(self):
        """No spread if book is empty."""
        book = OrderBook(outcome_id="token_1", bids=[], asks=[])
        
        result = calculate_spread(book)
        
        assert result is None
