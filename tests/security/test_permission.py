"""Tests for security/permission.py — PermissionState management."""

import pytest
from xxcode.security.permission import (
    PermissionState,
    needs_user_permission,
    SHELL_RULE_PREFIX,
)


class TestPermissionState:
    def test_default_state(self):
        ps = PermissionState()
        assert ps.yolo_mode is False
        assert len(ps.confirmed_paths) == 0
        assert len(ps.confirmed_tools) == 0
        assert len(ps.confirmed_command_rules) == 0

    def test_yolo_mode_skips_all(self):
        ps = PermissionState(yolo_mode=True)
        assert ps.is_path_confirmed("/any/path") is True
        assert ps.is_tool_confirmed("any_tool") is True
        assert ps.is_command_rule_confirmed("rm -rf /") is True

    # ── Path confirmation ──

    def test_confirm_path(self):
        ps = PermissionState()
        ps.confirm_path("/home/user/project")
        assert ps.is_path_confirmed("/home/user/project") is True
        assert ps.is_path_confirmed("/home/user/project/sub/file.py") is True

    def test_child_path_inherits_confirmation(self):
        ps = PermissionState()
        ps.confirm_path("/home/user/project")
        assert ps.is_path_confirmed("/home/user/project/src/main.py") is True

    def test_parent_path_not_confirmed(self):
        ps = PermissionState()
        ps.confirm_path("/home/user/project/src")
        assert ps.is_path_confirmed("/home/user") is False

    def test_unconfirmed_path(self):
        ps = PermissionState()
        assert ps.is_path_confirmed("/etc/passwd") is False

    def test_path_normalization(self):
        ps = PermissionState()
        ps.confirm_path("C:\\Users\\admin\\project")
        # Should work with forward slashes too
        assert ps.is_path_confirmed("C:/Users/admin/project/file.py") is True

    # ── Tool confirmation ──

    def test_confirm_tool(self):
        ps = PermissionState()
        ps.confirm_tool("read_file")
        assert ps.is_tool_confirmed("read_file") is True

    def test_unconfirmed_tool(self):
        ps = PermissionState()
        assert ps.is_tool_confirmed("run_shell") is False

    # ── Command rule confirmation ──

    def test_confirm_command_prefix(self):
        ps = PermissionState()
        ps.confirm_command_prefix("git status")
        rule = ps._command_rule_for_prefix("git status")
        assert rule in ps.confirmed_command_rules

    def test_empty_prefix_not_stored(self):
        ps = PermissionState()
        ps.confirm_command_prefix("  ")
        assert len(ps.confirmed_command_rules) == 0

    def test_command_rule_format(self):
        rule = PermissionState._command_rule_for_prefix("git")
        assert rule == f"{SHELL_RULE_PREFIX}git:*)"


class TestPermissionStateSerialization:
    def test_roundtrip(self):
        ps = PermissionState(
            confirmed_paths={"/home/user/project", "/tmp"},
            confirmed_tools={"read_file", "grep_search"},
            confirmed_command_rules={"Bash(git status:*)", "Bash(ls:*)"},
            yolo_mode=True,
        )
        data = ps.to_dict()
        restored = PermissionState.from_dict(data)

        assert restored.yolo_mode == ps.yolo_mode
        assert restored.confirmed_paths == ps.confirmed_paths
        assert restored.confirmed_tools == ps.confirmed_tools
        assert restored.confirmed_command_rules == ps.confirmed_command_rules

    def test_from_empty_dict(self):
        ps = PermissionState.from_dict({})
        assert ps.yolo_mode is False
        assert len(ps.confirmed_paths) == 0

    def test_partial_dict(self):
        ps = PermissionState.from_dict({"yolo_mode": True})
        assert ps.yolo_mode is True
        assert len(ps.confirmed_paths) == 0


class TestNeedsUserPermission:
    def _make_state(self, **kwargs):
        return PermissionState(**kwargs)

    def test_yolo_skips(self):
        state = self._make_state(yolo_mode=True)
        from types import SimpleNamespace
        tool_input = SimpleNamespace(file_path="/etc/passwd")
        assert needs_user_permission("write_file", tool_input, state) is False

    def test_tool_confirmed_skips(self):
        state = self._make_state()
        state.confirm_tool("read_file")
        from types import SimpleNamespace
        tool_input = SimpleNamespace()
        assert needs_user_permission("read_file", tool_input, state) is False

    def test_path_confirmed_skips(self):
        state = self._make_state()
        state.confirm_path("/home/user/project")
        from types import SimpleNamespace
        tool_input = SimpleNamespace(file_path="/home/user/project/file.py")
        assert needs_user_permission("write_file", tool_input, state) is False

    def test_unconfirmed_requires_permission(self):
        state = self._make_state()
        from types import SimpleNamespace
        tool_input = SimpleNamespace(file_path="/etc/hosts")
        assert needs_user_permission("write_file", tool_input, state) is True

    def test_no_file_path_field(self):
        state = self._make_state()
        from types import SimpleNamespace
        tool_input = SimpleNamespace(command="ls")
        assert needs_user_permission("run_shell", tool_input, state) is True

    def test_none_file_path(self):
        state = self._make_state()
        from types import SimpleNamespace
        tool_input = SimpleNamespace(file_path=None)
        assert needs_user_permission("write_file", tool_input, state) is True
