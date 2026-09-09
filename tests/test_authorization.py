from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from fixture_loader import load_authorization_store
from legaldesk.authorization import (
    AuthorizationDenied,
    InMemoryAuthorizationStore,
    VerifiedIdentity,
    build_request_context,
)


class AuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = load_authorization_store()

    def test_authorized_user_gets_server_derived_scope(self) -> None:
        context = build_request_context(
            VerifiedIdentity("idp|alice-fictional"),
            "mat_sundial",
            self.store,
            correlation_id="8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279",
        )
        self.assertEqual(context.user_id, "usr_alice")
        self.assertEqual(context.tenant_id, "tnt_aurora")
        self.assertEqual(context.matter_id, "mat_sundial")

    def test_user_a_cannot_access_user_b_matter(self) -> None:
        with self.assertRaisesRegex(AuthorizationDenied, "access denied"):
            build_request_context(
                VerifiedIdentity("idp|alice-fictional"),
                "mat_glacier",
                self.store,
            )

    def test_unknown_identity_and_unknown_matter_have_same_denial(self) -> None:
        attempts = (
            (VerifiedIdentity("idp|unknown"), "mat_sundial"),
            (VerifiedIdentity("idp|alice-fictional"), "mat_unknown"),
        )
        for identity, matter_id in attempts:
            with self.subTest(identity=identity.subject, matter_id=matter_id):
                with self.assertRaisesRegex(AuthorizationDenied, "access denied"):
                    build_request_context(identity, matter_id, self.store)

    def test_archived_matter_is_denied(self) -> None:
        with self.assertRaises(AuthorizationDenied):
            build_request_context(
                VerifiedIdentity("idp|alice-fictional"),
                "mat_archived",
                self.store,
            )

    def test_membership_must_agree_on_user_and_matter(self) -> None:
        matter = self.store.matters_by_id["mat_sundial"]
        inconsistent = replace(matter, tenant_id="tnt_borealis")
        store = InMemoryAuthorizationStore(
            users_by_subject=self.store.users_by_subject,
            matters_by_id={**self.store.matters_by_id, matter.matter_id: inconsistent},
        )
        with self.assertRaises(AuthorizationDenied):
            build_request_context(
                VerifiedIdentity("idp|alice-fictional"),
                "mat_sundial",
                store,
            )

    def test_correlation_id_must_be_uuid(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a UUID"):
            build_request_context(
                VerifiedIdentity("idp|alice-fictional"),
                "mat_sundial",
                self.store,
                correlation_id="browser-controlled-free-text",
            )


if __name__ == "__main__":
    unittest.main()
