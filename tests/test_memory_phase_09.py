from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))

from fixture_loader import load_authorization_store  # noqa: E402
from legaldesk.authorization import (  # noqa: E402
    AuthorizationDenied,
    RequestContext,
    VerifiedIdentity,
)
from legaldesk.memory import (  # noqa: E402
    AgentCoreMemoryClient,
    InMemoryConversationBindingStore,
    InMemoryShortTermMemory,
    LongTermMemoryDisabled,
    MemoryPolicy,
    MemoryScope,
    derive_memory_scope_for_identity,
)
from legaldesk_agent import HarnessMemoryScope, HarnessInvoker  # noqa: E402


def context(*, user: str = "usr_alice", matter: str = "mat_sundial") -> RequestContext:
    tenant = "tnt_borealis" if user == "usr_bob" or matter == "mat_glacier" else "tnt_aurora"
    return RequestContext("00000000-0000-4000-8000-000000000001", user, tenant, matter, frozenset())


class FakeMemoryClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create_event(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"event": {"eventId": "evt-1"}}

    def list_events(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"events": []}

    def get_event(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"event": {"eventId": kwargs["eventId"]}}

    def delete_event(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"eventId": kwargs["eventId"]}


class FakeHarnessClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def invoke_harness(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"stream": []}


class Phase09MemoryTests(unittest.TestCase):
    def scope(self, *, session: str = "session-a", conversation: str = "conversation-a") -> MemoryScope:
        auth = load_authorization_store()
        bindings = InMemoryConversationBindingStore()
        authorized_context = context()
        bindings.bind(
            authorized_context,
            conversation_id=conversation,
            session_selector=session,
        )
        return derive_memory_scope_for_identity(
            VerifiedIdentity("idp|alice-fictional"),
            authorized_context.matter_id,
            conversation,
            session,
            auth,
            bindings,
        )

    def test_same_session_continuity_and_determinism(self) -> None:
        first = self.scope()
        same = self.scope()
        store = InMemoryShortTermMemory()
        store.append_event(first, role="USER", text="Which deadline is mentioned?")
        self.assertEqual(first, same)
        self.assertEqual(len(store.list_events(same)), 1)

    def test_other_session_and_actor_matter_are_isolated(self) -> None:
        first = self.scope()
        other_session = self.scope(session="session-b")
        bob_context = context(user="usr_bob", matter="mat_glacier")
        bob_bindings = InMemoryConversationBindingStore()
        bob_bindings.bind(bob_context, conversation_id="conversation-a", session_selector="session-a")
        other_actor_matter = derive_memory_scope_for_identity(
            VerifiedIdentity("idp|bob-fictional"),
            "mat_glacier",
            "conversation-a",
            "session-a",
            load_authorization_store(),
            bob_bindings,
        )
        store = InMemoryShortTermMemory()
        store.append_event(first, role="ASSISTANT", text="Only scoped context.")
        self.assertEqual(store.list_events(other_session), ())
        self.assertEqual(store.list_events(other_actor_matter), ())
        self.assertNotEqual(first.actor_id, other_actor_matter.actor_id)
        self.assertNotIn("usr_alice", first.actor_id)
        self.assertNotIn("tnt_aurora", first.actor_id)
        self.assertNotIn("mat_sundial", first.actor_id)

    def test_scope_rejects_raw_or_unauthorized_inputs(self) -> None:
        with self.assertRaises(TypeError):
            MemoryScope("ldactor-forged", "00000000-0000-4000-8000-000000000000", "forged")
        forged = object.__new__(MemoryScope)
        object.__setattr__(forged, "actor_id", "ldactor-" + "a" * 48)
        object.__setattr__(forged, "session_id", "00000000-0000-4000-8000-000000000000")
        object.__setattr__(forged, "namespace", "forged")
        store = InMemoryShortTermMemory()
        with self.assertRaises(TypeError):
            store.append_event(forged, role="USER", text="must not reach store")  # type: ignore[arg-type]
        fake = FakeMemoryClient()
        with self.assertRaises(TypeError):
            AgentCoreMemoryClient("LegalDeskPhase09-memory-id", client=fake).append_event(
                forged, role="USER", text="must not reach AWS"  # type: ignore[arg-type]
            )
        with self.assertRaises(TypeError):
            HarnessMemoryScope.from_derived(forged)

    def test_identity_wrapper_authorizes_before_deriving_scope(self) -> None:
        store = load_authorization_store()
        bindings = InMemoryConversationBindingStore()
        bindings.bind(context(), conversation_id="conversation-a", session_selector="session-a")
        scope = derive_memory_scope_for_identity(
            VerifiedIdentity("idp|alice-fictional"),
            "mat_sundial",
            "conversation-a",
            "session-a",
            store,
            bindings,
        )
        self.assertTrue(scope.actor_id.startswith("ldactor-"))
        self.assertNotIn("usr_alice", scope.actor_id)
        self.assertNotIn("tnt_aurora", scope.actor_id)
        self.assertNotIn("mat_sundial", scope.actor_id)
        with self.assertRaises(AuthorizationDenied):
            derive_memory_scope_for_identity(
                VerifiedIdentity("idp|alice-fictional"),
                "mat_glacier",
                "conversation-a",
                "session-a",
                store,
                bindings,
            )
        with self.assertRaises(AuthorizationDenied):
            derive_memory_scope_for_identity(
                VerifiedIdentity("idp|unknown-fictional"),
                "mat_sundial",
                "conversation-a",
                "session-a",
                store,
                bindings,
            )
        for conversation, session in (("unknown-conversation", "session-a"), ("conversation-a", "unknown-session")):
            with self.subTest(conversation=conversation, session=session), self.assertRaises(AuthorizationDenied):
                derive_memory_scope_for_identity(
                    VerifiedIdentity("idp|alice-fictional"),
                    "mat_sundial",
                    conversation,
                    session,
                    store,
                    bindings,
                )
        with self.assertRaises(AuthorizationDenied):
            derive_memory_scope_for_identity(
                VerifiedIdentity("idp|bob-fictional"),
                "mat_sundial",
                "conversation-a",
                "session-a",
                store,
                bindings,
            )

    def test_long_term_policy_rejects_prohibited_and_safe_writes(self) -> None:
        self.assertFalse(MemoryPolicy.long_term_enabled)
        for candidate in (
            "raw contract paragraph",
            "legal conclusion: liable",
            "sensitive case fact",
            "secret-token-value",
            "innocuous display preference",
        ):
            with self.subTest(candidate=candidate), self.assertRaises(LongTermMemoryDisabled):
                MemoryPolicy.reject_long_term("write")

    def test_agentcore_client_uses_scoped_ids_and_skips_long_term_extraction(self) -> None:
        fake = FakeMemoryClient()
        client = AgentCoreMemoryClient("LegalDeskPhase09-memory-id", client=fake)
        scope = self.scope()
        client.append_event(scope, role="USER", text="A short-lived question")
        call = fake.calls[0]
        self.assertEqual(call["actorId"], scope.actor_id)
        self.assertEqual(call["sessionId"], scope.session_id)
        self.assertEqual(call["extractionMode"], "SKIP")
        self.assertNotIn("memoryStrategyId", call)
        with self.assertRaises(LongTermMemoryDisabled):
            client.retrieve_memory_records()

    def test_list_events_forwards_supported_pagination_options(self) -> None:
        fake = FakeMemoryClient()
        client = AgentCoreMemoryClient("LegalDeskPhase09-memory-id", client=fake)
        scope = self.scope()
        client.list_events(scope, maxResults=25, nextToken="next-page", includePayloads=False)
        call = fake.calls[0]
        self.assertEqual(call["maxResults"], 25)
        self.assertEqual(call["nextToken"], "next-page")
        self.assertFalse(call["includePayloads"])
        for bad in (
            {"maxItems": 10},
            {"maxResults": 0},
            {"maxResults": 101},
            {"maxResults": True},
            {"nextToken": ""},
            {"includePayloads": "false"},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                client.list_events(scope, **bad)

    def test_real_event_ids_are_forwarded_and_malformed_ids_rejected(self) -> None:
        fake = FakeMemoryClient()
        client = AgentCoreMemoryClient("LegalDeskPhase09-memory-id", client=fake)
        scope = self.scope()
        client.get_event(scope, "123#ABCdef09")
        client.delete_event(scope, "456#aF09")
        self.assertEqual(fake.calls[0]["eventId"], "123#ABCdef09")
        self.assertEqual(fake.calls[1]["eventId"], "456#aF09")
        for malformed in ("event-1", "123#zz", "#abc", "123#", ""):
            with self.subTest(malformed=malformed):
                with self.assertRaises(ValueError):
                    client.get_event(scope, malformed)
                with self.assertRaises(ValueError):
                    client.delete_event(scope, malformed)

    def test_harness_only_forwards_actor_from_typed_memory_scope(self) -> None:
        fake = FakeHarnessClient()
        invoker = HarnessInvoker(fake, "arn:test")
        scope = self.scope()
        harness_scope = HarnessMemoryScope.from_derived(scope)
        invoker.invoke("hello", memory_scope=harness_scope)
        self.assertEqual(fake.calls[0]["actorId"], scope.actor_id)
        self.assertEqual(fake.calls[0]["runtimeSessionId"], scope.session_id)
        self.assertNotIn("tenant", fake.calls[0])
        with self.assertRaises(TypeError):
            HarnessMemoryScope("usr_alice", scope.session_id)
        with self.assertRaises(TypeError):
            HarnessMemoryScope.from_derived(object())


class Phase09InfrastructureTests(unittest.TestCase):
    def test_memory_template_is_short_term_only_and_explicitly_cleanable(self) -> None:
        template = (ROOT / "infra" / "cloudformation" / "phase-09-memory.yaml").read_text(encoding="utf-8")
        self.assertIn("Type: AWS::BedrockAgentCore::Memory", template)
        self.assertIn("EventExpiryDuration: 7", template)
        self.assertIn("DeletionPolicy: Delete", template)
        self.assertIn("UpdateReplacePolicy: Delete", template)
        self.assertNotIn("MemoryStrategies", template)

    def test_harness_memory_attachment_is_disabled_by_default_and_scoped(self) -> None:
        template = (ROOT / "infra" / "cloudformation" / "phase-01-harness.yaml").read_text(encoding="utf-8")
        self.assertIn('Default: "false"', template)
        self.assertIn("EnablePhase09Memory", template)
        self.assertIn("AttachPhase09Memory", template)
        self.assertIn("Resource: !Ref Phase09MemoryArn", template)
        for action in ("CreateEvent", "DeleteEvent", "GetEvent", "ListEvents", "RetrieveMemoryRecords"):
            self.assertIn(f"bedrock-agentcore:{action}", template)
        self.assertIn("AgentCoreMemoryConfiguration", template)
        self.assertIn("MessagesCount: 10", template)


if __name__ == "__main__":
    unittest.main()
