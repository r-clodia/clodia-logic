"""Self-test del provider_store e del ricablaggio di api/providers (Fase 4).

Esegui con::

    python3 -m server.api.test_provider_store   # dalla root del repo

Simula il gateway in-memory (monkeypatch di `requests` e del mint ckt1): non
tocca PKI reale, né il vault, né la rete.
"""
from __future__ import annotations

import warnings


class _Resp:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._p = payload

    def json(self):
        if self._p is None:
            raise ValueError("no json")
        return self._p


class _FakeGateway:
    """Finto gateway: tiene un dizionario pid→bundle in memoria."""

    def __init__(self):
        import requests
        self.RequestException = requests.RequestException
        self.store: dict[str, dict] = {}
        self.down = False

    def _boom(self):
        raise self.RequestException("gateway giù (simulato)")

    def get(self, url, headers=None, timeout=None):
        if self.down:
            self._boom()
        if url.endswith("/internal/providers"):
            return _Resp(200, {"ids": sorted(self.store)})
        pid = url.rsplit("/", 1)[1]
        if pid in self.store:
            return _Resp(200, self.store[pid])
        return _Resp(404, {"error": "not_found"})

    def put(self, url, headers=None, json=None, timeout=None):
        if self.down:
            self._boom()
        pid = url.rsplit("/", 1)[1]
        self.store[pid] = json
        return _Resp(200, {"ok": True, "id": pid})

    def delete(self, url, headers=None, timeout=None):
        if self.down:
            self._boom()
        pid = url.rsplit("/", 1)[1]
        existed = self.store.pop(pid, None) is not None
        return _Resp(200, {"ok": True, "id": pid, "removed": existed})


def main() -> int:
    warnings.filterwarnings("ignore")
    from . import provider_store
    from . import providers as prov

    fake = _FakeGateway()
    provider_store.requests = fake  # type: ignore[assignment]
    provider_store.pki.mint_session_token = lambda *a, **k: "ckt1.fake"  # type: ignore

    ok = 0
    fail = 0

    def check(name: str, cond: bool) -> None:
        nonlocal ok, fail
        print(f"  {'✓' if cond else '✗'} {name}")
        if cond:
            ok += 1
        else:
            fail += 1

    # catalogo: i provider definiti nel repo devono esserci
    check("catalogo non vuoto", bool(prov._CATALOG))
    check("anthropic nel catalogo", "anthropic" in prov._CATALOG)

    # 1. all'inizio nessuno collegato
    check("connected vuoto", prov.connected_provider_ids() == set())

    # 2. write apikey → _read round-trip
    prov._write("openai", {"method": "apikey", "api_key": "sk-test"})
    check("read apikey", (prov._read("openai") or {}).get("api_key") == "sk-test")

    # 3. connected riflette il vault, intersecato col catalogo
    check("connected = {openai}", prov.connected_provider_ids() == {"openai"})

    # 4. provider_env inietta la env apikey del catalogo
    env = prov.provider_env()
    apikey_env = prov._CATALOG["openai"]["apikey_env"]
    check("provider_env apikey", env.get(apikey_env) == "sk-test")

    # 5. subscription anthropic con token non in scadenza → env senza refresh
    prov._write("anthropic", {"method": "subscription", "access_token": "tok-live",
                              "refresh_token": "ref", "expires_at": 9999999999})
    env = prov.provider_env()
    check("provider_env subscription", env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-live")
    check("connected = entrambi", prov.connected_provider_ids() == {"openai", "anthropic"})

    # 6. disconnect rimuove dal vault
    import asyncio
    asyncio.run(prov.disconnect("openai"))
    check("disconnect openai", prov._read("openai") is None)
    check("connected = {anthropic}", prov.connected_provider_ids() == {"anthropic"})

    # 7. gateway giù → _read degrada a None (fail-safe), niente eccezione
    fake.down = True
    check("read degradato a None", prov._read("anthropic") is None)
    # connected_provider_ids invece propaga (a valle è fail-open)
    try:
        prov.connected_provider_ids()
        check("connected propaga su down", False)
    except provider_store.ProviderStoreError:
        check("connected propaga su down", True)
    fake.down = False

    print(f"\n{ok} ok, {fail} fail")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
