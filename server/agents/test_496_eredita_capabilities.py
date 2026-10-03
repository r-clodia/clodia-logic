"""Un seed figlio eredita le SKILL del padre, non solo i verbi (#496).

Il difetto non era «l'eredità non c'è»: era che c'erano due eredità diverse.
I verbi passavano da `effective_tool_permissions` — transitiva, con limite di
profondità e taglio dei cicli — mentre le capabilities le risolveva a mano
`EphemeralWorkspace`, **a un livello solo** e in un punto solo. Risultato:
`tomato.officer` con `parents: [officer]` materializzava zero skill ovunque
tranne che nello spawn, e un nonno non arrivava nemmeno lì.

I casi qui sotto fissano la regola nella forma in cui la issue la chiede, e in
particolare quella che costa di più scoprire dopo: un ciclo nei `parents` non
deve appendere il boot, e una wildcard deve restare una wildcard — l'espansione
nel catalog è a valle, in `materialize_capabilities`, e anticiparla qui vorrebbe
dire congelare il catalog al momento della risoluzione.
"""
from __future__ import annotations

import unittest
import unittest.mock
from pathlib import Path

import yaml

from ..config import workspace_path
from .inheritance import effective_capabilities, effective_tool_permissions


def _seeds_del_pack() -> dict:
    out = {}
    base = Path(workspace_path("catalogs/packs"))
    for pack in sorted(base.iterdir()) if base.is_dir() else []:
        for d in sorted((pack / "agents").iterdir()) if (pack / "agents").is_dir() else []:
            f = d / "agent.yaml"
            if f.is_file():
                out[d.name] = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return out


def _spec(name, caps=None, parents=None, verbs=None):
    """Spec finta: il risolutore legge per attributo o per chiave, e qui si usa
    la forma dict proprio per tenere il test lontano da `AgentSpec` — la regola
    è sulla catena, non sul modello pydantic."""
    return {"name": name, "capabilities": list(caps or []),
            "parents": list(parents or []), "tool_permissions": list(verbs or [])}


def _registry(*specs):
    return {s["name"]: s for s in specs}


class EreditaCapabilitiesTests(unittest.TestCase):
    def test_il_figlio_prende_le_skill_del_padre(self) -> None:
        """Il caso della issue: `tomato.officer` ha `capabilities: []` e deve
        comunque avere le skill di `officer`."""
        specs = _registry(
            _spec("officer", caps=["kanban-operations", "it-pack/incident-response"]),
            _spec("tomato.officer", caps=[], parents=["officer"]),
        )
        self.assertEqual(
            effective_capabilities("tomato.officer", specs),
            ["kanban-operations", "it-pack/incident-response"],
        )

    def test_le_proprie_vengono_prima_e_non_si_duplicano(self) -> None:
        """Una capability dichiarata anche dal padre compare UNA volta sola, e
        l'ordine resta «prima le mie»: è l'ordine in cui le legge chi materializza
        le skill, e un duplicato là diventa una copia di cartella in più."""
        specs = _registry(
            _spec("officer", caps=["kanban-operations", "it-pack/incident-response"]),
            _spec("tomato.officer", caps=["it-pack/incident-response", "topic-files"],
                  parents=["officer"]),
        )
        self.assertEqual(
            effective_capabilities("tomato.officer", specs),
            ["it-pack/incident-response", "topic-files", "kanban-operations"],
        )

    def test_la_catena_e_transitiva_su_tre_livelli(self) -> None:
        """Il nonno conta. È ciò che l'union a un livello di `EphemeralWorkspace`
        perdeva in silenzio: nessun errore, solo una skill che non c'è."""
        specs = _registry(
            _spec("nonno", caps=["a"]),
            _spec("padre", caps=["b"], parents=["nonno"]),
            _spec("figlio", caps=["c"], parents=["padre"]),
        )
        self.assertEqual(effective_capabilities("figlio", specs), ["c", "b", "a"])

    def test_un_ciclo_viene_tagliato(self) -> None:
        """Due seed che si dichiarano genitori a vicenda sono un file scritto
        male, non un motivo per appendere il boot."""
        specs = _registry(
            _spec("a", caps=["skill-a"], parents=["b"]),
            _spec("b", caps=["skill-b"], parents=["a"]),
        )
        self.assertEqual(sorted(effective_capabilities("a", specs)),
                         ["skill-a", "skill-b"])

    def test_la_catena_troppo_profonda_si_tronca(self) -> None:
        """Stesso limite dei verbi, e per la stessa ragione: oltre una certa
        profondità la dichiarazione non la legge più nessuno, e una catena
        costruita apposta non deve diventare un costo di avvio."""
        specs = _registry(*[
            _spec(f"s{i}", caps=[f"cap{i}"], parents=([f"s{i + 1}"] if i < 20 else []))
            for i in range(21)
        ])
        caps = effective_capabilities("s0", specs)
        self.assertIn("cap0", caps)
        self.assertNotIn("cap20", caps)

    def test_la_wildcard_resta_una_wildcard(self) -> None:
        """`anthropic-pack/*` non si espande qui: la espande
        `skill_sync.materialize_capabilities` leggendo il catalog nel momento in
        cui copia. Espanderla qui vorrebbe dire fotografare il catalog adesso e
        consegnare al figlio una lista che invecchia."""
        specs = _registry(
            _spec("padre", caps=["anthropic-pack/*"]),
            _spec("figlio", caps=[], parents=["padre"]),
        )
        self.assertEqual(effective_capabilities("figlio", specs), ["anthropic-pack/*"])

    def test_un_genitore_che_non_esiste_non_rompe(self) -> None:
        """Il registry può non avere l'antenato (pack non installato): il figlio
        resta con le sue, non solleva."""
        specs = _registry(_spec("figlio", caps=["mia"], parents=["fantasma"]))
        self.assertEqual(effective_capabilities("figlio", specs), ["mia"])

    def test_un_seed_sconosciuto_non_eredita_nulla(self) -> None:
        self.assertEqual(effective_capabilities("mai-visto", {}), [])
        self.assertEqual(effective_capabilities("", {}), [])

    def test_archseed_e_antenato_anche_per_le_skill(self) -> None:
        """Stessa regola dei verbi, nello stesso modulo: se un giorno l'arciseed
        dichiarasse un pavimento di skill, quel pavimento sarebbe di tutti senza
        che nessun seed debba scrivere una riga. Oggi non ne ha, quindi la
        differenza osservabile è zero — ed è il motivo per cui la simmetria si
        fissa adesso che è gratis, invece di scoprirla divergente dopo."""
        specs = _registry(
            _spec("archseed", caps=["pavimento"]),
            _spec("clodia", caps=["mia"], parents=["archseed"]),
            _spec("senza-parents", caps=["mia"]),
        )
        self.assertEqual(effective_capabilities("clodia", specs), ["mia", "pavimento"])
        # Non dichiararlo non serve a uscirne: vale per le skill come per i verbi.
        self.assertEqual(effective_capabilities("senza-parents", specs),
                         ["mia", "pavimento"])

    def test_i_verbi_restano_quelli_di_prima(self) -> None:
        """Le due risoluzioni condividono la camminata: questo test è qui perché
        rifattorizzarla non deve cambiare l'esito dei verbi, che è la parte con
        conseguenze di sicurezza."""
        specs = _registry(
            _spec("padre", caps=["x"], verbs=["topic.put"]),
            _spec("figlio", caps=[], parents=["padre"], verbs=["topic.delete_file"]),
        )
        self.assertEqual(effective_tool_permissions("figlio", specs),
                         ["topic.delete_file", "topic.put"])


class SeedVeriTests(unittest.TestCase):
    """Sui seed davvero installati l'eredità non deve TOGLIERE niente.

    Il rischio di una funzione nuova usata in cinque punti è che un seed senza
    `parents` ci passi attraverso e ne esca diverso. Qui si misura su quelli
    veri, non su finzioni."""

    def test_nessun_seed_perde_le_proprie_capabilities(self) -> None:
        # I seed si leggono dai file del pack, non dal registry: il registry
        # carica dalla data dir, che nei test è la root del repo e non contiene
        # agenti — un test che gira su zero seed passerebbe senza misurare nulla.
        specs = _seeds_del_pack()
        self.assertTrue(specs, "nessun seed nel pack: il test non misurerebbe nulla")
        for nome, spec in specs.items():
            effettive = effective_capabilities(nome, specs)
            for cap in (spec.get("capabilities") or []):
                self.assertIn(cap, effettive, f"{nome} ha perso '{cap}'")


class CallSiteTests(unittest.TestCase):
    """I consumatori veri, non solo la funzione pura.

    Una funzione nuova e corretta che nessuno chiama è il modo più economico di
    chiudere una issue senza risolverla: questi casi passano dai punti in cui la
    skill ereditata deve VEDERSI — il contesto che l'agente legge nello spawn,
    le pill del benvenuto e il profilo di routing.
    """

    def setUp(self) -> None:
        from .models import AgentSpec
        self.padre = AgentSpec(name="officer", display_name="Officer",
                               description="capo turno", type="bot",
                               capabilities=["it-pack/incident-response"])
        self.figlio = AgentSpec(name="tomato.officer", display_name="Tomato Officer",
                                description="capo turno di tomato", type="bot",
                                capabilities=[], parents=["officer"])
        self._patch = unittest.mock.patch.object(
            __import__("server.agents.loader", fromlist=["registry"]).registry,
            "list", return_value=[self.padre, self.figlio])
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_il_contesto_dello_spawn_elenca_le_skill_ereditate(self) -> None:
        """`AGENTS.md` del workspace diceva «(nessuna)» a un agente che aveva le
        skill del padre materializzate due righe sotto."""
        import tempfile
        from pathlib import Path as _P
        from .workspace import _build_codex_agents_md
        with tempfile.TemporaryDirectory() as d:
            md = _build_codex_agents_md(self.figlio, _P(d))
        self.assertIn("it-pack/incident-response", md)

    def test_le_pill_del_benvenuto_vedono_la_skill_ereditata(self) -> None:
        from ..api.topic_playbooks import _agent_skill_names
        with unittest.mock.patch(
                "server.agents.loader.registry.get",
                side_effect=lambda n: {"officer": self.padre,
                                       "tomato.officer": self.figlio}[n]):
            self.assertIn("it-pack/incident-response", _agent_skill_names("tomato.officer"))

    def test_il_profilo_di_routing_prende_i_pezzi_ereditati(self) -> None:
        """Senza, un seed derivato non ha nessun pezzo di dominio: score 0
        ovunque, cioè non lo instrada nessuno."""
        # Il profilo non contiene lo slug ma le sue PAROLE: il confronto è su
        # un embedding di testo, non su un identificatore.
        from ..api.responder_routing import _profile_pieces
        pezzi = _profile_pieces(self.figlio)
        self.assertTrue(any("incident" in p for p in pezzi),
                        f"nessun pezzo dalla skill ereditata: {pezzi}")

    def test_la_scheda_dell_agente_mostra_dichiarate_ed_effettive(self) -> None:
        """Le due convivono: la dichiarazione dice cosa c'è nel file, le
        effettive cosa l'agente sa fare, e la differenza è l'eredità."""
        from ..api.agent_registry import _effective_caps
        self.assertEqual(self.figlio.capabilities, [])
        self.assertEqual(_effective_caps(self.figlio), ["it-pack/incident-response"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
