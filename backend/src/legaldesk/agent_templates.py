"""Versioned backend templates for agentic data answers (ADR-021 D3).

The agent never writes the text the user reads. Every sentence of a data block
comes from this module; the agent only selects phrases by id. The whole set is
pinned by ``TEMPLATES_SHA256`` so that changing any phrase is a reviewed,
versioned change (see ``tests/test_phase15_agent_contracts.py``).
"""

from __future__ import annotations

import hashlib
import json
from types import MappingProxyType
from typing import Mapping


TEMPLATES_VERSION = "1.1.1"

_INTRODUCTIONS = {
    "answer_from_documents": "Esto es lo que dicen los documentos del expediente.",
    "data_from_documents": "Estos son los datos extraídos de los documentos del expediente.",
    "data_and_text_from_documents": (
        "Esto es lo que consta en los documentos del expediente, "
        "en datos extraídos y en su texto."
    ),
    "inventory_of_documents": "Estos son los documentos del expediente.",
}
_TRANSITIONS = {
    "continue_with_text": "A continuación, lo que dicen los documentos:",
    "continue_with_data": "A continuación, los datos extraídos:",
    "continue_with_more_data": "Además, estos otros datos extraídos:",
}
# Group titles, keyed by criterion id and, for period/range criteria, by which
# bounds the agent supplied. ``{...}`` placeholders are filled by the backend
# from validated params only.
_GROUP_TITLES = {
    "all_documents": "Documentos consultados",
    "expires_in_period.both": "Vencen entre el {from} y el {to}",
    "expires_in_period.from": "Vencen a partir del {from}",
    "expires_in_period.to": "Vencen hasta el {to}",
    "starts_in_period.both": "Entran en vigor entre el {from} y el {to}",
    "starts_in_period.from": "Entran en vigor a partir del {from}",
    "starts_in_period.to": "Entran en vigor hasta el {to}",
    "automatic_renewal": "Con renovación automática",
    "no_automatic_renewal": "Sin renovación automática",
    "amount_in_range.both": "Importe entre {min} y {max}",
    "amount_in_range.min": "Importe desde {min}",
    "amount_in_range.max": "Importe hasta {max}",
    "document_type_is.CONTRACT": "Contratos",
    "document_type_is.DEMAND": "Demandas",
    "document_type_is.JUDGMENT": "Sentencias",
}
_FIELD_LABELS = {
    "amount": "Importe",
    "appeal_information": "Información sobre recursos",
    "automatic_renewal": "Renovación automática",
    "case_number": "Número de procedimiento",
    "claimants": "Demandantes",
    "claimed_amount": "Importe reclamado",
    "claims": "Pretensiones",
    "costs_statement": "Costas",
    "court": "Tribunal",
    "currency": "Moneda",
    "decision_date": "Fecha de la resolución",
    "defendants": "Demandados",
    "document_type": "Tipo de documento",
    "effective_date": "Fecha de entrada en vigor",
    "explicit_expiration_date": "Fecha de vencimiento",
    "filing_date": "Fecha de presentación",
    "general_document_evidence": "Evidencia general del documento",
    "governing_law": "Ley aplicable",
    "initial_duration_unit": "Unidad de la duración inicial",
    "initial_duration_value": "Duración inicial",
    "jurisdiction": "Jurisdicción",
    "operative_ruling": "Fallo",
    "parties": "Partes",
    "renewal_period_unit": "Unidad del periodo de renovación",
    "renewal_period_value": "Periodo de renovación",
    "subtype": "Subtipo",
    "termination_notice_unit": "Unidad del preaviso de resolución",
    "termination_notice_value": "Preaviso de resolución",
}
_VALUES = {"true": "sí", "false": "no"}
_LINES = {
    "fact": "{document}: {field}: {value}",
    "pending": "{document}: {field}: {value} (pendiente de revisión humana)",
}
_AGGREGATES = {
    "count": "Número de documentos: {value}",
    "min": "Mínimo de {field}: {value}",
    "max": "Máximo de {field}: {value}",
    "min_none": "Mínimo de {field}: sin datos aceptados para calcularlo",
    "max_none": "Máximo de {field}: sin datos aceptados para calcularlo",
    "pending_excluded": "{text} ({pending} valores pendientes de revisión humana no incluidos)",
}
# Inventory of the documents of the matter (list_matter_documents): the backend
# writes every line from the metadata the tool returned; no document content.
_INVENTORY_LINES = {"document": "{document}: {media}, {status}"}
_INVENTORY_MEDIA = {"application/pdf": "PDF", "text/plain": "texto", "other": "otro formato"}
_INVENTORY_STATUS = {
    "PENDING_UPLOAD": "pendiente de subida",
    "UPLOADED": "subido",
    "PENDING_INGESTION": "pendiente de indexación",
    "INDEXED": "indexado",
    "FAILED": "con error",
    # Any lifecycle status this version does not know yet: a generic label, never the raw value.
    "other": "estado no reconocido",
}
_INVENTORY_TITLE = "Documentos del expediente"
_NOTICES = {
    "inventory_empty": "El expediente no tiene documentos con ese criterio.",
    "omitted_block": "No se ha podido mostrar una parte de la respuesta.",
    "omitted_blocks": "No se han podido mostrar {count} partes de la respuesta.",
    "tool_result": "Resultado de la búsqueda en los documentos:",
}

INTRODUCTIONS: Mapping[str, str] = MappingProxyType(_INTRODUCTIONS)
TRANSITIONS: Mapping[str, str] = MappingProxyType(_TRANSITIONS)
GROUP_TITLES: Mapping[str, str] = MappingProxyType(_GROUP_TITLES)
LINES: Mapping[str, str] = MappingProxyType(_LINES)
FIELD_LABELS: Mapping[str, str] = MappingProxyType(_FIELD_LABELS)
VALUES: Mapping[str, str] = MappingProxyType(_VALUES)
AGGREGATES: Mapping[str, str] = MappingProxyType(_AGGREGATES)
NOTICES: Mapping[str, str] = MappingProxyType(_NOTICES)
INVENTORY_LINES: Mapping[str, str] = MappingProxyType(_INVENTORY_LINES)
INVENTORY_MEDIA: Mapping[str, str] = MappingProxyType(_INVENTORY_MEDIA)
INVENTORY_STATUS: Mapping[str, str] = MappingProxyType(_INVENTORY_STATUS)
INVENTORY_TITLE = _INVENTORY_TITLE


def _compute_sha256() -> str:
    canonical = json.dumps(
        {
            "version": TEMPLATES_VERSION,
            "introductions": _INTRODUCTIONS,
            "transitions": _TRANSITIONS,
            "groupTitles": _GROUP_TITLES,
            "fieldLabels": _FIELD_LABELS,
            "values": _VALUES,
            "lines": _LINES,
            "aggregates": _AGGREGATES,
            "notices": _NOTICES,
            "inventoryLines": _INVENTORY_LINES,
            "inventoryMedia": _INVENTORY_MEDIA,
            "inventoryStatus": _INVENTORY_STATUS,
            "inventoryTitle": _INVENTORY_TITLE,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


TEMPLATES_SHA256 = _compute_sha256()


def fill(template: str, **values: object) -> str:
    """Fill a server-owned template; values are never interpreted as templates."""

    return template.format(**{key: str(value) for key, value in values.items()})


__all__ = [
    "AGGREGATES",
    "FIELD_LABELS",
    "GROUP_TITLES",
    "INTRODUCTIONS",
    "INVENTORY_LINES",
    "INVENTORY_MEDIA",
    "INVENTORY_STATUS",
    "INVENTORY_TITLE",
    "LINES",
    "NOTICES",
    "TEMPLATES_SHA256",
    "TEMPLATES_VERSION",
    "TRANSITIONS",
    "VALUES",
    "fill",
]
