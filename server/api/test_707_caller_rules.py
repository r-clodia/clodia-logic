"""Agent-server follow-ups of #705 (clodia-platform#707).

#705 put a signed caller in front of every route. A signed caller is not a
decision: an agent's own token is one too. These five routes decided as if
being signed were enough, or did not decide at all:

1. `POST /clodia/jobs` created a job for any signed caller, an agent included,
   skipping `jobs.propose` and the owner's approval.
2. `POST /clodia/jobs/propose` took `requested_by` from the body.
3. `GET /clodia/datastores/rag/{collection}/documents` read any collection with
   the agent-server's service identity after a `rag.list` decision that an
   agent's token could pass.
4. `announce/internal` and `runtime-facts/internal` let everyone through when
   no orchestrator secret was configured.
5. `GET /api/agents` returned every full record, emails included, to any
   signed caller.

Each test below goes through the real application (`create_app`, global
guard included), with the CA stood in by a token table.
"""
from __future__ import annotations

import os
from unittest import mock
from unittest.mock import patch

from ..agents.models import AgentSpec
from . import agent_registry as AR, caller_auth, datastores, gateway_pdp, rag_store
from .test_705_every_route_has_a_caller import CLEARANCE, TOKENS, _AppCase

#: The #705 table plus a token an AGENT carries on a person's behalf.
TOKENS_707 = {
    **TOKENS,
    "tok-clodia-for-davide": {"agent": "clodia", "on_behalf": True,
                              "principal": "davide", "human_role": "admin"},
}


def _verify(token: str) -> dict:
    claims = TOKENS_707.get(token)
    if claims is None:
        raise PermissionError("firma token non valida")
    return dict(claims)


class _Case707(_AppCase):
    def setUp(self) -> None:
        super().setUp()
        from ..colony import pki
        p = patch.object(pki, "verify_session_token", side_effect=_verify)
        p.start()
        self.addCleanup(p.stop)


# ── 1. POST /clodia/jobs: persons only ───────────────────────────────────────
class JobCreateTests(_Case707):
    def setUp(self) -> None:
        super().setUp()
        from ..scheduler import api as japi
        self.created = []

        def _create(**kw):
            self.created.append(kw)
            return {"id": 9, "enabled": False, **kw}
        for p in (patch.object(japi.db, "create_job", side_effect=_create),
                  patch.object(japi, "_require_valid_agent", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def _post(self, headers):
        return self.client.post("/clodia/jobs", headers=headers, json={
            "name": "daily", "cron_expr": "0 9 * * *", "prompt": "do it",
            "agent": "clodia", "enabled": False})

    def test_an_agent_token_cannot_create_a_job(self) -> None:
        for tok in ("tok-clodia", "tok-ophelia"):
            r = self._post(self.as_(tok))
            self.assertEqual(r.status_code, 403, r.text)
            self.assertIn("jobs.propose", r.json()["detail"])
        self.assertEqual(self.created, [])

    def test_nor_an_agent_carrying_a_persons_token(self) -> None:
        r = self._post(self.as_("tok-clodia-for-davide"))
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.created, [])

    def test_nor_the_gateway_secret_alone(self) -> None:
        r = self._post(self.as_(gateway=True))
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.created, [])

    def test_a_person_creates_it_and_owns_it(self) -> None:
        for tok, who in (("tok-giovanni", "giovanni"), ("tok-davide", "davide")):
            r = self._post(self.as_(tok))
            self.assertEqual(r.status_code, 201, r.text)
            self.assertEqual(self.created[-1]["owner"], who)


# ── 2. propose: requested_by from the signed caller ──────────────────────────
class ProposeTests(_Case707):
    def setUp(self) -> None:
        super().setUp()
        from ..scheduler import api as japi, proposals
        self.proposed = []

        def _create(**kw):
            self.proposed.append(kw)
            return {"id": 1, **kw}
        for p in (patch.object(proposals, "create", side_effect=_create),
                  patch.object(japi.db, "get_job_by_name", return_value=None),
                  patch.object(japi, "_require_valid_agent", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def _post(self, headers, **body):
        return self.client.post("/clodia/jobs/propose", headers=headers, json={
            "name": "daily", "cron_expr": "0 9 * * *", "prompt": "do it",
            "agent": "clodia", **body})

    def test_the_gateway_fills_requested_by_and_is_believed(self) -> None:
        r = self._post(self.as_(gateway=True), requested_by="messaggero")
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.proposed[-1]["requested_by"], "messaggero")

    def test_a_signed_caller_is_its_own_requested_by(self) -> None:
        r = self._post(self.as_("tok-ophelia"))
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.proposed[-1]["requested_by"], "ophelia")
        r = self._post(self.as_("tok-ophelia"), requested_by="ophelia")
        self.assertEqual(r.status_code, 201, r.text)

    def test_a_body_naming_someone_else_is_refused(self) -> None:
        r = self._post(self.as_("tok-ophelia"), requested_by="davide")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.proposed, [])


# ── 3. RAG documents: a person, with the right checks ────────────────────────
class RagDocumentsTests(_Case707):
    TIERS = {"eu-normativa": "SEAL-1", "riservata": "SEAL-3", "topic-SEAL-1-ops": "SEAL-1"}

    def setUp(self) -> None:
        super().setUp()
        self.read = []
        self.asked = []

        def _docs(collection):
            self.read.append(collection)
            return [{"name": "GDPR"}]

        def _authz(request, tool):
            self.asked.append(tool)
            return "x"
        for p in (patch.object(rag_store, "list_documents", side_effect=_docs),
                  patch.object(rag_store, "collection_tier",
                               side_effect=lambda c: self.TIERS.get(c)),
                  patch.object(gateway_pdp, "require_authz", side_effect=_authz),
                  patch.object(gateway_pdp, "human_clearance",
                               side_effect=lambda n: CLEARANCE.get(n))):
            p.start()
            self.addCleanup(p.stop)

    def _get(self, collection, headers):
        return self.client.get(f"/clodia/datastores/rag/{collection}/documents",
                               headers=headers)

    def test_an_agent_token_is_refused_before_any_read(self) -> None:
        for headers in (self.as_("tok-clodia"), self.as_("tok-clodia-for-davide"),
                        self.as_(gateway=True)):
            r = self._get("eu-normativa", headers)
            self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.read, [])
        self.assertEqual(self.asked, [])

    def test_a_person_within_clearance_reads_it(self) -> None:
        r = self._get("eu-normativa", self.as_("tok-giovanni"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["documents"], [{"name": "GDPR"}])
        self.assertEqual(self.asked, ["rag.list"])

    def test_above_the_persons_clearance_is_refused(self) -> None:
        r = self._get("riservata", self.as_("tok-giovanni"))          # SEAL-1 < SEAL-3
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.read, [])
        r = self._get("riservata", self.as_("tok-davide"))            # SEAL-3
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_topic_index_is_for_an_admin(self) -> None:
        r = self._get("topic-SEAL-1-ops", self.as_("tok-giovanni"))
        self.assertEqual(r.status_code, 403, r.text)
        r = self._get("topic-SEAL-1-ops", self.as_("tok-davide"))
        self.assertEqual(r.status_code, 200, r.text)

    def test_an_unknown_collection_is_404_and_an_unknowable_tier_503(self) -> None:
        r = self._get("non-esiste", self.as_("tok-davide"))
        self.assertEqual(r.status_code, 404, r.text)
        with patch.object(rag_store, "collection_tier",
                          side_effect=rag_store.RagStoreError("giù")):
            r = self._get("eu-normativa", self.as_("tok-davide"))
        self.assertEqual(r.status_code, 503, r.text)
        self.assertEqual(self.read, [])

    def test_the_pdp_still_decides(self) -> None:
        with patch.object(gateway_pdp, "require_authz",
                          side_effect=datastores.HTTPException(403, "no")):
            r = self._get("eu-normativa", self.as_("tok-giovanni"))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.read, [])


class CollectionTierTests(_Case707):
    def _resp(self, status, body=None):
        return mock.Mock(status_code=status, json=lambda: body or {})

    def test_it_reads_the_tier_and_fails_strictly(self) -> None:
        with patch.object(rag_store, "_headers", return_value={}):
            with patch.object(rag_store.requests, "get", return_value=self._resp(
                    200, {"collections": [{"collection": "a", "tier": "SEAL-2"},
                                          {"collection": "b"}]})):
                self.assertEqual(rag_store.collection_tier("a"), "SEAL-2")
                self.assertEqual(rag_store.collection_tier("b"), "SEAL-0")
                self.assertIsNone(rag_store.collection_tier("c"))
            with patch.object(rag_store.requests, "get", return_value=self._resp(502)):
                with self.assertRaises(rag_store.RagStoreError):
                    rag_store.collection_tier("a")


# ── 4. internal routes fail closed without a secret ──────────────────────────
class InternalRoutesFailClosedTests(_Case707):
    def _without_secret(self):
        env = {k: v for k, v in os.environ.items() if k != "CLODIA_ORCHESTRATOR_SECRET"}
        return mock.patch.dict(os.environ, env, clear=True)

    def test_an_agent_token_cannot_announce_or_read_runtime_facts(self) -> None:
        from . import channels as ch
        with self._without_secret(), \
                patch.object(ch.topics_client, "async_open_topic",
                             return_value={"meta": {"tier": "SEAL-1"}}), \
                patch.object(ch.bus, "publish") as publish:
            for headers in (self.as_("tok-clodia"),
                            {**self.as_("tok-clodia"), "X-Orchestrator-Secret": ""},
                            {**self.as_("tok-clodia"), "X-Orchestrator-Secret": "x"}):
                r = self.client.post("/clodia/channels/SEAL-1/ops/announce/internal",
                                     headers=headers,
                                     json={"id": "m1", "author": "davide", "text": "hi"})
                self.assertEqual(r.status_code, 403, r.text)
                r = self.client.post("/clodia/channels/runtime-facts/internal",
                                     headers=headers,
                                     json={"tier": "SEAL-1", "name": "ops",
                                           "agent": "clodia"})
                self.assertEqual(r.status_code, 403, r.text)
            publish.assert_not_called()

    def test_with_a_secret_only_the_gateway_passes(self) -> None:
        from . import channels as ch
        with patch.object(ch.topics_client, "async_open_topic",
                          return_value={"meta": {"tier": "SEAL-1"}}), \
                patch.object(ch, "publish_channel_message", return_value=True):
            r = self.client.post("/clodia/channels/SEAL-1/ops/announce/internal",
                                 headers=self.as_("tok-clodia"),
                                 json={"id": "m2", "author": "davide", "text": "hi"})
            self.assertEqual(r.status_code, 403, r.text)
            r = self.client.post("/clodia/channels/SEAL-1/ops/announce/internal",
                                 headers=self.as_(gateway=True),
                                 json={"id": "m3", "author": "davide", "text": "hi"})
            self.assertEqual(r.status_code, 200, r.text)


# ── 5. GET /api/agents: filtered by who reads ────────────────────────────────
def _spec(name: str, **kw) -> AgentSpec:
    return AgentSpec.model_validate({"name": name, "description": "d",
                                     "display_name": name.capitalize(),
                                     "system_prompt": "SYSTEM PROMPT TEXT", **kw})


SPECS = [
    _spec("davide", type="human", role="superadmin", clearance="SEAL-3",
          email="davide@example.org"),
    _spec("giovanni", type="human", role="member", clearance="SEAL-1",
          email="giovanni@example.org"),
    _spec("clodia", model="claude-sonnet-4-5", email="clodia-bot@example.org",
          multi_spawn=True, max_spawns=3),
]
EMAILS = ("davide@example.org", "giovanni@example.org", "clodia-bot@example.org")


class AgentsListTests(_Case707):
    def setUp(self) -> None:
        super().setUp()
        for p in (patch.object(AR.registry, "list", return_value=list(SPECS)),
                  patch.object(AR.registry, "errors", return_value={"broken": "/clodia/agents/broken"}),
                  patch.object(AR.registry, "warnings", return_value={}),
                  patch.object(AR, "_connected_safe", return_value=set()),
                  patch.object(AR, "_provider_fields", return_value={"provider": "p1"}),
                  patch.object(AR, "_identity_info", return_value=None),
                  patch.object(AR.pause_mod, "list_paused", return_value=[])):
            p.start()
            self.addCleanup(p.stop)

    def _get(self, headers) -> dict:
        r = self.client.get("/api/agents", headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_the_gateway_and_an_admin_read_full_records(self) -> None:
        for headers in (self.as_(gateway=True), self.as_("tok-davide")):
            d = self._get(headers)
            for e in EMAILS:
                self.assertIn(e, str(d))
            self.assertIn("errors", d)
            self.assertIn("SYSTEM PROMPT TEXT", str(d))

    def test_a_person_reads_what_the_ui_renders_without_other_emails(self) -> None:
        d = self._get(self.as_("tok-giovanni"))
        by = {a["name"]: a for a in d["agents"]}
        self.assertEqual(set(by), {"davide", "giovanni", "clodia"})
        self.assertNotIn("davide@example.org", str(d))
        self.assertNotIn("clodia-bot@example.org", str(d))
        self.assertNotIn("SYSTEM PROMPT TEXT", str(d))
        self.assertNotIn("errors", d)
        self.assertNotIn("base_dir", d)
        # one's own address stays
        self.assertEqual(by["giovanni"]["contact_channels"], {"email": "giovanni@example.org"})
        # what the WebUI reads from this list (agents table, job form, topic pages,
        # new-seed ancestors)
        for k in ("display_name", "type", "multi_spawn", "max_spawns", "provider",
                  "avatar_color", "role", "clearance"):
            self.assertIn(k, by["clodia"] if k not in ("role", "clearance") else by["davide"])
        for a in by.values():
            extra = set(a) - AR.PERSON_FIELDS
            if a["name"] != "giovanni":
                self.assertEqual(extra, set(), a["name"])

    def test_an_agent_token_reads_a_minimal_public_view(self) -> None:
        for tok in ("tok-clodia", "tok-clodia-for-davide"):
            d = self._get(self.as_(tok))
            self.assertEqual(set(d), {"agents"})
            for a in d["agents"]:
                self.assertLessEqual(set(a), AR.AGENT_FIELDS)
            for e in EMAILS:
                self.assertNotIn(e, str(d))
            self.assertEqual({a["name"] for a in d["agents"]},
                             {"davide", "giovanni", "clodia"})


class SignedPersonTests(_Case707):
    def test_only_a_persons_own_session_is_a_person(self) -> None:
        def req(tok):
            return mock.Mock(headers={"authorization": f"Bearer {tok}"},
                             state=mock.Mock(spec=[]))
        self.assertEqual(caller_auth.signed_person(req("tok-giovanni")), "giovanni")
        self.assertIsNone(caller_auth.signed_person(req("tok-clodia")))
        self.assertIsNone(caller_auth.signed_person(req("tok-clodia-for-davide")))
        self.assertIsNone(caller_auth.signed_person(req("forged")))

