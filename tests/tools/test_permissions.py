"""Tests for BashTool/permissions.py — command prefix extraction and permissions."""

import pytest
from xxcode.tools.BashTool._tokenizer import (
    extract_base_command as canonical_extract_base_command,
    normalize_base_token as canonical_normalize_base_token,
    split_pipeline as canonical_split_pipeline,
    strip_all_safe_env_prefixes as canonical_strip_all_safe_env_prefixes,
    strip_safe_env_vars as canonical_strip_safe_env_vars,
    tokenize as canonical_tokenize,
)
from xxcode.tools.BashTool.permissions import (
    get_simple_command_prefix,
    strip_safe_env_vars,
    strip_all_safe_env_prefixes,
    tokenize_command,
    aggregate_compound_permissions,
    PermissionResult,
    Risk,
    ParseResult,
)


class TestGetSimpleCommandPrefix:
    def test_git_status(self):
        assert get_simple_command_prefix("git status") == "git status"

    def test_git_log(self):
        assert get_simple_command_prefix("git log --oneline") == "git log"

    def test_npm_run(self):
        assert get_simple_command_prefix("npm run build") == "npm run"

    def test_pip_list(self):
        assert get_simple_command_prefix("pip list") == "pip list"

    def test_docker_ps(self):
        assert get_simple_command_prefix("docker ps -a") == "docker ps"

    def test_kubectl_get(self):
        assert get_simple_command_prefix("kubectl get pods") == "kubectl get"

    def test_single_word_returns_none(self):
        assert get_simple_command_prefix("ls") is None

    def test_flag_second_token_returns_none(self):
        assert get_simple_command_prefix("ls --help") is None

    def test_sudo_blocked(self):
        assert get_simple_command_prefix("sudo make install") is None

    def test_bash_blocked(self):
        assert get_simple_command_prefix("bash -c 'echo hello'") is None

    def test_sh_blocked(self):
        assert get_simple_command_prefix("sh script.sh") is None

    def test_doas_blocked(self):
        assert get_simple_command_prefix("doas rm file") is None

    def test_pkexec_blocked(self):
        assert get_simple_command_prefix("pkexec gedit") is None

    def test_su_blocked(self):
        assert get_simple_command_prefix("su - user") is None

    def test_env_blocked(self):
        assert get_simple_command_prefix("env VAR=val cmd") is None

    def test_exec_blocked(self):
        assert get_simple_command_prefix("exec ls") is None

    def test_nohup_blocked(self):
        assert get_simple_command_prefix("nohup long_running &") is None

    def test_nice_blocked(self):
        assert get_simple_command_prefix("nice -n 10 cmd") is None

    def test_safe_env_stripped(self):
        assert get_simple_command_prefix("NODE_ENV=prod npm run build") == "npm run"

    def test_path_prefixed_command(self):
        assert get_simple_command_prefix("/usr/bin/git status") == "git status"

    def test_git_minus_c_handling(self):
        prefix = get_simple_command_prefix("git -c user.name=test status")
        assert prefix == "git status" or prefix is None

    def test_empty_command(self):
        assert get_simple_command_prefix("") is None


class TestStripSafeEnvVars:
    def test_strips_safe_var(self):
        assert strip_safe_env_vars("NODE_ENV=prod npm run build") == "npm run build"

    def test_preserves_unsafe_var(self):
        result = strip_safe_env_vars("LD_PRELOAD=evil.so curl")
        assert "LD_PRELOAD" in result

    def test_no_env_var(self):
        assert strip_safe_env_vars("ls -la") == "ls -la"

    def test_empty(self):
        assert strip_safe_env_vars("") == ""

    def test_whitespace_handling(self):
        # Leading whitespace is stripped by lstrip() loop before env var detection
        assert strip_safe_env_vars("  NODE_ENV=test ls") == "ls"


class TestStripAllSafeEnvPrefixes:
    def test_multiple_safe_vars(self):
        result = strip_all_safe_env_prefixes("NODE_ENV=test LANG=C python script.py")
        assert result == "python script.py"

    def test_unsafe_var_stops(self):
        result = strip_all_safe_env_prefixes("NODE_ENV=test LD_PRELOAD=evil ls")
        assert "LD_PRELOAD" in result
        assert "NODE_ENV" not in result


class TestPermissionWrappersWithQuotedEnvValues:
    def test_permissions_strip_safe_env_vars_supports_quoted_values(self):
        result = strip_safe_env_vars('NODE_ENV="prod test" npm run build')
        assert result == "npm run build"

    def test_permissions_strip_safe_env_vars_preserves_unknown_env_only_input(self):
        assert strip_safe_env_vars("FOO=bar") == "FOO=bar"

    def test_permissions_strip_safe_env_vars_preserves_safe_env_without_trailing_command(self):
        assert strip_safe_env_vars("NODE_ENV=prod") == "NODE_ENV=prod"
        assert strip_safe_env_vars("NODE_ENV=prod   ") == "NODE_ENV=prod   "

    def test_get_simple_command_prefix_supports_quoted_safe_env_values(self):
        prefix = get_simple_command_prefix('NODE_ENV="prod test" npm run build')
        assert prefix == "npm run"


class TestTokenizeCommand:
    def test_simple(self):
        tokens = tokenize_command("ls -la")
        assert tokens == ["ls", "-la"]

    def test_multiple_args(self):
        tokens = tokenize_command("git commit -m 'hello world'")
        assert tokens == ["git", "commit", "-m", "'hello world'"]

    def test_pipes(self):
        tokens = tokenize_command("cat file | grep pattern")
        assert "|" in tokens

    def test_empty(self):
        assert tokenize_command("") == []


class TestCanonicalTokenizerPrimitives:
    def test_split_pipeline_treats_ampersand_as_background_separator(self):
        result = canonical_split_pipeline("make & npm run build")
        assert result == ["make", "npm run build"]

    def test_tokenize_preserves_escaped_spaces(self):
        tokens = canonical_tokenize(r"echo hello\ world")
        assert tokens == ["echo", "hello world"]

    def test_canonical_strip_safe_env_vars_accepts_empty_string(self):
        assert canonical_strip_safe_env_vars("") == ""

    def test_canonical_strip_safe_env_vars_preserves_unknown_env_only_input(self):
        assert canonical_strip_safe_env_vars("FOO=bar") == "FOO=bar"

    def test_canonical_strip_safe_env_vars_preserves_safe_env_without_command(self):
        assert canonical_strip_safe_env_vars("NODE_ENV=prod") == "NODE_ENV=prod"
        assert canonical_strip_safe_env_vars("NODE_ENV=prod   ") == "NODE_ENV=prod   "

    def test_canonical_strip_safe_env_vars_supports_quoted_values(self):
        result = canonical_strip_safe_env_vars('NODE_ENV="prod test" npm run build')
        assert result == "npm run build"

    def test_canonical_strip_safe_env_vars_keeps_unsafe_env_prefix_before_safe_command(self):
        result = canonical_strip_safe_env_vars("LD_PRELOAD=evil.so ls")
        assert result == "LD_PRELOAD=evil.so ls"

    def test_canonical_strip_safe_env_vars_accepts_single_char_unknown_env_name(self):
        assert canonical_strip_safe_env_vars("v=1 ls") == "v=1 ls"

    def test_canonical_strip_all_safe_env_prefixes_supports_multiple_prefixes(self):
        result = canonical_strip_all_safe_env_prefixes(
            'NODE_ENV="prod test" LANG=C python script.py'
        )
        assert result == "python script.py"

    def test_canonical_extract_base_command_handles_quoted_env_and_windows_exe(self):
        base = canonical_extract_base_command(
            r'NODE_ENV="prod test" C:\tools\git.exe status'
        )
        assert base == "git"

    def test_canonical_normalize_base_token_accepts_empty_string(self):
        assert canonical_normalize_base_token("") == ""


class TestAggregateCompoundPermissions:
    def test_all_safe(self):
        sub_results = [
            PermissionResult(allowed=True, risk=Risk.SAFE, needs_user_decision=False),
            PermissionResult(allowed=True, risk=Risk.SAFE, needs_user_decision=False),
        ]
        result = aggregate_compound_permissions(sub_results)
        assert result.allowed is True

    def test_one_needs_decision(self):
        sub_results = [
            PermissionResult(allowed=True, risk=Risk.SAFE, needs_user_decision=False),
            PermissionResult(allowed=False, risk=Risk.NORMAL, needs_user_decision=True),
        ]
        result = aggregate_compound_permissions(sub_results)
        assert result.needs_user_decision is True
        assert result.allowed is False

    def test_one_denied_with_decision(self):
        sub_results = [
            PermissionResult(allowed=True, risk=Risk.SAFE, needs_user_decision=False),
            PermissionResult(allowed=False, risk=Risk.CRITICAL, needs_user_decision=True),
        ]
        result = aggregate_compound_permissions(sub_results)
        assert result.allowed is False

    def test_empty(self):
        result = aggregate_compound_permissions([])
        assert result.allowed is False
