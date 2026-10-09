"""Versioned, data-driven IDP schema registry."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .models import DocumentType, IDPContractError, IDP_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class IDPFieldSpec:
    name: str
    value_type: str
    description: str
    evidence_required: bool = True
    review_sensitive: bool = False


@dataclass(frozen=True, slots=True)
class IDPSchema:
    document_type: DocumentType
    version: str
    fields: Mapping[str, IDPFieldSpec]

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.fields:
            raise IDPContractError("schema is invalid")
        if any(name != spec.name for name, spec in self.fields.items()):
            raise IDPContractError("schema field names are inconsistent")

    def field(self, name: str) -> IDPFieldSpec:
        try:
            return self.fields[name]
        except KeyError:
            raise IDPContractError("field is not in the selected schema") from None


def _fields(*specs: tuple[str, str, str, bool, bool]) -> Mapping[str, IDPFieldSpec]:
    return MappingProxyType({
        name: IDPFieldSpec(name, value_type, description, evidence_required, review_sensitive)
        for name, value_type, description, evidence_required, review_sensitive in specs
    })


_CONTRACT = _fields(
    ("parties", "array[string]", "Named contract parties", True, False),
    ("effective_date", "date", "Explicit effective date", True, False),
    ("explicit_expiration_date", "date", "Explicit expiration date", True, False),
    ("initial_duration_value", "number", "Explicit initial duration", True, False),
    ("initial_duration_unit", "string", "Initial duration unit", True, False),
    ("automatic_renewal", "boolean", "Explicit automatic renewal", True, True),
    ("renewal_period_value", "number", "Renewal period value", True, True),
    ("renewal_period_unit", "string", "Renewal period unit", True, True),
    ("termination_notice_value", "number", "Termination notice value", True, True),
    ("termination_notice_unit", "string", "Termination notice unit", True, True),
    ("amount", "number", "Explicit monetary amount", True, False),
    ("currency", "string", "Explicit currency", True, False),
    ("jurisdiction", "string", "Jurisdiction stated in the document", True, True),
    ("governing_law", "string", "Explicit governing law", True, True),
    ("subtype", "string", "Supported subtype such as NDA", True, False),
)

_DEMAND = _fields(
    ("claimants", "array[string]", "Claimants", True, False),
    ("defendants", "array[string]", "Defendants", True, False),
    ("court", "string", "Named court", True, False),
    ("case_number", "string", "Case number", True, False),
    ("filing_date", "date", "Filing date", True, False),
    ("claims", "array[string]", "Claims or requested remedies", True, True),
    ("claimed_amount", "number", "Claimed monetary amount", True, False),
    ("currency", "string", "Claimed amount currency", True, False),
)

_JUDGMENT = _fields(
    ("court", "string", "Named court", True, False),
    ("case_number", "string", "Case number", True, False),
    ("decision_date", "date", "Decision date", True, False),
    ("parties", "array[string]", "Judgment parties", True, False),
    ("operative_ruling", "string", "Operative ruling", True, True),
    ("costs_statement", "string", "Costs statement", True, True),
    ("appeal_information", "string", "Appeal information", True, True),
)

_UNKNOWN = _fields(
    ("document_type", "string", "Observed document type or ambiguity", True, True),
    ("general_document_evidence", "string", "General identifying evidence", True, False),
)


DEFAULT_SCHEMAS: Mapping[tuple[DocumentType, str], IDPSchema] = MappingProxyType({
    (DocumentType.CONTRACT, IDP_SCHEMA_VERSION): IDPSchema(DocumentType.CONTRACT, IDP_SCHEMA_VERSION, _CONTRACT),
    (DocumentType.DEMAND, IDP_SCHEMA_VERSION): IDPSchema(DocumentType.DEMAND, IDP_SCHEMA_VERSION, _DEMAND),
    (DocumentType.JUDGMENT, IDP_SCHEMA_VERSION): IDPSchema(DocumentType.JUDGMENT, IDP_SCHEMA_VERSION, _JUDGMENT),
    (DocumentType.UNKNOWN, IDP_SCHEMA_VERSION): IDPSchema(DocumentType.UNKNOWN, IDP_SCHEMA_VERSION, _UNKNOWN),
})


class IDPSchemaRegistry:
    def __init__(self, schemas: Mapping[tuple[DocumentType, str], IDPSchema] | None = None) -> None:
        self._schemas = dict(DEFAULT_SCHEMAS if schemas is None else schemas)
        if not self._schemas:
            raise IDPContractError("schema registry cannot be empty")

    def get(self, document_type: DocumentType | str, version: str = IDP_SCHEMA_VERSION) -> IDPSchema:
        try:
            selected = document_type if isinstance(document_type, DocumentType) else DocumentType(document_type)
        except ValueError:
            raise IDPContractError("document type is unsupported") from None
        try:
            return self._schemas[(selected, version)]
        except KeyError:
            raise IDPContractError("schema version is unsupported") from None

    def versions(self, document_type: DocumentType | str) -> tuple[str, ...]:
        selected = document_type if isinstance(document_type, DocumentType) else DocumentType(document_type)
        return tuple(sorted(version for kind, version in self._schemas if kind is selected))

    def supported_types(self) -> tuple[DocumentType, ...]:
        return tuple(sorted({kind for kind, _version in self._schemas}, key=lambda item: item.value))


__all__ = ["DEFAULT_SCHEMAS", "IDPFieldSpec", "IDPSchema", "IDPSchemaRegistry"]
