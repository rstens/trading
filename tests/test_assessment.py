"""Unit tests for the per-section traffic-light assessment."""

import pytest

from webui.assessment import GREEN, AMBER, RED, assess_job, assess_section

pytestmark = pytest.mark.unit


class TestRatingBased:
    @pytest.mark.parametrize("rating,expected", [
        ("Buy", GREEN),
        ("Overweight", GREEN),
        ("Hold", AMBER),          # pessimism: balanced view is amber, not green
        ("Underweight", RED),     # pessimism: cautious view is red
        ("Sell", RED),
    ])
    def test_five_tier_research_and_portfolio(self, rating, expected):
        for key in ("research", "portfolio"):
            assert assess_section(key, f"**Recommendation**: {rating}\n...") == expected
            assert assess_section(key, f"**Rating**: {rating}\n...") == expected

    @pytest.mark.parametrize("action,expected", [
        ("Buy", GREEN), ("Hold", AMBER), ("Sell", RED),
    ])
    def test_trader_action(self, action, expected):
        md = f"**Action**: {action}\n\n**Reasoning**: x\n\nFINAL TRANSACTION PROPOSAL: **{action.upper()}**"
        assert assess_section("trader", md) == expected

    @pytest.mark.parametrize("level,expected", [
        ("LOW", GREEN), ("MODERATE", AMBER), ("ELEVATED", RED), ("SEVERE", RED),
    ])
    def test_hedging_weakness(self, level, expected):
        assert assess_section("hedging", f"Overall weakness: {level}. Justification...") == expected

    def test_hedging_falls_back_to_most_severe_mentioned(self):
        # No "weakness: X" label, but SEVERE named → pessimism picks it.
        txt = "The scale runs low to high. Here the picture is clearly SEVERE downside."
        assert assess_section("hedging", txt) == RED

    def test_summary_parses_buy_sell_hold(self):
        assert assess_section("summary", "Recommendation: BUY with high conviction.") == GREEN
        assert assess_section("summary", "**Action**: Sell — exit now.") == RED


class TestLexicon:
    def test_clearly_bullish_is_green(self):
        txt = ("Strong revenue growth, robust margins, accelerating momentum and a "
               "compelling breakout. Fundamentals are healthy and the outlook is favorable "
               "with clear upside; the company continues to outperform.")
        assert assess_section("fundamentals", txt) == GREEN

    def test_clearly_bearish_is_red(self):
        txt = ("Deteriorating margins, declining revenue, a guidance cut and a sharp "
               "selloff. Weakness across the board with mounting headwinds and downside risk; "
               "the stock continues to underperform.")
        assert assess_section("market", txt) == RED

    def test_mixed_is_amber(self):
        txt = ("Revenue growth is strong but margins are declining; momentum looks robust "
               "yet headwinds are mounting. A genuinely balanced picture.")
        assert assess_section("market", txt) == AMBER

    def test_no_signal_language_defaults_amber_not_green(self):
        # Pessimism: absence of evidence is not evidence of health.
        assert assess_section("news", "The company held its annual general meeting on Tuesday.") == AMBER

    def test_distress_flag_forces_red_when_not_clearly_bullish(self):
        txt = ("The company announced a dilutive financing and flagged going concern. "
               "Some growth remains.")
        assert assess_section("fundamentals", txt) == RED

    def test_slight_bearish_lean_trips_red(self):
        # Pessimism bias: not a clear positive majority → red, not amber.
        txt = "Margins improved but guidance was cut and the outlook weakened with rising risk."
        assert assess_section("market", txt) == RED


class TestAssessJob:
    def test_only_sections_with_content_get_a_color(self):
        tab_sections = [
            ("market", "Market Analyst", "market_report"),
            ("portfolio", "Portfolio Manager", "final_trade_decision"),
            ("hedging", "Hedging Agent", "hedging_report"),
        ]
        state = {
            "market_report": "Robust growth, strong momentum, healthy and compelling upside.",
            "final_trade_decision": "**Rating**: Sell\n...",
            # hedging_report absent → no dot
        }
        out = assess_job(state, tab_sections)
        assert out == {"market": GREEN, "portfolio": RED}

    def test_empty_state_is_empty(self):
        assert assess_job(None, [("market", "Market Analyst", "market_report")]) == {}

    def test_blank_string_yields_no_color(self):
        assert assess_section("market", "   ") is None
