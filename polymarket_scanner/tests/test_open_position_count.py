"""Regression tests: the open-position risk gate must be derived from the DB.

The in-memory counter only ever leaked upward — resolutions and most
closure paths never decremented it — so it latched at MAX_OPEN_POSITIONS
on Jul 19 and blocked every trade for weeks while true exposure was one
position.  A restart had the opposite bug: the counter reset to 0 and
understated real exposure.
"""

from decimal import Decimal

import pytest

from polymarket_scanner.executor import TradingExecutor


@pytest.fixture
def ex():
    e = TradingExecutor(paper_trading=True)
    e.balance = Decimal("100")
    e._initialized = True
    return e


class TestDbDerivedCount:
    def test_leaked_memory_counter_must_not_block_trading(self, ex, monkeypatch):
        """The incident: memory says 15, the DB says 1 — trade must be allowed."""
        ex.open_positions = 15
        monkeypatch.setattr("polymarket_scanner.reconcile.count_truly_open",
                            lambda db_path=None: 1)
        allowed, reason = ex.check_risk_limits(Decimal("1"))
        assert allowed, reason

    def test_cold_start_must_not_reset_exposure(self, ex, monkeypatch):
        """The restart bug: memory says 0, the DB says 15 — trade must block."""
        ex.open_positions = 0
        monkeypatch.setattr("polymarket_scanner.reconcile.count_truly_open",
                            lambda db_path=None: 15)
        allowed, reason = ex.check_risk_limits(Decimal("1"))
        assert not allowed
        assert "Max open positions" in reason

    def test_db_error_falls_back_to_cached_count(self, ex, monkeypatch):
        """If the DB read fails, fail safe on the cached value."""
        ex.open_positions = 15

        def boom(db_path=None):
            raise RuntimeError("db locked")
        monkeypatch.setattr("polymarket_scanner.reconcile.count_truly_open", boom)
        allowed, reason = ex.check_risk_limits(Decimal("1"))
        assert not allowed

    def test_initialize_reconciles_count_from_db(self, monkeypatch):
        """Startup must load real exposure, not start at zero."""
        monkeypatch.setattr("polymarket_scanner.reconcile.count_truly_open",
                            lambda db_path=None: 7)
        e = TradingExecutor(paper_trading=True)
        e.initialize()
        assert e.open_positions == 7
