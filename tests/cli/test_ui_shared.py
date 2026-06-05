"""Tests for cli/ui_shared.py helpers."""

import pytest
from xxcode.cli.ui_shared import (
    ASCII_SAFE,
    DISPLAY_RISK_LABELS,
    PHASE1_PERMISSION_ACTION_LABELS,
    RICH_UNICODE,
    build_session_toolbar,
    calculate_session_cost,
    detect_display_mode,
    get_display_symbols,
    normalize_permission_answer,
    translate_backend_risk_level,
)


class TestNormalizePermissionAnswer:
    def test_once_aliases(self):
        assert normalize_permission_answer("y") == "once"
        assert normalize_permission_answer("yes") == "once"
        assert normalize_permission_answer("Y") == "once"

    def test_deny_aliases(self):
        assert normalize_permission_answer("n") == "deny"
        assert normalize_permission_answer("no") == "deny"
        assert normalize_permission_answer("d") == "deny"
        assert normalize_permission_answer("deny") == "deny"
        assert normalize_permission_answer("never") == "deny"

    def test_always_aliases(self):
        assert normalize_permission_answer("a") == "always"
        assert normalize_permission_answer("always") == "always"

    def test_empty_defaults_to_deny(self):
        assert normalize_permission_answer("") == "deny"
        assert normalize_permission_answer("  ") == "deny"

    def test_unknown_defaults_to_deny(self):
        assert normalize_permission_answer("xyz") == "deny"


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


class TestRiskTranslation:
    def test_backend_normal_maps_to_medium_display_risk(self):
        assert translate_backend_risk_level("normal") == "medium"

    def test_backend_high_maps_to_high_display_risk(self):
        assert translate_backend_risk_level("high") == "high"

    def test_direct_ui_risk_is_passthrough(self):
        assert translate_backend_risk_level("low") == "low"
        assert translate_backend_risk_level("medium") == "medium"


class TestDisplayModeDetection:
    def test_utf8_prefers_rich_unicode(self):
        assert detect_display_mode("utf-8") == RICH_UNICODE
        assert detect_display_mode("utf8") == RICH_UNICODE

    def test_non_utf8_prefers_ascii_safe(self):
        assert detect_display_mode("cp1252") == ASCII_SAFE
        assert detect_display_mode("gbk") == ASCII_SAFE

    def test_missing_encoding_falls_back_to_ascii_safe(self):
        assert detect_display_mode(None) == ASCII_SAFE


class TestDisplaySymbols:
    def test_ascii_safe_prompt_and_toolbar_symbols(self):
        symbols = get_display_symbols(ASCII_SAFE)
        assert symbols["prompt.normal"] == ">"
        assert symbols["prompt.yolo"] == "!"
        assert symbols["toolbar.separator"] == " | "

    def test_ascii_safe_tool_icon_mapping(self):
        symbols = get_display_symbols(ASCII_SAFE)
        assert symbols["tool.read_file"] == "[R]"
        assert symbols["tool.write_file"] == "[W]"
        assert symbols["tool.run_shell"] == "[S]"

    def test_rich_unicode_preserves_current_symbols(self):
        symbols = get_display_symbols(RICH_UNICODE)
        assert symbols["prompt.normal"] == "❯"
        assert symbols["prompt.yolo"] == "⚡"
        assert symbols["toolbar.separator"] == " │ "


class TestSharedCopyConstants:
    def test_phase1_permission_action_labels_are_three_actions(self):
        assert list(PHASE1_PERMISSION_ACTION_LABELS) == ["允许一次", "本会话总是允许", "拒绝"]

    def test_display_risk_labels_are_centralized(self):
        assert DISPLAY_RISK_LABELS["low"] == "低风险"
        assert DISPLAY_RISK_LABELS["medium"] == "需确认"
        assert DISPLAY_RISK_LABELS["high"] == "高风险"


class TestToolbarSeparatorOverride:
    def test_build_session_toolbar_accepts_custom_separator(self):
        from types import SimpleNamespace

        state = SimpleNamespace(
            turn_count=5,
            total_input_tokens=9000,
            total_output_tokens=3000,
            permission_state=SimpleNamespace(yolo_mode=True),
        )

        toolbar = build_session_toolbar(
            state,
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
            separator=" | ",
        )

        assert toolbar == "T5 | 12K tok | $0.0720 | ⚡ YOLO"
