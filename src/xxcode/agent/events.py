"""StreamEvent — yielded by CoreExecutionEngine._query_loop() and QueryEngine.submit_message()."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StreamEvent:
    """One event from the agent's streaming output.

    Types: text, tool_call, tool_result, thinking, error, cost, done
    """

    type: str
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
