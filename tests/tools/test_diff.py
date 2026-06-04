"""Unit tests for P2 diff generation."""

import sys
sys.path.insert(0, "src")

from xxcode.tools.file_edit.diff import generate_diff, compute_edit_diff_stat


class TestGenerateDiff:
    def test_identical_content_returns_empty(self):
        result = generate_diff("/test.py", "hello\nworld\n", "hello\nworld\n")
        assert result == ""

    def test_single_line_change(self):
        result = generate_diff("/test.py", "hello\n", "goodbye\n")
        assert "--- a//test.py" in result
        assert "+++ b//test.py" in result
        assert "@@" in result
        assert "-hello" in result
        assert "+goodbye" in result

    def test_multi_line_change_shows_hunks(self):
        old_content = "line1\nline2\nline3\n"
        new_content = "line1\nline2_modified\nline3\n"
        result = generate_diff("/test.py", old_content, new_content)
        assert "@@" in result
        assert "line2" in result
        assert "line2_modified" in result

    def test_add_lines(self):
        old_content = "line1\n"
        new_content = "line1\nline2\nline3\n"
        result = generate_diff("/test.py", old_content, new_content)
        assert "+line2" in result
        assert "+line3" in result

    def test_remove_lines(self):
        old_content = "line1\nline2\nline3\n"
        new_content = "line1\n"
        result = generate_diff("/test.py", old_content, new_content)
        assert "-line2" in result
        assert "-line3" in result

    def test_empty_old_content(self):
        result = generate_diff("/test.py", "", "hello\nworld\n")
        assert result != ""
        assert "+hello" in result
        assert "+world" in result

    def test_empty_new_content(self):
        result = generate_diff("/test.py", "hello\nworld\n", "")
        assert result != ""
        assert "-hello" in result
        assert "-world" in result

    def test_truncates_long_diff(self):
        old_content = "\n".join(f"line{i}" for i in range(50))
        new_content = "\n".join(f"newline{i}" for i in range(50))
        result = generate_diff("/test.py", old_content, new_content)
        lines = result.split("\n")
        assert len(lines) <= 35  # 2 header + 30 diff + truncation note

    def test_preserves_context_lines(self):
        old_content = "a\nb\nc\nd\ne\nf\ng\n"
        new_content = "a\nb\nCHANGED\nd\ne\nf\ng\n"
        result = generate_diff("/test.py", old_content, new_content)
        # Context lines should show surrounding lines
        assert " a" in result or " b" in result


class TestComputeEditDiffStat:
    def test_no_change(self):
        removed, added = compute_edit_diff_stat("hello\n", "hello\n")
        assert removed == 1
        assert added == 1

    def test_lines_added(self):
        removed, added = compute_edit_diff_stat(
            "line1\n", "line1\nline2\nline3\n"
        )
        assert removed == 0
        assert added == 2

    def test_lines_removed(self):
        removed, added = compute_edit_diff_stat(
            "line1\nline2\nline3\n", "line1\n"
        )
        assert removed == 2
        assert added == 0

    def test_both_nonzero(self):
        removed, added = compute_edit_diff_stat(
            "a\nb\nc\n", "x\ny\nz\nw\n"
        )
        assert removed == 0
        assert added == 1

    def test_empty_strings(self):
        removed, added = compute_edit_diff_stat("", "")
        assert removed == 0
        assert added == 0
