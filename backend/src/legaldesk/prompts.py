"""Server-side, versioned system prompt loading for LegalDesk chat."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


MAX_SYSTEM_PROMPT_BYTES = 32 * 1024
_PROMPT_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
DEFAULT_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parents[3] / "prompts" / "legaldesk-system.md"
)


class PromptConfigurationError(ValueError):
    """The server-side prompt artifact is missing or invalid."""


@dataclass(frozen=True, slots=True)
class SystemPromptArtifact:
    """Validated prompt content plus immutable metadata for request tracing."""

    prompt_id: str
    version: str
    content: str
    sha256: str


class SystemPromptProvider(Protocol):
    """Server-side provider interface; browser input cannot select a prompt."""

    def load(self) -> SystemPromptArtifact: ...


class FileSystemSystemPromptProvider:
    """Load one configured, server-controlled UTF-8 Markdown prompt artifact."""

    def __init__(self, path: Path = DEFAULT_SYSTEM_PROMPT_PATH) -> None:
        if not isinstance(path, Path):
            raise TypeError("prompt path must be configured by the server as a Path")
        self._path = path

    def load(self) -> SystemPromptArtifact:
        try:
            with self._path.open("rb") as prompt_file:
                raw = prompt_file.read(MAX_SYSTEM_PROMPT_BYTES + 1)
        except OSError as exc:
            raise PromptConfigurationError("system prompt artifact is unavailable") from exc

        if len(raw) > MAX_SYSTEM_PROMPT_BYTES:
            raise PromptConfigurationError("system prompt artifact exceeds the size limit")
        if raw.startswith(b"\xef\xbb\xbf"):
            raise PromptConfigurationError("system prompt artifact must not contain a UTF-8 BOM")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PromptConfigurationError("system prompt artifact is not valid UTF-8") from exc

        lines = text.splitlines()
        if len(lines) < 4 or lines[0] != "---":
            raise PromptConfigurationError("system prompt metadata is missing")
        try:
            metadata_end = lines.index("---", 1)
        except ValueError as exc:
            raise PromptConfigurationError("system prompt metadata is unterminated") from exc

        metadata: dict[str, str] = {}
        for line in lines[1:metadata_end]:
            key, separator, value = line.partition(":")
            if not separator or key not in {"id", "version"} or key in metadata:
                raise PromptConfigurationError("system prompt metadata is invalid")
            value = value.strip()
            if not value:
                raise PromptConfigurationError("system prompt metadata is invalid")
            metadata[key] = value
        if set(metadata) != {"id", "version"}:
            raise PromptConfigurationError("system prompt metadata is incomplete")
        if _PROMPT_ID.fullmatch(metadata["id"]) is None:
            raise PromptConfigurationError("system prompt id is invalid")
        if _SEMVER.fullmatch(metadata["version"]) is None:
            raise PromptConfigurationError("system prompt version is invalid")

        content = "\n".join(lines[metadata_end + 1 :]).strip()
        if not content:
            raise PromptConfigurationError("system prompt content is empty")
        if any(
            unicodedata.category(character) == "Cc" and character not in "\n\r\t"
            for character in content
        ):
            raise PromptConfigurationError("system prompt content contains control characters")

        return SystemPromptArtifact(
            prompt_id=metadata["id"],
            version=metadata["version"],
            content=content,
            sha256=hashlib.sha256(raw).hexdigest(),
        )


DEFAULT_SYSTEM_PROMPT_PROVIDER = FileSystemSystemPromptProvider()
