"""Unit tests for detection module."""

import pytest
from decimal import Decimal

from polymarket_scanner.models import (
    Market, 
    Outcome, 
    OrderBook, 
    OrderBookLevel,
    OpportunityType,
)
from polymarket_scanner.detection import (
    check_complement_arbitrage,
    check_multi_outcome_arbitrage,
    check_positive_ev,
    scan_market_for_opportunities,
)


def create_orderbook(outcome_id: str, best_ask: Decimal, ask_size: Decimal = Decimal("100")) -> OrderBook:
    """Helper to create a simple orderbook."""
    return OrderBook(
        outcome_id=outcome_id,
        bids=[OrderBookLevel(best_ask - Decimal("0.02"), ask_size)],
        asks=[OrderBookLevel(best_ask, ask_size)],
    )


class TestComplementArbitrage:
    """Tests for complement (Yes/No) arbitrage detection."""
    
    def test_arb_exists_when_sum_less_than_one(self):
        """Arbitrage exists when Yes + No costs < 1."""
        market = Market(
            market_id="market_1",
            event_id="event_1",
            question="Will X happen?",
            outcomes=[
                Outcome("yes_token", "market_1", "Yes"),
                Outcome("no_token", "market_1", "No"),
            ],
        )
        
        # Yes @ 0.45, No @ 0.45 = 0.90 total (before slippage)
        # With 1% slippage: ~0.909 < 1.0, still arb
        orderbooks = {
            "yes_token": create_orderbook("yes_token", Decimal("0.45")),
            "no_token": create_orderbook("no_token", Decimal("0.45")),
        }
        
        result = check_complement_arbitrage(market, orderbooks, Decimal("10"))
        
        assert result is not None
        assert result.opportunity_type == OpportunityType.COMPLEMENT_ARB
        assert result.profit_bound > 0
        assert result.confidence_score == 1.0  # Mathematical guarantee
    
    def test_no_arb_when_sum_equals_one(self):
        """No arbitrage when Yes + No >= 1."""
        market = Market(
            market_id="market_1",
            event_id="event_1",
            question="Will X happen?",
            outcomes=[
                Outcome("yes_token", "market_1", "Yes"),
                Outcome("no_token", "market_1", "No"),
            ],
        )
        
        # Yes @ 0.50, No @ 0.50 = 1.00 total (before slippage)
        # With slippage > 1.0, no arb
        orderbooks = {
            "yes_token": create_orderbook("yes_token", Decimal("0.50")),
            "no_token": create_orderbook("no_token", Decimal("0.50")),
        }
        
        result = check_complement_arbitrage(market, orderbooks, Decimal("10"))
        
        assert result is None
    
    def test_no_arb_when_sum_greater_than_one(self):
        """No arbitrage when Yes + No > 1."""
        market = Market(
            market_id="market_1",
            event_id="event_1",
            question="Will X happen?",
            outcomes=[
                Outcome("yes_token", "market_1", "Yes"),
                Outcome("no_token", "market_1", "No"),
            ],
        )
        
        # Yes @ 0.60, No @ 0.50 = 1.10 total
        orderbooks = {
            "yes_token": create_orderbook("yes_token", Decimal("0.60")),
            "no_token": create_orderbook("no_token", Decimal("0.50")),
        }
        
        result = check_complement_arbitrage(market, orderbooks, Decimal("10"))
        
        assert result is None
    
    def test_no_arb_with_missing_orderbook(self):
        """No opportunity if orderbook is missing."""
        market = Market(
            market_id="market_1",
            event_id="event_1",
            question="Will X happen?",
            outcomes=[
                Outcome("yes_token", "market_1", "Yes"),
                Outcome("no_token", "market_1", "No"),
            ],
        )
        
        # Only one orderbook
        orderbooks = {
            "yes_token": create_orderbook("yes_token", Decimal("0.45")),
        }
        
        result = check_complement_arbitrage(market, orderbooks, Decimal("10"))
        
        assert result is None
    
    def test_arb_rationale_includes_prices(self):
        """Rationale should include the actual prices used."""
        market = Market(
            market_id="market_1",
            event_id="event_1",
            question="Will X happen?",
            outcomes=[
                Outcome("yes_token", "market_1", "Yes"),
                Outcome("no_token", "market_1", "No"),
            ],
        )
        
        orderbooks = {
            "yes_token": create_orderbook("yes_token", Decimal("0.40")),
            "no_token": create_orderbook("no_token", Decimal("0.40")),
        }
        
        result = check_complement_arbitrage(market, orderbooks, Decimal("10"))
        
        assert result is not None
        assert "Yes@" in result.rationale
        assert "No@" in result.rationale
        assert "< 1.0" in result.rationale


class TestMultiOutcomeArbitrage:
    """Tests for multi-outcome arbitrage."""
    
    def test_arb_when_sum_less_than_one(self):
        """Arbitrage exists when sum of all outcomes < 1."""
        market = Market(
            market_id="market_1",
            event_id="event_1",
            question="Who will win?",
            outcomes=[
                Outcome("a_token", "market_1", "Candidate A"),
                Outcome("b_token", "market_1", "Candidate B"),
                Outcome("c_token", "market_1", "Candidate C"),
            ],
        )
        
        # 0.30 + 0.30 + 0.30 = 0.90 before slippage
        orderbooks = {
            "a_token": create_orderbook("a_token", Decimal("0.30")),
            "b_token": create_orderbook("b_token", Decimal("0.30")),
            "c_token": create_orderbook("c_token", Decimal("0.30")),
        }
        
        result = check_multi_outcome_arbitrage(market, orderbooks, Decimal("10"))
        
        assert result is not None
        assert result.opportunity_type == OpportunityType.MULTI_OUTCOME_ARB
    
    def test_no_arb_for_binary_market(self):
        """Multi-outcome arb should not apply to binary markets."""
        market = Market(
            market_id="market_1",
            event_id="event_1",
            question="Will X happen?",
            outcomes=[
                Outcome("yes_token", "market_1", "Yes"),
                Outcome("no_token", "market_1", "No"),
            ],
        )
        
        orderbooks = {
            "yes_token": create_orderbook("yes_token", Decimal("0.40")),
            "no_token": create_orderbook("no_token", Decimal("0.40")),
        }
        
        result = check_multi_outcome_arbitrage(market, orderbooks, Decimal("10"))
        
        assert result is None  # Binary should use complement check


class TestPositiveEV:
    """Tests for positive expected value detection."""
    
    def test_positive_ev_detected(self):
        """Detect +EV when external prob > cost."""
        outcome = Outcome("yes_token", "market_1", "Yes")
        book = create_orderbook("yes_token", Decimal("0.50"))
        
        # External estimate: 60%, cost: 50% -> EV = 10%
        result = check_positive_ev(
            outcome, book,
            external_prob=Decimal("0.60"),
            size=Decimal("10"),
        )
        
        assert result is not None
        assert result.opportunity_type == OpportunityType.POSITIVE_EV
        assert result.confidence_score < 1.0  # Not guaranteed
    
    def test_no_ev_when_overpriced(self):
        """No +EV when external prob < cost."""
        outcome = Outcome("yes_token", "market_1", "Yes")
        book = create_orderbook("yes_token", Decimal("0.70"))
        
        # External estimate: 60%, cost: 70% -> EV = -10%
        result = check_positive_ev(
            outcome, book,
            external_prob=Decimal("0.60"),
            size=Decimal("10"),
        )
        
        assert result is None


class TestScanMarket:
    """Tests for the scan_market_for_opportunities function."""
    
    def test_scan_binary_market(self):
        """Scanning binary market uses complement check."""
        market = Market(
            market_id="market_1",
            event_id="event_1",
            question="Will X happen?",
            outcomes=[
                Outcome("yes_token", "market_1", "Yes"),
                Outcome("no_token", "market_1", "No"),
            ],
        )
        
        orderbooks = {
            "yes_token": create_orderbook("yes_token", Decimal("0.40")),
            "no_token": create_orderbook("no_token", Decimal("0.40")),
        }
        
        results = scan_market_for_opportunities(market, orderbooks)
        
        assert len(results) == 1
        assert results[0].opportunity_type == OpportunityType.COMPLEMENT_ARB
    
    def test_scan_returns_empty_for_fair_market(self):
        """No opportunities in fairly priced market."""
        market = Market(
            market_id="market_1",
            event_id="event_1",
            question="Will X happen?",
            outcomes=[
                Outcome("yes_token", "market_1", "Yes"),
                Outcome("no_token", "market_1", "No"),
            ],
        )
        
        # Fair pricing: 50/50
        orderbooks = {
            "yes_token": create_orderbook("yes_token", Decimal("0.52")),
            "no_token": create_orderbook("no_token", Decimal("0.52")),
        }
        
        results = scan_market_for_opportunities(market, orderbooks)
        
        assert len(results) == 0
