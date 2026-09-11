"""La barra del contesto si misura contro la finestra della stanza.

clodia-logic#398 — la parte che è codice.

## Cosa chiedeva la issue, e cosa ho trovato

#398 osservava `fullstack-dev` con `agent_sdk: codex` e provider effettivo
`claude-team` (sdk `claude`), e proponeva due ipotesi: drift di configurazione,
oppure un resolver che sceglie il provider ignorando l'sdk dichiarato.

**La seconda è falsa**, ed è verificabile: con `providers: [codex, claude-team]`
e `model: gpt-5.6-sol`, `candidate_providers` ritorna già `['codex']` da solo —
`claude-team` dichiara `models: ["claude-*"]` e viene scartato. Lo stato
osservato può nascere solo da uno stack per-provider o da un override manuale,
che sono fatti di CONFIGURAZIONE d'istanza e non vivono in questo repository.

E l'accoppiata sdk-dichiarato ≠ sdk-del-provider **non è un difetto**:
`agent_runtime_sdk()` segue deliberatamente il provider effettivo, «permette
catene di fallback cross-SDK (scaleway→opencode, aws-region-eu→claude)».

## Il difetto vero, dove la issue stessa indicava

La issue citava `server/agents/model_context.py` — «un mismatch sdk/provider
rischia di far leggere la finestra sbagliata» — e lì il difetto c'è.
`model_context_window()` risolve la coppia **(harness, modello)**, perché lo
stesso modello ha finestre diverse a seconda della CLI che lo comanda. Ma il suo
unico chiamante, `_agent_context()`, gli passava:

    model_context_window(spec.model, spec.agent_sdk)

cioè la coppia **DICHIARATA**, fuori da qualunque stanza — mentre la sessione di
quella stanza gira sul provider scelto per il TIER, con il modello abbinato a
quel provider e sotto l'harness di quel provider.

È il difetto di clodia-platform#322, stessa classe, in un punto che #390 non ha
raggiunto: un valore fuori-stanza esposto come se fosse quello in uso. Lì si
vedeva come un nome di modello sbagliato; qui come una **barra di occupazione
misurata contro il righello sbagliato** — e `_agent_context` il tier ce l'ha già
in mano.

Il caso peggiore non è accademico: un agente dichiarato su `claude-opus-5`
(1M sotto l'harness claude) che in una stanza gira su `scaleway`/`glm-5.1`
(200k sotto opencode) mostra la barra su un righello **cinque volte** troppo
lungo. Un contesto quasi pieno si legge come vuoto — che è il momento esatto in
cui quella barra servirebbe.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import channels


class _Chat:
    def __init__(self, tokens: int):
        self._t = tokens

    def to_dict(self):
        return {"context_tokens": self._t}


class _Manager:
    def __init__(self, chat=None):
        self._chat = chat

    def get(self, chat_id):
        if self._chat is None:
            raise KeyError(chat_id)
        return self._chat


class _Spec:
    """Dichiarato su claude/opus-5 (1M), ma con uno stack anche su scaleway."""

    def __init__(self, name="ricercatore", model="claude-opus-5", sdk="claude"):
        self.name = name
        self.type = "bot"
        self.model = model
        self.agent_sdk = sdk


def _contesto(spec, override, chat=None, tier="SEAL-1"):
    """`_agent_context` con la stanza che risolve su `override`."""
    def _ovr(kind, t):
        if override is None:
            raise channels.ProviderNotConnected(kind, "nessuno")
        return dict(override)

    with patch.object(channels, "manager", _Manager(chat)), \
         patch.object(channels, "topic_runtime_override", _ovr):
        return channels._agent_context(tier, "stanza", spec, tier)


class La_finestra_e_quella_dello_stack_di_stanza(unittest.TestCase):

    def test_stack_cross_sdk_non_usa_la_finestra_dichiarata(self) -> None:
        """IL CASO. Dichiarato claude/opus-5 → 1M; in stanza scaleway/glm-5.1
        sotto opencode → 200k. Misurare 150k token contro 1M dice «15%» quando
        il vero è «75%»."""
        ctx = _contesto(
            _Spec(),
            {"provider": "scaleway", "model": "glm-5.1"},
            chat=_Chat(150_000))
        self.assertIsNotNone(ctx)
        self.assertEqual(200_000, ctx["window"],
                         "la barra usa il righello dello stack fuori-stanza")
        self.assertEqual(0.75, ctx["pct"])

    def test_stack_di_stanza_uguale_al_dichiarato_non_cambia_niente(self) -> None:
        """Il caso normale — la stragrande maggioranza degli agenti ha uno stack
        solo — deve restare identico a prima."""
        ctx = _contesto(
            _Spec(),
            {"provider": "anthropic-api", "model": "claude-opus-5"},
            chat=_Chat(250_000))
        self.assertEqual(1_000_000, ctx["window"])
        self.assertEqual(0.25, ctx["pct"])

    def test_l_harness_conta_quanto_il_modello(self) -> None:
        """Lo STESSO modello ha finestre diverse secondo la CLI che lo comanda:
        `gpt-5.5` è 400k dentro Codex. Leggere il modello giusto sotto l'sdk
        sbagliato è metà della correzione, e metà non basta."""
        ctx = _contesto(
            _Spec(model="gpt-5.5", sdk="codex"),
            {"provider": "openai-api", "model": "gpt-5.5"},
            chat=_Chat(200_000))
        self.assertEqual(400_000, ctx["window"])

    def test_senza_sessione_la_finestra_e_gia_quella_giusta(self) -> None:
        """La barra a zero dichiara comunque un righello: se è quello sbagliato,
        lo è dal primo istante."""
        ctx = _contesto(_Spec(), {"provider": "scaleway", "model": "glm-5.1"})
        self.assertEqual({"used": 0, "window": 200_000, "pct": 0.0}, ctx)


class Una_stanza_che_non_risolve_non_inventa_un_righello(unittest.TestCase):

    def test_nessun_provider_idoneo_niente_barra(self) -> None:
        """Stessa scelta di `agent_effective_model_for_tier` (#390): ripiegare
        sul dichiarato rimetterebbe in circolo esattamente il valore
        fuori-stanza che questa correzione toglie. In quel tier l'agente non
        prende turni, e `_eligibility` lo dice già con ⚠️."""
        self.assertIsNone(_contesto(_Spec(), None, chat=_Chat(10_000)))


class Il_chiamante_passa_il_tier_del_topic(unittest.TestCase):
    """`tier` (quello dell'URL) e `tier_real` (quello scritto nel meta del topic)
    possono differire, e il provider si sceglie sul secondo: `_eligibility`
    accanto usa già `tier_real`."""

    def test_la_rotta_passa_il_tier_reale(self) -> None:
        from pathlib import Path
        src = (Path(__file__).parent / "channels.py").read_text(encoding="utf-8")
        self.assertIn("_agent_context(tier, name, spec, tier_real)", src,
                      "la rotta risolve lo stack della stanza su un tier che "
                      "può non essere quello del topic")


class Il_modello_e_l_harness_si_leggono_insieme(unittest.TestCase):
    """La lezione di #315, riaffermata: provider e modello escono dalla stessa
    scelta e vanno letti dalla stessa risposta. `_topic_runtime_field` lo
    prometteva nel docstring e chiamava `topic_runtime_override` una volta per
    campo — due risoluzioni indipendenti, cioè il posto in cui possono
    divergere."""

    def test_una_sola_risoluzione_per_stanza(self) -> None:
        chiamate = []

        def _ovr(kind, t):
            chiamate.append((kind, t))
            return {"provider": "scaleway", "model": "glm-5.1"}

        with patch.object(channels, "manager", _Manager(_Chat(1000))), \
             patch.object(channels, "topic_runtime_override", _ovr):
            channels._agent_context("SEAL-1", "stanza", _Spec(), "SEAL-1")
        self.assertEqual(1, len(chiamate),
                         f"provider e modello risolti separatamente: {chiamate}")


if __name__ == "__main__":
    unittest.main()
