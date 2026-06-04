"""Unit tests for EditFileTool - P0/P1/P2 features: error codes, preprocessing,
quote normalization, readFileState, cascading protection, line ending preservation,
diff generation, backfill, notebook redirect."""

import asyncio
import json
import os
import sys

sys.path.insert(0, "src")

import tempfile
from pathlib import Path

from xxcode.tools.file_edit.tool import (
    _check_cascading_edit,
    _count_occurrences,
    _find_actual_string,
    _line_similarity,
    _match_failure_snippet,
    _normalize_quotes,
    _preserve_quote_style,
    _record_applied_edit,
    _strip_trailing_whitespace,
    EditFileTool,
)
from xxcode.tools.file_edit.types import (
    EditErrorCode,
    EditFileInput,
    FileStateEntry,
    _format_error,
    detect_line_endings,
)
from xxcode.tools.file_edit.ui import (
    _parse_error_code,
    _render_edit_error,
    render_tool_use as ui_render_tool_use,
)

# Test helpers: run async methods synchronously
def _run(tool, inp, context=None):
    return asyncio.run(tool.execute(inp, context if context is not None else {}))

def _validate(tool, inp, context=None):
    return asyncio.run(tool.validate_input(inp, context if context is not None else {}))


# -------------------------------------------------------------------------
# Quote normalization
# -------------------------------------------------------------------------

class TestNormalizeQuotes:
    def test_straight_quotes_unchanged(self):
        text = 'He said "hello" and \'goodbye\''
        assert _normalize_quotes(text) == text

    def test_curly_double_open(self):
        assert _normalize_quotes("“quote”") == '"quote"'

    def test_curly_double_close(self):
        assert _normalize_quotes("say “word” now") == 'say "word" now'

    def test_curly_single_quotes(self):
        assert _normalize_quotes("‘word’") == "'word'"

    def test_mixed_curly_straight(self):
        text = 'She said “hello” and "goodbye"'
        assert _normalize_quotes(text) == 'She said "hello" and "goodbye"'

    def test_no_curly_quotes(self):
        text = "plain text without any quotes at all"
        assert _normalize_quotes(text) == text

    def test_empty_string(self):
        assert _normalize_quotes("") == ""


# -------------------------------------------------------------------------
# Trailing whitespace stripping
# -------------------------------------------------------------------------

class TestStripTrailingWhitespace:
    def test_strips_trailing_spaces(self):
        result = _strip_trailing_whitespace("hello   \nworld  \n", "/test/file.py")
        assert result == "hello\nworld\n"

    def test_strips_trailing_tabs(self):
        result = _strip_trailing_whitespace("hello\t\t\nworld\t\n", "/test/file.py")
        assert result == "hello\nworld\n"

    def test_preserves_markdown_trailing_spaces(self):
        text = "line with hard break  \nnext line\n"
        result = _strip_trailing_whitespace(text, "/docs/readme.md")
        assert result == text

    def test_preserves_mdx_trailing_spaces(self):
        text = "line with hard break  \nnext line\n"
        result = _strip_trailing_whitespace(text, "/docs/guide.mdx")
        assert result == text

    def test_no_trailing_whitespace_unchanged(self):
        text = "clean\nlines\n"
        result = _strip_trailing_whitespace(text, "/src/main.py")
        assert result == text

    def test_empty_string(self):
        result = _strip_trailing_whitespace("", "/src/main.py")
        assert result == ""


# -------------------------------------------------------------------------
# Two-stage matching
# -------------------------------------------------------------------------

class TestFindActualString:
    def test_exact_match_fast_path(self):
        content = "hello world\nfoo bar\n"
        found = _find_actual_string(content, "foo bar")
        assert found == "foo bar"

    def test_quote_normalized_match(self):
        content = 'She said “hello” to everyone.'
        search = 'She said "hello" to everyone.'
        found = _find_actual_string(content, search)
        assert found == content

    def test_returns_original_on_exact(self):
        content = "original text"
        found = _find_actual_string(content, "original")
        assert found == "original"

    def test_no_match_returns_none(self):
        content = "line one\nline two\n"
        found = _find_actual_string(content, "line three")
        assert found is None

    def test_match_at_start_of_file(self):
        content = "first line\nsecond line\n"
        found = _find_actual_string(content, "first line")
        assert found == "first line"

    def test_match_at_end_of_file(self):
        content = "first line\nlast line"
        found = _find_actual_string(content, "last line")
        assert found == "last line"


class TestCountOccurrences:
    def test_count_exact_match(self):
        content = "hello world\nhello world\n"
        assert _count_occurrences(content, "hello") == 2

    def test_no_match(self):
        assert _count_occurrences("abc\ndef\n", "xyz") == 0

    def test_curly_quote_counting(self):
        content = 'She said “hello”. He said “hello” too.'
        search = '"hello"'
        assert _count_occurrences(content, search) == 2


# -------------------------------------------------------------------------
# Quote style preservation
# -------------------------------------------------------------------------

class TestPreserveQuoteStyle:
    def test_maps_straight_to_curly_last_wins(self):
        # Positional zip mapping: the last curly variant for a given
        # straight quote wins. Here " comes first, " second.
        original = 'say "hello"'
        actual = 'say “hello”'
        new = 'respond "hi"'
        result = _preserve_quote_style(original, actual, new)
        assert result == 'respond ”hi”'

    def test_no_curly_in_actual_returns_unchanged(self):
        result = _preserve_quote_style('say "hello"', 'say "hello"', 'respond "hi"')
        assert result == 'respond "hi"'

    def test_empty_new_string(self):
        result = _preserve_quote_style('"a"', '“a”', "")
        assert result == ""

    def test_mixed_single_double_curly(self):
        original = "He said \"hello\" and 'goodbye'"
        actual = "He said “hello” and ‘goodbye’"
        new = "She said \"hi\" and 'bye'"
        result = _preserve_quote_style(original, actual, new)
        assert result == "She said ”hi” and ’bye’"


# -------------------------------------------------------------------------
# Line similarity
# -------------------------------------------------------------------------

class TestLineSimilarity:
    def test_identical_lines(self):
        assert _line_similarity("hello world", "hello world") == 1.0

    def test_partial_overlap(self):
        assert 0.0 < _line_similarity("hello world", "hello there") < 1.0

    def test_no_overlap(self):
        assert _line_similarity("abc def", "xyz pqr") == 0.0

    def test_one_empty(self):
        assert _line_similarity("hello", "") == 0.0

    def test_both_empty(self):
        assert _line_similarity("", "") == 1.0  # identical strings


# -------------------------------------------------------------------------
# Match failure snippet
# -------------------------------------------------------------------------

class TestMatchFailureSnippet:
    def test_finds_closest_line(self):
        content = "import os\nimport sys\nimport json\n"
        result = _match_failure_snippet(content, "import sys\n")
        assert "Closest match" in result

    def test_empty_first_line_returns_empty(self):
        result = _match_failure_snippet("some content", "\n\nrest")
        assert result == ""

    def test_no_similar_lines(self):
        result = _match_failure_snippet("aaa\nbbb\nccc\n", "zzzzzzzzz")
        assert "No similar lines found" in result


# -------------------------------------------------------------------------
# Error codes
# -------------------------------------------------------------------------

class TestErrorCodes:
    def test_format_error_with_detail(self):
        result = _format_error(EditErrorCode.STRING_NOT_FOUND, "not found in file")
        assert "[ErrCode 8]" in result
        assert "STRING_NOT_FOUND" in result
        assert "not found in file" in result

    def test_format_error_no_detail(self):
        result = _format_error(EditErrorCode.NO_OP)
        assert "[ErrCode 1]" in result
        assert "NO_OP" in result

    def test_all_codes_unique_values(self):
        values = [e.value for e in EditErrorCode]
        assert len(values) == len(set(values))

    def test_ok_is_zero(self):
        assert EditErrorCode.OK.value == 0


# -------------------------------------------------------------------------
# UI: error code parsing
# -------------------------------------------------------------------------

class TestParseErrorCode:
    def test_parses_valid_code(self):
        assert _parse_error_code("[ErrCode 8] STRING_NOT_FOUND: not found") == EditErrorCode.STRING_NOT_FOUND

    def test_parses_code_1(self):
        assert _parse_error_code("[ErrCode 1] NO_OP: same strings") == EditErrorCode.NO_OP

    def test_no_err_code_returns_none(self):
        assert _parse_error_code("Error: something went wrong") is None

    def test_invalid_number_returns_none(self):
        assert _parse_error_code("[ErrCode 99] UNKNOWN: blah") is None


class TestRenderEditError:
    def test_string_not_found(self):
        result = _render_edit_error("[ErrCode 8] STRING_NOT_FOUND: not in file")
        assert "re-read the file" in result.lower()

    def test_multiple_matches(self):
        result = _render_edit_error("[ErrCode 9] MULTIPLE_MATCHES: found 5 matches")
        assert "replace_all=true" in result

    def test_no_op(self):
        result = _render_edit_error("[ErrCode 1] NO_OP: same strings")
        assert "identical" in result.lower()

    def test_unknown_code_fallback(self):
        result = _render_edit_error("[ErrCode 11] WRITE_FAILED: disk full")
        assert "file write error" in result.lower()

    def test_no_code_fallback(self):
        result = _render_edit_error("Random error message here")
        assert "Edit failed" in result


# -------------------------------------------------------------------------
# Integration: end-to-end edit pipeline
# -------------------------------------------------------------------------

class TestEditFileIntegration:
    def test_basic_replacement(self):
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            Path(filepath).write_text("hello world\n", encoding="utf-8")

            inp = EditFileInput(
                file_path=filepath,
                old_string="hello world",
                new_string="goodbye world",
            )
            result = _run(tool, inp)
            assert "Edit applied successfully" in result

            content = Path(filepath).read_text(encoding="utf-8")
            assert content == "goodbye world\n"

    def test_no_op_rejected(self):
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            Path(filepath).write_text("hello\n", encoding="utf-8")

            inp = EditFileInput(
                file_path=filepath,
                old_string="hello",
                new_string="hello",
            )
            is_valid, error = _validate(tool, inp)
            assert not is_valid
            assert "[ErrCode 1]" in error

    def test_string_not_found_error(self):
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            Path(filepath).write_text("hello world\n", encoding="utf-8")

            inp = EditFileInput(
                file_path=filepath,
                old_string="nonexistent text",
                new_string="replacement",
            )
            result = _run(tool, inp)
            assert "[ErrCode 8]" in result

    def test_curly_quote_matching(self):
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            Path(filepath).write_text(
                'greeting = “hello world”\n', encoding="utf-8"
            )

            inp = EditFileInput(
                file_path=filepath,
                old_string='greeting = "hello world"',
                new_string='greeting = "goodbye world"',
            )
            result = _run(tool, inp)
            assert "Edit applied successfully" in result

            content = Path(filepath).read_text(encoding="utf-8")
            assert content == 'greeting = ”goodbye world”\n'

    def test_trailing_whitespace_stripped(self):
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            Path(filepath).write_text("line one\nline two\n", encoding="utf-8")

            inp = EditFileInput(
                file_path=filepath,
                old_string="line one",
                new_string="line one updated   \t  ",
            )
            result = _run(tool, inp)
            assert "Edit applied successfully" in result

            content = Path(filepath).read_text(encoding="utf-8")
            assert "   " not in content
            assert content == "line one updated\nline two\n"

    def test_file_not_found_error(self):
        tool = EditFileTool()
        inp = EditFileInput(
            file_path="/nonexistent/path/to/file.py",
            old_string="x",
            new_string="y",
        )
        is_valid, error = _validate(tool, inp)
        assert not is_valid
        assert "[ErrCode 4]" in error

    def test_replace_all(self):
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            Path(filepath).write_text("TODO\nTODO\nTODO\n", encoding="utf-8")

            inp = EditFileInput(
                file_path=filepath,
                old_string="TODO",
                new_string="DONE",
                replace_all=True,
            )
            result = _run(tool, inp)
            assert "3 occurrence(s)" in result
            content = Path(filepath).read_text(encoding="utf-8")
            assert content == "DONE\nDONE\nDONE\n"

    def test_multiple_matches_no_replace_all(self):
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            Path(filepath).write_text("dup\ndup\n", encoding="utf-8")

            inp = EditFileInput(
                file_path=filepath,
                old_string="dup",
                new_string="unique",
            )
            result = _run(tool, inp)
            assert "[ErrCode 9]" in result

    def test_delete_with_newline_heuristic(self):
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            Path(filepath).write_text("keep me\nremove me\nkeep me\n", encoding="utf-8")

            inp = EditFileInput(
                file_path=filepath,
                old_string="remove me",
                new_string="",
            )
            result = _run(tool, inp)
            assert "Edit applied successfully" in result
            content = Path(filepath).read_text(encoding="utf-8")
            assert content == "keep me\nkeep me\n"


# -------------------------------------------------------------------------
# UI rendering
# -------------------------------------------------------------------------

class TestUIRenderUse:
    def test_single_line_diff(self):
        result = ui_render_tool_use("/src/main.py", "old_func", "new_func")
        assert "edit_file" in result
        assert "-old_func" in result
        assert "+new_func" in result

    def test_multi_line_diff(self):
        result = ui_render_tool_use(
            "/src/main.py", "line1\nline2\nline3", "line1\nchanged\nline3",
        )
        assert "edit_file" in result
        assert "-" in result
        assert "+" in result

    def test_replace_all_label(self):
        result = ui_render_tool_use("/src/main.py", "TODO", "DONE", replace_all=True)
        assert "(all)" in result


# -------------------------------------------------------------------------
# P1: Line ending detection
# -------------------------------------------------------------------------

class TestDetectLineEndings:
    def test_lf_only(self):
        assert detect_line_endings("line1\nline2\n") == "\n"

    def test_crlf_dominant(self):
        assert detect_line_endings("line1\r\nline2\r\n") == "\r\n"

    def test_crlf_wins_with_majority(self):
        content = "a\r\nb\r\nc\nd\r\n"
        assert detect_line_endings(content) == "\r\n"

    def test_lf_wins_with_majority(self):
        content = "a\r\nb\nc\nd\n"
        assert detect_line_endings(content) == "\n"

    def test_empty_content_defaults_lf(self):
        assert detect_line_endings("") == "\n"


# -------------------------------------------------------------------------
# P1: FileStateEntry serialization
# -------------------------------------------------------------------------

class TestFileStateEntry:
    def test_to_dict_and_back(self):
        entry = FileStateEntry(
            content="hello\nworld\n",
            timestamp=1234567890.5,
            is_partial_view=False,
            line_endings="\n",
        )
        d = entry.to_dict()
        restored = FileStateEntry.from_dict(d)
        assert restored.content == entry.content
        assert restored.timestamp == entry.timestamp
        assert restored.is_partial_view == entry.is_partial_view
        assert restored.line_endings == entry.line_endings

    def test_partial_view_flag(self):
        entry = FileStateEntry(
            content="partial", timestamp=0.0,
            is_partial_view=True, line_endings="\r\n",
        )
        assert entry.is_partial_view is True
        assert entry.line_endings == "\r\n"


# -------------------------------------------------------------------------
# P1: Read-before-edit enforcement (ErrCode 6, 7)
# -------------------------------------------------------------------------

class TestReadBeforeEdit:
    def test_unread_file_blocked(self):
        """ErrCode 6: editing a file that was never read must fail."""
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            Path(filepath).write_text("hello\n", encoding="utf-8")

            inp = EditFileInput(
                file_path=filepath,
                old_string="hello",
                new_string="goodbye",
            )
            # No readFileState in context → ErrCode 6
            is_valid, error = _validate(tool, inp)
            assert not is_valid
            assert "[ErrCode 6]" in error

    def test_partial_view_blocked(self):
        """ErrCode 6: partially read files cannot be edited."""
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            Path(filepath).write_text("hello\nworld\n", encoding="utf-8")

            # Simulate partial read via a mock AgentState
            class MockState:
                read_file_state = {}

            mock_state = MockState()
            mock_state.read_file_state[filepath] = FileStateEntry(
                content="hello\nworld\n",
                timestamp=os.path.getmtime(filepath),
                is_partial_view=True,
            )

            inp = EditFileInput(
                file_path=filepath,
                old_string="hello",
                new_string="goodbye",
            )
            is_valid, error = _validate(tool, inp, {"parent_state": mock_state})
            assert not is_valid
            assert "[ErrCode 6]" in error

    def test_stale_read_blocked(self):
        """ErrCode 7: file modified after read must be re-read."""
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            # Write DIFFERENT content than what's in the cache so the
            # Windows content-comparison fallback also triggers the error.
            Path(filepath).write_text("modified content\n", encoding="utf-8")

            class MockState:
                read_file_state = {}

            mock_state = MockState()
            mock_state.read_file_state[filepath] = FileStateEntry(
                content="original content\n",
                timestamp=0.0,  # Very old timestamp
            )

            inp = EditFileInput(
                file_path=filepath,
                old_string="modified content",
                new_string="goodbye",
            )
            is_valid, error = _validate(tool, inp, {"parent_state": mock_state})
            assert not is_valid
            assert "[ErrCode 7]" in error

    def test_read_file_allows_edit(self):
        """Having a fresh readFileState entry allows the edit."""
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            Path(filepath).write_text("hello\n", encoding="utf-8")

            class MockState:
                read_file_state = {}

            mock_state = MockState()
            mock_state.read_file_state[filepath] = FileStateEntry(
                content="hello\n",
                timestamp=os.path.getmtime(filepath),
                is_partial_view=False,
            )

            inp = EditFileInput(
                file_path=filepath,
                old_string="hello",
                new_string="goodbye",
            )
            is_valid, error = _validate(tool, inp, {"parent_state": mock_state})
            assert is_valid

    def test_new_file_semantic_bypasses_read_check(self):
        """Empty old_string (create-new-file) skips readFileState check."""
        tool = EditFileTool()
        inp = EditFileInput(
            file_path="/nonexistent/path.py",
            old_string="",
            new_string="# new file\n",
        )
        # Should fail at FILE_NOT_FOUND, not UNREAD_FILE
        is_valid, error = _validate(tool, inp)
        assert "[ErrCode 4]" in error  # FILE_NOT_FOUND, not UNREAD_FILE


# -------------------------------------------------------------------------
# P1: Cascading edit protection
# -------------------------------------------------------------------------

class TestCascadingEditProtection:
    def test_no_previous_edits_passes(self):
        result = _check_cascading_edit({}, "/test.py", "bar()")
        assert result is None

    def test_old_is_substring_of_previous_new(self):
        context = {}
        _record_applied_edit(context, "/test.py", "foo()", "foo() // calls bar()")
        result = _check_cascading_edit(context, "/test.py", "bar()")
        assert result is not None
        assert "Cascading edit" in result

    def test_distinct_strings_passes(self):
        context = {}
        _record_applied_edit(context, "/test.py", "foo()", "foo_v2()")
        result = _check_cascading_edit(context, "/test.py", "bar()")
        assert result is None

    def test_different_files_no_conflict(self):
        context = {}
        _record_applied_edit(context, "/file_a.py", "foo", "foo // calls bar")
        result = _check_cascading_edit(context, "/file_b.py", "bar")
        assert result is None

    def test_empty_old_string_skipped(self):
        context = {}
        _record_applied_edit(context, "/test.py", "old", "contains bar")
        result = _check_cascading_edit(context, "/test.py", "")
        assert result is None

    def test_trimmed_newline_in_old(self):
        context = {}
        _record_applied_edit(context, "/test.py", "x", "line with foo\n")
        result = _check_cascading_edit(context, "/test.py", "foo\n")
        assert result is not None
        assert "Cascading edit" in result


# -------------------------------------------------------------------------
# P1: Line ending preservation (integration)
# -------------------------------------------------------------------------

class TestLineEndingPreservation:
    def test_crlf_preserved_after_edit(self):
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            # Write a CRLF file using binary mode
            raw = b"line one\r\nline two\r\n"
            Path(filepath).write_bytes(raw)

            # Provide a valid readFileState to pass validation
            class MockState:
                read_file_state = {}

            mock_state = MockState()
            mock_state.read_file_state[filepath] = FileStateEntry(
                content="line one\nline two\n",
                timestamp=os.path.getmtime(filepath),
                is_partial_view=False,
                line_endings="\r\n",
            )

            inp = EditFileInput(
                file_path=filepath,
                old_string="line one",
                new_string="line one updated",
            )
            result = _run(tool, inp, {"parent_state": mock_state})
            assert "Edit applied successfully" in result

            # Verify CRLF preserved
            disk_bytes = Path(filepath).read_bytes()
            assert b"\r\n" in disk_bytes
            assert disk_bytes == b"line one updated\r\nline two\r\n"

    def test_lf_preserved_after_edit(self):
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            # Write an LF file using binary mode
            raw = b"line one\nline two\n"
            Path(filepath).write_bytes(raw)

            class MockState:
                read_file_state = {}

            mock_state = MockState()
            mock_state.read_file_state[filepath] = FileStateEntry(
                content="line one\nline two\n",
                timestamp=os.path.getmtime(filepath),
                is_partial_view=False,
                line_endings="\n",
            )

            inp = EditFileInput(
                file_path=filepath,
                old_string="line one",
                new_string="line one updated",
            )
            result = _run(tool, inp, {"parent_state": mock_state})
            assert "Edit applied successfully" in result

            disk_bytes = Path(filepath).read_bytes()
            assert b"\r\n" not in disk_bytes
            assert disk_bytes == b"line one updated\nline two\n"


# -------------------------------------------------------------------------
# P2: backfill_observable_input — path resolution
# -------------------------------------------------------------------------

class TestBackfillObservableInput:
    def test_resolves_relative_path(self):
        tool = EditFileTool()
        inp = EditFileInput(
            file_path="relative/file.py",
            old_string="old",
            new_string="new",
        )
        enriched = tool.backfill_observable_input(inp, {"cwd": "/home/user"})
        assert isinstance(enriched, EditFileInput)
        assert Path(enriched.file_path).is_absolute()
        assert enriched.old_string == "old"
        assert enriched.new_string == "new"
        assert enriched.replace_all is False

    def test_absolute_path_unchanged(self):
        tool = EditFileTool()
        # Use a platform-absolute path
        abs_path = str(Path("/absolute/path/file.py").resolve())
        inp = EditFileInput(
            file_path=abs_path,
            old_string="old",
            new_string="new",
            replace_all=True,
        )
        enriched = tool.backfill_observable_input(inp, {"cwd": "/ignored"})
        assert enriched.file_path == abs_path
        assert enriched.replace_all is True

    def test_default_cwd_fallback(self):
        tool = EditFileTool()
        inp = EditFileInput(
            file_path="relative/file.py",
            old_string="old",
            new_string="new",
        )
        enriched = tool.backfill_observable_input(inp, {})
        assert Path(enriched.file_path).is_absolute()

    def test_returns_new_instance(self):
        tool = EditFileTool()
        inp = EditFileInput(
            file_path="test.py",
            old_string="x",
            new_string="y",
        )
        enriched = tool.backfill_observable_input(inp, {"cwd": "/tmp"})
        assert enriched is not inp


# -------------------------------------------------------------------------
# P2: Notebook redirect — .ipynb extension → NOTEBOOK_REDIRECT
# -------------------------------------------------------------------------

class TestNotebookRedirect:
    def test_ipynb_file_redirected(self):
        """ErrCode 5: .ipynb files must use notebook_edit, not edit_file."""
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "notebook.ipynb")
            nb = json.dumps({
                "nbformat": 4, "nbformat_minor": 5,
                "metadata": {}, "cells": [],
            })
            Path(filepath).write_text(nb, encoding="utf-8")

            inp = EditFileInput(
                file_path=filepath,
                old_string="old",
                new_string="new",
            )
            is_valid, error = _validate(tool, inp)
            assert not is_valid
            assert "[ErrCode 5]" in error

    def test_py_file_not_redirected(self):
        """Regular .py files should not trigger NOTEBOOK_REDIRECT."""
        tool = EditFileTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "script.py")
            Path(filepath).write_text("hello\n", encoding="utf-8")

            # Without readFileState, it should fail at UNREAD_FILE (6),
            # not NOTEBOOK_REDIRECT (5).
            inp = EditFileInput(
                file_path=filepath,
                old_string="hello",
                new_string="world",
            )
            is_valid, error = _validate(tool, inp)
            assert "[ErrCode 6]" in error  # UNREAD_FILE, not NOTEBOOK_REDIRECT
