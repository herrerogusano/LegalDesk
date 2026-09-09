from __future__ import annotations

import json
from pathlib import Path

from legaldesk.authorization import InMemoryAuthorizationStore
from legaldesk.domain.models import Matter, MatterStatus, User


def load_authorization_store() -> InMemoryAuthorizationStore:
    path = Path(__file__).parent / "fixtures" / "multi_tenant.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    users = [
        User(
            user_id=item["userId"],
            verified_subject=item["verifiedSubject"],
            tenant_ids=frozenset(item["tenantIds"]),
            roles=frozenset(item["roles"]),
        )
        for item in payload["users"]
    ]
    matters = [
        Matter(
            matter_id=item["matterId"],
            tenant_id=item["tenantId"],
            name=item["name"],
            authorized_user_ids=frozenset(item["authorizedUserIds"]),
            status=MatterStatus(item["status"]),
        )
        for item in payload["matters"]
    ]
    return InMemoryAuthorizationStore(
        users_by_subject={user.verified_subject: user for user in users},
        matters_by_id={matter.matter_id: matter for matter in matters},
    )
