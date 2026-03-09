"""Relationship modeling between markets and outcomes."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .models import Market, Outcome


@dataclass
class ConstraintResult:
    """Result of a constraint check."""
    satisfied: bool
    violation_amount: Optional[Decimal] = None  # How much constraint is violated
    description: str = ""


class Constraint(ABC):
    """Abstract base class for market constraints."""
    
    @abstractmethod
    def check(self, probabilities: dict[str, Decimal]) -> ConstraintResult:
        """Check if the constraint is satisfied.
        
        Args:
            probabilities: Mapping of outcome_id -> implied probability
            
        Returns:
            ConstraintResult indicating if constraint holds
        """
        pass
    
    @abstractmethod
    def get_arb_condition(self, costs: dict[str, Decimal]) -> Optional[Decimal]:
        """Calculate arbitrage profit if constraint is violated.
        
        Args:
            costs: Mapping of outcome_id -> effective cost to buy
            
        Returns:
            Profit bound per dollar risked if arb exists, None otherwise
        """
        pass


class ComplementConstraint(Constraint):
    """Constraint for binary Yes/No markets: P(Yes) + P(No) = 1."""
    
    def __init__(self, yes_outcome_id: str, no_outcome_id: str):
        self.yes_id = yes_outcome_id
        self.no_id = no_outcome_id
    
    def check(self, probabilities: dict[str, Decimal]) -> ConstraintResult:
        """Check if Yes + No probabilities sum to 1."""
        p_yes = probabilities.get(self.yes_id, Decimal(0))
        p_no = probabilities.get(self.no_id, Decimal(0))
        
        total = p_yes + p_no
        violation = abs(total - Decimal(1))
        
        return ConstraintResult(
            satisfied=violation < Decimal("0.001"),  # 0.1% tolerance
            violation_amount=violation if violation >= Decimal("0.001") else None,
            description=f"P(Yes)={p_yes:.4f} + P(No)={p_no:.4f} = {total:.4f}"
        )
    
    def get_arb_condition(self, costs: dict[str, Decimal]) -> Optional[Decimal]:
        """Check for complement arbitrage.
        
        Arbitrage exists if cost(Yes) + cost(No) < 1.0
        
        Returns:
            Profit per unit if arb exists (1 - total_cost), None otherwise
        """
        cost_yes = costs.get(self.yes_id)
        cost_no = costs.get(self.no_id)
        
        if cost_yes is None or cost_no is None:
            return None
        
        total_cost = cost_yes + cost_no
        
        if total_cost < Decimal(1):
            return Decimal(1) - total_cost
        
        return None


class MutuallyExclusiveConstraint(Constraint):
    """Constraint for multi-outcome markets where exactly one wins."""
    
    def __init__(self, outcome_ids: list[str]):
        self.outcome_ids = outcome_ids
    
    def check(self, probabilities: dict[str, Decimal]) -> ConstraintResult:
        """Check if all outcome probabilities sum to 1."""
        total = sum(
            probabilities.get(oid, Decimal(0)) 
            for oid in self.outcome_ids
        )
        
        violation = abs(total - Decimal(1))
        
        return ConstraintResult(
            satisfied=violation < Decimal("0.01"),  # 1% tolerance
            violation_amount=violation if violation >= Decimal("0.01") else None,
            description=f"Sum of {len(self.outcome_ids)} outcomes = {total:.4f}"
        )
    
    def get_arb_condition(self, costs: dict[str, Decimal]) -> Optional[Decimal]:
        """Check for multi-outcome arbitrage.
        
        Arbitrage exists if sum of all buy costs < 1.0
        """
        total_cost = Decimal(0)
        
        for oid in self.outcome_ids:
            cost = costs.get(oid)
            if cost is None:
                return None  # Missing data
            total_cost += cost
        
        if total_cost < Decimal(1):
            return Decimal(1) - total_cost
        
        return None


class ConditionalConstraint(Constraint):
    """Constraint for conditional markets: P(Y|not X) type.
    
    This is complex and requires understanding the exact resolution rules.
    Implementation is a placeholder for extension.
    """
    
    def __init__(
        self,
        condition_outcome_id: str,  # X
        conditional_outcome_id: str,  # Y|not X
        joint_outcome_id: Optional[str] = None,  # Y and not X, if exists
    ):
        self.condition_id = condition_outcome_id
        self.conditional_id = conditional_outcome_id
        self.joint_id = joint_outcome_id
    
    def check(self, probabilities: dict[str, Decimal]) -> ConstraintResult:
        """Check conditional probability constraint.
        
        P(Y and not X) = P(not X) * P(Y | not X)
        """
        p_not_x = Decimal(1) - probabilities.get(self.condition_id, Decimal(0))
        p_y_given_not_x = probabilities.get(self.conditional_id, Decimal(0))
        
        if self.joint_id:
            p_joint = probabilities.get(self.joint_id, Decimal(0))
            expected_joint = p_not_x * p_y_given_not_x
            violation = abs(p_joint - expected_joint)
            
            return ConstraintResult(
                satisfied=violation < Decimal("0.05"),
                violation_amount=violation,
                description=f"P(Y∧¬X)={p_joint:.4f} vs P(¬X)*P(Y|¬X)={expected_joint:.4f}"
            )
        
        # Without joint outcome, we can only check bounds
        return ConstraintResult(
            satisfied=True,
            description="Conditional constraint requires joint outcome for verification"
        )
    
    def get_arb_condition(self, costs: dict[str, Decimal]) -> Optional[Decimal]:
        """Conditional arbitrage is complex and market-dependent."""
        # This would require specific logic based on resolution rules
        return None


def detect_complement_relationship(market: Market) -> Optional[ComplementConstraint]:
    """Detect if a market has a binary Yes/No complement relationship.
    
    Args:
        market: The market to analyze
        
    Returns:
        ComplementConstraint if binary market detected, None otherwise
    """
    if len(market.outcomes) != 2:
        return None
    
    outcome_texts = [o.text.lower() for o in market.outcomes]
    
    # Check for standard Yes/No pattern
    if sorted(outcome_texts) == ["no", "yes"]:
        yes_outcome = next(o for o in market.outcomes if o.text.lower() == "yes")
        no_outcome = next(o for o in market.outcomes if o.text.lower() == "no")
        return ComplementConstraint(yes_outcome.outcome_id, no_outcome.outcome_id)
    
    # Any binary market is a complement by default
    return ComplementConstraint(
        market.outcomes[0].outcome_id,
        market.outcomes[1].outcome_id
    )


def detect_mutually_exclusive_relationship(market: Market) -> Optional[MutuallyExclusiveConstraint]:
    """Detect if a market has mutually exclusive multi-outcome structure.
    
    Args:
        market: The market to analyze
        
    Returns:
        MutuallyExclusiveConstraint if detected, None otherwise
    """
    if len(market.outcomes) <= 2:
        return None  # Use complement for binary
    
    # Multi-outcome market - assume mutually exclusive
    return MutuallyExclusiveConstraint(
        [o.outcome_id for o in market.outcomes]
    )
