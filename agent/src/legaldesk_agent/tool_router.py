"""Explicit tool routing seam; it does not infer or authorize requests."""

from __future__ import annotations

from enum import StrEnum


class ToolTarget(StrEnum):
    MCP = "mcp"
    LAMBDA = "lambda"


class LegalDeskTool(StrEnum):
    LIST_MATTER_DOCUMENTS = "list_matter_documents"
    GET_DOCUMENT_METADATA = "get_document_metadata"
    CREATE_REVIEW_TASK = "create_review_task"


TOOL_TARGETS = {
    LegalDeskTool.LIST_MATTER_DOCUMENTS: ToolTarget.MCP,
    LegalDeskTool.GET_DOCUMENT_METADATA: ToolTarget.MCP,
    LegalDeskTool.CREATE_REVIEW_TASK: ToolTarget.LAMBDA,
}


def select_tool(tool_name: str) -> tuple[LegalDeskTool, ToolTarget]:
    """Route an explicitly named tool; no model-generated scope is interpreted."""

    try:
        tool = LegalDeskTool(tool_name)
    except (ValueError, TypeError) as exc:
        raise ValueError("unsupported tool") from exc
    return tool, TOOL_TARGETS[tool]


__all__ = ["LegalDeskTool", "TOOL_TARGETS", "ToolTarget", "select_tool"]
