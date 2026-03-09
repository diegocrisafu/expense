"""Data models for Polymarket Scanner."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


class OpportunityType(Enum):
    """Types of detected opportunities."""
    COMPLEMENT_ARB = "complement_arb"
    MULTI_OUTCOME_ARB = "multi_outcome_arb"
    POSITIVE_EV = "positive_ev"


@dataclass
class OrderBookLevel:
    """A single price level in the order book."""
    price: Decimal
    size: Decimal


@dataclass
class OrderBook:
    """Order book for an outcome."""
    outcome_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)  # Descending by price
    asks: list[OrderBookLevel] = field(default_factory=list)  # Ascending by price
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    @property
    def best_bid(self) -> Optional[Decimal]:
        """Highest bid price."""
        return self.bids[0].price if self.bids else None
    
    @property
    def best_ask(self) -> Optional[Decimal]:
        """Lowest ask price."""
        return self.asks[0].price if self.asks else None
    
    @property
    def midpoint(self) -> Optional[Decimal]:
        """Midpoint between best bid and ask."""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None


@dataclass
class Outcome:
    """An outcome within a market (e.g., Yes or No)."""
    outcome_id: str  # Token ID
    market_id: str
    text: str  # "Yes", "No", or custom text
    orderbook: Optional[OrderBook] = None


@dataclass
class Market:
    """A prediction market."""
    market_id: str
    event_id: str
    question: str
    outcomes: list[Outcome] = field(default_factory=list)
    end_time: Optional[datetime] = None
    resolution_source: Optional[str] = None
    active: bool = True
    closed: bool = False
    
    @property
    def is_binary(self) -> bool:
        """Check if this is a binary Yes/No market."""
        return len(self.outcomes) == 2


@dataclass
class Event:
    """An event containing one or more markets."""
    event_id: str
    title: str
    markets: list[Market] = field(default_factory=list)


@dataclass
class Opportunity:
    """A detected trading opportunity."""
    market_id: str
    opportunity_type: OpportunityType
    profit_bound: Decimal  # Guaranteed profit (arb) or expected profit (EV)
    confidence_score: float  # 1.0 for guaranteed arb, lower for EV
    required_size: Decimal
    liquidity_available: Decimal
    rationale: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            "market_id": self.market_id,
            "opportunity_type": self.opportunity_type.value,
            "profit_bound": str(self.profit_bound),
            "confidence_score": self.confidence_score,
            "required_size": str(self.required_size),
            "liquidity_available": str(self.liquidity_available),
            "rationale": self.rationale,
            "timestamp": self.timestamp.isoformat(),
        }
