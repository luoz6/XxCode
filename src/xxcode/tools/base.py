"""XxCode 工具系统底层契约核心 — 所有工具的抽象基类与执行结果数据结构。

本模块是项目内置工具（read_file / write_file / edit_file / grep_search /
glob_match / run_shell）、未来 MCP 扩展工具、REPL 交互工具统一遵循的
底层契约。模块包含六大核心组件：

  1. Tool            — 工具抽象基类，定义 8 阶段执行流水线、安全策略、
                       UI 渲染扩展点（含分组渲染与回填）、结果截断落盘
  2. ToolCall        — 模型发出的待执行工具调用
  3. ToolResult      — 工具执行结果，覆盖业务数据返回 / 对话消息注入 /
                       上下文修改三大能力
  4. TOOL_DEFAULTS   — 全局默认安全配置，贯彻 fail-closed（默认关闭）原则
  5. build_tool      — 工厂函数，合并工具类定义、全局默认值与调用方覆盖

全程遵循 "错误即数据" 哲学：流水线任何阶段的失败都转化为 is_error=True
的 ToolResult，绝不抛出异常。默认禁止并发、默认判定非只读、默认判定需
审批、默认判定具破坏性，子类必须显式声明安全能力才能放宽限制。

UI 渲染架构遵循 "渲染即工具" 模式：
  - render_tool_use:       单实例调用描述
  - render_grouped_tool_use: 批量合并渲染（滑动窗口分组器触发）
  - render_tool_result:    结果摘要
  - backfill_observable_input: 生成 UI 富信息副本，保护 API 参数纯净
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# ═════════════════════════════════════════════════════════════════════
# 全局默认安全配置 — 贯彻 fail-closed（默认关闭）原则
# ═════════════════════════════════════════════════════════════════════

TOOL_DEFAULTS: dict[str, Any] = {
    "is_concurrency_safe": False,
    "is_read_only": False,
    "auto_approve": False,
    "timeout_seconds": 30.0,
    "is_destructive": True,
    "max_output_chars": 50_000,
}


# ═════════════════════════════════════════════════════════════════════
# 工具抽象基类
# ═════════════════════════════════════════════════════════════════════

class Tool(ABC):
    """所有工具的抽象基类 — 定义接口元数据、核心执行方法、安全策略。

    每个工具实例包含三类元数据（name / description / input_schema），
    其中 input_schema 是一个 pydantic BaseModel 子类，由框架自动生成
    JSON Schema 发送给模型，用于指导模型输出合法的 tool_use 参数。

    8 阶段执行流水线:
      1. 工具查找:    ToolRegistry → 按名称/别名定位 Tool 实例，检查废弃别名
      2. Schema 校验: Pydantic model_validate → 类型/结构检查
      3. 并行启动:    pre_tool_hook + Bash 分类器投机识别安全命令
      4. 权限链:      分类器自动审批 → is_read_only → needs_permission → 交互式确认
      5. 核心执行:    tool.execute(validated_input, context) + 超时/沙箱保护
      6. 结果格式化:  tool.format_large_result(content, max_chars) → 截断/落盘
      7. Post-Hook:   成功 → post_tool_use / 失败 → post_tool_fail
      8. 消息发射:    tool_result block 注入对话历史

    子类最少实现:
      - 类属性: name, description, input_schema
      - 实例方法: execute(input, context) → str

    UI 渲染契约 ("渲染即工具" 模式):
      - render_tool_use()          → 单实例调用描述（CLI / Web / IDE 通用）
      - render_tool_result()       → 执行结果摘要
      - render_grouped_tool_use()  → (可选) 批量合并渲染，由滑动窗口分组器触发
      - backfill_observable_input()→ (可选) 生成 UI 富信息副本，补全路径/行号等
    """

    # ── 元数据（子类必须覆盖）──────────────────────────────────
    name: str
    description: str
    input_schema: type[BaseModel]
    aliases: list[str] = []
    deprecated_aliases: dict[str, str] = {}

    # ── 两阶段验证 ────────────────────────────────────────

    async def validate_input(self, input: BaseModel, context: dict[str, Any]) -> tuple[bool, str]:
        """Stage 2: 业务逻辑验证（在 Pydantic Schema 校验之后执行）。

        Pydantic 负责类型与必填检查（Stage 1），此方法负责运行时业务规则
        检查（Stage 2）——文件是否存在、参数值是否合法、依赖是否满足等。

        Returns:
            (True, "")           — 验证通过，可以继续执行。
            (False, error_msg)   — 验证失败，error_msg 将包装为
                                   ToolResult(is_error=True) 返回给模型。
        """
        return True, ""

    # ── 核心执行接口 ────────────────────────────────────────

    @abstractmethod
    async def execute(self, input: BaseModel, context: dict[str, Any]) -> str:
        """执行工具并返回原始结果字符串。"""
        ...

    # ── 结果截断与落盘 ──────────────────────────────────────

    # Per-tool max output chars override.  Set to a class-level attribute
    # to specify a tool-specific threshold (e.g. ReadFileTool → 200K,
    # BashTool → 100K).  If None, falls back to TOOL_DEFAULTS.
    _max_output_chars: int | None = None

    # ── Lazy loading (Section 4.10) ──────────────────────────────
    # Set _should_defer = True on tools that should NOT appear in the
    # initial tool list sent to the model.  The model discovers them
    # via ToolSearchTool and loads them on demand with select:Name.
    _should_defer: bool = False
    # Free-text hint for keyword matching.  Include synonyms,
    # alternative names, and related concepts.  Example for a
    # notebook tool: "notebook jupyter ipynb cell".
    _search_hint: str = ""

    def get_max_output_chars(self) -> int:
        """Return this tool's max output character limit.

        Checks in order:
          1. Instance-level _max_output_chars or max_output_chars (from build_tool factory)
          2. Class-level _max_output_chars
          3. TOOL_DEFAULTS global value
        """
        # Use __dict__ to only check instance attributes, not class-level
        instance_val = self.__dict__.get("_max_output_chars")
        if instance_val is None:
            instance_val = self.__dict__.get("max_output_chars")
        if instance_val is not None:
            return instance_val
        cls_val = getattr(type(self), "_max_output_chars", None)
        if cls_val is not None:
            return cls_val
        return TOOL_DEFAULTS["max_output_chars"]

    async def format_large_result(
        self,
        content: str,
        max_chars: int,
        tool_use_id: str = "",
        session_dir: str = "",
    ) -> str:
        """Stage 6: 超长结果截断与落盘 — 防止撑爆上下文窗口。

        Enforces two tiers:
          - Tier 1 (max_chars): persist-to-disk + preview
          - Tier 2 (absolute): hard truncation safety ceiling
        """
        if len(content) <= max_chars:
            return content

        from xxcode.core.budget import apply_tool_result_budget, clamp_to_absolute_max

        # Tier 2: Absolute ceiling — hard truncate before any I/O
        from xxcode.config import get_config
        cfg = get_config()
        if len(content) > cfg.max_tool_result_chars_absolute:
            content = clamp_to_absolute_max(
                content, cfg.max_tool_result_chars_absolute,
            )

        return await apply_tool_result_budget(
            raw_output=content,
            tool_use_id=tool_use_id,
            session_dir=Path(session_dir) if session_dir else Path("."),
            max_chars=max_chars,
        )

    # ── 安全策略（子类可选覆盖）────────────────────────────
    #
    # Insight 4.11.2: Security as behavior functions.
    # is_read_only, is_destructive, and is_concurrency_safe are NOT static
    # booleans — they accept the validated input so subclass overrides can
    # give context-aware answers.  BashTool running "ls" → read_only=True,
    # destructive=False; BashTool running "rm -rf /" → read_only=False,
    # destructive=True.

    def is_read_only(self, input: BaseModel | None = None) -> bool:
        """Check if this tool is read-only.

        Args:
            input: Optional validated input model.  Tools may use this
                   to give a context-aware answer — e.g. BashTool can
                   return True for "ls" but False for "rm".
        """
        if hasattr(self, "_is_read_only"):
            return self._is_read_only
        return TOOL_DEFAULTS["is_read_only"]

    def is_concurrency_safe(self, input: BaseModel | None = None) -> bool:
        """Check if this tool is safe to run concurrently with others.

        Args:
            input: Optional validated input model for context-aware decisions.
        """
        if hasattr(self, "_is_concurrency_safe"):
            return self._is_concurrency_safe
        return TOOL_DEFAULTS["is_concurrency_safe"]

    def needs_permission(self, input: BaseModel) -> bool:
        return not self.is_read_only(input)

    def is_destructive(self, input: BaseModel | None = None) -> bool:
        """Check if this tool is destructive.

        Tools can override to make the decision input-aware — a BashTool
        running 'ls' is not destructive, but 'rm' is.

        Args:
            input: Optional validated input model. When provided, tools
                   may use it to give a more specific answer.
        """
        if hasattr(self, "_is_destructive"):
            return self._is_destructive
        return TOOL_DEFAULTS["is_destructive"]

    # ── Polymorphic permission helpers (Insight 4.11.1) ─────────
    # Replace hardcoded tool.name checks with polymorphic dispatch
    # so the permission pipeline doesn't need to know about specific
    # tool types.  New tools opt in by overriding these predicates.

    def has_command_classifier(self) -> bool:
        """Return True if this tool has a command classifier for auto-approval.

        BashTool overrides this — safe commands (ls, cat, etc.) skip the
        permission prompt via classify_command() in the resolver.
        """
        return False

    def confirms_file_paths(self) -> bool:
        """Return True if this tool should auto-confirm file paths on grant.

        WriteFileTool and EditFileTool override this — when the user grants
        permission, the file path is added to the confirmed-paths whitelist
        so subsequent edits to the same file skip the prompt.
        """
        return False

    def supports_sibling_abort(self) -> bool:
        """Return True if this tool participates in the sibling abort cascade.

        When a bash tool fails, the executor aborts all other in-flight
        bash tools (since the shell environment may be in an unknown state).
        Tools override this to opt into (or out of) that cascade.
        """
        return False

    # ── 运行时自检 ─────────────────────────────────────────

    def is_enabled(self) -> bool:
        if hasattr(self, "_is_enabled"):
            return self._is_enabled
        return self._check_enabled()

    def _check_enabled(self) -> bool:
        return True

    # ═══════════════════════════════════════════════════════════════
    # UI 渲染契约 — "渲染即工具" 模式
    #
    # 四个渲染接口构成完整的 UI 扩展体系：
    #   1. render_tool_use         — 必选，子类强烈建议覆盖
    #   2. render_tool_result      — 必选，子类强烈建议覆盖
    #   3. render_grouped_tool_use — 可选，默认合并渲染
    #   4. backfill_observable_input — 可选，默认透传
    #
    # 渲染函数必须是纯函数（或仅依赖 context），不能产生副作用。
    # backfill_observable_input 必须返回新对象，严禁修改传入的
    # API 参数对象，防止污染 Token 缓存。
    # ═══════════════════════════════════════════════════════════════

    def render_tool_use(self, input: BaseModel) -> str:
        """生成单个工具调用的用户可读描述文本（CLI 单行格式化）。

        此方法将核心计算逻辑与 UI 渲染解耦 —— CLI / Web / IDE 等不同
        前端可调用此方法获取统一的工具调用描述，然后按各自风格渲染。

        子类覆盖示例:
          - ReadFileTool:   "📖 Read 5 files: src/main.py, ..."
          - EditFileTool:   "📝 Edit src/main.py:  -old  +new"
          - RunShellTool:   "💻 ls -la /tmp"

        Args:
            input: Pydantic 模型实例（已校验）。

        Returns:
            人类可读的工具调用描述字符串，不含 ANSI 转义（由 UI 层添加）。
        """
        return f"{self.name}: {input}"

    def render_tool_result(self, content: str, is_error: bool) -> str:
        """生成工具执行结果的人类可读摘要文本。

        与 render_tool_use() 对称 —— 将原始的 content 字符串转换为
        适合终端/前端展示的格式化摘要。

        子类覆盖示例:
          - EditFileTool:   成功 → 显示 +/- 行数；失败 → 显示上下文不匹配
          - ReadFileTool:   显示读取的行数
          - RunShellTool:   显示命令返回码

        Args:
            content:  工具执行的原始输出字符串（可能已截断）。
            is_error: 该结果是否为错误。

        Returns:
            人类可读的结果摘要字符串。
        """
        if is_error:
            return f"Error: {content[:200]}"
        if len(content) <= 200:
            return content
        return content[:200] + "…"

    def render_grouped_tool_use(self, inputs: list[BaseModel]) -> str:
        """（可选）批量合并渲染 —— 将多个同类型工具调用合并为一行摘要。

        由终端 UI 的滑动窗口分组器触发：当识别到连续的同类 tool_use
        事件时，将其收集到 Buffer 中；当类型切换或出现非工具事件时，
        统一调用此方法进行批量渲染。

        默认实现逐个调用 render_tool_use() 并用换行连接。
        子类可覆盖为紧凑的合并渲染，例如:
          - ReadFileTool: "📖 Read 3 files: a.py, b.py, c.py"
          - EditFileTool: "📝 2 edits in src/main.py"

        Args:
            inputs: 同类型工具调用的 Pydantic 模型列表。

        Returns:
            批量渲染后的描述字符串。
        """
        if len(inputs) == 1:
            return self.render_tool_use(inputs[0])
        parts = [self.render_tool_use(inp) for inp in inputs]
        return "\n".join(parts)

    def backfill_observable_input(
        self, input: BaseModel, context: dict[str, Any],
    ) -> BaseModel:
        """（可选）生成 UI 渲染用的"富信息"输入副本。

        在 UI 准备渲染 ToolCall 时调用，允许工具在不污染 API 请求参数的
        前提下补全展示用信息（如绝对路径、行号范围、文件大小等）。

        默认实现直接返回原始 input。
        子类覆盖时必须 COPY 一份新对象后修改，严禁原地修改传入参数。

        Args:
            input:   原始 Pydantic 模型实例（已校验）。
            context: 执行上下文字典（cwd, config 等）。

        Returns:
            富信息模型实例（可与原始类型相同，也可为不同展示类型）。
        """
        return input

    # ── API 协议导出 ────────────────────────────────────────

    def to_api_schema(self) -> dict[str, Any]:
        """生成符合 Anthropic API 协议的工具 schema 字典。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema.model_json_schema(),
        }


# ═════════════════════════════════════════════════════════════════════
# 工具调用 — 模型发出的待处理请求
# ═════════════════════════════════════════════════════════════════════

@dataclass
class ToolCall:
    """模型在流式响应中生成的单次工具调用。"""

    id: str
    name: str
    input: dict[str, Any]


# ═════════════════════════════════════════════════════════════════════
# 工具执行结果 — 业务数据返回 / 对话消息注入 / 上下文修改
# ═════════════════════════════════════════════════════════════════════

@dataclass
class ToolResult:
    """工具执行结果，承载三大核心能力。"""

    tool_use_id: str
    content: str
    is_error: bool = False


# ═════════════════════════════════════════════════════════════════════
# 工具工厂 — 标准化实例构建与配置合并
# ═════════════════════════════════════════════════════════════════════

def build_tool(
    tool_cls: type[Tool],
    *,
    name: str | None = None,
    description: str | None = None,
    timeout: float | None = None,
    max_output_chars: int | None = None,
    is_concurrency_safe: bool | None = None,
    is_read_only: bool | None = None,
    is_destructive: bool | None = None,
    is_enabled: bool | None = None,
    should_defer: bool | None = None,
    search_hint: str | None = None,
    **overrides,
) -> Tool:
    """工厂函数 — 创建标准化配置的工具实例。

    配置合并规则（优先级从高到低）:
      1. **overrides          — 调用方显式传入的覆盖值
      2. 函数参数 (name / description / timeout / max_output_chars /
         is_concurrency_safe / is_read_only / is_destructive)
      3. TOOL_DEFAULTS 全局默认值
      4. tool_cls 类属性定义值
    """
    instance = tool_cls()

    if name is not None:
        instance.name = name
    if description is not None:
        instance.description = description

    if timeout is not None:
        setattr(instance, "timeout_seconds", timeout)
    elif not hasattr(instance, "timeout_seconds"):
        setattr(instance, "timeout_seconds", TOOL_DEFAULTS["timeout_seconds"])

    if max_output_chars is not None:
        setattr(instance, "_max_output_chars", max_output_chars)

    if is_concurrency_safe is not None:
        instance._is_concurrency_safe = is_concurrency_safe
    if is_read_only is not None:
        instance._is_read_only = is_read_only
    if is_destructive is not None:
        instance._is_destructive = is_destructive
    if is_enabled is not None:
        instance._is_enabled = is_enabled
    if should_defer is not None:
        instance._should_defer = should_defer
    if search_hint is not None:
        instance._search_hint = search_hint

    for key, value in overrides.items():
        setattr(instance, key, value)

    return instance
