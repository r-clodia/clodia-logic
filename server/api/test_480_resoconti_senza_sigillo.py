"""I resoconti della piattaforma non emettono il sigillo vivo (clodia-platform#480, parte c).

#480 nasce da un messaggio incollato: le `@` di un dialogo di routing riportato
sono state lette come convocazioni e il router ha aperto una disambiguazione a
tre vie invece di instradare. La cintura sta nel parser (parte (a),
`mentions.py`, due copie); la **causa** sta qui.

La piattaforma impone ai suoi agenti una regola che lei stessa violava: «quando
NOMINI un agente in un resoconto, scrivi il nome senza `@`, perché il `@` in una
frase di racconto apre un turno a chi non ti aveva chiesto niente». Misurato
prima del fix: su 11 template campionati il parser leggeva **15 menzioni vive**,
di cui 13 erano resoconti.

L'invariante, detta a voce alta: **un testo che RACCONTA non convoca.** Il nome
resta leggibile — in grassetto, non nudo — ma non è più un sigillo.

Le due eccezioni sono convocazioni vere e restano tali: il guardiano a cui si
chiede la diagnosi, e l'autore a cui si chiede di disambiguare. Sono qui sotto
come test, non come commento, perché un fix che le togliesse sembrerebbe più
coerente ed è esattamente l'errore da impedire.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels, mentions


def sigilli(testo: str) -> list[str]:
    """I `@nome` VIVI in un testo, letti col solo riconoscitore del sigillo.

    Non si usa `extract_tags` qui, ed è una scelta che vale la pena spiegare:
    la parte (a) di #480 insegna al parser a ignorare proprio questi template
    quando sono riportati, quindi `extract_tags` tornerebbe vuoto anche su un
    resoconto che il sigillo ce l'ha ancora. Il test passerebbe per il motivo
    sbagliato e non misurerebbe più niente. Qui la domanda è un'altra e più
    diretta: **questo testo contiene un sigillo?**
    """
    return [m.group("nome").lower()
            for m in mentions._MENTION_RE.finditer(testo or "")]


class _Spec:
    def __init__(self, name, type_="bot"):
        self.name = name
        self.type = type_


class _Chat:
    def __init__(self, origin):
        self.origin = origin


CHAIN = ["human:davide", "agent:clodia", "agent:fullstack-dev"]


class ElencoOrTests(unittest.TestCase):
    """`_elenco_or` è il punto condiviso: sei chiamanti compongono da lì la
    frase che nomina gli agenti. Correggerlo qui vale per tutti e sei, e per
    il settimo che verrà."""

    def test_un_elenco_non_convoca_nessuno(self):
        self.assertEqual(sigilli(channels._elenco_or(["clodia"])), [])
        self.assertEqual(
            sigilli(channels._elenco_or(["clodia", "sysadmin"])), [])
        self.assertEqual(
            sigilli(channels._elenco_or(["a", "b", "c"])), [])

    def test_i_nomi_restano_leggibili(self):
        testo = channels._elenco_or(["clodia", "sysadmin"])
        self.assertIn("clodia", testo)
        self.assertIn("sysadmin", testo)
        self.assertIn(" o ", testo, "la congiunzione serve a chi legge")

    def test_il_dedup_non_e_stato_perso_nel_cambio(self):
        # #256: «scegli fra worker e worker» non è una scelta.
        self.assertEqual(channels._elenco_or(["worker", "worker"]),
                         channels._elenco_or(["worker"]))


class DialogoDiRoutingTests(unittest.TestCase):
    """Il dialogo è il testo dell'incidente: va reso muto SENZA rompere la
    pill, che è l'unica strada che l'utente ha per rispondere."""

    def _testo(self, nomi):
        # Stessa espressione del dispatcher (`channels._dispatch`).
        return (f"Routing: scegli {channels._elenco_or(nomi)}.\n\n"
                f"<!-- choices={','.join(nomi)} -->")

    def test_il_dialogo_non_convoca_nessuno(self):
        self.assertEqual(sigilli(self._testo(["clodia", "sysadmin"])), [])

    def test_la_pill_resta_risolvibile(self):
        """Round-trip: il dialogo composto oggi deve ancora essere leggibile
        da chi risolve la risposta dell'utente. Senza questo test il fix
        zittisce il dialogo e insieme la sola via d'uscita che offriva."""
        quote = "> router: " + self._testo(["worker", "accountant"]).split("\n")[0]
        content = quote + "\n\n@router accountant"
        specs = {"worker": _Spec("worker"), "accountant": _Spec("accountant")}
        with patch.object(channels.registry, "get_by_name",
                          lambda n: specs.get(n)), \
             patch.object(channels, "_provider_seal_ok", lambda *_a: True):
            scelta, _src = channels._routing_dialog_reply(
                content, ["worker", "accountant"], "SEAL-1",
                request={"owner": "davide", "source": {"text": "fai tu"}})
        self.assertEqual([s.name for s, _w in scelta], ["accountant"])

    def test_i_dialoghi_gia_in_cronologia_restano_risolvibili(self):
        """Quelli scritti prima di questa PR hanno il sigillo. Sono in
        cronologia e restano cliccabili: il resolver legge entrambe le forme."""
        content = ("> router: Routing: scegli @worker o @accountant.\n\n"
                   "@router worker")
        specs = {"worker": _Spec("worker"), "accountant": _Spec("accountant")}
        with patch.object(channels.registry, "get_by_name",
                          lambda n: specs.get(n)), \
             patch.object(channels, "_provider_seal_ok", lambda *_a: True):
            scelta, _src = channels._routing_dialog_reply(
                content, ["worker", "accountant"], "SEAL-1",
                request={"owner": "davide", "source": {"text": "fai tu"}})
        self.assertEqual([s.name for s, _w in scelta], ["worker"])


class AnnuncioDiTurnoFallitoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.posted: list[tuple] = []
        self.turns: list[tuple] = []

    def _patches(self, watcher=_Spec("sysadmin")):
        def post(tier, name, author, text, kind="human", **_kw):
            self.posted.append((author, text, kind))
            return {"id": "1", "author": author, "text": text, "kind": kind, "ts": "1"}

        async def start_turn(tier, name, tier_real, spec, principal, text, kind, **_kw):
            self.turns.append((spec.name, kind))
            return True

        async def channel_message(*_a, **_k):
            return None

        return (
            patch.object(channels.topics_client, "post_message", post),
            patch.object(channels.topics_client, "open_topic",
                         lambda *_a, **_k: {"meta": {"tier": "SEAL-1"}}),
            patch.object(channels.registry, "get_by_name",
                         lambda n: watcher if n == "sysadmin" else None),
            patch.object(channels, "_provider_seal_ok", lambda *_a: True),
            patch.object(channels, "_start_turn", start_turn),
            patch.object(channels, "_channel_message", channel_message),
            patch.object(channels, "_topic_title", lambda *_a: "T"),
        )

    async def _run(self, responder, **kw):
        ps = self._patches(**kw)
        for p in ps:
            p.start()
        try:
            await channels._announce_failure(
                "SEAL-1", "acme", responder, RuntimeError("codex exit 1"))
        finally:
            for p in ps:
                p.stop()

    async def test_chi_e_caduto_e_nominato_non_convocato(self):
        await self._run("fullstack-dev-271")
        _a, testo, _k = self.posted[0]
        self.assertIn("fullstack-dev-271", testo, "il nome serve a chi legge")
        self.assertNotIn("fullstack-dev-271", sigilli(testo),
                         "chi è appena morto non va risvegliato da un resoconto")

    async def test_il_guardiano_resta_convocato_davvero(self):
        """L'eccezione, ed è il punto: qui il `@` non racconta, chiede. Se
        sparisse, il turno di diagnosi non partirebbe più e il fix di #480
        avrebbe spento un meccanismo invece di correggerlo."""
        await self._run("fullstack-dev-271")
        _a, testo, _k = self.posted[0]
        self.assertIn("sysadmin", sigilli(testo))
        self.assertEqual([t[0] for t in self.turns], ["sysadmin"])


class ResocontoDiFineTurnoTests(unittest.IsolatedAsyncioTestCase):
    """`_report_back` compone il testo che il chiamante riceve come prompt —
    ed è il testo che più spesso viene ricopiato in chat da chi riferisce."""

    def setUp(self):
        self.turns: list[tuple] = []

    async def _run(self):
        async def start_turn(tier, name, tier_real, s, principal, text, kind, **kw):
            self.turns.append((s.name, text))
            return True

        ps = (
            patch.object(channels, "_start_turn", start_turn),
            patch.object(channels.registry, "get_by_name",
                         lambda n: _Spec("clodia") if n == "clodia" else None),
            patch.object(channels.topics_client, "open_topic",
                         lambda *_a, **_k: {"meta": {
                             "tier": "SEAL-1",
                             "participants": ["clodia", "fullstack-dev"]}}),
            patch.object(channels, "_provider_seal_ok", lambda *_a: True),
        )
        for p in ps:
            p.start()
        try:
            await channels._report_back("SEAL-1", "acme", "fullstack-dev",
                                        _Chat(CHAIN), "fatto", 0)
        finally:
            for p in ps:
                p.stop()

    async def test_il_resoconto_nomina_senza_convocare(self):
        await self._run()
        self.assertEqual(len(self.turns), 1)
        _nome, testo = self.turns[0]
        self.assertIn("fullstack-dev", testo, "il nome di chi ha finito serve")
        self.assertEqual(sigilli(testo), [],
                         "un resoconto di fine turno non convoca nessuno")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
