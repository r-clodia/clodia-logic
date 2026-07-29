"""Test selezione risponditore del canale (rango + tag + clearance)."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ..agents.models import AgentSpec
from ..core.models import MessageRequest
from . import channels


def _a(name, type="normal", clearance="P0", created_at=None, role=None) -> AgentSpec:
    return AgentSpec.model_validate({
        "name": name, "description": "d", "display_name": name, "type": type,
        "clearance": clearance, "created_at": created_at, "role": role,
        **({"model": "m", "system_prompt": "s.md"} if type != "human" else {}),
    })


class ResponderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agents = {
            "clodia": _a("clodia", "super", "P3", "2026-01-01T00:00:00Z"),
            "ophelia": _a("ophelia", "super", "P3", "2026-01-01T00:00:01Z"),
            "worker": _a("worker", "normal", "P1", "2026-02-01T00:00:00Z"),
            "accountant": _a("accountant", "normal", "P1", "2026-02-01T00:00:01Z"),
            "owner": _a("owner", "human", role="superadmin"),
        }
        self._orig = channels.registry.get_by_name
        channels.registry.get_by_name = lambda n: self.agents.get(n)

    def tearDown(self) -> None:
        channels.registry.get_by_name = self._orig

    def test_highest_rank_ai_responds(self) -> None:
        r = channels._pick_responder(["owner", "worker", "clodia"], "P0", None)
        self.assertEqual(r.name, "clodia")  # super > normal; umano non risponde

    def test_seniority_clodia_over_ophelia(self) -> None:
        r = channels._pick_responder(["ophelia", "clodia"], "P0", None)
        self.assertEqual(r.name, "clodia")

    def test_tag_overrides_rank(self) -> None:
        r = channels._pick_responder(["clodia", "worker"], "P0", "worker")
        self.assertEqual(r.name, "worker")

    def test_clearance_excludes_low(self) -> None:
        # canale P2: worker (P1) escluso, clodia (P3) ok
        r = channels._pick_responder(["worker", "clodia"], "P2", None)
        self.assertEqual(r.name, "clodia")
        # canale P2 con solo worker (P1) → nessun risponditore
        self.assertIsNone(channels._pick_responder(["worker"], "P2", None))

    def test_tag_low_clearance_falls_back(self) -> None:
        # worker taggato ma clearance insufficiente (P2) → escluso → fallback clodia
        r = channels._pick_responder(["worker", "clodia"], "P2", "worker")
        self.assertEqual(r.name, "clodia")

    def test_tag_parse(self) -> None:
        self.assertEqual(channels._tagged("ehi @worker puoi farlo?"), "worker")
        self.assertIsNone(channels._tagged("nessun tag qui"))

    def test_decompose_structured_multi_intent_messages(self) -> None:
        self.assertEqual(
            channels._decompose_intents(
                "- Aggiorna il summary del topic\n"
                "- Invia il preventivo al cliente"
            ),
            ["Aggiorna il summary del topic", "Invia il preventivo al cliente"],
        )
        self.assertEqual(
            channels._decompose_intents(
                "Puoi aggiornare il summary? Puoi inviare il preventivo?"
            ),
            ["Puoi aggiornare il summary?", "Puoi inviare il preventivo?"],
        )
        self.assertEqual(
            channels._decompose_intents(
                "Aggiorna il summary del topic e anche prepara il preventivo finale"
            ),
            ["Aggiorna il summary del topic", "prepara il preventivo finale"],
        )
        self.assertEqual(
            channels._decompose_intents("Aggiorna il summary del topic"),
            ["Aggiorna il summary del topic"],
        )

    def test_soft_fallback_returns_multiple_specialists(self) -> None:
        scored = [
            (self.agents["worker"], 0.70),
            (self.agents["accountant"], 0.68),
        ]
        trace = {}
        with (
            patch.object(channels, "_provider_seal_ok", return_value=True),
            patch.object(channels, "_routing_mode", return_value="relevance"),
            patch.object(
                channels.responder_routing, "pick_by_exemplar", return_value=None
            ),
            patch.object(
                channels.responder_routing, "score_specialists",
                return_value=scored,
            ),
            patch.object(channels.responder_routing, "decide", return_value=None),
            patch.object(channels.responder_routing, "THRESHOLD", 0.75),
            patch.object(channels.responder_routing, "FALLBACK_SOFT_RATIO", 0.87),
        ):
            picked = channels._pick_responder(
                ["clodia", "worker", "accountant"],
                "P0",
                None,
                "richiesta ambigua",
                trace=trace,
                multi=True,
            )

        self.assertEqual([spec.name for spec in picked], ["worker", "accountant"])
        self.assertEqual(trace["mode"], "relevance-multi")
        self.assertEqual(trace["chosen"], "worker, accountant")
        self.assertEqual(trace["chosen_agents"], ["worker", "accountant"])

    def test_multi_intent_plan_routes_and_batches_by_agent(self) -> None:
        def score(_specialists, intent):
            if "summary" in intent:
                return [
                    (self.agents["worker"], 0.91),
                    (self.agents["accountant"], 0.30),
                ]
            return [
                (self.agents["accountant"], 0.92),
                (self.agents["worker"], 0.25),
            ]

        trace = {}
        with (
            patch.object(channels, "_provider_seal_ok", return_value=True),
            patch.object(channels, "_routing_mode", return_value="relevance"),
            patch.object(
                channels.responder_routing, "pick_by_exemplar", return_value=None
            ),
            patch.object(
                channels.responder_routing, "score_specialists", side_effect=score
            ),
            patch.object(
                channels.responder_routing, "decide",
                side_effect=lambda scored: scored[0],
            ),
        ):
            plan = channels._routing_plan(
                ["clodia", "worker", "accountant"],
                "P0",
                "- Aggiorna il summary del topic\n"
                "- Invia il preventivo al cliente",
                trace=trace,
            )

        self.assertEqual(
            [(spec.name, prompt) for spec, prompt in plan],
            [
                ("worker", "Aggiorna il summary del topic"),
                ("accountant", "Invia il preventivo al cliente"),
            ],
        )
        self.assertEqual(trace["mode"], "multi-intent")
        self.assertEqual(trace["chosen_agents"], ["worker", "accountant"])
        self.assertEqual(
            [route["chosen"] for route in trace["routes"]],
            ["worker", "accountant"],
        )

    def test_multi_intent_unmatched_tasks_go_to_coordinator(self) -> None:
        def score(_specialists, intent):
            if "summary" in intent:
                return [(self.agents["worker"], 0.91)]
            return [(self.agents["worker"], 0.20)]

        with (
            patch.object(channels, "_provider_seal_ok", return_value=True),
            patch.object(channels, "_routing_mode", return_value="relevance"),
            patch.object(
                channels.responder_routing, "pick_by_exemplar", return_value=None
            ),
            patch.object(
                channels.responder_routing, "score_specialists", side_effect=score
            ),
            patch.object(
                channels.responder_routing, "decide",
                side_effect=lambda scored: (
                    scored[0] if scored and scored[0][1] >= 0.75 else None
                ),
            ),
        ):
            plan = channels._routing_plan(
                ["clodia", "worker", "accountant"],
                "P0",
                "- Aggiorna il summary del topic\n"
                "- Organizza la richiesta non classificata",
            )

        self.assertEqual(
            [(spec.name, prompt) for spec, prompt in plan],
            [
                ("worker", "Aggiorna il summary del topic"),
                ("clodia", "Organizza la richiesta non classificata"),
            ],
        )

    def test_channel_alias_expansion_is_exact_and_case_insensitive(self) -> None:
        profile = SimpleNamespace(channel_aliases={
            "save": "aggiorniamo summary e tldr",
            "$status": "dammi lo stato",
            None: "non deve essere usato",
            "broken": 42,
        })
        with patch.object(channels._iprofile, "load", return_value=profile):
            self.assertEqual(
                channels._expand_aliases("  $SAVE\n"),
                "aggiorniamo summary e tldr",
            )
            self.assertEqual(channels._expand_aliases("$status"), "dammi lo stato")
            self.assertEqual(channels._expand_aliases("$unknown"), "$unknown")
            self.assertEqual(channels._expand_aliases("$broken"), "$broken")
            self.assertEqual(channels._expand_aliases("$save ora"), "$save ora")
            self.assertEqual(channels._expand_aliases("usa $save"), "usa $save")

    def test_alias_does_not_interfere_with_soft_agent_tags(self) -> None:
        profile = SimpleNamespace(channel_aliases={"save": "salva il topic"})
        with patch.object(channels._iprofile, "load", return_value=profile):
            content = channels._expand_aliases("$clodia controlla")
        self.assertEqual(content, "$clodia controlla")
        self.assertEqual(channels._tags(content), ([], ["clodia"]))

    def test_channel_meta_defaults_to_clodia(self) -> None:
        meta = channels._channel_meta({"title": "Aiuto"}, "owner", "support")
        self.assertEqual(meta["contact_agent"], "clodia")
        self.assertEqual(meta["participants"], ["owner", "clodia"])

    def test_channel_meta_uses_requested_contact_agent(self) -> None:
        meta = channels._channel_meta(
            {"title": "Aiuto", "type": "infra", "contact_agent": "Helpdesk"},
            "owner",
            "support",
        )
        self.assertEqual(meta["contact_agent"], "helpdesk")
        self.assertEqual(meta["participants"], ["owner", "helpdesk"])
        self.assertEqual(meta["type"], "infra")


class ParticipantJoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_agent_is_forced_to_introduce_itself(self) -> None:
        agent = _a("worker", "normal", "P1", "2026-02-01T00:00:00Z")
        original_get = channels.registry.get_by_name
        original_run = channels.run_topic_turn
        original_spawn = channels._spawn_bg
        queued = []
        run = AsyncMock(return_value=("worker", "ciao"))
        try:
            channels.registry.get_by_name = lambda name: agent if name == "worker" else None
            channels.run_topic_turn = run
            channels._spawn_bg = lambda coroutine: queued.append(coroutine)

            result = {"participants": ["owner", "worker"], "added": True}
            did_queue = channels._queue_join_introduction(
                "P1", "ops", {"participants": ["owner"]}, "worker", result
            )

            self.assertTrue(did_queue)
            self.assertEqual(len(queued), 1)
            await queued[0]
            kwargs = run.await_args.kwargs
            self.assertEqual(kwargs["responder_hint"], "worker")
            self.assertIn("presentati in una sola riga", kwargs["directive"])
        finally:
            channels.registry.get_by_name = original_get
            channels.run_topic_turn = original_run
            channels._spawn_bg = original_spawn

    async def test_idempotent_or_human_add_does_not_trigger(self) -> None:
        human = _a("owner", "human", role="superadmin")
        original_get = channels.registry.get_by_name
        original_spawn = channels._spawn_bg
        try:
            channels.registry.get_by_name = lambda _name: human
            channels._spawn_bg = lambda _coroutine: self.fail("non deve accodare")
            self.assertFalse(channels._queue_join_introduction(
                "P1", "ops", {}, "owner",
                {"participants": ["owner"], "added": True},
            ))
            self.assertFalse(channels._queue_join_introduction(
                "P1", "ops", {}, "worker",
                {"participants": ["owner", "worker"], "added": False},
            ))
        finally:
            channels.registry.get_by_name = original_get
            channels._spawn_bg = original_spawn


class ChannelQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.agent = _a("clodia", "super", "P3", "2026-01-01T00:00:00Z")
        self.posts: list[tuple[str, str, str]] = []
        self.sent = asyncio.Event()
        self.release = asyncio.Event()
        self._orig_principal = channels._principal_from_request
        self._orig_open_topic = channels.topics_client.open_topic
        self._orig_list_messages = channels.topics_client.list_messages
        self._orig_post_message = channels.topics_client.post_message
        self._orig_touch = channels.access_log.touch
        self._orig_activity = channels.activity_log.append
        self._orig_pick = channels._pick_responder
        self._orig_manager_get = channels.manager.get
        self._orig_manager_create = channels.manager.create
        self._orig_channel_message = channels._channel_message
        self._orig_typing = channels._typing

        class FakeChat:
            principal = ""

            async def send_user_message(chat_self, _prompt: str) -> str:
                self.sent.set()
                await self.release.wait()
                return "risposta"

        async def create(**_kwargs):
            return FakeChat()

        async def noop_async(*_args, **_kwargs):
            return None

        channels._principal_from_request = lambda _request: "owner"
        channels.topics_client.open_topic = lambda _tier, _name: {
            "meta": {"tier": "P0", "owner": "owner", "participants": ["owner", "clodia"]}
        }
        channels.topics_client.list_messages = lambda *_args, **_kwargs: []
        channels.topics_client.post_message = (
            lambda _tier, _name, author, text, kind="human", **_kwargs:
                self.posts.append((author, text, kind))
        )
        channels.access_log.touch = lambda *_args, **_kwargs: None
        channels.activity_log.append = lambda *_args, **_kwargs: None
        channels._pick_responder = lambda *_args, **_kwargs: self.agent
        channels.manager.get = lambda _chat_id: (_ for _ in ()).throw(KeyError(_chat_id))
        channels.manager.create = create
        channels._channel_message = noop_async
        channels._typing = noop_async

    async def asyncTearDown(self) -> None:
        channels._principal_from_request = self._orig_principal
        channels.topics_client.open_topic = self._orig_open_topic
        channels.topics_client.list_messages = self._orig_list_messages
        channels.topics_client.post_message = self._orig_post_message
        channels.access_log.touch = self._orig_touch
        channels.activity_log.append = self._orig_activity
        channels._pick_responder = self._orig_pick
        channels.manager.get = self._orig_manager_get
        channels.manager.create = self._orig_manager_create
        channels._channel_message = self._orig_channel_message
        channels._typing = self._orig_typing

    async def test_channel_post_queues_responder_without_waiting_for_reply(self) -> None:
        res = await channels.channel_post("P0", "ops", MessageRequest(content="@clodia vai"), object())

        self.assertTrue(res["posted"])
        self.assertTrue(res["queued"])
        self.assertEqual(res["responder"], "clodia")
        self.assertEqual(self.posts, [("owner", "@clodia vai", "human")])

        await self.sent.wait()
        self.assertEqual(self.posts, [("owner", "@clodia vai", "human")])

        self.release.set()
        for _ in range(10):
            if len(self.posts) > 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.posts[-1], ("clodia", "risposta", "ai"))

    async def test_channel_post_persists_expanded_alias(self) -> None:
        profile = SimpleNamespace(channel_aliases={
            "save": "aggiorniamo summary e tldr del topic",
        })
        with patch.object(channels._iprofile, "load", return_value=profile):
            result = await channels.post_channel_message(
                "P0", "ops", "$save", "owner", respond=False,
            )

        self.assertEqual(result, {"posted": True, "responder": None})
        self.assertEqual(
            self.posts,
            [("owner", "aggiorniamo summary e tldr del topic", "human")],
        )

    async def test_channel_post_queues_routing_plan_per_agent(self) -> None:
        worker = _a("worker", "normal", "P1")
        accountant = _a("accountant", "normal", "P1")
        start = AsyncMock(return_value=True)

        def routing_plan(_participants, _tier, _message, trace=None):
            if trace is not None:
                trace.update({
                    "mode": "multi-intent",
                    "chosen": "worker, accountant",
                })
            return [
                (worker, "Aggiorna il summary"),
                (accountant, "Invia il preventivo"),
            ]

        with (
            patch.object(
                channels,
                "_routing_plan",
                side_effect=routing_plan,
            ),
            patch.object(channels, "_start_turn", start),
            patch.object(channels, "_provider_seal_ok", return_value=True),
        ):
            result = await channels.post_channel_message(
                "P0",
                "ops",
                "Aggiorna il summary e anche invia il preventivo",
                "owner",
            )

        self.assertEqual(result["responders"], ["worker", "accountant"])
        self.assertEqual(start.await_count, 2)
        self.assertEqual(
            [call.args[5] for call in start.await_args_list],
            ["Aggiorna il summary", "Invia il preventivo"],
        )
        self.assertEqual(
            [call.args[6] for call in start.await_args_list],
            ["routed", "routed"],
        )


class MessageFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.created: list[dict] = []
        self._originals = {
            "principal": channels._principal_from_request,
            "member": channels._require_member,
            "open": channels.topics_client.open_topic,
            "messages": channels.topics_client.list_messages,
            "registry": channels.registry.get_by_name,
            "create": channels.agent_feedback.create,
            "publish": channels.bus.publish,
            "spawn": channels._spawn_bg,
            "generate": channels._generate_feedback_lesson,
        }
        # Lesson generata dal passaggio sincrono (issue #39); None = vet rifiuta.
        self.generated: str | None = "In situazioni analoghe, continua a: usare esempi."

        async def generate(*_args, **_kwargs):
            return self.generated

        channels._generate_feedback_lesson = generate
        channels._principal_from_request = lambda _request: "owner"
        channels._require_member = lambda *_args, **_kwargs: None
        channels.topics_client.open_topic = lambda *_args: {
            "meta": {"owner": "owner", "participants": ["owner", "clodia"]}
        }
        channels.topics_client.list_messages = lambda *_args, **_kwargs: [
            {"id": "m1", "kind": "ai", "author": "clodia", "text": "Risposta"}
        ]
        channels.registry.get_by_name = lambda _name: object()

        def create(**kwargs):
            self.created.append(kwargs)
            lesson = kwargs.get("lesson")
            final = lesson if lesson is not None else kwargs["comment"]
            return {**kwargs, "id": "f1",
                    "status": "learned" if final else "recorded", "lesson": final}

        async def publish(_event):
            return None

        channels.agent_feedback.create = create
        channels.bus.publish = publish
        channels._spawn_bg = lambda _coro: self.fail("non deve creare task in background")

    async def asyncTearDown(self) -> None:
        channels._principal_from_request = self._originals["principal"]
        channels._require_member = self._originals["member"]
        channels.topics_client.open_topic = self._originals["open"]
        channels.topics_client.list_messages = self._originals["messages"]
        channels.registry.get_by_name = self._originals["registry"]
        channels.agent_feedback.create = self._originals["create"]
        channels.bus.publish = self._originals["publish"]
        channels._spawn_bg = self._originals["spawn"]
        channels._generate_feedback_lesson = self._originals["generate"]

    @staticmethod
    def request(body: dict):
        class FakeRequest:
            async def json(self):
                return body
        return FakeRequest()

    async def test_comment_is_required_for_both_ratings(self) -> None:
        for rating in ("thumbs_up", "thumbs_down"):
            with self.assertRaisesRegex(Exception, "comment obbligatorio"):
                await channels.channel_message_feedback(
                    "P0", "ops", "m1", self.request({"rating": rating, "comment": "  "})
                )
        self.assertEqual(self.created, [])

    async def test_raw_comment_and_generated_lesson_coexist(self) -> None:
        # #39: commento grezzo conservato per audit, lesson GENERATA (≠ commento)
        # iniettata; generazione sincrona, nessun task in background.
        result = await channels.channel_message_feedback(
            "P0", "ops", "m1",
            self.request({"rating": "thumbs_up", "comment": "  Usa più esempi.  "}),
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(self.created[0]["comment"], "Usa più esempi.")  # audit grezzo
        self.assertEqual(self.created[0]["lesson"], self.generated)      # generata
        self.assertNotEqual(self.created[0]["lesson"], self.created[0]["comment"])
        self.assertEqual(result["feedback"]["status"], "learned")

    async def test_rejected_lesson_is_audit_only(self) -> None:
        # Vet rifiuta (None) → riga solo-audit: commento conservato, niente
        # iniezione (lesson vuota, status recorded).
        self.generated = None
        result = await channels.channel_message_feedback(
            "P0", "ops", "m1",
            self.request({"rating": "thumbs_down", "comment": "Troppo vago."}),
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(self.created[0]["comment"], "Troppo vago.")
        self.assertEqual(self.created[0]["lesson"], "")
        self.assertEqual(result["feedback"]["status"], "recorded")


class GenerateFeedbackLessonTests(unittest.IsolatedAsyncioTestCase):
    """Il generatore sincrono: rating-aware + vet prima di persistere."""

    async def asyncSetUp(self) -> None:
        self._orig_get = channels.manager.get
        self._orig_create = channels.manager.create
        self.prompts: list[str] = []
        self.replies: list[str] = []
        test = self

        class FakeChat:
            principal = ""

            async def send_user_message(self, prompt: str) -> str:
                test.prompts.append(prompt)
                return test.replies.pop(0) if test.replies else ""

        async def create(**_kwargs):
            return FakeChat()

        channels.manager.get = lambda _cid: (_ for _ in ()).throw(KeyError(_cid))
        channels.manager.create = create

    async def asyncTearDown(self) -> None:
        channels.manager.get = self._orig_get
        channels.manager.create = self._orig_create

    async def test_thumbs_up_generates_reinforcement_then_vets(self) -> None:
        self.replies = [
            "In situazioni analoghe, continua a: fornire esempi concreti.",
            '{"ok": true, "lesson": "In situazioni analoghe, continua a: dare esempi."}',
        ]
        out = await channels._generate_feedback_lesson(
            "clodia", "thumbs_up", "buoni esempi", "output valutato")
        self.assertEqual(out, "In situazioni analoghe, continua a: dare esempi.")
        self.assertIn("continua a", self.prompts[0])  # rating-aware (👍)

    async def test_thumbs_down_generates_correction(self) -> None:
        self.replies = [
            "In situazioni analoghe, evita di: rispondere senza citare la fonte.",
            '{"ok": true, "lesson": "In situazioni analoghe, evita di: omettere le fonti."}',
        ]
        out = await channels._generate_feedback_lesson(
            "clodia", "thumbs_down", "manca la fonte", "output")
        self.assertIn("evita di", out)
        self.assertIn("evita di", self.prompts[0])  # rating-aware (👎)

    async def test_no_lesson_returns_none_without_vet(self) -> None:
        self.replies = ["NO_LESSON"]  # nessun vet: se venisse chiamato, pop→""
        out = await channels._generate_feedback_lesson(
            "clodia", "thumbs_up", "ok", "output")
        self.assertIsNone(out)
        self.assertEqual(len(self.prompts), 1)  # solo generazione, niente vet

    async def test_vet_rejection_returns_none(self) -> None:
        self.replies = [
            "Per Acme Srl usa il fatturato 3,2M.",  # candidata sporca
            '{"ok": false, "lesson": ""}',
        ]
        out = await channels._generate_feedback_lesson(
            "clodia", "thumbs_up", "commento", "output")
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
