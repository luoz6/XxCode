<h1 align="center">XxCode</h1>

<p align="center">一个以 harness 为中心的 Python AI coding agent</p>

<p align="center">
  <code>Double-layer agent loop</code> · <code>Tool harness</code> · <code>Context compression</code> · <code>Memory</code> · <code>Task runtime</code> · <code>Skills / MCP / Worktree</code>
</p>

XxCode 不是把“对话 + 执行”塞进一个黑盒循环里的聊天机器人。它把 agent 的关键能力拆成清晰的 runtime 边界：对话编排、工具调度、权限判定、任务隔离、上下文压缩和记忆回收，各自独立、各自可测。

## 亮点一览

<table>
  <tr>
    <td valign="top" width="50%">
      <strong>双层 Agent Loop</strong><br>
      <code>QueryEngine</code> 负责外层会话编排，<code>CoreExecutionEngine</code> 负责内层 tool loop，职责分离更清楚，也更容易做二次开发。
    </td>
    <td valign="top" width="50%">
      <strong>Tool Harness</strong><br>
      工具调度不是“能不能调用”这么简单，而是把并发、预检、权限和结果收集统一进一条执行链。
    </td>
  </tr>
  <tr>
    <td valign="top" width="50%">
      <strong>四层上下文压缩</strong><br>
      <code>Snip -> Micro -> Collapse -> AutoCompact</code> 逐级压缩，而不是直接粗暴截断，保留更多决策信息。
    </td>
    <td valign="top" width="50%">
      <strong>Memory System</strong><br>
      用 <code>MEMORY.md</code> 做入口索引，按当前 query 召回少量高相关记忆，再注入完整内容；后台 extraction 持续沉淀 <code>user / project / feedback / reference</code> 四类记忆。
    </td>
  </tr>
  <tr>
    <td valign="top" width="50%">
      <strong>Task Runtime</strong><br>
      把子任务、worker、scope 和通知机制做成独立运行时，避免“开个子任务”退化成一次普通递归请求。
    </td>
    <td valign="top" width="50%">
      <strong>Skills / MCP / Worktree</strong><br>
      扩展能力挂在统一 runtime 上，既能接技能，也能接 MCP，还能做 worktree 隔离。
    </td>
  </tr>
  <tr>
    <td valign="top" width="50%">
      <strong>Multi-Agent Runtime</strong><br>
      支持 Coordinator 编排后台 worker，围绕 task、scope 和通知机制组织多 Agent 协作，而不是简单多开几次模型请求。
    </td>
    <td valign="top" width="50%">
      <strong>权限与执行边界</strong><br>
      shell、文件编辑和任务执行都有明确权限判断链路，安全模型是 runtime 的一部分，不是事后打补丁。
    </td>
  </tr>
</table>

## 它和普通 Agent 的区别

| 维度 | 普通 Agent | XxCode |
| --- | --- | --- |
| 核心关注 | 聊天体验优先 | harness 和 runtime 优先 |
| 执行方式 | 单一大循环 | 编排、执行、工具分层 |
| 安全模型 | 事后补救 | 权限链路前置 |
| 上下文处理 | 截断或堆叠 | 四层渐进压缩 |
| 扩展方式 | 往主循环堆逻辑 | 通过 skills / MCP / worker 挂接 |
| 状态管理 | 常常共享同一上下文 | task、scope、session 分离 |

## 运行路径

```text
用户输入
  -> CLI 解析配置和参数
  -> Memory bootstrap / recall 准备
  -> Context pipeline 压缩上下文
  -> QueryEngine 处理会话状态与 turn
  -> CoreExecutionEngine 进入 tool loop
  -> Tool harness 调度工具
  -> Permission pipeline 判定是否允许执行
  -> Task runtime 处理子任务、worker 和通知
  -> 返回结果并进入下一轮
```

## 核心模块

| 模块 | 作用 |
| --- | --- |
| [`src/xxcode/agent/query_engine.py`](src/xxcode/agent/query_engine.py) | 外层会话管理，负责状态初始化、turn 提交、skill 处理和核心循环调度 |
| [`src/xxcode/agent/loop.py`](src/xxcode/agent/loop.py) | 内层执行引擎，负责 tool loop、上下文准备、恢复与结果回写 |
| [`src/xxcode/agent/tools_executor.py`](src/xxcode/agent/tools_executor.py) | 工具调度器，负责并发、权限和结果收集 |
| [`src/xxcode/agent/task_runtime.py`](src/xxcode/agent/task_runtime.py) | 子任务运行时，管理 task、scope、worker 生命周期 |
| [`src/xxcode/tools/agent/tool.py`](src/xxcode/tools/agent/tool.py) | 子 Agent 入口，支持后续 worker 和 worktree 隔离 |
| [`src/xxcode/tools/BashTool/permissions.py`](src/xxcode/tools/BashTool/permissions.py) | shell 命令的多层权限分析与规则建议 |
| [`src/xxcode/context/pipeline.py`](src/xxcode/context/pipeline.py) | 四层上下文压缩管道 |
| [`src/xxcode/memory/recall.py`](src/xxcode/memory/recall.py) | 基于 `MEMORY.md` 的记忆回收 |
| [`src/xxcode/memory/index.py`](src/xxcode/memory/index.py) | 生成 `MEMORY.md` 索引并控制入口大小预算 |

## 快速开始

```powershell
pip install -e .

# Required
$env:XXCODE_API_KEY="your_api_key"
$env:XXCODE_API_BASE_URL="your_base_url"

# Optional: defaults to deepseek-v4-pro
$env:XXCODE_API_MODEL="deepseek-v4-pro"

xxcode
```

模型配置：

- 默认模型是 `deepseek-v4-pro`
- 可用环境变量 `XXCODE_API_MODEL` 覆盖
- 兼容 `ANTHROPIC_MODEL` 作为回退变量
- 也可以用 `--model` 按单次会话覆盖
- 代码当前支持 `claude-*`、`deepseek-*`、`gpt-*`、`o1`、`o3`、`o4`

例如：

```powershell
xxcode --model claude-sonnet-4-6
xxcode --model gpt-4o
```

单次执行：

```powershell
xxcode --model deepseek-v4-pro -p "帮我分析当前仓库里的测试结构"
```

常用参数：

- `--model`：覆盖默认模型
- `--cwd`：指定工作目录
- `--resume`：恢复已有会话
- `--list`：列出已保存会话
- `--bare`：关闭 auto-memory 等持久特性
- `--yolo`：跳过权限确认
- `--ui-backend`：切换终端 UI 后端
- `--verbose`：开启调试日志
- `--max-tokens`：限制单轮输出 tokens

## 测试与评测

使用下面这些命令来运行 benchmark 测试、生成报告，并将当前版本与 baseline profile 进行对比。

仅运行 benchmark 测试：

```powershell
python -m pytest tests/benchmark -q
```

仅生成 benchmark 报告：

```powershell
python scripts/run_unified_eval_report.py
```

运行完整 benchmark 套件：

```powershell
python scripts/run_benchmark_suite.py
```

使用显式指定的报告目录和工作目录来运行完整套件：

```powershell
python scripts/run_benchmark_suite.py --output-dir .tmp/benchmark-reports --work-dir .tmp/unified-eval-run
```

与 baseline profile 进行对比：

```powershell
python scripts/run_benchmark_suite.py --baseline-profile memory_off
python scripts/run_benchmark_suite.py --baseline-profile context_off
python scripts/run_benchmark_suite.py --baseline-profile security_relaxed
```

## 适合谁

- 想研究 agent 内核、执行链路和 runtime 设计的人。
- 想做二次开发，扩展 tool、memory、task 或多 Agent 能力的人。
- 想直接看一个 harness-first AI coding agent 是怎么落地的人。

## 后续文档

后续会继续补充更系统的文档讲解，重点会覆盖 memory、multi-agent、tool harness、权限链路和上下文工程这些部分。

当前已经补充的文档：

- [`docs/agent-loop-query-engine-and-loop-explained.md`](docs/agent-loop-query-engine-and-loop-explained.md)：从 `QueryEngine` 入口到 `CoreExecutionEngine` 内层 tool loop，按主执行路径拆解 XxCode 的核心 agent loop。

## 进一步阅读

如果你想继续往下看，建议从这些入口开始：

1. [`docs/agent-loop-query-engine-and-loop-explained.md`](docs/agent-loop-query-engine-and-loop-explained.md)
2. [`src/xxcode/agent/loop.py`](src/xxcode/agent/loop.py)
3. [`src/xxcode/agent/task_runtime.py`](src/xxcode/agent/task_runtime.py)
4. [`src/xxcode/agent/tools_executor.py`](src/xxcode/agent/tools_executor.py)
5. [`src/xxcode/tools/agent/tool.py`](src/xxcode/tools/agent/tool.py)
6. [`src/xxcode/tools/BashTool/permissions.py`](src/xxcode/tools/BashTool/permissions.py)

