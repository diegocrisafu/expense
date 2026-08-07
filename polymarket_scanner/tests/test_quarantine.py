"""Unit tests for quarantine verdict logic."""

from polymarket_scanner.quarantine import classify_fill


def market(closed, our_winner, token="tok_yes"):
    return {
        "closed": closed,
        "tokens": [
            {"token_id": token, "winner": our_winner},
            {"token_id": "tok_no", "winner": not our_winner},
        ],
    }


class TestClassifyFill:
    def test_settled_loser_is_phantom(self):
        assert classify_fill("tok_yes", market(True, False)) == "phantom"

    def test_settled_winner_is_genuine(self):
        assert classify_fill("tok_yes", market(True, True)) == "genuine"

    def test_open_market_is_unverified(self):
        assert classify_fill("tok_yes", market(False, False)) == "unverified"

    def test_missing_market_is_unverified(self):
        assert classify_fill("tok_yes", None) == "unverified"

    def test_unknown_token_is_unverified(self):
        assert classify_fill("tok_other", market(True, False)) == "unverified"
