"""Tests for security/classifier.py — bash command classification."""

import pytest
from xxcode.security.classifier import (
    CommandClass,
    ClassifierResult,
    classify_command,
    is_safe_command,
    strip_safe_env_vars,
    _split_pipeline,
    _extract_base_command,
    _tokenize_command,
)


class TestStripSafeEnvVars:
    def test_strips_safe_var(self):
        assert strip_safe_env_vars("NODE_ENV=prod npm run build") == "npm run build"
        assert strip_safe_env_vars("LANG=en_US.UTF-8 ls") == "ls"

    def test_strips_multiple_safe_vars(self):
        result = strip_safe_env_vars("NODE_ENV=test LANG=C python script.py")
        assert result == "python script.py"

    def test_keeps_unsafe_var(self):
        result = strip_safe_env_vars("LD_PRELOAD=evil.so curl example.com")
        assert result == "LD_PRELOAD=evil.so curl example.com"

    def test_no_env_vars(self):
        assert strip_safe_env_vars("ls -la") == "ls -la"

    def test_empty_command(self):
        assert strip_safe_env_vars("") == ""


class TestSplitPipeline:
    def test_single_command(self):
        assert _split_pipeline("ls -la") == ["ls -la"]

    def test_and_operator(self):
        result = _split_pipeline("ls && echo done")
        assert result == ["ls", "echo done"]

    def test_or_operator(self):
        result = _split_pipeline("false || echo fail")
        assert result == ["false", "echo fail"]

    def test_semicolon(self):
        result = _split_pipeline("ls; pwd")
        assert result == ["ls", "pwd"]

    def test_pipe(self):
        result = _split_pipeline("cat file | grep pattern")
        assert result == ["cat file", "grep pattern"]

    def test_operator_in_quotes_not_split(self):
        result = _split_pipeline("echo 'hello && world'")
        assert len(result) == 1
        assert "&&" in result[0]

    def test_complex_pipeline(self):
        result = _split_pipeline("ls && echo 'a|b' || pwd")
        assert result == ["ls", "echo 'a|b'", "pwd"]


class TestExtractBaseCommand:
    def test_simple_command(self):
        base, sub, has_sudo = _extract_base_command("ls -la")
        assert base == "ls"
        assert sub == "-la"

    def test_compound_command(self):
        base, sub, has_sudo = _extract_base_command("git status")
        assert base == "git"
        assert sub == "status"
        assert has_sudo is False

    def test_with_env_vars(self):
        base, sub, has_sudo = _extract_base_command("FOO=bar ls -la")
        assert base == "ls"

    def test_sudo_detection(self):
        base, sub, has_sudo = _extract_base_command("sudo rm -rf /")
        assert base == "rm"
        assert has_sudo is True

    def test_doas_detection(self):
        _, _, has_sudo = _extract_base_command("doas ls")
        assert has_sudo is True

    def test_pkexec_detection(self):
        _, _, has_sudo = _extract_base_command("pkexec gedit")
        assert has_sudo is True

    def test_path_prefixed_command(self):
        base, _, _ = _extract_base_command("/usr/bin/git status")
        assert base == "git"

    def test_empty_command(self):
        base, sub, has_sudo = _extract_base_command("")
        assert base is None


class TestClassifierSharedWrappers:
    def test_background_ampersand_splits_into_segments(self):
        result = _split_pipeline("make & npm run build")
        assert result == ["make", "npm run build"]

    def test_tokenize_command_handles_escaped_spaces(self):
        tokens = _tokenize_command(r"echo hello\ world")
        assert tokens == ["echo", "hello world"]

    def test_strip_safe_env_vars_supports_quoted_values(self):
        result = strip_safe_env_vars('NODE_ENV="prod test" npm run build')
        assert result == "npm run build"

    def test_extract_base_command_normalizes_windows_exe_and_ignores_redirect(self):
        base, sub, has_sudo = _extract_base_command(
            r'NODE_ENV="prod test" C:\tools\git.exe status > out.txt'
        )
        assert (base, sub, has_sudo) == ("git", "status", False)


class TestClassifyCommand:
    # ── SAFE commands ──

    def test_ls_is_safe(self):
        result = classify_command("ls -la")
        assert result.command_class == CommandClass.SAFE

    def test_cat_is_safe(self):
        result = classify_command("cat file.txt")
        assert result.command_class == CommandClass.SAFE

    def test_find_is_safe(self):
        result = classify_command("find . -name '*.py'")
        assert result.command_class == CommandClass.SAFE

    def test_grep_is_safe(self):
        result = classify_command("grep pattern file")
        assert result.command_class == CommandClass.SAFE

    def test_echo_is_safe(self):
        result = classify_command("echo hello")
        assert result.command_class == CommandClass.SAFE

    def test_git_status_is_safe(self):
        result = classify_command("git status")
        assert result.command_class == CommandClass.SAFE

    def test_git_log_is_safe(self):
        result = classify_command("git log --oneline")
        assert result.command_class == CommandClass.SAFE

    def test_git_diff_is_safe(self):
        result = classify_command("git diff HEAD~1")
        assert result.command_class == CommandClass.SAFE

    def test_npm_list_is_safe(self):
        result = classify_command("npm list")
        assert result.command_class == CommandClass.SAFE

    def test_docker_ps_is_safe(self):
        result = classify_command("docker ps")
        assert result.command_class == CommandClass.SAFE

    def test_kubectl_get_is_safe(self):
        result = classify_command("kubectl get pods")
        assert result.command_class == CommandClass.SAFE

    def test_pip_list_is_safe(self):
        result = classify_command("pip list")
        assert result.command_class == CommandClass.SAFE

    # ── DANGEROUS commands ──

    def test_sudo_is_dangerous(self):
        result = classify_command("sudo ls")
        assert result.command_class == CommandClass.DANGEROUS

    def test_rm_is_dangerous(self):
        result = classify_command("rm file.txt")
        assert result.command_class == CommandClass.DANGEROUS

    def test_chmod_777_is_dangerous(self):
        result = classify_command("chmod 777 script.sh")
        assert result.command_class == CommandClass.DANGEROUS

    # ── NEEDS_PERMISSION commands ──

    def test_git_commit_needs_permission(self):
        result = classify_command("git commit -m 'msg'")
        assert result.command_class == CommandClass.NEEDS_PERMISSION

    def test_npm_install_needs_permission(self):
        result = classify_command("npm install express")
        assert result.command_class == CommandClass.NEEDS_PERMISSION

    def test_pip_install_needs_permission(self):
        result = classify_command("pip install requests")
        assert result.command_class == CommandClass.NEEDS_PERMISSION

    def test_unknown_command_needs_permission(self):
        result = classify_command("some_unknown_command --flag")
        assert result.command_class == CommandClass.NEEDS_PERMISSION

    # ── Pipeline handling ──

    def test_safe_pipeline_stays_safe(self):
        result = classify_command("ls && cat file.txt")
        assert result.command_class == CommandClass.SAFE

    def test_pipeline_with_dangerous_needs_permission(self):
        result = classify_command("ls && sudo rm -rf /")
        assert result.command_class == CommandClass.NEEDS_PERMISSION

    def test_pipe_with_safe_commands(self):
        result = classify_command("cat file | grep pattern")
        assert result.command_class == CommandClass.SAFE

    # ── Sudo-adjacent commands ──

    def test_doas_is_dangerous(self):
        result = classify_command("doas cat /etc/shadow")
        assert result.command_class == CommandClass.DANGEROUS

    # ── Env var stripping ──

    def test_safe_env_var_stripped(self):
        result = classify_command("NODE_ENV=prod ls -la")
        assert result.command_class == CommandClass.SAFE


class TestIsSafeCommand:
    def test_safe_commands(self):
        assert is_safe_command("ls") is True
        assert is_safe_command("cat file.txt") is True
        assert is_safe_command("git status") is True

    def test_unsafe_commands(self):
        assert is_safe_command("rm file.txt") is False
        assert is_safe_command("sudo ls") is False
        assert is_safe_command("pip install x") is False
