import asyncio
from types import SimpleNamespace

from xxcode.cli import PromptToolkitFullscreenUI, XxCodeTerminalUI, create_ui
from xxcode.cli.terminal_ui import _build_toolbar
from xxcode.cli.theme import PROMPT_SYMBOLS
from xxcode.cli.theme import tool_risk_level
from xxcode.cli.ui_shared import (
    YOLO_LABEL,
    build_session_toolbar,
    format_cwd_for_display,
)
from xxcode.config import Config
from xxcode.main import run_single_shot
from xxcode.agent import StreamEvent
from xxcode.ui.runtime import RenderFrame


class _RecordingConsole:
    def __init__(self):
        self.calls = []

    def print(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _make_config(cwd, **overrides):
    return Config(cwd=cwd, **overrides)


def _make_toolbar_state(turn_count, total_input_tokens, total_output_tokens, yolo_mode):
    return SimpleNamespace(
        turn_count=turn_count,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        permission_state=SimpleNamespace(yolo_mode=yolo_mode),
    )


def test_create_ui_defaults_to_legacy_terminal(tmp_path):
    config = _make_config(
        tmp_path,
        api_input_price_per_1k=0.003,
        api_output_price_per_1k=0.015,
    )
    ui = create_ui(config)
    assert isinstance(ui, XxCodeTerminalUI)


def test_legacy_terminal_ui_render_summary_is_safe_before_manual_reset(tmp_path):
    config = _make_config(
        tmp_path,
        api_input_price_per_1k=0.003,
        api_output_price_per_1k=0.015,
    )
    ui = XxCodeTerminalUI(config)
    ui.console = _RecordingConsole()

    class _State:
        turn_count = 1
        total_input_tokens = 12
        total_output_tokens = 34

    ui.render_summary(_State())

    assert ui._start_time > 0
    assert ui._tool_successes == 0
    assert ui._tool_errors == 0
    assert len(ui.console.calls) == 2


def test_legacy_terminal_ui_first_text_event_is_safe_before_manual_reset(tmp_path):
    config = _make_config(tmp_path)
    ui = XxCodeTerminalUI(config)
    ui.console = _RecordingConsole()

    ui.render_event(StreamEvent(type="text", content="hello"))

    assert ui._text_buffer == "hello"
    assert ui._thinking is False
    assert ui._tool_buffer == []
    assert ui.console.calls[0][0] == ("hello",)
    assert ui.console.calls[0][1] == {"end": "", "markup": False}


def test_create_ui_can_select_prompt_toolkit_fullscreen(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = create_ui(config)
    assert isinstance(ui, PromptToolkitFullscreenUI)


def test_prompt_toolkit_fullscreen_permission_answers_accept_first_letter_fallback(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)

    assert ui._normalize_permission_answer("yep") == "once"
    assert ui._normalize_permission_answer("always please") == "always"
    assert ui._normalize_permission_answer("deny everything") == "deny"
    assert ui._normalize_permission_answer("") == "deny"


def test_shell_risk_defaults_to_low_for_non_destructive_commands():
    assert tool_risk_level("run_shell", {"command": "echo hello"}) == "low"


def test_format_cwd_for_display_returns_short_paths_unchanged():
    cwd = r"F:\agent\XxCode"
    assert format_cwd_for_display(cwd) == cwd


def test_format_cwd_for_display_middle_truncates_long_paths():
    cwd = r"F:\agent\XxCode\very\long\workspace\path\with\many\nested\folders\project"

    formatted = format_cwd_for_display(cwd, max_width=30)

    assert formatted == r"F:\agent\XxCode...ders\project"
    assert formatted.startswith(r"F:\agent\XxCode")
    assert formatted.endswith(r"ders\project")


def test_build_session_toolbar_uses_richer_separator_and_yolo_label():
    state = _make_toolbar_state(5, 9000, 3000, True)

    toolbar = build_session_toolbar(
        state,
        input_price_per_1k=0.003,
        output_price_per_1k=0.015,
    )

    assert toolbar == "T5 │ 12K tok │ $0.0720 │ ⚡ YOLO"


def test_build_session_toolbar_keeps_zero_value_sections_hidden():
    state = _make_toolbar_state(0, 0, 0, False)

    toolbar = build_session_toolbar(
        state,
        input_price_per_1k=0.003,
        output_price_per_1k=0.015,
    )

    assert toolbar == ""


def test_legacy_terminal_ui_render_welcome_includes_model_cwd_and_shortcuts(tmp_path):
    long_cwd = tmp_path / "nested" / "workspace" / "with" / "many" / "folders" / "for" / "display"
    config = _make_config(long_cwd)
    ui = XxCodeTerminalUI(config)
    ui.console = _RecordingConsole()

    ui.render_welcome(session_id="sess-demo")

    assert len(ui.console.calls) == 2
    panel = ui.console.calls[1][0][0]
    # The renderable is a Rich Table — render via a temp Console to extract text.
    import io
    from rich.console import Console as RichConsole
    from xxcode.cli.theme import RICH_THEME
    buf = io.StringIO()
    RichConsole(
        file=buf,
        force_terminal=False,
        theme=RICH_THEME,
        width=140,
    ).print(panel.renderable)
    welcome_text = buf.getvalue()

    assert "XxCode" in welcome_text
    assert "Coding Agent CLI" in welcome_text
    assert "calm shell, ready for work" in welcome_text
    assert "WORKSPACE" in welcome_text
    assert "MODEL" in welcome_text
    assert "SESSION" in welcome_text
    assert "sess-demo" in welcome_text
    assert "APPROVAL" in welcome_text
    assert "SKILLS" in welcome_text
    assert "MEMORY" in welcome_text
    assert "WORKTREES" in welcome_text
    assert config.api_model in welcome_text
    assert format_cwd_for_display(str(long_cwd), max_width=38) in welcome_text
    assert "/help" in welcome_text
    assert "Ctrl+C" in welcome_text
    assert "Ctrl+D" in welcome_text
    assert "Ctrl+J" in welcome_text


def test_legacy_terminal_toolbar_highlights_yolo_segment(tmp_path):
    config = _make_config(tmp_path, api_model="claude-sonnet-4-6")
    state = _make_toolbar_state(5, 9000, 3000, True)

    fragments = _build_toolbar(state, config)

    assert fragments == [
        ("class:bottom-toolbar", "T5 │ 12K tok │ $0.0720 │ "),
        ("class:bottom-toolbar.yolo", YOLO_LABEL),
    ]


def test_legacy_terminal_get_input_only_uses_prompt_symbol_and_toolbar(tmp_path):
    class _FakePromptSession:
        def __init__(self):
            self.calls = []

        async def prompt_async(self, prompt_parts, bottom_toolbar=None):
            self.calls.append((prompt_parts, bottom_toolbar))
            return "  hello world  "

    config = _make_config(tmp_path, api_model="claude-sonnet-4-6")
    ui = XxCodeTerminalUI(config)
    fake_prompt = _FakePromptSession()
    ui.prompt_session = fake_prompt
    ui._has_prompt_toolkit = True

    state = _make_toolbar_state(5, 9000, 3000, True)

    result = asyncio.run(ui.get_input(state))

    assert result == "hello world"
    assert fake_prompt.calls == [
        (
            [("class:prompt.yolo", f"{PROMPT_SYMBOLS['yolo']} ")],
            [
                ("class:bottom-toolbar", "T5 │ 12K tok │ $0.0720 │ "),
                ("class:bottom-toolbar.yolo", YOLO_LABEL),
            ],
        )
    ]


def test_legacy_terminal_get_input_uses_ascii_safe_prompt_symbol_when_configured(tmp_path, monkeypatch):
    class _FakePromptSession:
        def __init__(self):
            self.calls = []

        async def prompt_async(self, prompt_parts, bottom_toolbar=None):
            self.calls.append((prompt_parts, bottom_toolbar))
            return "demo"

    config = _make_config(tmp_path, api_model="claude-sonnet-4-6")
    ui = XxCodeTerminalUI(config)
    ui.prompt_session = _FakePromptSession()
    ui._has_prompt_toolkit = True
    monkeypatch.setattr("xxcode.cli.terminal_ui._current_display_symbols", lambda console=None: {
        "prompt.normal": ">",
        "prompt.yolo": "!",
        "toolbar.separator": " | ",
        "marker.permission": "*",
        "marker.success": "OK",
        "marker.error": "X",
        "tool.default": "[T]",
    })

    result = asyncio.run(ui.get_input(_make_toolbar_state(1, 0, 0, False)))

    assert result == "demo"
    assert ui.prompt_session.calls[0][0] == [("class:prompt.normal", "> ")]


def test_legacy_terminal_toolbar_uses_ascii_safe_separator_when_requested(tmp_path):
    config = _make_config(tmp_path, api_model="claude-sonnet-4-6")
    state = _make_toolbar_state(5, 9000, 3000, True)

    fragments = _build_toolbar(state, config, separator=" | ")

    assert fragments == [
        ("class:bottom-toolbar", "T5 | 12K tok | $0.0720 | "),
        ("class:bottom-toolbar.yolo", YOLO_LABEL),
    ]


def test_prompt_toolkit_fullscreen_ui_updates_internal_frame_state(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    frame = RenderFrame(
        transcript_entries=[
            {"kind": "assistant", "text": "hello"},
            {"kind": "tool_call", "text": "[tool] read_file"},
        ],
        tasks={
            "task-1": {
                "task_id": "task-1",
                "worker_label": "worker-1",
                "status": "running",
                "input_tokens": 3,
                "output_tokens": 2,
                "tool_use_count": 1,
                "duration_ms": 120,
            }
        },
        task_activity_entries=[
            {
                "task_id": "task-1",
                "worker_label": "worker-1",
                "status": "completed",
                "summary": "Worker completed request.",
                "result_text": "done",
                "input_tokens": 3,
                "output_tokens": 2,
                "tool_use_count": 1,
                "duration_ms": 120,
            }
        ],
        input_enabled=False,
        input_mode="normal",
        input_placeholder="Agent is responding...",
        active_modal="permission_request",
        warnings=["careful"],
    )

    ui.mount(frame)
    ui.update(frame)
    ui.show_modal(
        {
            "kind": "tool_permission",
            "tool_name": "write_file",
            "target_summary": str(tmp_path / "demo.txt"),
            "backend_risk_level": "high",
            "display_risk_level": "high",
            "summary_lines": ["demo content"],
            "action_labels": ["允许一次", "本会话总是允许", "拒绝"],
        }
    )

    assert "worker-1" in ui._tasks_text
    assert "Recent Task Activity:" in ui._tasks_text
    assert "Worker completed request." in ui._tasks_text
    assert "对话记录" in ui._transcript_control.text
    assert "hello" in ui._transcript_control.text
    assert "[tool] read_file" in ui._transcript_control.text
    assert "任务概览" in ui._tasks_control.text
    assert "write_file" in ui._modal_text
    assert "权限请求待处理" in ui._status_control.text
    assert "输入已锁定：" in ui._input_meta_control.text
    assert ui._input_field.buffer.read_only is True

    ui.clear_modal()
    assert ui._modal_text == ""
    assert "careful" in ui._status_control.text


def test_prompt_toolkit_fullscreen_visible_panes_skip_modal_until_shown(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)

    assert ui._visible_panes() == ["transcript", "tasks", "input"]

    ui.show_modal(
        {
            "kind": "tool_permission",
            "tool_name": "write_file",
            "target_summary": str(tmp_path / "demo.txt"),
            "backend_risk_level": "high",
            "display_risk_level": "high",
            "summary_lines": ["demo"],
            "action_labels": ["允许一次", "本会话总是允许", "拒绝"],
        }
    )

    assert ui._visible_panes() == ["transcript", "tasks", "modal", "input"]


def test_prompt_toolkit_fullscreen_cycle_focus_skips_hidden_modal(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)

    assert ui._focused_pane == "input"

    ui._cycle_focus(1)
    assert ui._focused_pane == "transcript"

    ui._cycle_focus(1)
    assert ui._focused_pane == "tasks"

    ui._cycle_focus(1)
    assert ui._focused_pane == "input"


def test_prompt_toolkit_fullscreen_cycle_focus_includes_modal_when_visible(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    ui.show_modal(
        {
            "kind": "tool_permission",
            "tool_name": "write_file",
            "target_summary": str(tmp_path / "demo.txt"),
            "backend_risk_level": "high",
            "display_risk_level": "high",
            "summary_lines": ["demo"],
            "action_labels": ["允许一次", "本会话总是允许", "拒绝"],
        }
    )

    assert ui._focused_pane == "modal"

    ui._cycle_focus(1)
    assert ui._focused_pane == "input"

    ui._cycle_focus(-1)
    assert ui._focused_pane == "modal"


def test_prompt_toolkit_fullscreen_slice_viewport_text_uses_scroll_offset(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    text = "l1\nl2\nl3\nl4\nl5"

    visible = ui._slice_viewport_text(text, offset=1, height=3)

    assert visible == "l2\nl3\nl4"


def test_prompt_toolkit_fullscreen_scroll_offset_clamps_to_available_lines(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)

    ui._pane_scroll_offsets["transcript"] = 0
    ui._scroll_pane("transcript", text="a\nb\nc", delta=10, height=2)
    assert ui._pane_scroll_offsets["transcript"] == 1

    ui._scroll_pane("transcript", text="a\nb\nc", delta=-10, height=2)
    assert ui._pane_scroll_offsets["transcript"] == 0


def test_prompt_toolkit_fullscreen_scroll_active_pane_updates_transcript_offset(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    ui._focused_pane = "transcript"
    long_text = "\n".join(f"l{i}" for i in range(1, 26))
    ui._transcript_chunks = [long_text]
    ui._transcript_text_cache = long_text

    ui._scroll_active_pane(1)

    assert ui._pane_scroll_offsets["transcript"] == 1


def test_prompt_toolkit_fullscreen_slice_viewport_text_handles_empty_text(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)

    assert ui._slice_viewport_text("", offset=0, height=3) == ""


def test_prompt_toolkit_fullscreen_ui_preserves_stable_transcript_prefix(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)

    first = RenderFrame(
        transcript_entries=[
            {"kind": "assistant", "text": "hello"},
        ]
    )
    second = RenderFrame(
        transcript_entries=[
            {"kind": "assistant", "text": "hello world"},
            {"kind": "tool_call", "text": "[tool] read_file"},
        ]
    )

    ui.mount(first)
    assert ui._transcript_chunks == ["hello"]

    ui.update(second)

    assert ui._transcript_chunks == ["hello world", "[tool] read_file\n"]
    assert ui._transcript_control.text == "  对话记录\nhello world[tool] read_file"


def test_prompt_toolkit_fullscreen_ui_merges_adjacent_assistant_and_thinking_entries(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    frame = RenderFrame(
        transcript_entries=[
            {"kind": "assistant", "text": "hello"},
            {"kind": "assistant", "text": " world"},
            {"kind": "thinking", "text": "plan"},
            {"kind": "thinking", "text": " more"},
            {"kind": "tool_result", "text": "[tool result] ok"},
        ]
    )

    ui.mount(frame)

    assert ui._transcript_chunks == [
        "hello world",
        "[thinking collapsed]",
        "工具结果: ok\n",
    ]
    assert ui._transcript_control.text == "  对话记录\nhello world[thinking collapsed]工具结果: ok"


def test_prompt_toolkit_fullscreen_ui_preserves_stable_task_prefix(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)

    first = RenderFrame(
        tasks={
            "task-1": {
                "task_id": "task-1",
                "worker_label": "worker-1",
                "status": "running",
                "input_tokens": 3,
                "output_tokens": 2,
                "tool_use_count": 1,
                "duration_ms": 120,
            }
        }
    )
    second = RenderFrame(
        tasks={
            "task-1": {
                "task_id": "task-1",
                "worker_label": "worker-1",
                "status": "running",
                "input_tokens": 3,
                "output_tokens": 2,
                "tool_use_count": 1,
                "duration_ms": 120,
            },
            "task-2": {
                "task_id": "task-2",
                "worker_label": "worker-2",
                "status": "completed",
                "input_tokens": 8,
                "output_tokens": 5,
                "tool_use_count": 2,
                "duration_ms": 420,
            },
        }
    )

    ui.mount(first)
    first_chunks = list(ui._task_chunks)

    ui.update(second)

    assert first_chunks == [
        "Task Snapshots:",
        "- worker-1 [running] tools=1 usage=3/2 duration=120ms",
    ]
    assert ui._task_chunks[:2] == first_chunks
    assert ui._task_chunks[2] == "- worker-2 [completed] tools=2 usage=8/5 duration=420ms"
    assert ui._tasks_text.startswith("Task Snapshots:\n- worker-1 [running]")


def test_prompt_toolkit_fullscreen_transcript_control_adds_region_heading(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    frame = RenderFrame(transcript_entries=[{"kind": "assistant", "text": "hello"}])

    ui.mount(frame)

    assert "对话记录" in ui._transcript_control.text


def test_prompt_toolkit_fullscreen_tasks_control_adds_region_heading(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    frame = RenderFrame(
        tasks={
            "task-1": {
                "task_id": "task-1",
                "worker_label": "worker-1",
                "status": "running",
                "input_tokens": 3,
                "output_tokens": 2,
                "tool_use_count": 1,
                "duration_ms": 120,
            }
        }
    )

    ui.mount(frame)

    assert "任务概览" in ui._tasks_control.text


def test_prompt_toolkit_fullscreen_clear_modal_restores_non_modal_focus(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)

    ui.show_modal(
        {
            "kind": "tool_permission",
            "tool_name": "write_file",
            "target_summary": str(tmp_path / "demo.txt"),
            "backend_risk_level": "high",
            "display_risk_level": "high",
            "summary_lines": ["demo"],
            "action_labels": ["允许一次", "本会话总是允许", "拒绝"],
        }
    )

    ui.clear_modal()
    assert ui._focused_pane in ("input", "transcript")


def test_prompt_toolkit_fullscreen_search_matches_transcript_lines(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    ui._transcript_text_cache = "alpha\nbeta match\ngamma match\ndelta"

    ui._set_transcript_search_query("match")

    assert ui._transcript_search_query == "match"
    assert ui._transcript_search_matches == [1, 2]
    assert ui._transcript_search_index == 0


def test_prompt_toolkit_fullscreen_search_navigation_wraps(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    ui._transcript_text_cache = "alpha\nbeta match\ngamma match\ndelta"
    ui._set_transcript_search_query("match")

    ui._step_transcript_search(1)
    assert ui._transcript_search_index == 1

    ui._step_transcript_search(1)
    assert ui._transcript_search_index == 0

    ui._step_transcript_search(-1)
    assert ui._transcript_search_index == 1


def test_prompt_toolkit_fullscreen_search_navigation_scrolls_transcript_viewport(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    ui._transcript_text_cache = "\n".join(f"line {i}" for i in range(1, 41))
    ui._set_transcript_search_query("line 30")

    ui._step_transcript_search(1)

    assert ui._transcript_search_index == 0
    assert ui._pane_scroll_offsets["transcript"] > 0


def test_prompt_toolkit_fullscreen_search_clears_when_query_empty(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    ui._transcript_text_cache = "alpha\nbeta match\ngamma match\ndelta"
    ui._set_transcript_search_query("match")

    ui._set_transcript_search_query("")

    assert ui._transcript_search_query == ""
    assert ui._transcript_search_matches == []
    assert ui._transcript_search_index == -1


def test_prompt_toolkit_fullscreen_escape_cancels_search_mode(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    ui._input_mode = "search_query"
    ui._input_enabled = True

    ui._cancel_search_mode()

    assert ui._input_mode == "normal"
    assert ui._input_placeholder == "请输入消息并回车。"


def test_prompt_toolkit_fullscreen_enter_search_mode_is_ignored_during_permission_flow(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        ui._pending_permission_future = loop.create_future()
        ui._enter_search_mode()
        assert ui._input_mode != "search_query"
    finally:
        loop.close()


def test_prompt_toolkit_fullscreen_thinking_blocks_render_collapsed_by_default(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    frame = RenderFrame(
        transcript_entries=[
            {"kind": "assistant", "text": "hello"},
            {"kind": "thinking", "text": "plan one"},
            {"kind": "thinking", "text": " plan two"},
        ]
    )

    ui.mount(frame)

    assert "[thinking collapsed]" in ui._transcript_control.text
    assert "plan one" not in ui._transcript_control.text


def test_prompt_toolkit_fullscreen_toggle_thinking_expands_transcript_block(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    frame = RenderFrame(
        transcript_entries=[
            {"kind": "assistant", "text": "hello"},
            {"kind": "thinking", "text": "plan one"},
            {"kind": "thinking", "text": " plan two"},
        ]
    )

    ui.mount(frame)
    ui._toggle_thinking_visibility()

    assert ui._thinking_expanded is True
    assert "plan one" in ui._transcript_control.text
    assert "plan two" in ui._transcript_control.text


def test_prompt_toolkit_fullscreen_task_activity_can_toggle_detail_mode(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    frame = RenderFrame(
        task_activity_entries=[
            {
                "task_id": "task-1",
                "worker_label": "worker-1",
                "status": "completed",
                "summary": "Worker completed request.",
                "result_text": "full result text",
                "input_tokens": 3,
                "output_tokens": 2,
                "tool_use_count": 1,
                "duration_ms": 120,
            }
        ]
    )

    ui.mount(frame)
    assert "full result text" not in ui._tasks_control.text

    ui._toggle_task_activity_detail()

    assert ui._task_activity_expanded is True
    assert "full result text" in ui._tasks_control.text


def test_prompt_toolkit_fullscreen_tool_result_blocks_show_more_readable_prefix(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    frame = RenderFrame(
        transcript_entries=[
            {"kind": "tool_result", "text": "[tool result] line one\nline two"},
        ]
    )

    ui.mount(frame)

    assert "[tool result]" not in ui._transcript_control.text
    assert "工具结果:" in ui._transcript_control.text
    assert "line one" in ui._transcript_control.text


def test_ui_runtime_builds_phase1_permission_modal_schema(tmp_path):
    config = _make_config(tmp_path)
    from xxcode.agent.query_engine import QueryEngine
    from xxcode.tools import ToolCall
    from xxcode.ui.runtime import UiEvent, UiRuntime

    engine = QueryEngine(config)

    class _NoopUi:
        async def ask_permission(self, tc, dangerous=False):
            return "deny"

        def show_modal(self, modal_state):
            self.modal_state = modal_state

        def clear_modal(self):
            pass

        def mount(self, initial_frame):
            pass

        def update(self, frame):
            self.frame = frame

        def shutdown(self, final_snapshot):
            pass

    ui = _NoopUi()
    runtime = UiRuntime(engine=engine, ui=ui)
    event = UiEvent(
        type="permission_requested",
        content="write_file",
        metadata={
            "tool_call": ToolCall(
                id="tool-1",
                name="write_file",
                input={"file_path": str(tmp_path / "demo.txt"), "content": "hello"},
            ),
            "risk": "normal",
            "dangerous": False,
        },
    )

    modal_state = runtime._build_permission_modal_state(event)

    assert modal_state["kind"] == "tool_permission"
    assert modal_state["tool_name"] == "write_file"
    assert modal_state["backend_risk_level"] == "normal"
    assert modal_state["display_risk_level"] == "medium"
    assert modal_state["target_summary"].endswith("demo.txt")
    assert modal_state["action_labels"] == ["允许一次", "本会话总是允许", "拒绝"]
    assert "hello" in "\n".join(modal_state["summary_lines"])


def test_ui_runtime_distinguishes_skill_shell_and_mcp_modal_kinds(tmp_path):
    config = _make_config(tmp_path)
    from types import SimpleNamespace
    from xxcode.agent.query_engine import QueryEngine
    from xxcode.ui.runtime import UiEvent, UiRuntime

    engine = QueryEngine(config)
    ui = SimpleNamespace()
    runtime = UiRuntime(engine=engine, ui=ui)

    skill_event = UiEvent(
        type="permission_requested",
        metadata={
            "skill_shell_request": SimpleNamespace(skill_name="demo-skill", command="npm test"),
            "risk": "high",
            "dangerous": True,
        },
    )
    trust_event = UiEvent(
        type="permission_requested",
        metadata={
            "mcp_trust_request": [{"name": "docs", "command": "node", "args": ["server.js"]}],
            "risk": "high",
            "dangerous": True,
        },
    )

    skill_modal = runtime._build_permission_modal_state(skill_event)
    trust_modal = runtime._build_permission_modal_state(trust_event)

    assert skill_modal["kind"] == "skill_shell_permission"
    assert trust_modal["kind"] == "mcp_trust_permission"
    assert skill_modal["display_risk_level"] == "high"
    assert trust_modal["display_risk_level"] == "high"


def test_ui_runtime_records_recent_task_activity_entries(tmp_path):
    config = _make_config(tmp_path)
    from xxcode.agent.query_engine import QueryEngine
    from xxcode.ui.runtime import TaskUiEvent, UiRuntime

    engine = QueryEngine(config)

    class _NoopUI:
        def update(self, frame):
            self.frame = frame

    ui = _NoopUI()
    runtime = UiRuntime(engine=engine, ui=ui)
    runtime.task_sink.emit(
        TaskUiEvent(
            type="task_summary_available",
            task_id="task-1",
            record={
                "task_id": "task-1",
                "worker_label": "worker-1",
                "status": "completed",
                "input_tokens": 3,
                "output_tokens": 2,
                "tool_use_count": 1,
                "duration_ms": 120,
            },
            summary="Worker completed request.",
            result_text="done",
        )
    )

    import asyncio

    asyncio.run(runtime._drain_task_events())

    assert runtime.frame.task_activity_entries
    assert runtime.frame.task_activity_entries[-1]["summary"] == "Worker completed request."


def test_prompt_toolkit_fullscreen_ui_respects_frame_input_state(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)
    frame = RenderFrame(
        input_enabled=False,
        input_mode="tty_handoff_blocked",
        input_placeholder="Native terminal tool owns stdin.",
    )

    ui.mount(frame)

    assert ui._input_field.buffer.read_only is True
    assert "输入已锁定：" in ui._input_meta_control.text
    assert "Native terminal tool owns stdin." in ui._input_meta_control.text

    frame.input_enabled = True
    frame.input_mode = "normal"
    frame.input_placeholder = "Type a message."
    ui.update(frame)

    assert ui._input_field.buffer.read_only is False
    assert "输入就绪：" in ui._input_meta_control.text
    assert "Type a message." in ui._input_meta_control.text


def test_prompt_toolkit_fullscreen_modal_formats_phase1_schema(tmp_path):
    config = _make_config(tmp_path, ui_backend="prompt_toolkit_fullscreen")
    ui = PromptToolkitFullscreenUI(config)

    text = ui._format_modal(
        {
            "kind": "tool_permission",
            "tool_name": "write_file",
            "target_summary": str(tmp_path / "demo.txt"),
            "backend_risk_level": "normal",
            "display_risk_level": "medium",
            "summary_lines": ["hello world"],
            "action_labels": ["允许一次", "本会话总是允许", "拒绝"],
        }
    )

    assert "权限请求" in text
    assert "write_file" in text
    assert "需确认" in text
    assert "hello world" in text
    assert "允许一次" in text


def test_run_single_shot_uses_ui_runtime_backend_contract(tmp_path):
    class _FakeTaskRuntime:
        def __init__(self):
            self.sinks = []

        def set_task_event_sink(self, sink):
            self.sinks.append(sink)

    class _FakeCoreEngine:
        def __init__(self):
            self.task_runtime = _FakeTaskRuntime()

    class _FakeEngine:
        def __init__(self):
            self.core_engine = _FakeCoreEngine()
            self.shutdown_called = False
            self.calls = []
            self._last_state = None

        async def submit_message(self, user_input, state=None, *, session_id=None):
            self.calls.append(
                {
                    "user_input": user_input,
                    "state": state,
                    "session_id": session_id,
                }
            )
            yield StreamEvent(type="text", content="hello")
            yield StreamEvent(type="done", content="")

        async def shutdown(self):
            self.shutdown_called = True

    class _FrameOnlyUi:
        uses_frame_transcript = True

        def __init__(self):
            self.mounted = []
            self.updated = []
            self.shutdown_frames = []

        def mount(self, initial_frame):
            self.mounted.append(initial_frame)

        def update(self, frame):
            self.updated.append(frame)

        def shutdown(self, final_snapshot):
            self.shutdown_frames.append(final_snapshot)

    engine = _FakeEngine()
    ui = _FrameOnlyUi()

    run_single_shot(engine, ui, "demo prompt")

    assert engine.calls and engine.calls[0]["user_input"] == "demo prompt"
    assert engine.calls[0]["session_id"]
    assert ui.mounted
    assert ui.updated
    assert ui.updated[-1].transcript_entries[-1]["text"] == "hello"
    assert ui.shutdown_frames
    assert engine.shutdown_called is True
    assert engine.core_engine.task_runtime.sinks[-1] is None
