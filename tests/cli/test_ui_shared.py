"""Tests for cli/ui_shared.py helpers."""

import pytest
from xxcode.cli.ui_shared import (
    build_session_toolbar,
    calculate_session_cost,
    normalize_permission_answer,
)


class TestNormalizePermissionAnswer:
    def test_yes(self):
        assert normalize_permission_answer("y") == "yes"
        assert normalize_permission_answer("yes") == "yes"
        assert normalize_permission_answer("Y") == "yes"

    def test_no(self):
        assert normalize_permission_answer("n") == "no"
        assert normalize_permission_answer("no") == "no"
        assert normalize_permission_answer("N") == "no"

    def test_always(self):
        assert normalize_permission_answer("a") == "always"
        assert normalize_permission_answer("always") == "always"

    def test_deny_all(self):
        assert normalize_permission_answer("d") == "deny_all"
        assert normalize_permission_answer("deny") == "deny_all"
        assert normalize_permission_answer("deny_all") == "deny_all"
        assert normalize_permission_answer("never") == "deny_all"

    def test_empty_defaults_to_no(self):
        assert normalize_permission_answer("") == "no"
        assert normalize_permission_answer("  ") == "no"

    def test_unknown_first_char(self):
        assert normalize_permission_answer("xyz") == "no"


class TestCalculateSessionCost:
    def test_zero_cost(self):
        cost = calculate_session_cost(0, 0, input_price_per_1k=0.003, output_price_per_1k=0.015)
        assert cost == 0.0

    def test_input_only_cost(self):
        cost = calculate_session_cost(1000, 0, input_price_per_1k=0.003, output_price_per_1k=0.015)
        assert cost == 0.003

    def test_output_only_cost(self):
        cost = calculate_session_cost(0, 1000, input_price_per_1k=0.003, output_price_per_1k=0.015)
        assert cost == 0.015

    def test_mixed_cost(self):
        cost = calculate_session_cost(2000, 500, input_price_per_1k=0.01, output_price_per_1k=0.02)
        assert cost == 0.03  # (2000/1000)*0.01 + (500/1000)*0.02 = 0.02 + 0.01

    def test_fractional_tokens(self):
        cost = calculate_session_cost(500, 250, input_price_per_1k=0.003, output_price_per_1k=0.015)
        assert cost == pytest.approx(0.0015 + 0.00375, rel=1e-9)


class TestBuildSessionToolbar:
    def _make_state(self, **kwargs):
        from types import SimpleNamespace
        defaults = dict(turn_count=0, total_input_tokens=0, total_output_tokens=0, permission_state=None)
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_none_state_returns_empty(self):
        assert build_session_toolbar(None, input_price_per_1k=0.003, output_price_per_1k=0.015) == ""

    def test_turns_only(self):
        state = self._make_state(turn_count=5)
        result = build_session_toolbar(state, input_price_per_1k=0.003, output_price_per_1k=0.015)
        assert "T5" in result

    def test_zero_turns_hidden(self):
        state = self._make_state(turn_count=0)
        result = build_session_toolbar(state, input_price_per_1k=0.003, output_price_per_1k=0.015)
        assert "T0" not in result

    def test_tokens_below_1k(self):
        state = self._make_state(turn_count=1, total_input_tokens=500, total_output_tokens=200)
        result = build_session_toolbar(state, input_price_per_1k=0.003, output_price_per_1k=0.015)
        assert "700 tok" in result

    def test_tokens_above_1k(self):
        state = self._make_state(turn_count=1, total_input_tokens=2000, total_output_tokens=500)
        result = build_session_toolbar(state, input_price_per_1k=0.003, output_price_per_1k=0.015)
        assert "2K tok" in result

    def test_zero_tokens_hidden(self):
        state = self._make_state(turn_count=1, total_input_tokens=0, total_output_tokens=0)
        result = build_session_toolbar(state, input_price_per_1k=0.003, output_price_per_1k=0.015)
        assert "tok" not in result
        assert "0 tok" not in result

    def test_cost_shown(self):
        state = self._make_state(turn_count=2, total_input_tokens=1000, total_output_tokens=500)
        result = build_session_toolbar(state, input_price_per_1k=0.003, output_price_per_1k=0.015)
        assert "$" in result

    def test_very_low_cost_hidden(self):
        state = self._make_state(turn_count=1, total_input_tokens=1, total_output_tokens=1)
        result = build_session_toolbar(state, input_price_per_1k=0.003, output_price_per_1k=0.015)
        assert "$" not in result  # Cost < 0.0001

    def test_yolo_mode_shown(self):
        ps = type("PS", (), {"yolo_mode": True})()
        state = self._make_state(turn_count=3, total_input_tokens=1000, total_output_tokens=500, permission_state=ps)
        result = build_session_toolbar(state, input_price_per_1k=0.003, output_price_per_1k=0.015)
        assert "YOLO" in result

    def test_yolo_absent_when_off(self):
        ps = type("PS", (), {"yolo_mode": False})()
        state = self._make_state(turn_count=1, permission_state=ps)
        result = build_session_toolbar(state, input_price_per_1k=0.003, output_price_per_1k=0.015)
        assert "YOLO" not in result

    def test_display_order(self):
        """Verify display priority: turn, token, cost, mode."""
        from xxcode.cli.ui_shared import TOOLBAR_SEPARATOR
        ps = type("PS", (), {"yolo_mode": True})()
        state = self._make_state(turn_count=5, total_input_tokens=2000, total_output_tokens=500, permission_state=ps)
        result = build_session_toolbar(state, input_price_per_1k=0.003, output_price_per_1k=0.015)
        parts = result.split(TOOLBAR_SEPARATOR)
        assert parts[0] == "T5"
        assert "K tok" in parts[1]
        assert "$" in parts[2]
        assert "YOLO" in parts[-1]
