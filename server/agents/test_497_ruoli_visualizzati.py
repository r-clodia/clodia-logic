"""I ruoli MOSTRATI dei partecipanti di una stanza (#497).

La lista diceva `contributor` per tutti: in `SEAL-2/titul-brightnode` clodia,
segretario, messaggero, sysadmin e avvocato leggevano la stessa parola, quindi
la pagina non diceva chi possiede la stanza, chi la conduce e chi ci sta per un
ruolo di piattaforma.

Due cose che questi test tengono ferme, perché sono quelle che si romperebbero
per prime:

1. **chi conduce non si decide qui.** Il manager lo dà `coordinator.pick`, che è
   l'unico posto dove la ruling dell'11 ago 2026 è scritta. Questo modulo lo
   RICEVE. Se un giorno la precedenza cambia, cambia in un file e il badge la
   segue da sé;
2. **nessuno di questi ruoli autorizza niente.** Il `role` tecnico
   (`owner`/`contributor`/`reader`) resta quello di prima e viaggia accanto, e
   un `reader` conserva il suo marchio di sola lettura qualunque badge porti.
"""
from __future__ import annotations

import unittest

from .display_roles import OWNER, MANAGER, STAFF, CONTRIBUTOR, display_roles


class _Seed:
    def __init__(self, name, staff=False, deputy=False):
        self.name = name
        self.staff = staff
        self.deputy = deputy


#: La colonia della tabella di accettazione della issue.
_SEEDS = {
    "clodia": _Seed("clodia"),
    "segretario": _Seed("segretario", staff=True, deputy=True),
    "messaggero": _Seed("messaggero", staff=True),
    "sysadmin": _Seed("sysadmin", staff=True),
    "avvocato": _Seed("avvocato"),
}


def _meta(participants, owner="davide"):
    return {"owner": owner, "participants": list(participants)}


def _righe(participants, manager, owner="davide", meta=None):
    return {r["name"]: r for r in display_roles(
        meta or _meta(participants, owner), manager=manager, seeds=_SEEDS)}


class AccettazioneTests(unittest.TestCase):
    """La tabella della issue, alla lettera."""

    PIENA = ["clodia", "segretario", "messaggero", "sysadmin", "avvocato"]

    def test_stanza_con_clodia(self) -> None:
        r = _righe(self.PIENA, manager="clodia")
        self.assertEqual(r["davide"]["display_role"], OWNER)
        self.assertEqual(r["clodia"]["display_role"], MANAGER)
        self.assertEqual(r["segretario"]["display_role"], STAFF)
        self.assertEqual(r["segretario"]["marks"], ["staff", "deputy"])
        self.assertEqual(r["messaggero"]["display_role"], STAFF)
        self.assertEqual(r["messaggero"]["marks"], ["staff"])
        self.assertEqual(r["sysadmin"]["marks"], ["staff"])
        self.assertEqual(r["avvocato"]["display_role"], CONTRIBUTOR)
        self.assertEqual(r["avvocato"]["marks"], [])

    def test_stanza_senza_clodia_il_vice_conduce(self) -> None:
        """«staff · deputy · acting manager», e nessun altro manager."""
        senza = [p for p in self.PIENA if p != "clodia"]
        r = _righe(senza, manager="segretario")
        self.assertEqual(r["segretario"]["display_role"], MANAGER)
        self.assertEqual(r["segretario"]["marks"],
                         ["staff", "deputy", "acting manager"])
        manager = [n for n, v in r.items() if v["display_role"] == MANAGER]
        self.assertEqual(manager, ["segretario"])

    def test_clodia_che_entra_cambia_il_badge(self) -> None:
        """Il badge segue la composizione REALE: è la stessa stanza letta due
        volte, con e senza di lei."""
        senza = _righe([p for p in self.PIENA if p != "clodia"],
                       manager="segretario")
        con = _righe(self.PIENA, manager="clodia")
        self.assertIn("acting manager", senza["segretario"]["marks"])
        self.assertNotIn("acting manager", con["segretario"]["marks"])

    def test_un_seed_staff_nuovo_si_dichiara_da_solo(self) -> None:
        """Nessuna lista da aggiornare: basta l'attributo del seed. È il punto
        della issue — una lista nel frontend è una lista che il prossimo seed
        non aggiorna."""
        seeds = dict(_SEEDS, archivista=_Seed("archivista", staff=True))
        righe = {r["name"]: r for r in display_roles(
            _meta(["archivista", "avvocato"]), manager=None, seeds=seeds)}
        self.assertEqual(righe["archivista"]["display_role"], STAFF)
        self.assertEqual(righe["avvocato"]["display_role"], CONTRIBUTOR)


class RuoloTecnicoTests(unittest.TestCase):
    """Il display non tocca i permessi: li AFFIANCA."""

    def test_il_reader_conserva_il_marchio_di_sola_lettura(self) -> None:
        meta = {"owner": "davide",
                "participants": {"sysadmin": "reader", "avvocato": "reader"}}
        r = {x["name"]: x for x in display_roles(meta, manager=None, seeds=_SEEDS)}
        self.assertTrue(r["sysadmin"]["readonly"])
        self.assertEqual(r["sysadmin"]["display_role"], STAFF)
        self.assertTrue(r["avvocato"]["readonly"])
        self.assertEqual(r["avvocato"]["role"], "reader")

    def test_il_ruolo_tecnico_viaggia_accanto(self) -> None:
        meta = {"owner": "davide", "participants": {"avvocato": "contributor"}}
        r = {x["name"]: x for x in display_roles(meta, manager=None, seeds=_SEEDS)}
        self.assertEqual(r["avvocato"]["role"], "contributor")
        self.assertEqual(r["davide"]["role"], "owner")
        self.assertFalse(r["davide"]["readonly"])

    def test_la_forma_legacy_a_lista_vale_contributor(self) -> None:
        """Un topic che nessuno ha ancora toccato conserva la LISTA: deve
        leggersi come la mappa, non sparire."""
        r = {x["name"]: x for x in display_roles(
            _meta(["avvocato"]), manager=None, seeds=_SEEDS)}
        self.assertEqual(r["avvocato"]["role"], "contributor")
        self.assertFalse(r["avvocato"]["readonly"])


class OrdineEFormaTests(unittest.TestCase):
    def test_l_ordine_e_owner_manager_staff_contributor(self) -> None:
        """L'ordine lo decide il server: tre client che lo re-inventano sono tre
        ordini che divergono alla prima aggiunta."""
        righe = display_roles(
            _meta(["avvocato", "messaggero", "clodia", "segretario"]),
            manager="clodia", seeds=_SEEDS)
        self.assertEqual([r["name"] for r in righe],
                         ["davide", "clodia", "messaggero", "segretario", "avvocato"])
        self.assertEqual([r["sort"] for r in righe], sorted(r["sort"] for r in righe))

    def test_l_owner_compare_anche_se_non_e_fra_i_participants(self) -> None:
        """Chi ha creato la stanza a volte non è in `participants`: non mostrarlo
        era il modo in cui la stanza sembrava non avere padrone."""
        righe = display_roles({"owner": "davide", "participants": ["avvocato"]},
                              manager=None, seeds=_SEEDS)
        self.assertEqual(righe[0]["name"], "davide")
        self.assertEqual(righe[0]["display_role"], OWNER)

    def test_l_owner_vince_sul_resto(self) -> None:
        """Un umano owner che fosse anche staff resterebbe owner: la proprietà
        della stanza non è un grado, ed è la riga più in alto."""
        seeds = dict(_SEEDS, davide=_Seed("davide", staff=True, deputy=True))
        righe = {r["name"]: r for r in display_roles(
            _meta(["davide"]), manager="davide", seeds=seeds)}
        self.assertEqual(righe["davide"]["display_role"], OWNER)

    def test_una_stanza_senza_coordinatore_non_ne_inventa_uno(self) -> None:
        """`manager=None` è un esito possibile (nessun coordinatore idoneo al
        tier): meglio nessun badge che un badge sbagliato."""
        righe = display_roles(_meta(["avvocato", "messaggero"]),
                              manager=None, seeds=_SEEDS)
        self.assertEqual([r for r in righe if r["display_role"] == MANAGER], [])

    def test_un_partecipante_senza_seed_e_un_contributor(self) -> None:
        """Gli umani invitati non hanno un `agent.yaml`: non devono far saltare
        la derivazione."""
        righe = {r["name"]: r for r in display_roles(
            _meta(["ospite"]), manager=None, seeds=_SEEDS)}
        self.assertEqual(righe["ospite"]["display_role"], CONTRIBUTOR)
        self.assertEqual(righe["ospite"]["marks"], [])

    def test_nessun_duplicato_se_l_owner_e_anche_partecipante(self) -> None:
        righe = display_roles(_meta(["davide", "avvocato"]), manager=None,
                              seeds=_SEEDS)
        self.assertEqual([r["name"] for r in righe].count("davide"), 1)


class SeedVeriTests(unittest.TestCase):
    """Gli attributi devono stare davvero nei file dei seed, non solo nel modello."""

    def test_i_tre_seed_di_piattaforma_sono_staff(self) -> None:
        seeds = _seeds_del_pack()
        for nome in ("segretario", "messaggero", "sysadmin"):
            self.assertTrue(seeds.get(nome, {}).get("staff"),
                            f"{nome} non dichiara `staff: true`")

    def test_solo_il_segretario_e_deputy(self) -> None:
        seeds = _seeds_del_pack()
        vice = sorted(n for n, s in seeds.items() if s.get("deputy"))
        self.assertEqual(vice, ["segretario"])

    def test_il_vice_e_anche_il_secondo_coordinatore_dichiarato(self) -> None:
        """Le due dichiarazioni devono dire la stessa cosa. Se un giorno la
        precedenza di `coordinator.DECLARED` cambia e il flag `deputy` resta
        dov'è, il badge «acting manager» finisce sul seed sbagliato — e nessuno
        se ne accorgerebbe guardando una delle due metà."""
        from .coordinator import DECLARED
        seeds = _seeds_del_pack()
        vice = [n for n, s in seeds.items() if s.get("deputy")]
        self.assertEqual(vice, [DECLARED[-1]])

    def test_i_coordinatori_non_sono_contributor_qualsiasi(self) -> None:
        """clodia non è staff (è il capitano, non il personale) ma non deve
        nemmeno leggersi come un contributor: lo copre il badge manager, e
        questo test lo fissa perché è l'unica riga della tabella che non nasce
        da un attributo."""
        seeds = _seeds_del_pack()
        self.assertFalse(seeds.get("clodia", {}).get("staff"))


def _seeds_del_pack() -> dict:
    import yaml
    from pathlib import Path
    from ..config import workspace_path
    out = {}
    base = Path(workspace_path("catalogs/packs"))
    for pack in sorted(base.iterdir()) if base.is_dir() else []:
        agents = pack / "agents"
        for d in sorted(agents.iterdir()) if agents.is_dir() else []:
            f = d / "agent.yaml"
            if f.is_file():
                out[d.name] = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return out




class RottaTests(unittest.TestCase):
    """La derivazione deve arrivare davvero nella risposta che la pagina legge.

    Una funzione corretta che nessuna rotta chiama chiude la issue senza
    risolverla: è il motivo per cui questo caso passa da `_ruoli_mostrati`, con
    il coordinatore patchato (chi coordina lo decide `coordinator.pick`, e qui
    si misura il cablaggio, non di nuovo la precedenza).
    """

    def _specs(self):
        from .models import AgentSpec
        def bot(nome, **extra):
            return AgentSpec.model_validate(
                {"name": nome, "description": "d", "display_name": nome,
                 "type": "bot", "system_prompt": "s.md", **extra})
        return {"clodia": bot("clodia"),
                "segretario": bot("segretario", staff=True, deputy=True),
                "messaggero": bot("messaggero", staff=True),
                "avvocato": bot("avvocato")}

    def _chiama(self, partecipanti, coordinatore):
        from unittest.mock import patch
        from ..api import channels as C
        specs = self._specs()
        meta = {"owner": "davide", "participants": list(partecipanti)}
        with patch.object(C, "_coordinatore_al_tier", return_value=coordinatore), \
             patch.object(C.registry, "get_by_name", side_effect=specs.get):
            return {r["name"]: r for r in C._ruoli_mostrati(meta, "SEAL-2")}

    def test_la_stanza_intera_esce_dalla_rotta(self) -> None:
        r = self._chiama(["clodia", "segretario", "messaggero", "avvocato"], "clodia")
        self.assertEqual(r["davide"]["display_role"], OWNER)
        self.assertEqual(r["clodia"]["display_role"], MANAGER)
        self.assertEqual(r["segretario"]["marks"], ["staff", "deputy"])
        self.assertEqual(r["messaggero"]["display_role"], STAFF)
        self.assertEqual(r["avvocato"]["display_role"], CONTRIBUTOR)

    def test_senza_clodia_il_vice_risulta_acting(self) -> None:
        r = self._chiama(["segretario", "messaggero", "avvocato"], "segretario")
        self.assertEqual(r["segretario"]["display_role"], MANAGER)
        self.assertIn("acting manager", r["segretario"]["marks"])

    def test_un_coordinatore_non_calcolabile_non_fa_sparire_la_lista(self) -> None:
        """Un badge mancante è meno grave di una stanza che non si apre."""
        from unittest.mock import patch
        from ..api import channels as C
        specs = self._specs()
        with patch.object(C, "_coordinatore_al_tier", side_effect=RuntimeError("giù")), \
             patch.object(C.registry, "get_by_name", side_effect=specs.get):
            righe = C._ruoli_mostrati(
                {"owner": "davide", "participants": ["clodia", "avvocato"]}, "SEAL-2")
        self.assertEqual({r["name"] for r in righe}, {"davide", "clodia", "avvocato"})
        self.assertEqual([r for r in righe if r["display_role"] == MANAGER], [])

    def test_il_coordinatore_lo_decide_coordinator_non_questo_modulo(self) -> None:
        """La precedenza NON è riscritta qui: se `_coordinatore_al_tier` dice
        `messaggero`, il badge manager va a messaggero. Il test è volutamente
        contro-intuitivo — misura che non esista una seconda copia della
        ruling nascosta in questo percorso."""
        r = self._chiama(["clodia", "segretario", "messaggero"], "messaggero")
        self.assertEqual(r["messaggero"]["display_role"], MANAGER)
        self.assertEqual(r["clodia"]["display_role"], CONTRIBUTOR)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
