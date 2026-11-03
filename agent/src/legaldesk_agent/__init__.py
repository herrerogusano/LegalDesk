"""Application-side adapter for the managed LegalDesk AgentCore Harness."""

from .client import (
    AgentChatResult,
    AgentTelemetryEvent,
    AgentToolCall,
    HarnessInvocationError,
    HarnessMemoryScope,
    HarnessInvocationScope,
    HarnessInvoker,
    HarnessToolResult,
    HarnessUsage,
    InvokeResult,
    invoke_agent_chat,
    new_session_id,
)
from .local_agent import LocalAgent
from .tool_router import LegalDeskTool, ToolTarget, select_tool
from .trace_evidence import (
    HarnessEvidenceCollector,
    StructuredHarnessEvidence,
    normalize_harness_evidence,
)

__all__ = [
    "AgentChatResult",
    "AgentTelemetryEvent",
    "AgentToolCall",
    "HarnessInvocationError",
    "HarnessMemoryScope",
    "HarnessInvocationScope",
    "HarnessInvoker",
    "HarnessToolResult",
    "HarnessUsage",
    "InvokeResult",
    "invoke_agent_chat",
    "LocalAgent",
    "new_session_id",
    "LegalDeskTool",
    "ToolTarget",
    "select_tool",
    "HarnessEvidenceCollector",
    "StructuredHarnessEvidence",
    "normalize_harness_evidence",
]
