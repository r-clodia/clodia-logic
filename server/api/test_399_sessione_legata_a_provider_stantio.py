"""Una sessione viva legata a un provider che non serve più il suo modello.

clodia-logic#399 — punto 1 («il binding provider↔sessione va rifatto»).

## Perché la correzione di #333 da sola non basta

Il turno che ha aperto questa issue era la **riprova** dopo che il routing era
già stato corretto: `runtime.agents` diceva `provider: openai-api`, e l'errore è
arrivato identico, con il messaggio che solo l'account ChatGPT può produrre.
Nel log la riga che spiega tutto è una sola:

    TTFT … spawn_wait_ms=0 session_ready_ms=0      <- sessione RIUSATA, non nuova

Il provider si lega alla sessione quando la sessione nasce. La correzione della
configurazione non raggiunge un processo già vivo, e chi ha aperto la issue ha
dovuto eseguire `runtime.restart_agent(ophelia)` **a mano** per farla valere.

## La macchina per accorgersene c'è già, e fa tre domande su quattro

`_provider_della_stanza_ancora_valido()` (clodia-platform#305) ricontrolla a
ogni turno il provider della sessione viva e, se non regge più, la distrugge
perché venga ricreata con quello giusto. Ma la domanda che pone —
`provider_usable_for_tier()` — è: **connesso? non in pausa? SEAL ≥ tier?**

Non chiede se quel provider serva ancora il MODELLO dell'agente. Quindi una
sessione legata a `codex` con `gpt-5-codex` passa il ricontrollo — `codex` è
connesso, non è in pausa, il suo SEAL basta — e continua a sbattere contro lo
stesso 400 finché non interviene una persona.

È anche il motivo per cui #333 e questa issue viaggiano insieme: con la sola
#333 le sessioni NUOVE nascono giuste e quelle VIVE restano rotte; con la sola
#399 la quarta domanda esiste ma la risposta è ancora «sì, lo serve», perché a
dirlo è il catalogo che #333 corregge.

La quarta domanda non aggiunge un restart forzato: fa in modo che quello che già
c'è se ne accorga.
"""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from . import channels, providers


class _Chat:
    def __init__(self, provider: str | None):
        self._runtime_override = {"provider": provider} if provider else {}


class _Spec:
    """`ophelia` come la descrive la issue: un modello che l'abbonamento rifiuta."""

    def __init__(self, name="ophelia", model="gpt-5-codex", provider_models=None):
        self.name = name
        self.clearance = "SEAL-1"
        self.model = model
        self.agent_sdk = "codex"
        self.provider_models = provider_models or {}


class _Manager:
    def __init__(self, chat: _Chat | None):
        self._chat = chat
        self.cancellate: list[str] = []

    def get(self, chat_id):
        if self._chat is None:
            raise KeyError(chat_id)
        return self._chat

    async def delete(self, chat_id):
        self.cancellate.append(chat_id)
        self._chat = None


CHAT_ID = "chan:SEAL-1:stanza:ophelia"


def _chiedi(spec, mgr, sostituto="openai-api", occupata=False, usable=None):
    """Esegue il ricontrollo della stanza con l'infrastruttura sostituita."""
    async def _annuncia(tier, name, s, pid):
        return None
    toppe = [
        patch.object(channels, "manager", mgr),
        patch.object(channels, "_topic_provider", lambda s, t: sostituto),
        patch.object(channels, "_chat_busy", lambda cid: occupata),
        patch.object(channels, "_announce_provider_inadeguato", _annuncia),
    ]
    if usable is not None:
        toppe.append(patch.object(providers, "provider_usable_for_tier", usable))
    for t in toppe:
        t.start()
    try:
        return asyncio.run(channels._provider_della_stanza_ancora_valido(
            "SEAL-1", "stanza", "SEAL-1", spec, CHAT_ID))
    finally:
        for t in toppe:
            t.stop()


class Il_ricontrollo_chiede_anche_del_modello(unittest.TestCase):
    """Il difetto è nella DOMANDA, non nella risposta: il modello dell'agente
    non arrivava proprio a chi deve giudicare."""

    def test_il_modello_dell_agente_viene_passato(self) -> None:
        visto: dict = {}

        def _spia(pid, tier, model=None, provider_models=None):
            visto.update(pid=pid, tier=tier, model=model,
                         provider_models=provider_models)
            return True

        _chiedi(_Spec(), _Manager(_Chat("codex")), usable=_spia)
        self.assertEqual("codex", visto.get("pid"))
        self.assertEqual("gpt-5-codex", visto.get("model"),
                         "il ricontrollo giudica il provider senza sapere quale "
                         "modello dovrà servire")

    def test_passa_anche_gli_stack_per_provider(self) -> None:
        """Il modello EFFETTIVO di un provider può essere l'override per-provider,
        non il `model` top-level: giudicare sul secondo è la stessa svista di
        clodia-platform#325, spostata qui."""
        visto: dict = {}

        def _spia(pid, tier, model=None, provider_models=None):
            visto.update(provider_models=provider_models)
            return True

        spec = _Spec(provider_models={"codex": "gpt-5.5"})
        _chiedi(spec, _Manager(_Chat("codex")), usable=_spia)
        self.assertEqual({"codex": "gpt-5.5"}, visto.get("provider_models"))


class Il_giudizio_sul_provider_include_il_modello(unittest.TestCase):
    """`provider_usable_for_tier` sono «le condizioni che
    `effective_provider_for_tier` applica quando SCEGLIE, qui poste su un
    provider GIÀ scelto». Il modello era una di quelle, e mancava."""

    def _infra_perfetta(self):
        """Connesso, non in pausa, SEAL sufficiente: le altre tre risposte sono
        tutte «sì», così a decidere resta solo la quarta."""
        return [
            patch.object(providers, "connected_provider_ids",
                         lambda: {"codex", "openai-api"}),
            patch.object(providers, "_load_paused", lambda: set()),
            patch.object(providers, "provider_meets_tier", lambda pid, tier: True),
        ]

    def _usable(self, pid, model, provider_models=None):
        toppe = self._infra_perfetta()
        for t in toppe:
            t.start()
        try:
            return providers.provider_usable_for_tier(
                pid, "SEAL-1", model, provider_models)
        finally:
            for t in toppe:
                t.stop()

    def test_provider_che_non_serve_piu_il_modello_non_e_spendibile(self) -> None:
        self.assertFalse(self._usable("codex", "gpt-5-codex"))

    def test_provider_che_lo_serve_resta_spendibile(self) -> None:
        self.assertTrue(self._usable("openai-api", "gpt-5-codex"))

    def test_senza_modello_il_giudizio_e_quello_di_prima(self) -> None:
        """Back-compat: `model=None` non deve diventare un rifiuto, o ogni
        chiamante che non passa il modello vedrebbe sparire i suoi provider."""
        self.assertTrue(self._usable("codex", None))

    def test_lo_stack_per_provider_ha_la_precedenza(self) -> None:
        """Lo stesso agente su `codex` con un modello diverso per quel provider:
        a contare è il modello che QUEL provider servirà."""
        self.assertTrue(self._usable("codex", "gpt-5-codex", {"codex": "gpt-5.5"}))


class La_sessione_stantia_viene_rifatta(unittest.TestCase):
    """L'esito che chiude l'incidente: il turno riparte da solo, senza che
    qualcuno debba lanciare `restart_agent` a mano."""

    def _con_catalogo_vero(self, fn):
        toppe = [
            patch.object(providers, "connected_provider_ids",
                         lambda: {"codex", "openai-api"}),
            patch.object(providers, "_load_paused", lambda: set()),
            patch.object(providers, "provider_meets_tier", lambda pid, tier: True),
        ]
        for t in toppe:
            t.start()
        try:
            return fn()
        finally:
            for t in toppe:
                t.stop()

    def test_la_sessione_legata_all_abbonamento_viene_distrutta(self) -> None:
        mgr = _Manager(_Chat("codex"))
        ok = self._con_catalogo_vero(lambda: _chiedi(_Spec(), mgr))
        self.assertTrue(ok, "il turno deve partire, col provider nuovo")
        self.assertEqual([CHAT_ID], mgr.cancellate,
                         "la sessione stantia sopravvive: il turno successivo "
                         "sbatterà di nuovo sullo stesso 400")

    def test_una_sessione_gia_sana_non_si_tocca(self) -> None:
        """Ricreare una sessione costa il contesto della conversazione: si paga
        quando serve, non per abitudine."""
        mgr = _Manager(_Chat("openai-api"))
        ok = self._con_catalogo_vero(lambda: _chiedi(_Spec(), mgr))
        self.assertTrue(ok)
        self.assertEqual([], mgr.cancellate)

    def test_una_sessione_occupata_si_rimanda(self) -> None:
        """Vale anche qui la scelta di #305: interrompere un turno in corso non
        lo rende retroattivamente valido."""
        mgr = _Manager(_Chat("codex"))
        ok = self._con_catalogo_vero(
            lambda: _chiedi(_Spec(), mgr, occupata=True))
        self.assertTrue(ok)
        self.assertEqual([], mgr.cancellate)


class Entrambi_i_punti_restano_coperti(unittest.TestCase):
    """La guard strutturale di #305, riaffermata: se un domani nascesse un terzo
    punto che crea sessioni di canale, deve porre le stesse QUATTRO domande."""

    def test_ogni_creazione_di_sessione_e_preceduta_dal_controllo(self) -> None:
        src = (Path(__file__).parent / "channels.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("override = topic_runtime_override("),
                         src.count("await _provider_della_stanza_ancora_valido("))


if __name__ == "__main__":
    unittest.main()
