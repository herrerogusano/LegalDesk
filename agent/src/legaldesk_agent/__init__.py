"""Application-side adapter for the managed LegalDesk AgentCore Harness."""

from .client import (
    AgentTelemetryEvent,
    HarnessInvocationError,
    HarnessMemoryScope,
    HarnessInvoker,
    InvokeResult,
    new_session_id,
)
from .local_agent import LocalAgent
from .tool_router import LegalDeskTool, ToolTarget, select_tool

__all__ = [
    "AgentTelemetryEvent",
    "HarnessInvocationError",
    "HarnessMemoryScope",
    "HarnessInvoker",
    "InvokeResult",
    "LocalAgent",
    "new_session_id",
    "LegalDeskTool",
    "ToolTarget",
    "select_tool",
]
