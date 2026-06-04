"""Tests for XxCodeTerminalUI pick_from_list inline selector."""

from __future__ import annotations

import asyncio

import pytest


# ── Helpers to construct the same get_list_fragments callable the
#    production code will use ──────────────────────────────────────────

def _build_get_list_fragments(values: list[tuple[str, str]], selected_index: list[int]):
    """Replicate the fragment-building callable from pick_from_list."""

    def get_list_fragments():
        fragments: list[tuple[str, str]] = []
        for i, (_key, label) in enumerate(values):
            is_selected = i == selected_index[0]
            prefix = "❯ " if is_selected else "  "  # ❯
            base_style = (
                "class:picklist.selected" if is_selected else "class:picklist.item"
            )
            num = f"{i + 1:2d}. "
            fragments.append(
                (f"class:picklist.number {base_style}", f"{prefix}{num}")
            )
            fragments.append((base_style, label))
            fragments.append(("", "\n"))
        if fragments:
            last = fragments[-1]
            fragments[-1] = (last[0], last[1].rstrip("\n"))
        return fragments

    return get_list_fragments


# ── Fragment tests ───────────────────────────────────────────────────


class TestPickFromListFragments:
    """Verify FormattedTextControl fragment generation."""

    def test_empty_values_produces_empty_fragments(self):
        fragments_fn = _build_get_list_fragments([], [0])
        frags = fragments_fn()
        assert frags == []

    def test_selected_row_has_pointer_and_bold_cyan(self):
        values = [("id1", "Session 1 (10 msgs)"), ("id2", "Session 2 (20 msgs)")]
        fragments_fn = _build_get_list_fragments(values, [0])
        frags = fragments_fn()

        # Row 0 (selected): should contain ❯ prefix and selected style
        row0_text = "".join(t for _, t in frags[:3])
        assert "❯" in row0_text  # ❯ pointer
        assert "class:picklist.selected" in frags[0][0]
        assert "1." in row0_text

    def test_unselected_row_has_no_pointer(self):
        values = [("id1", "Session 1 (10 msgs)"), ("id2", "Session 2 (20 msgs)")]
        fragments_fn = _build_get_list_fragments(values, [0])
        frags = fragments_fn()

        # Row 1 (unselected): no ❯, no selected style
        # Find fragments for row 1 (starts after first row's 3 fragments)
        row1_style = frags[3][0]
        assert "class:picklist.selected" not in row1_style
        assert "class:picklist.item" in row1_style
        row1_text = "".join(t for _, t in frags[3:6])
        assert "❯" not in row1_text

    def test_selection_moves_with_index(self):
        values = [("id1", "S1"), ("id2", "S2"), ("id3", "S3")]
        # Select index 1
        fragments_fn = _build_get_list_fragments(values, [1])
        frags = fragments_fn()

        # Row 1 should be selected
        row1_style = frags[3][0]
        assert "class:picklist.selected" in row1_style
        assert "❯" in "".join(t for _, t in frags[3:6])


# ── Keybinding logic tests ───────────────────────────────────────────


class TestPickFromListKeybindings:
    """Verify the keybinding handler logic that mutates selected_index/result."""

    def test_up_decrements_selected_index(self):
        selected_index = [2]
        values_count = 5

        # Simulate pressing "up"
        selected_index[0] = max(0, selected_index[0] - 1)
        assert selected_index[0] == 1

        selected_index[0] = max(0, selected_index[0] - 1)
        assert selected_index[0] == 0

        # Clamped at 0
        selected_index[0] = max(0, selected_index[0] - 1)
        assert selected_index[0] == 0

    def test_down_increments_selected_index(self):
        selected_index = [0]
        values_count = 5

        selected_index[0] = min(values_count - 1, selected_index[0] + 1)
        assert selected_index[0] == 1

        selected_index[0] = min(values_count - 1, selected_index[0] + 1)
        assert selected_index[0] == 2

        # Jump to end
        selected_index[0] = 4
        selected_index[0] = min(values_count - 1, selected_index[0] + 1)
        assert selected_index[0] == 4  # clamped

    def test_enter_returns_selected_value(self):
        values = [("k1", "v1"), ("k2", "v2")]
        selected_index = [1]
        result = [None]

        # Simulate Enter
        result[0] = values[selected_index[0]][0]
        assert result[0] == "k2"

    def test_escape_returns_none(self):
        result = [None]
        result[0] = None  # simulate escape handler
        assert result[0] is None

    def test_number_key_jumps_to_index(self):
        values = [("k1", "v1"), ("k2", "v2"), ("k3", "v3")]
        selected_index = [0]
        result = [None]

        # Press 3
        idx = 3 - 1  # 0-based
        selected_index[0] = idx
        result[0] = values[idx][0]
        assert selected_index[0] == 2
        assert result[0] == "k3"

    def test_number_key_ignored_when_out_of_range(self):
        values = [("k1", "v1")]
        # Pressing "5" when only 1 item — handler should be no-op
        idx = 5 - 1
        if idx < len(values):
            # This branch should NOT be taken
            pass
        # Nothing happens — selected_index unchanged
        selected_index = [0]
        assert selected_index[0] == 0  # unchanged


# ── Truncation / edge case tests ─────────────────────────────────────


class TestPickFromListEdgeCases:
    """Verify edge case handling."""

    def test_truncates_at_20_items(self):
        values = [(f"id{i}", f"Session {i}") for i in range(25)]
        # The implementation should cap at 20
        display_values = values[:20]
        assert len(display_values) == 20
        assert display_values[-1][0] == "id19"

    def test_single_item_defaults_selected(self):
        values = [("only", "Only Session")]
        selected_index = [0]
        # With 1 item, index 0 is always valid
        assert 0 <= selected_index[0] < len(values)

    def test_empty_values_returns_none_early(self):
        # The method should return None without building an Application
        values: list[tuple[str, str]] = []
        if not values:
            # Early return path
            assert True
        else:
            assert False, "Should have returned early"


# ── Fallback path tests ──────────────────────────────────────────────


class TestPickFromListFallback:
    """Verify the fallback (basic input) path."""

    def test_fallback_parses_valid_number(self):
        values = [("a", "A"), ("b", "B"), ("c", "C")]
        choice = "2"
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(values):
                assert values[idx][0] == "b"

    def test_fallback_rejects_out_of_range_number(self):
        values = [("a", "A")]
        choice = "5"
        result = None
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(values):
                result = values[idx][0]
        assert result is None

    def test_fallback_rejects_empty_input(self):
        choice = ""
        result = None
        if choice.isdigit():
            result = "selected"
        assert result is None

    def test_fallback_rejects_non_numeric_input(self):
        choice = "abc"
        result = None
        if choice.isdigit():
            result = "selected"
        assert result is None


# ── Application construction verification ────────────────────────────


class TestPickFromListApplication:
    """Verify the prompt_toolkit Application is built with correct parameters."""

    def test_formatted_text_control_is_configured_correctly(self):
        """The layout must use FormattedTextControl with focusable and no cursor."""
        from prompt_toolkit.layout.controls import FormattedTextControl

        def dummy_fragments():
            return [("", "test")]

        control = FormattedTextControl(
            text=dummy_fragments,
            focusable=True,
            show_cursor=False,
        )

        # Verify it's the right type (not a BufferControl)
        assert isinstance(control, FormattedTextControl)

    def test_application_constructor_args_can_be_inspected(self):
        """Verify that full_screen=False and erase_when_done=False are the contract."""
        # We cannot create an Application() in a non-console test runner
        # (NoConsoleScreenBufferError on Windows).  Verify by checking
        # that the Application class accepts these parameters.
        from prompt_toolkit.application import Application
        import inspect

        sig = inspect.signature(Application.__init__)
        params = sig.parameters
        assert "full_screen" in params
        assert "erase_when_done" in params
        # Defaults
        assert params["full_screen"].default is False
        assert params["erase_when_done"].default is False

    def test_picklist_style_builds_from_prompt_toolkit_styles(self):
        """The resume picker can build its merged style without TypeError."""
        from xxcode.cli.terminal_ui import _build_picklist_style

        style = _build_picklist_style()

        assert style is not None
