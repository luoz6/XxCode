# Bash Security Parser Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify duplicated Bash parsing and safe environment-variable stripping logic so `BashTool` permissions and the speculative classifier interpret commands consistently without changing the current security posture.

**Architecture:** Keep the refactor primitive-focused. First, extend `src/xxcode/tools/BashTool/_tokenizer.py` so it owns canonical env stripping, base-token normalization, tokenization, pipeline splitting, and base-command extraction. Then convert `permissions.py` and `security/classifier.py` into thin consumers and compatibility wrappers over those canonical helpers, with every change driven by focused failing tests on the exact parsing deltas this refactor is meant to fix.

**Tech Stack:** Python 3.11, pytest, regex-lite parsing helpers, `xxcode.tools.BashTool._tokenizer`, `xxcode.tools.BashTool.permissions`, `xxcode.security.classifier`

---

## File Structure

- Modify: `src/xxcode/tools/BashTool/_tokenizer.py`
  Responsibility: canonical shell primitives for tokenization, pipeline splitting, safe env stripping, base-token normalization, and normalized base-command extraction.
- Modify: `src/xxcode/tools/BashTool/permissions.py`
  Responsibility: permission-policy layer that consumes canonical tokenizer/env helpers while preserving current public function names.
- Modify: `src/xxcode/security/classifier.py`
  Responsibility: safe-command policy plus compatibility wrappers over canonical shell primitives.
- Modify: `tests/tools/test_permissions.py`
  Responsibility: regression coverage for canonical tokenizer/env helpers and permission wrappers.
- Modify: `tests/security/test_classifier.py`
  Responsibility: regression coverage for classifier wrappers and classifier behavior on shell edge cases.

## Task 1: Extend `_tokenizer.py` Into The Canonical Shell Primitive Module

**Files:**
- Modify: `src/xxcode/tools/BashTool/_tokenizer.py`
- Modify: `tests/tools/test_permissions.py`

- [ ] **Step 1: Write the failing tests for canonical env stripping and quoted-value handling**

Add these imports near the top of `tests/tools/test_permissions.py`:

```python
from xxcode.tools.BashTool._tokenizer import (
    extract_base_command as canonical_extract_base_command,
    split_pipeline as canonical_split_pipeline,
    strip_all_safe_env_prefixes as canonical_strip_all_safe_env_prefixes,
    strip_safe_env_vars as canonical_strip_safe_env_vars,
    tokenize as canonical_tokenize,
)
```

Add these tests below the existing tokenizer tests:

```python
class TestCanonicalTokenizerPrimitives:
    def test_split_pipeline_treats_ampersand_as_background_separator(self):
        result = canonical_split_pipeline("make & npm run build")
        assert result == ["make", "npm run build"]

    def test_tokenize_preserves_escaped_spaces(self):
        tokens = canonical_tokenize(r"echo hello\ world")
        assert tokens == ["echo", "hello world"]

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
```

- [ ] **Step 2: Run the focused tests to verify they fail first**

Run:

```powershell
py -3.11 -m pytest tests/tools/test_permissions.py::TestCanonicalTokenizerPrimitives -v
```

Expected:

- FAIL during collection because `_tokenizer.py` does not yet export
  `strip_safe_env_vars` / `strip_all_safe_env_prefixes`
- or FAIL on one of:
  - quoted env-value handling
  - safe env without trailing command
  - unsafe env prefix preservation
  - Windows `.exe` base-command normalization

- [ ] **Step 3: Add canonical env stripping and update `extract_base_command` in `_tokenizer.py`**

Update `src/xxcode/tools/BashTool/_tokenizer.py` so the top of the file contains:

```python
from __future__ import annotations

import re


SAFE_ENV_VARS: set[str] = {
    "GOEXPERIMENT", "GOOS", "GOARCH", "GOPATH", "GOROOT",
    "GOPROXY", "GOMODCACHE", "GONOSUMCHECK", "GONOSUMDB", "GOPRIVATE",
    "RUST_BACKTRACE", "RUST_LOG", "RUSTFLAGS",
    "NODE_ENV", "NODE_OPTIONS",
    "PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONWARNINGS",
    "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_TIME",
    "HOME", "USER", "PATH", "TERM", "SHELL",
    "CI", "GITHUB_ACTIONS", "GITLAB_CI",
    "DISPLAY", "WAYLAND_DISPLAY",
    "EDITOR", "VISUAL", "PAGER",
}

_ENV_NAME_RE = re.compile(r"^[A-Za-z_]\w*$")
```

Add these helpers below `split_pipeline()`:

```python
def _split_first_shell_token(command: str) -> tuple[str, str] | None:
    cleaned = command.lstrip()
    if not cleaned:
        return None

    in_single = False
    in_double = False
    i = 0
    n = len(cleaned)

    while i < n:
        ch = cleaned[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch in (" ", "\t") and not in_single and not in_double:
            break
        i += 1

    return cleaned[:i], cleaned[i:]


def _leading_env_name(token: str) -> str | None:
    if "=" not in token:
        return None
    name, _value = token.split("=", 1)
    if not _ENV_NAME_RE.fullmatch(name):
        return None
    return name


def strip_safe_env_vars(command: str) -> str:
    cleaned = command.lstrip()
    if not cleaned:
        return ""

    split = _split_first_shell_token(cleaned)
    if split is None:
        return cleaned

    token, rest = split
    env_name = _leading_env_name(token)
    if env_name is None or env_name not in SAFE_ENV_VARS:
        return cleaned

    if not rest.strip():
        return cleaned

    return rest.lstrip()


def strip_all_safe_env_prefixes(command: str) -> str:
    previous = None
    current = command
    while previous != current:
        previous = current
        current = strip_safe_env_vars(current)
    return current


def normalize_base_token(token: str) -> str:
    if "=" in token:
        return token

    token = token.rsplit("/", 1)[-1]
    token = token.rsplit("\\", 1)[-1]

    if "." in token:
        name_part = token.rsplit(".", 1)[0]
        if name_part:
            token = name_part

    return token
```

Replace `extract_base_command()` with:

```python
def extract_base_command(command: str) -> str | None:
    cleaned = strip_all_safe_env_prefixes(command.strip())
    if not cleaned:
        return None

    tokens = tokenize(cleaned)
    if not tokens:
        return None

    base = tokens[0]
    if base in ("sudo", "doas", "pkexec") and len(tokens) > 1:
        base = tokens[1]

    return normalize_base_token(base)
```

- [ ] **Step 4: Run the focused tokenizer primitive tests**

Run:

```powershell
py -3.11 -m pytest tests/tools/test_permissions.py::TestCanonicalTokenizerPrimitives -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/tools/test_permissions.py src/xxcode/tools/BashTool/_tokenizer.py
git commit -m "refactor: centralize bash tokenizer primitives and env parsing"
```

## Task 2: Convert `permissions.py` To Thin Wrappers Over `_tokenizer.py`

**Files:**
- Modify: `src/xxcode/tools/BashTool/permissions.py`
- Modify: `tests/tools/test_permissions.py`

- [ ] **Step 1: Write the failing wrapper-level permission tests**

Add these tests to `tests/tools/test_permissions.py` under the existing
`TestStripSafeEnvVars` and `TestGetSimpleCommandPrefix` sections:

```python
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
```

- [ ] **Step 2: Run the focused permission-wrapper tests to verify they fail first**

Run:

```powershell
py -3.11 -m pytest tests/tools/test_permissions.py::TestPermissionWrappersWithQuotedEnvValues -v
```

Expected: FAIL because `permissions.py` still uses the old regex-based env
stripping functions.

- [ ] **Step 3: Replace local env stripping in `permissions.py` with imported canonical helpers**

Change the imports at the top of `src/xxcode/tools/BashTool/permissions.py` to:

```python
from ._tokenizer import (
    split_pipeline as _split_compound,
    strip_all_safe_env_prefixes as _canonical_strip_all_safe_env_prefixes,
    strip_safe_env_vars as _canonical_strip_safe_env_vars,
    tokenize,
)
```

Delete the local `_ENV_ASSIGN_RE` constant and replace the two wrapper
functions with:

```python
def strip_safe_env_vars(command: str) -> str:
    """Strip safe environment variable assignments from the command."""
    return _canonical_strip_safe_env_vars(command)


def strip_all_safe_env_prefixes(command: str) -> str:
    """Repeatedly strip safe env var prefixes."""
    return _canonical_strip_all_safe_env_prefixes(command)
```

Remove the now-unused `import re` only if nothing else in the file references
it after this change. Leave unrelated import cleanup out of scope for this
refactor.

- [ ] **Step 4: Run the full permission test file**

Run:

```powershell
py -3.11 -m pytest tests/tools/test_permissions.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/tools/test_permissions.py src/xxcode/tools/BashTool/permissions.py
git commit -m "refactor: reuse canonical bash env stripping"
```

## Task 3: Refactor `security/classifier.py` To Use Canonical Shell Primitives

**Files:**
- Modify: `src/xxcode/security/classifier.py`
- Modify: `tests/security/test_classifier.py`
- Test: `tests/tools/test_security_checks.py`

- [ ] **Step 1: Write the failing classifier-wrapper and edge-case tests**

Update the import block in `tests/security/test_classifier.py` to include
`_tokenize_command`:

```python
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
```

Add these tests:

```python
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

    def test_extract_base_command_preserves_unsafe_env_prefix(self):
        base, sub, has_sudo = _extract_base_command("LD_PRELOAD=evil.so ls")
        assert (base, sub, has_sudo) == ("LD_PRELOAD=evil.so", "ls", False)

    @pytest.mark.xfail(reason="Known limitation: sudo option prefixes are not normalized in this phase")
    def test_extract_base_command_documents_sudo_option_prefix_limitation(self):
        base, sub, has_sudo = _extract_base_command("sudo -u root ls")
        assert (base, sub, has_sudo) == ("ls", None, True)
```

- [ ] **Step 2: Run the focused classifier tests to verify they fail first**

Run:

```powershell
py -3.11 -m pytest tests/security/test_classifier.py::TestClassifierSharedWrappers -v
```

Expected: FAIL on at least:

- background `&` splitting
- escaped-space tokenization
- quoted env stripping
- Windows `.exe` base-command normalization
- or unsafe env prefix preservation

- [ ] **Step 3: Refactor `classifier.py` into policy plus thin compatibility wrappers**

Replace the top imports in `src/xxcode/security/classifier.py` with:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto

from ..tools.BashTool._tokenizer import (
    normalize_base_token as _canonical_normalize_base_token,
    split_pipeline as _canonical_split_pipeline,
    strip_all_safe_env_prefixes as _canonical_strip_all_safe_env_prefixes,
    strip_safe_env_vars as _canonical_strip_safe_env_vars,
    tokenize as _canonical_tokenize,
)
from .patterns import is_dangerous
```

Delete the local `SAFE_ENV_VARS` constant.

Add these module-level constants near the safe-command tables:

```python
_PRIVILEGE_PREFIXES = ("sudo", "doas", "pkexec")
_COMMAND_STOP_TOKENS = {
    ">", ">>", "<", "2>", "1>", "&>", "2>&1", "1>&2",
    "|", ";", "&", "&&", "||",
}
```

Replace the helper functions at the bottom of the file with:

```python
def strip_safe_env_vars(command: str) -> str:
    """Strip safe environment variable assignments from a command prefix."""
    return _canonical_strip_safe_env_vars(command)


def _split_pipeline(command: str) -> list[str]:
    """Split a command by shell control operators using the canonical helper."""
    return _canonical_split_pipeline(command)


def _tokenize_command(command: str) -> list[str]:
    """Tokenize a command using the canonical shell tokenizer."""
    return _canonical_tokenize(command)


def _extract_base_command(command: str) -> tuple[str | None, str | None, bool]:
    """Extract base command, subcommand, and privilege-elevation flag."""
    cleaned = _canonical_strip_all_safe_env_prefixes(command.strip())
    tokens = _tokenize_command(cleaned)

    filtered: list[str] = []
    for token in tokens:
        if token in _COMMAND_STOP_TOKENS:
            break
        filtered.append(token)

    if not filtered:
        return None, None, False

    has_sudo = filtered[0] in _PRIVILEGE_PREFIXES
    idx = 1 if has_sudo else 0
    if idx >= len(filtered):
        return None, None, has_sudo

    base = _canonical_normalize_base_token(filtered[idx])
    subcommand = filtered[idx + 1] if idx + 1 < len(filtered) else None
    return base, subcommand, has_sudo
```

Keep `classify_command()` behavior unchanged except for relying on these
wrappers.

- [ ] **Step 4: Run the classifier and adjacent shell safety tests**

Run:

```powershell
py -3.11 -m pytest tests/security/test_classifier.py tests/tools/test_permissions.py tests/tools/test_security_checks.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/security/test_classifier.py tests/tools/test_permissions.py src/xxcode/security/classifier.py
git commit -m "refactor: unify bash classifier parsing"
```

## Self-Review

- Spec coverage:
  - canonical env stripping in `_tokenizer.py`: covered by Task 1
  - `permissions.py` consuming canonical helpers: covered by Task 2
  - `classifier.py` consuming canonical helpers while preserving wrappers: covered by Task 3
  - explicit edge-case TDD coverage: covered across Tasks 1 and 3
- Placeholder scan:
  - no `TBD`, `TODO`, or deferred code steps remain
- Type consistency:
  - `_tokenizer.normalize_base_token()` becomes the shared normalization primitive
  - `_tokenizer.extract_base_command()` remains `str | None`
  - `classifier._extract_base_command()` remains `tuple[str | None, str | None, bool]`
  - `strip_safe_env_vars()` and `strip_all_safe_env_prefixes()` stay publicly available from `permissions.py`
  - the documented `sudo -u root ls` limitation is captured as `xfail`, not fixed opportunistically

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-04-bash-security-parser-unification.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
