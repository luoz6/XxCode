"""Unit tests for NotebookEditTool — P2."""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, "src")

from pathlib import Path

from xxcode.tools.notebook_edit import NotebookEditTool, NotebookEditInput


def _run(tool, inp, context=None):
    return asyncio.run(tool.execute(inp, context if context is not None else {}))


def _validate(tool, inp, context=None):
    return asyncio.run(tool.validate_input(inp, context if context is not None else {}))


def _make_notebook(cells=None):
    """Create a minimal valid notebook dict."""
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": cells or [],
    }


def _make_cell(cell_id, source, cell_type="code"):
    return {
        "id": cell_id,
        "cell_type": cell_type,
        "metadata": {},
        "source": source,
    }


class TestNotebookEditValidation:
    def test_notebook_not_found(self):
        tool = NotebookEditTool()
        inp = NotebookEditInput(
            notebook_path="/nonexistent/notebook.ipynb",
            cell_id="abc123",
            new_source="print('hello')",
        )
        is_valid, error = _validate(tool, inp)
        assert not is_valid
        assert "File not found" in error

    def test_not_a_notebook_extension(self):
        tool = NotebookEditTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.py")
            Path(filepath).write_text("hello\n", encoding="utf-8")

            inp = NotebookEditInput(
                notebook_path=filepath,
                cell_id="abc123",
                new_source="print('hello')",
            )
            is_valid, error = _validate(tool, inp)
            assert not is_valid
            assert ".ipynb" in error

    def test_insert_requires_cell_type(self):
        tool = NotebookEditTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.ipynb")
            nb = _make_notebook([_make_cell("c1", "print('a')")])
            Path(filepath).write_text(json.dumps(nb), encoding="utf-8")

            inp = NotebookEditInput(
                notebook_path=filepath,
                cell_id="c1",
                new_source="print('b')",
                edit_mode="insert",
            )
            is_valid, error = _validate(tool, inp)
            assert not is_valid
            assert "cell_type" in error


class TestNotebookEditExecution:
    def test_replace_cell_source(self):
        tool = NotebookEditTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.ipynb")
            nb = _make_notebook([_make_cell("c1", "print('hello')")])
            Path(filepath).write_text(json.dumps(nb), encoding="utf-8")

            inp = NotebookEditInput(
                notebook_path=filepath,
                cell_id="c1",
                new_source="print('world')",
            )
            result = _run(tool, inp)
            assert "Notebook edit applied" in result

            updated = json.loads(Path(filepath).read_text(encoding="utf-8"))
            assert updated["cells"][0]["source"] == "print('world')"
            assert updated["cells"][0]["cell_type"] == "code"

    def test_replace_with_cell_type_change(self):
        tool = NotebookEditTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.ipynb")
            nb = _make_notebook([_make_cell("c1", "print('hello')", "code")])
            Path(filepath).write_text(json.dumps(nb), encoding="utf-8")

            inp = NotebookEditInput(
                notebook_path=filepath,
                cell_id="c1",
                new_source="# Markdown content",
                cell_type="markdown",
            )
            result = _run(tool, inp)
            assert "Notebook edit applied" in result

            updated = json.loads(Path(filepath).read_text(encoding="utf-8"))
            assert updated["cells"][0]["cell_type"] == "markdown"

    def test_insert_cell_after(self):
        tool = NotebookEditTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.ipynb")
            nb = _make_notebook([
                _make_cell("c1", "print('first')"),
                _make_cell("c2", "print('third')"),
            ])
            Path(filepath).write_text(json.dumps(nb), encoding="utf-8")

            inp = NotebookEditInput(
                notebook_path=filepath,
                cell_id="c1",
                new_source="print('second')",
                cell_type="code",
                edit_mode="insert",
            )
            result = _run(tool, inp)
            assert "Notebook edit applied" in result

            updated = json.loads(Path(filepath).read_text(encoding="utf-8"))
            assert len(updated["cells"]) == 3
            assert updated["cells"][1]["source"] == "print('second')"

    def test_insert_cell_at_beginning_when_id_not_found(self):
        tool = NotebookEditTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.ipynb")
            nb = _make_notebook([_make_cell("c1", "print('existing')")])
            Path(filepath).write_text(json.dumps(nb), encoding="utf-8")

            inp = NotebookEditInput(
                notebook_path=filepath,
                cell_id="nonexistent",
                new_source="print('first')",
                cell_type="code",
                edit_mode="insert",
            )
            result = _run(tool, inp)
            assert "Notebook edit applied" in result

            updated = json.loads(Path(filepath).read_text(encoding="utf-8"))
            assert len(updated["cells"]) == 2
            assert updated["cells"][0]["source"] == "print('first')"

    def test_delete_cell(self):
        tool = NotebookEditTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.ipynb")
            nb = _make_notebook([
                _make_cell("c1", "keep me"),
                _make_cell("c2", "remove me"),
            ])
            Path(filepath).write_text(json.dumps(nb), encoding="utf-8")

            inp = NotebookEditInput(
                notebook_path=filepath,
                cell_id="c2",
                new_source="",
                edit_mode="delete",
            )
            result = _run(tool, inp)
            assert "Notebook edit applied" in result

            updated = json.loads(Path(filepath).read_text(encoding="utf-8"))
            assert len(updated["cells"]) == 1
            assert updated["cells"][0]["id"] == "c1"

    def test_delete_nonexistent_cell_error(self):
        tool = NotebookEditTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.ipynb")
            nb = _make_notebook([_make_cell("c1", "only cell")])
            Path(filepath).write_text(json.dumps(nb), encoding="utf-8")

            inp = NotebookEditInput(
                notebook_path=filepath,
                cell_id="nonexistent",
                new_source="",
                edit_mode="delete",
            )
            result = _run(tool, inp)
            assert "not found" in result

    def test_cell_not_found_error(self):
        tool = NotebookEditTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.ipynb")
            nb = _make_notebook([_make_cell("c1", "only cell")])
            Path(filepath).write_text(json.dumps(nb), encoding="utf-8")

            inp = NotebookEditInput(
                notebook_path=filepath,
                cell_id="nonexistent",
                new_source="new content",
            )
            result = _run(tool, inp)
            assert "not found" in result

    def test_invalid_json_error(self):
        tool = NotebookEditTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.ipynb")
            Path(filepath).write_text("not valid json{{{", encoding="utf-8")

            inp = NotebookEditInput(
                notebook_path=filepath,
                cell_id="c1",
                new_source="print('hello')",
            )
            result = _run(tool, inp)
            assert "Failed to parse" in result

    def test_preserves_json_indentation(self):
        tool = NotebookEditTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = str(Path(tmpdir) / "test.ipynb")
            nb = _make_notebook([_make_cell("c1", "print('hello')")])
            original = json.dumps(nb, indent=2)
            Path(filepath).write_text(original, encoding="utf-8")

            inp = NotebookEditInput(
                notebook_path=filepath,
                cell_id="c1",
                new_source="print('world')",
            )
            result = _run(tool, inp)
            assert "Notebook edit applied" in result

            updated_text = Path(filepath).read_text(encoding="utf-8")
            # Should still be indented (not minified)
            assert "  " in updated_text or updated_text.count("\n") > 5


class TestNotebookEditUI:
    def test_render_tool_use(self):
        tool = NotebookEditTool()
        inp = NotebookEditInput(
            notebook_path="/path/to/notebook.ipynb",
            cell_id="abc12345",
            new_source="print('hello')",
        )
        rendered = tool.render_tool_use(inp)
        assert "notebook.ipynb" in rendered
        assert "abc12345" in rendered

    def test_backfill_resolves_relative_path(self):
        tool = NotebookEditTool()
        inp = NotebookEditInput(
            notebook_path="relative/notebook.ipynb",
            cell_id="c1",
            new_source="code",
        )
        enriched = tool.backfill_observable_input(inp, {"cwd": "/home/user"})
        assert isinstance(enriched, NotebookEditInput)
        assert Path(enriched.notebook_path).is_absolute()
