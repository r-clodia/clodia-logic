"""Every agent-server route has a signed caller (clodia-platform#705).

Every spawn shares the agent-server container's network. Until #705 about sixty
routes looked at no caller at all, and some carried contents: a topic's summary
at any tier, the jobs with their prompts; `restart-agent` stopped any agent's
sessions. The fix is a dependency of the whole application
(`caller_auth.require_signed_caller`), an explicit list of exceptions each with
its reason, and — for the routes that return contents — a decision on WHO the
caller is, because an agent's own token is a signed caller too.

The first test walks the application's routes: a route that answers a request
without credentials with anything but 401, and is not listed in
`caller_auth.PUBLIC`/`BOOTSTRAP`, fails it. A new route cannot be born open by
omission.
"""
from __future__ import annotations

import os
import re
import unittest
from unittest import mock
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from . import admin, caller_auth
from ..main import create_app

SECRET = caller_auth.GatewayRequestForTests.SECRET_FOR_TESTS

#: token → verified claims, standing in for the CA (`pki.verify_session_token`).
TOKENS = {
    "tok-davide": {"agent": "davide"},                      # person, admin, SEAL-3
    "tok-giovanni": {"agent": "giovanni"},                  # person, not admin
    "tok-clodia": {"agent": "clodia", "clearance": "SEAL-3"},
    "tok-clodia-low": {"agent": "clodia", "clearance": "SEAL-1"},  # spawn on a weak provider
    "tok-ophelia": {"agent": "ophelia", "clearance": "SEAL-3"},    # not in the room
}
HUMANS = {"davide", "giovanni"}
ADMINS = {"davide"}
CLEARANCE = {"davide": "SEAL-3", "giovanni": "SEAL-1", "clodia": "SEAL-3",
             "ophelia": "SEAL-3"}


def _verify(token: str) -> dict:
    claims = TOKENS.get(token)
    if claims is None:
        raise PermissionError("firma token non valida")
    return dict(claims)


def _routes(app):
    """`(methods, path, endpoint)` of every route, nested routers included.

    FastAPI ≥ 0.150 keeps an included router as one lazy entry in `app.routes`;
    `iter_route_contexts` flattens it. Older versions list the routes flat."""
    try:
        from fastapi.routing import iter_route_contexts
        items = list(iter_route_contexts(app.routes))
    except ImportError:  # pragma: no cover — older FastAPI
        items = list(app.routes)
    for rc in items:
        yield (set(getattr(rc, "methods", None) or ()), getattr(rc, "path", None),
               getattr(rc, "endpoint", None))


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


class _AppCase(unittest.TestCase):
    def setUp(self) -> None:
        from ..colony import pki
        for p in (
            patch.object(admin, "is_initialized", return_value=True),
            patch.object(pki, "verify_session_token", side_effect=_verify),
            patch.object(admin, "is_admin", side_effect=lambda n: n in ADMINS),
            patch.object(admin, "is_human", side_effect=lambda n: n in HUMANS),
            patch("server.api.topics._principal_clearance",
                  side_effect=lambda n: CLEARANCE.get(n, "SEAL-0")),
            mock.patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": SECRET}),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.app = create_app()
        self.client = TestClient(self.app)

    def as_(self, token: str | None = None, *, gateway: bool = False) -> dict:
        h = {}
        if token:
            h["Authorization"] = f"Bearer {token}"
        if gateway:
            h["X-Orchestrator-Secret"] = SECRET
        return h


class EveryRouteHasACallerTests(_AppCase):
    def test_no_route_answers_an_anonymous_caller_unless_listed(self) -> None:
        open_routes = []
        checked = 0
        for methods, path, endpoint in _routes(self.app):
            if not path or not methods:
                continue
            for method in sorted(methods - {"HEAD", "OPTIONS"}):
                if (method, path) in caller_auth.PUBLIC or (method, path) in caller_auth.BOOTSTRAP:
                    continue
                r = self.client.request(method, _concrete(path), json={})
                checked += 1
                if r.status_code != 401:
                    open_routes.append(f"{method} {path} → {r.status_code}"
                                       f" ({getattr(endpoint, '__module__', '?')})")
        self.assertGreater(checked, 150, "the walk saw too few routes: the "
                                         "route iteration is broken")
        self.assertEqual(open_routes, [], "routes open to an anonymous caller "
                         "(guard them, or list them in caller_auth.PUBLIC with a reason)")

    def test_a_forged_token_is_no_caller(self) -> None:
        r = self.client.get("/clodia/jobs", headers=self.as_("tok-forged"))
        self.assertEqual(r.status_code, 401)

    def test_a_wrong_gateway_secret_is_no_caller(self) -> None:
        r = self.client.get("/clodia/jobs", headers={"X-Orchestrator-Secret": "nope"})
        self.assertEqual(r.status_code, 401)

    def test_without_a_configured_secret_the_header_proves_nothing(self) -> None:
        with mock.patch.dict(os.environ, {"CLODIA_ORCHESTRATOR_SECRET": ""}):
            r = self.client.get("/clodia/jobs", headers={"X-Orchestrator-Secret": ""})
        self.assertEqual(r.status_code, 401)

    def test_every_exception_names_an_existing_route_and_a_reason(self) -> None:
        present = {(m, p) for ms, p, _e in _routes(self.app) for m in ms}
        for key, reason in caller_auth.PUBLIC.items():
            self.assertIn(key, present, f"stale PUBLIC entry {key}")
            self.assertTrue(reason.strip(), f"PUBLIC entry {key} without a reason")
        for key, (reason, _cond) in caller_auth.BOOTSTRAP.items():
            self.assertIn(key, present, f"stale BOOTSTRAP entry {key}")
            self.assertTrue(reason.strip())

    def test_first_admin_creation_is_open_only_before_the_claim(self) -> None:
        r = self.client.post("/api/agents", json={})
        self.assertEqual(r.status_code, 401)
        with patch.object(admin, "is_initialized", return_value=False):
            self.assertTrue(caller_auth.BOOTSTRAP[("POST", "/api/agents")][1]())

    def test_the_login_bootstrap_stays_reachable(self) -> None:
        r = self.client.post("/clodia/whoami", json={"token": "tok-forged"})
        self.assertEqual(r.status_code, 401)  # whoami's own verdict, not the guard's
        self.assertIn("principal", r.json().get("detail", ""))


ROOM = {"owner": "davide", "participants": ["davide", "clodia"], "tier": "SEAL-3"}


class TopicSummaryTests(_AppCase):
    def _get(self, headers):
        with patch("server.api.topics_client.async_open_topic",
                   AsyncMock(return_value={"summary": "SECRET SUMMARY", "meta": ROOM})):
            return self.client.get("/topics/SEAL-3/segreto/summary", headers=headers)

    def test_a_participant_within_clearance_reads_it(self) -> None:
        r = self._get(self.as_("tok-davide"))
        self.assertEqual((r.status_code, r.text), (200, "SECRET SUMMARY"))
        self.assertEqual(self._get(self.as_("tok-clodia")).status_code, 200)

    def test_an_agent_outside_the_room_does_not(self) -> None:
        r = self._get(self.as_("tok-ophelia"))
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("SECRET", r.text)

    def test_a_participant_below_the_tier_does_not(self) -> None:
        """Same agent, spawned on a provider that lowers its clearance."""
        self.assertEqual(self._get(self.as_("tok-clodia-low")).status_code, 403)

    def test_the_gateway_secret_alone_does_not_read_a_room(self) -> None:
        """The summary is a person's or agent's read: there is no gateway caller,
        and a bare secret names nobody who is in the room."""
        self.assertEqual(self._get(self.as_(gateway=True)).status_code, 401)

    def test_nobody_anonymous(self) -> None:
        self.assertEqual(self._get({}).status_code, 401)


class RestartAgentTests(_AppCase):
    def _post(self, headers):
        with patch("server.api.agents.manager.drop_agent",
                   AsyncMock(return_value=["chan:x"])) as drop:
            r = self.client.post("/clodia/runtime/restart-agent",
                                 json={"agent": "clodia"}, headers=headers)
        return r, drop

    def test_an_agent_token_cannot_restart_anyone(self) -> None:
        r, drop = self._post(self.as_("tok-ophelia"))
        self.assertEqual(r.status_code, 403)
        drop.assert_not_awaited()

    def test_a_person_who_is_not_admin_cannot_either(self) -> None:
        r, drop = self._post(self.as_("tok-giovanni"))
        self.assertEqual(r.status_code, 403)
        drop.assert_not_awaited()

    def test_the_paired_gateway_and_an_admin_can(self) -> None:
        for headers in (self.as_(gateway=True), self.as_("tok-davide")):
            r, drop = self._post(headers)
            self.assertEqual(r.status_code, 200, r.text)
            drop.assert_awaited_once_with("clodia")


JOBS = [
    {"id": 1, "name": "mine", "owner": "giovanni", "agent": "clodia", "mode": "agentic",
     "prompt": "P1"},
    {"id": 2, "name": "ophelia-runs", "owner": "davide", "agent": "ophelia",
     "mode": "agentic", "prompt": "P2"},
    {"id": 3, "name": "secret", "owner": "davide", "agent": "clodia", "mode": "agentic",
     "prompt": "SECRET PROMPT"},
]


class JobsTests(_AppCase):
    def setUp(self) -> None:
        super().setUp()
        from ..scheduler import api as japi
        for p in (patch.object(japi.db, "list_jobs", return_value=[dict(j) for j in JOBS]),
                  patch.object(japi.db, "get_job",
                               side_effect=lambda i: next((dict(j) for j in JOBS
                                                           if j["id"] == i), None)),
                  patch.object(japi.scheduler, "stale_reason", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def _ids(self, headers):
        r = self.client.get("/clodia/jobs", headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        return [j["id"] for j in r.json()]

    def test_the_gateway_and_an_admin_see_every_job(self) -> None:
        self.assertEqual(self._ids(self.as_(gateway=True)), [1, 2, 3])
        self.assertEqual(self._ids(self.as_("tok-davide")), [1, 2, 3])

    def test_anyone_else_sees_what_it_owns_or_runs(self) -> None:
        self.assertEqual(self._ids(self.as_("tok-giovanni")), [1])
        self.assertEqual(self._ids(self.as_("tok-ophelia")), [2])

    def test_a_job_one_may_not_read_is_a_404_not_its_prompt(self) -> None:
        r = self.client.get("/clodia/jobs/3", headers=self.as_("tok-ophelia"))
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("SECRET", r.text)
        r = self.client.get("/clodia/jobs/3", headers=self.as_(gateway=True))
        self.assertEqual(r.json()["prompt"], "SECRET PROMPT")

    def test_the_internal_switch_is_the_gateways_only(self) -> None:
        r = self.client.post("/clodia/jobs/3/set-enabled/internal",
                             json={"enabled": False}, headers=self.as_("tok-ophelia"))
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/clodia/jobs/report-status/internal",
                             json={"chat_id": "c", "status": "success"},
                             headers=self.as_("tok-clodia"))
        self.assertEqual(r.status_code, 403)


class DeclaredByTests(_AppCase):
    def test_inspect_topic_believes_by_only_from_the_gateway_or_the_signer(self) -> None:
        r = self.client.post("/clodia/runtime/inspect-topic",
                             json={"tier": "SEAL-3", "name": "segreto", "by": "sysadmin"},
                             headers=self.as_("tok-ophelia"))
        self.assertEqual(r.status_code, 403)

    def test_participants_internal_believes_by_only_from_the_gateway(self) -> None:
        with patch("server.api.topics_client.async_open_topic",
                   AsyncMock(return_value={"meta": ROOM})), \
             patch("server.api.topics_client.async_set_participant", AsyncMock()) as setp:
            r = self.client.post("/clodia/channels/SEAL-3/segreto/participants/internal",
                                 json={"agent": "ophelia", "by": "davide", "add": True},
                                 headers=self.as_("tok-ophelia"))
        self.assertEqual(r.status_code, 403)
        setp.assert_not_awaited()


class AdminActsTests(_AppCase):
    def test_an_agent_cannot_log_the_platform_out(self) -> None:
        with patch("server.api.auth._invalidate_creds_cache") as inval:
            r = self.client.post("/auth/logout", headers=self.as_("tok-clodia"))
        self.assertEqual(r.status_code, 403)
        inval.assert_not_called()

    def test_an_agent_cannot_import_a_skill_every_agent_reads(self) -> None:
        with patch("server.api.skill_import.import_url") as imp:
            r = self.client.post("/clodia/skills/import-url",
                                 json={"url": "https://example.invalid/x.zip"},
                                 headers=self.as_("tok-clodia"))
        self.assertEqual(r.status_code, 403)
        imp.assert_not_called()

    def test_legacy_topic_files_are_for_an_admin(self) -> None:
        r = self.client.get("/topics/confidential/x/file?path=summary.md",
                            headers=self.as_("tok-giovanni"))
        self.assertEqual(r.status_code, 403)

    def test_the_refresh_trigger_answers_a_stranger_without_contents(self) -> None:
        with patch("server.api.topics._pull_and_reindex",
                   return_value=("ok", {"tldr": "SECRET TLDR", "action_points": []})), \
             patch("server.api.topics.TOPICS_ROOT") as root:
            from pathlib import Path
            import tempfile
            td = tempfile.mkdtemp()
            (Path(td) / "personal" / "t" / ".git").mkdir(parents=True)
            root.__truediv__.side_effect = lambda c: Path(td) / c
            anon = self.client.post("/topics/personal/t/refresh")
            adm = self.client.post("/topics/personal/t/refresh",
                                   headers=self.as_("tok-davide"))
        self.assertEqual(anon.status_code, 200, anon.text)
        self.assertEqual(anon.json(), {"pull": "ok"})
        self.assertEqual(adm.json()["index"]["tldr"], "SECRET TLDR")


if __name__ == "__main__":
    unittest.main()


class ReviewFixesTests(_AppCase):
    """Independent review of #614 (19 Nov 2026): B1, S1, S3, M2."""

    def test_b1_a_signed_download_link_opens_without_a_header(self) -> None:
        from . import download_sign, topics as T
        exp, sig = download_sign.make(T._download_scope("SEAL-2", "t", "files/a.pdf"))
        with patch("server.api.topics_client.get_file", return_value=b"PDF") as gf, \
             patch("server.api.topics_client.open_topic", return_value={"meta": ROOM}):
            ok = self.client.get(f"/topics/SEAL-2/t/download?path=files/a.pdf&exp={exp}&sig={sig}")
            bad = self.client.get(f"/topics/SEAL-2/t/download?path=files/a.pdf&exp={exp}&sig=x{sig}")
            anon = self.client.get("/topics/SEAL-2/t/download?path=files/a.pdf")
        self.assertEqual((ok.status_code, ok.content), (200, b"PDF"))
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(anon.status_code, 401)
        gf.assert_called_once()

    def test_s1_files_refuse_an_agent_token(self) -> None:
        for path in ("/files/download?path=/tmp/x", "/files/check?path=/tmp/x"):
            r = self.client.get(path, headers=self.as_("tok-clodia"))
            self.assertEqual(r.status_code, 403, path)

    def test_s1_files_refuse_spawn_dirs_and_mcp_configs_even_to_a_person(self) -> None:
        import tempfile
        from pathlib import Path
        from . import files as F
        with tempfile.TemporaryDirectory() as td:
            data = Path(td).resolve()
            victim = data / "spawns" / "ophelia-3"
            victim.mkdir(parents=True)
            (victim / ".clodia-mcp.json").write_text("{}")
            (victim / "notes.md").write_text("x")
            ok = data / "dump.md"
            ok.write_text("x")
            tmp_cfg = Path(tempfile.mkdtemp(prefix="clodia-mcp-")).resolve()
            (tmp_cfg / ".clodia-mcp.json").write_text("{}")
            with patch.object(F, "CLODIA_DATA", data), \
                 patch.object(F, "ALLOWED_ROOTS", [data, Path("/tmp").resolve(),
                                                   Path(tempfile.gettempdir()).resolve()]):
                self.assertFalse(F._is_allowed(victim / ".clodia-mcp.json"))
                self.assertFalse(F._is_allowed(victim / "notes.md"))
                self.assertFalse(F._is_allowed(tmp_cfg / ".clodia-mcp.json"))
                self.assertTrue(F._is_allowed(ok))
                r = self.client.get(f"/files/check?path={victim / 'notes.md'}",
                                    headers=self.as_("tok-davide"))
                self.assertEqual(r.json()["allowed"], False)
            import shutil
            shutil.rmtree(tmp_cfg, ignore_errors=True)

    def test_s1_spawn_listing_is_for_persons(self) -> None:
        self.assertEqual(self.client.get("/api/spawns", headers=self.as_("tok-clodia")).status_code, 403)
        self.assertEqual(self.client.get("/api/spawns", headers=self.as_("tok-giovanni")).status_code, 200)

    def test_s3_pause_and_resume_are_the_gateways_or_an_admins(self) -> None:
        import asyncio
        from fastapi import HTTPException
        from . import agent_pause as AP

        class _R:
            def __init__(self, h):
                from types import SimpleNamespace
                self.headers = h
                self.state = SimpleNamespace()
        with patch.object(AP.registry, "get_by_name", return_value=object()), \
             patch.object(AP.pause_mod, "pause", return_value={"ok": True}) as pz:
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(AP.pause_agent("clodia", _R({"authorization": "Bearer tok-ophelia"})))
            self.assertEqual(cm.exception.status_code, 403)
            pz.assert_not_called()
            asyncio.run(AP.pause_agent("clodia", _R({"x-orchestrator-secret": SECRET})))
            pz.assert_called_once()

    def test_m2_a_non_ascii_secret_is_a_401_not_a_500(self) -> None:
        r = self.client.get("/clodia/jobs", headers={"X-Orchestrator-Secret": "sécrèt".encode("utf-8")})
        self.assertEqual(r.status_code, 401)
