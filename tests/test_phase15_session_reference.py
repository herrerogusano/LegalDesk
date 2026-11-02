"""Phase 15: a job never holds the session cookie value, only a derived reference."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fixture_loader import test_identity
from legaldesk.agent_chat import AgentChatRuntime, InputDecision, submit_agent_chat
from legaldesk.agent_jobs import DynamoDBChatJobStore
from legaldesk.quota import InMemoryQuotaLedger
from legaldesk.state import DynamoDBEphemeralStateStore, InMemoryEphemeralStateStore, SessionRecord, session_reference
from test_phase15_agent_jobs import FakeJobTable

COOKIE = "raw-cookie-value-that-must-not-rest-in-the-table"


class MapTable:
    def __init__(self):
        self.items = {}

    def put_item(self, *, Item, **_):
        self.items[(Item["pk"], Item["sk"])] = dict(Item)

    def get_item(self, *, Key, **_):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item else {}


class SessionReferenceTests(unittest.TestCase):
    def record(self):
        return SessionRecord(test_identity("idp|alice-fictional"), "access-token", "csrf", 1e12)

    def test_reference_is_the_digest_the_durable_store_keys_the_session_by(self) -> None:
        table = MapTable()
        store = DynamoDBEphemeralStateStore("metadata", table=table, clock=lambda: 100.0)
        store.put_session(COOKIE, self.record())
        reference = store.session_reference(COOKIE)
        self.assertNotIn(COOKIE, reference)
        self.assertEqual(len(reference), 64)
        self.assertTrue(all(COOKIE not in pk for pk, _ in table.items))  # the table never had the raw value
        self.assertEqual(store.get_session_by_reference(reference).access_token, "access-token")
        self.assertEqual(store.get_session_by_reference(reference).identity.subject, "idp|alice-fictional")

    def test_lookup_by_reference_rejects_anything_that_is_not_a_digest_or_has_no_session(self) -> None:
        for store in (DynamoDBEphemeralStateStore("metadata", table=MapTable(), clock=lambda: 100.0), InMemoryEphemeralStateStore()):
            store.put_session(COOKIE, self.record())
            for bad in ("", COOKIE, "A" * 64, "0" * 63, "0" * 64, None):
                self.assertIsNone(store.get_session_by_reference(bad), repr(bad))  # type: ignore[arg-type]

    def test_the_reference_cannot_be_used_as_a_cookie(self) -> None:
        store = InMemoryEphemeralStateStore()
        store.put_session(COOKIE, self.record())
        self.assertIsNone(store.get_session(store.session_reference(COOKIE)))
        self.assertIsNotNone(store.get_session_by_reference(store.session_reference(COOKIE)))

    def test_expired_sessions_are_not_resolved_by_reference(self) -> None:
        now = [100.0]
        store = InMemoryEphemeralStateStore(clock=lambda: now[0])
        now[0] = 100.0
        store.put_session(COOKIE, self.record())
        now[0] = 2e12
        self.assertIsNone(store.get_session_by_reference(session_reference(COOKIE)))

    def test_a_stored_job_record_never_contains_the_cookie_value(self) -> None:
        table = FakeJobTable()
        runtime = AgentChatRuntime(DynamoDBChatJobStore("metadata", table=table), type("Q", (), {"publish": lambda self, key: None})(),
                                   lambda q, c: InputDecision("allowed", q))
        job = submit_agent_chat(
            runtime, InMemoryEphemeralQuota(), subject="idp|alice-fictional", tenant_id="tnt", matter_id="mat",
            conversation_id="c", session_selector="s", correlation_id="x", question="q",
            session_ref=session_reference(COOKIE),
        )
        dump = json.dumps({str(k): {name: str(value) for name, value in item.items()} for k, item in table.items.items()})
        self.assertNotIn(COOKIE, dump)
        self.assertIn(session_reference(COOKIE), dump)
        self.assertNotIn(COOKIE, repr(job))


def InMemoryEphemeralQuota():
    return InMemoryQuotaLedger()


if __name__ == "__main__":
    unittest.main()
