"""Provider LLM generico via API: un endpoint deciso dall'admin (#265).

    «Implementiamo un provider generico che consenta di inserire host:port, in
    questo modo sarà possibile anche collegare modelli locali da ollama o
    lmstudio.»                                        — Davide, 23 ago 2026

Ciò che questi test tengono fermo sono i tre punti dove la feature si rompe in
silenzio:
  1. la chiave FACOLTATIVA — ollama non ne chiede una, e un provider "collegato"
     senza endpoint manderebbe il turno verso nessun host;
  2. la MUTUA ESCLUSIONE — un `OPENAI_BASE_URL` residuo dell'endpoint generico
     dirotterebbe verso di esso un agent assegnato a openai-api, che risponderebbe
     con un altro modello senza che nessuno lo abbia deciso;
  3. il SEAL DICHIARATO — l'host lo inserisce l'admin e la piattaforma non può
     verificarlo: SEAL-0, quindi mai in un topic con riservatezza.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import providers as P

PID = "generic-openai"


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(P.router)
    return TestClient(app)


class CatalogTests(unittest.TestCase):
    def test_the_generic_provider_is_in_the_catalog_but_opt_in(self) -> None:
        """Nel catalogo per chi lo dichiara, MAI nella selezione automatica: un
        endpoint arbitrario non deve dirottare un agent che non l'ha chiesto."""
        self.assertIn(PID, P._CATALOG)
        self.assertNotIn(PID, P.default_providers_for_sdk("opencode"))
        self.assertEqual(["scaleway"], P.default_providers_for_sdk("opencode"))

    def test_key_is_optional_and_endpoint_is_configurable(self) -> None:
        meta = P._CATALOG[PID]
        self.assertTrue(meta["apikey_optional"])
        self.assertEqual({"base_url": "OPENAI_BASE_URL", "model": "OPENAI_MODEL"},
                         meta["configurable"])
        self.assertEqual("opencode", meta["sdk"])

    def test_an_unverifiable_endpoint_declares_no_confidentiality(self) -> None:
        self.assertEqual("SEAL-0", P.provider_seal(PID))
        self.assertTrue(P.provider_meets_tier(PID, "SEAL-0"))
        self.assertFalse(P.provider_meets_tier(PID, "SEAL-1"))

    def test_it_serves_any_model_name(self) -> None:
        """Il nome del modello lo decide chi serve l'endpoint."""
        self.assertTrue(P.provider_supports_model(PID, "qwen3-coder:30b"))
        self.assertTrue(P.provider_supports_model(PID, "llama3.1"))


class BaseUrlNormalizationTests(unittest.TestCase):
    def test_host_port_becomes_an_openai_compatible_base_url(self) -> None:
        """La forma chiesta dalla issue. Senza schema `urlsplit` leggerebbe
        `localhost` come schema e `11434` come path."""
        self.assertEqual("http://localhost:11434/v1",
                         P.normalize_base_url("localhost:11434"))
        self.assertEqual("http://192.168.1.9:1234/v1",
                         P.normalize_base_url(" 192.168.1.9:1234 "))

    def test_an_explicit_path_is_kept(self) -> None:
        self.assertEqual("https://box.example/api/v1",
                         P.normalize_base_url("https://box.example/api/v1/"))
        self.assertEqual("http://localhost:1234/v1",
                         P.normalize_base_url("http://localhost:1234/v1"))

    def test_what_is_not_an_addressable_http_endpoint_is_refused(self) -> None:
        for raw in ("", "   ", "ftp://box/v1", "file:///etc/passwd"):
            with self.assertRaises(ValueError, msg=raw):
                P.normalize_base_url(raw)

    def test_credentials_in_the_url_are_refused(self) -> None:
        """Finirebbero in chiaro in un campo non segreto (ed esposto dalla lista);
        la chiave ha il suo campo, che sta nel vault."""
        with self.assertRaises(ValueError):
            P.normalize_base_url("http://user:pw@box:1234/v1")


class ConnectedTests(unittest.TestCase):
    def test_endpoint_without_key_counts_as_connected(self) -> None:
        """ollama e LM Studio non chiedono chiave: esigerla qui renderebbe il
        provider inutilizzabile proprio nel caso della issue."""
        self.assertTrue(P._bundle_usable(PID, {"method": "apikey",
                                               "base_url": "http://localhost:11434/v1"}))

    def test_no_endpoint_is_not_connected(self) -> None:
        """Un bundle senza endpoint verrebbe scelto come effettivo e il turno
        partirebbe verso nessun host."""
        self.assertFalse(P._bundle_usable(PID, {"method": "apikey"}))
        self.assertFalse(P._bundle_usable(PID, {"method": "apikey", "api_key": "sk-x"}))

    def test_the_other_providers_still_require_their_key(self) -> None:
        """La chiave facoltativa vale SOLO per chi la dichiara: nessuna regressione."""
        self.assertFalse(P._bundle_usable("openai-api",
                                          {"method": "apikey",
                                           "base_url": "http://localhost:11434/v1"}))
        self.assertTrue(P._bundle_usable("openai-api",
                                        {"method": "apikey", "api_key": "sk-x"}))


class EnvInjectionTests(unittest.TestCase):
    def test_configured_endpoint_and_model_reach_the_subprocess(self) -> None:
        bundle = {"method": "apikey", "base_url": "http://localhost:11434/v1",
                  "model": "qwen3-coder:30b"}
        with patch.object(P, "_read", return_value=bundle):
            env = P.provider_env(PID)
        self.assertEqual("http://localhost:11434/v1", env["OPENAI_BASE_URL"])
        self.assertEqual("qwen3-coder:30b", env["OPENAI_MODEL"])
        self.assertNotIn("OPENAI_API_KEY", env)  # nessuna chiave, nessuna env vuota

    def test_the_optional_key_is_injected_when_present(self) -> None:
        bundle = {"method": "apikey", "base_url": "https://box.example/v1",
                  "api_key": "sk-local"}
        with patch.object(P, "_read", return_value=bundle):
            env = P.provider_env(PID)
        self.assertEqual("sk-local", env["OPENAI_API_KEY"])

    def test_a_bundle_without_endpoint_injects_nothing(self) -> None:
        with patch.object(P, "_read", return_value={"method": "apikey"}):
            self.assertEqual({}, P.provider_env(PID))

    def test_the_configured_env_is_cleared_for_the_other_providers(self) -> None:
        """Il difetto già visto su Bedrock: una env residua del provider NON
        effettivo dirotta il turno. Il runtime azzera solo ciò che è elencato qui."""
        keys = P.all_provider_env_keys()
        self.assertIn("OPENAI_BASE_URL", keys)
        self.assertIn("OPENAI_MODEL", keys)

    def test_base_url_prefers_the_configured_one_over_the_static_one(self) -> None:
        """Punto unico per endpoint statico (scaleway) e configurato (#265)."""
        self.assertEqual("http://localhost:11434/v1",
                         P.provider_base_url(PID, {"base_url": "http://localhost:11434/v1"}))
        self.assertEqual("https://api.scaleway.ai/v1",
                         P.provider_base_url("scaleway", {}))


class ConnectEndpointTests(unittest.TestCase):
    def _post(self, payload: dict, pid: str = PID):
        with patch.object(P.gateway_pdp, "require_authz_async", new=AsyncMock()), \
             patch.object(P, "_write_async", new=AsyncMock()) as w:
            r = _app().post(f"/api/providers/{pid}/key", json=payload)
        return r, w

    def test_connecting_a_local_model_needs_only_host_port(self) -> None:
        r, w = self._post({"base_url": "localhost:11434"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual({"method": "apikey", "base_url": "http://localhost:11434/v1"},
                         w.await_args.args[1])
        self.assertEqual("http://localhost:11434/v1", r.json()["base_url"])

    def test_without_endpoint_nothing_is_deposited(self) -> None:
        r, w = self._post({"api_key": "sk-x"})
        self.assertEqual(400, r.status_code)
        w.assert_not_awaited()

    def test_a_bad_endpoint_is_refused_with_its_reason(self) -> None:
        r, w = self._post({"base_url": "ftp://box:1234"})
        self.assertEqual(400, r.status_code)
        self.assertIn("base_url", r.json()["detail"])
        w.assert_not_awaited()

    def test_an_endpoint_on_a_provider_that_has_none_is_refused(self) -> None:
        """Ignorarlo in silenzio farebbe credere all'admin di aver dirottato
        openai-api su un host che invece non verrà mai contattato."""
        r, w = self._post({"api_key": "sk-x", "base_url": "localhost:11434"},
                          pid="openai-api")
        self.assertEqual(400, r.status_code)
        w.assert_not_awaited()

    def test_the_key_is_still_mandatory_where_it_was(self) -> None:
        r, w = self._post({"api_key": "  "}, pid="openai-api")
        self.assertEqual(400, r.status_code)
        w.assert_not_awaited()

    def test_the_list_publishes_the_endpoint_but_never_the_key(self) -> None:
        bundle = {"method": "apikey", "base_url": "http://localhost:11434/v1",
                  "api_key": "sk-secret", "model": "qwen3-coder:30b"}
        with patch.object(P, "_read_async", new=AsyncMock(return_value=bundle)):
            r = _app().get("/api/providers")
        self.assertEqual(200, r.status_code, r.text)
        row = next(p for p in r.json()["providers"] if p["id"] == PID)
        self.assertEqual("http://localhost:11434/v1", row["base_url"])
        self.assertEqual(["base_url", "model"], row["configurable"])
        self.assertTrue(row["connected"])
        self.assertNotIn("sk-secret", r.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
