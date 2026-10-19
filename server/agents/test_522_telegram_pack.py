"""clodia-platform#522 fase 2 — comms-pack porta l'implementazione Telegram.

Fin qui il pack DICHIARAVA il connettore e a parlare con `api.telegram.org` era
il gateway. Adesso il codice sta in `plugins/comms-pack/mcp/telegram_mcp.py` e
il gateway lo monta come backend MCP `telegram`: i verbi tornano agli agenti con
lo stesso nome, attraverso il proxy, cioè dopo whitelist, deny, gate, egress e
taint.

Quello che può rompersi in silenzio, e che qui si misura:

1. **il manifest promette verbi che il server non implementa.** La promessa non
   fallisce: una mappatura di egress su un verbo inesistente semplicemente non
   si applica mai, e l'invio esce senza che nessuno lo controlli. Qui le due
   facce — `pack.yaml` e il server — si confrontano per nome;
2. **il contesto dichiarato non arriva.** Il gateway riempie gli argomenti che
   il manifest elenca in `context:`; se uno schema del server non li dichiara,
   il backend riceve un argomento che non conosce (o peggio lo ignora e usa un
   default). I due elenchi vanno confrontati, non guardati;
3. **il mount non è quello giusto.** Il backend DEVE chiamarsi come il namespace
   del connettore: montato con un altro nome, i verbi tornano come
   `<altro>.send` e nessuna delle mappature del manifest li descrive più. E la
   credenziale dev'essere quella dichiarata dal connettore, non un'altra voce
   del vault;
4. **il comportamento**: il lease appartiene all'agente che il GATEWAY dichiara,
   `send` senza `chat_id` va alla chat legata al canale chiamante, e una
   chiamata senza contesto non diventa «fai pure».

Il server si importa dal percorso del catalogo (non è un modulo del package:
vive dentro il pack ed è copiato in `/datadir/plugins/<plugin>/mcp/` all'install).
"""
from __future__ import annotations

import importlib.util
import json
import re
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from ..config import workspace_path

PACK = Path(workspace_path("catalogs/packs/comms-pack"))
PLUGIN = PACK / "plugins" / "comms-pack"
# comms-pack 2.1.0 (#522/#558): the code is the pack's GATEWAY component, no
# longer an MCP backend.
CORE_FILE = PACK / "gateway" / "telegram_core.py"
ENTRYPOINT = PACK / "gateway" / "telegram_component.py"
MCP_JSON = PLUGIN / ".mcp.json"
SEEDS = Path(workspace_path("catalogs/packs/base-pack/agents"))


def _manifest() -> dict:
    return yaml.safe_load((PACK / "pack.yaml").read_text(encoding="utf-8")) or {}


def _connector(cid: str = "telegram") -> dict:
    for entry in _manifest().get("connectors") or []:
        if isinstance(entry, dict) and entry.get("id") == cid:
            return entry
    raise AssertionError(f"comms-pack non dichiara più il connettore '{cid}'")


def _argomenti_di_contesto(cid: str = "telegram") -> set:
    """I nomi degli argomenti che il gateway riempie (`context:`).

    Il blocco è una LISTA di righe `{arg, value}` — la grammatica che il parser
    del gateway legge (`manifest._parse_context`). Letto come mapping darebbe
    i nomi sbagliati in silenzio: su una lista `set(...)` non solleva, itera
    gli elementi, e qui gli elementi sono dizionari.
    """
    righe = _connector(cid).get("context") or []
    assert isinstance(righe, list), (
        "`context:` dev'essere una lista di righe {arg, value}: la forma a "
        "mapping non viene letta dal gateway e il blocco resta inerte")
    return {str(r["arg"]) for r in righe}


def _core():
    """Il connettore, importato dal file del pack.

    È stdlib pura — l'involucro MCP è un altro file — quindi si importa qui
    senza avere `mcp`, e soprattutto senza avere la STESSA versione di `mcp`
    dell'immagine del gateway: un controllo che dipendesse da quello misurerebbe
    l'ambiente invece del connettore.
    """
    spec = importlib.util.spec_from_file_location("telegram_core_pack", CORE_FILE)
    module = importlib.util.module_from_spec(spec)
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True       # the gateway component ships no bytecode
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = before
    return module


class IlPackPortaIlCodiceTests(unittest.TestCase):
    """I file ci sono e dicono la stessa cosa del manifest."""

    def test_il_codice_e_un_componente_del_gateway(self) -> None:
        """comms-pack 2.1.0: Telegram is a `target: gateway` component, the
        MCP backend is gone (no `.mcp.json` mount left in the plugin)."""
        self.assertTrue(CORE_FILE.is_file(), CORE_FILE)
        self.assertTrue(ENTRYPOINT.is_file(), ENTRYPOINT)
        self.assertFalse(MCP_JSON.exists(), "il backend MCP `telegram` non c'è più")
        (c,) = _manifest().get("components") or []     # one per pack
        self.assertEqual(c["target"], "gateway")
        self.assertEqual(c["entry"], "comms_component")
        self.assertIn("telegram", c["connectors"])
        self.assertIn("telegram_bot_token", c["credentials"])

    def test_il_token_e_quello_dichiarato_dal_connettore(self) -> None:
        """The component reads the vault entry the connector declares, and
        the FIELD the credential form declares (`token`) — not another entry,
        not the bare `value` a `kind: form` credential does not have."""
        credenziale = next(p["credential"] for p in _connector()["providers"]
                           if p.get("credential"))
        voce = credenziale["vault_entry"]
        campi = {c["name"] for c in credenziale.get("fields") or []}
        testo = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertIn(f'CREDENTIAL = "{voce}"', testo)
        self.assertIn("token", campi)
        self.assertIn('bundle().get("token")', testo)
        (comp,) = _manifest()["components"]
        self.assertIn(voce, comp["credentials"])

    def test_ogni_verbo_promesso_dal_manifest_esiste_nel_server(self) -> None:
        """Il difetto silenzioso: una mappatura di egress su un verbo che
        nessuno implementa non dà errore, non si applica mai — e un invio senza
        mappatura è un invio che il PDP non controlla."""
        modulo = _core()
        implementati = {t["name"] for t in modulo.TOOL_SPECS}
        c = _connector()
        promessi = {str(v["verb"]) for v in c.get("verbs") or []}
        promessi |= {str(t) for t in c.get("taint") or []}
        card = c.get("tools_card") or {}
        for chiave in ("status_verb", "test_verb"):
            if card.get(chiave):
                promessi.add(str(card[chiave]))
        prefisso = f"{c['namespace']}."
        for verbo in sorted(promessi):
            with self.subTest(verbo=verbo):
                self.assertTrue(verbo.startswith(prefisso), verbo)
                nome = verbo[len(prefisso):]
                # `taint:` nomina letture che oggi NON sono tool (inbox lo è,
                # receive/pull sono la forma in cui arrivavano dal relay): si
                # verifica solo ciò che il manifest promette come VERBO.
                if verbo in {str(v["verb"]) for v in c.get("verbs") or []} \
                        or card.get("status_verb") == verbo:
                    self.assertIn(nome, implementati)

    def test_il_contesto_e_scritto_nella_grammatica_del_gateway(self) -> None:
        """`context:` è una LISTA di righe `{arg, value}`.

        Non è stile: è la forma che `manifest._parse_context` del gateway sa
        leggere. Scritto come mapping (`channel: channel`) il connettore non
        entra nel registry — il pack si installa, il blocco resta inerte, e il
        sintomo è un backend che riceve il contesto *del chiamante*. Qui si
        confrontano le chiavi di ogni riga, non solo il tipo del blocco.
        """
        righe = _connector().get("context")
        self.assertIsInstance(righe, list, righe)
        self.assertTrue(righe, "il connettore non dichiara più `context:`")
        for riga in righe:
            with self.subTest(riga=riga):
                self.assertIsInstance(riga, dict, riga)
                self.assertEqual(set(riga), {"arg", "value"}, riga)
                self.assertIn(riga["value"], ("channel", "agent"), riga)

    def test_ogni_tool_accetta_il_contesto_dichiarato(self) -> None:
        """Il contesto è per NAMESPACE: il gateway lo riempie su ogni verbo del
        connettore, quindi ogni tool del server deve dichiararlo nel proprio
        schema. Un tool che non lo fa riceve un argomento che non conosce."""
        modulo = _core()
        attesi = _argomenti_di_contesto()
        self.assertTrue(attesi, "il connettore non dichiara più `context:`")
        for tool in modulo.TOOL_SPECS:
            with self.subTest(tool=tool["name"]):
                props = set((tool["inputSchema"] or {}).get("properties") or {})
                self.assertTrue(attesi <= props, sorted(attesi - props))

    def test_il_contesto_non_e_obbligatorio_nello_schema(self) -> None:
        """Lo mette il gateway, non il chiamante: pretenderlo `required` farebbe
        fallire la validazione del client prima che il gateway lo scriva."""
        modulo = _core()
        attesi = _argomenti_di_contesto()
        for tool in modulo.TOOL_SPECS:
            with self.subTest(tool=tool["name"]):
                richiesti = set((tool["inputSchema"] or {}).get("required") or [])
                self.assertEqual(attesi & richiesti, set())


    def test_l_involucro_non_ridichiara_la_superficie(self) -> None:
        """L'involucro MCP deve essere una traduzione, non una seconda tabella.

        Due elenchi di verbi — uno qui e uno nel core — divergerebbero al primo
        tool aggiunto, e il sintomo sarebbe un verbo che esiste nel manifest,
        esiste nel codice e non compare al gateway. Se `mcp` è importabile in
        questo ambiente si confrontano i nomi per davvero; se non lo è (o è di
        una major diversa da quella dell'immagine) resta il controllo statico,
        che è comunque sulla sorgente e non su un commento.
        """
        sorgente = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertIn("core.TOOL_SPECS", sorgente)
        self.assertIn("core.DISPATCH", sorgente)
        # Every TOOL_SPEC is either an agent verb or one of the relay's two
        # services — nothing declared and silently not served.
        spec = importlib.util.spec_from_file_location("telegram_component_src", ENTRYPOINT)
        testo = ENTRYPOINT.read_text(encoding="utf-8")
        agent = set(re.findall(r'"([a-z_]+)"', testo.split("AGENT_VERBS = (", 1)[1].split(")", 1)[0]))
        servizi = {"updates", "download"}
        self.assertEqual(agent | servizi, {s["name"] for s in _core().TOOL_SPECS})
        self.assertEqual(agent & servizi, set())


    def test_il_manifest_dichiara_la_chat_come_default(self) -> None:
        """Il gateway riempie un argomento OMESSO solo se il manifest lo
        dichiara: senza questo blocco, `telegram.send` senza `chat_id`
        arriverebbe al backend senza chat e il PDP calcolerebbe una
        destinazione inesistente."""
        decls = {(d["verb"], d["arg"]): d["value"]
                 for d in _connector().get("defaults") or []}
        self.assertEqual(decls.get(("telegram.send", "chat_id")), "binding")
        self.assertEqual(decls.get(("telegram.send_file", "chat_id")), "binding")



class ILeaseSonoDellAgenteCheIlGatewayDichiaraTests(unittest.TestCase):
    """Il comportamento che dipende dal contesto, con l'API finta."""

    def setUp(self) -> None:
        self.modulo = _core()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.stato = base / "telegram-state.json"
        p = patch.dict(os.environ, {
            "CLODIA_TELEGRAM_STATE": str(self.stato),
            "TELEGRAM_BOT_TOKEN": "123:FINTO",
        })
        p.start()
        self.addCleanup(p.stop)
        self.chiamate: list = []

    def _api(self, risposte: dict):
        def fake(token, method, params=None, timeout=15):
            self.chiamate.append((method, params))
            return risposte.get(method, [])
        return patch.object(self.modulo, "api_call", fake)

    def _chat_nota(self, cid: str = "-100123") -> None:
        self.stato.write_text(json.dumps({
            "offset": 0, "bot_username": "clodiabot", "last_error": None,
            "chats": {cid: {"title": "Gruppo", "queue": [], "lease": None,
                            "last_preview": "", "type": "supergroup"}}}),
            encoding="utf-8")

    def test_il_lease_porta_il_nome_che_mette_il_gateway(self) -> None:
        self._chat_nota()
        with self._api({}):
            out = self.modulo.lease_acquire(
                {"chat_id": "-100123", "agent": "messaggero",
                 "channel": "SEAL-1/software-house"})
        self.assertEqual(out["holder"], "messaggero")
        stato = json.loads(self.stato.read_text(encoding="utf-8"))
        self.assertEqual(stato["chats"]["-100123"]["lease"]["holder"], "messaggero")

    def test_senza_agente_il_lease_si_rifiuta(self) -> None:
        """Un lease intestato alla stringa vuota non lo rilascia più nessuno, e
        «non so chi sei» non può valere come «vai pure»."""
        self._chat_nota()
        with self._api({}):
            with self.assertRaises(ValueError):
                self.modulo.lease_acquire({"chat_id": "-100123"})

    def test_consumare_senza_lease_e_rifiutato(self) -> None:
        self._chat_nota()
        with self._api({}):
            self.modulo.lease_acquire({"chat_id": "-100123", "agent": "clodia"})
            with self.assertRaises(PermissionError):
                self.modulo.poll({"chat_id": "-100123", "agent": "messaggero"})

    def test_la_chat_omessa_la_mette_il_gateway(self) -> None:
        """Il pack NON legge il registro dei binding: lo tiene il gateway, che
        riempie `chat_id` quando il chiamante lo omette (blocco `defaults:`).
        Arrivare qui senza chat significa che la chiamata non è passata di là —
        e allora si rifiuta, invece di indovinare."""
        with self._api({"sendMessage": {"message_id": 7}}):
            with self.assertRaises(ValueError):
                self.modulo.send({"text": "ciao", "agent": "messaggero",
                                  "channel": "SEAL-1/software-house"})
        self.assertEqual(self.chiamate, [])

    def test_con_la_chat_riempita_dal_gateway_si_spedisce(self) -> None:
        with self._api({"sendMessage": {"message_id": 7}}):
            out = self.modulo.send({"text": "ciao", "chat_id": "-100999",
                                    "agent": "messaggero",
                                    "channel": "SEAL-1/software-house"})
        self.assertEqual(out["chat_id"], "-100999")
        self.assertEqual(self.chiamate[0][1]["chat_id"], -100999)

    def test_il_pack_non_apre_il_registro_dei_binding(self) -> None:
        """La prova che la scorciatoia è sparita, e non solo commentata: il
        sorgente non nomina più né il file del registro né la variabile che lo
        indicava. Leggerlo significherebbe leggere i binding di TUTTI i
        connettori, con l'uid del gateway — ciò che clodia-platform#544 chiude."""
        sorgente = CORE_FILE.read_text(encoding="utf-8")
        for traccia in ("channel-bindings", "CLODIA_CHANNEL_BINDINGS",
                        "CLODIA_DATA"):
            with self.subTest(traccia=traccia):
                self.assertNotIn(traccia, sorgente)

    def test_il_token_non_passa_mai_dagli_argomenti(self) -> None:
        """Il bot con cui si spedisce è quello dell'istanza, non quello che il
        chiamante allega alla chiamata."""
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            with self.assertRaises(RuntimeError):
                self.modulo._token()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "vero"}):
            self.assertEqual(self.modulo._token(), "vero")

    def test_send_file_vuole_i_byte_dal_gateway(self) -> None:
        """Il pack non legge i file dei topic: lì stanno il compartimento e
        l'audit di ciò che attraversa il confine."""
        with self._api({}):
            with self.assertRaises(ValueError):
                self.modulo.send_file({"chat_id": "-100123", "filename": "x.pdf",
                                       "content_b64": ""})


class IlFileDiTopicLoConsegnaIlGatewayTests(unittest.TestCase):
    """`send_file` chiede un `path`; i byte li mette il gateway.

    Leggere un file di topic è lavoro di piattaforma — compartimento e audit di
    ciò che attraversa il confine — e resta di là anche ora che
    l'implementazione è di qua (clodia-tools, blocco `files:` del manifest).
    """

    def setUp(self) -> None:
        self.modulo = _core()
        self.decl = (_connector().get("files") or [{}])[0]

    def test_il_manifest_dichiara_il_file_di_send_file(self) -> None:
        self.assertEqual(self.decl.get("verb"), "telegram.send_file")
        self.assertEqual(self.decl.get("path_arg"), "path")
        self.assertEqual(self.decl.get("bytes_arg"), "content_b64")

    def test_lo_schema_chiede_il_path_e_non_i_byte(self) -> None:
        """L'argomento dell'AGENTE è il percorso: se lo schema pretendesse i
        byte, l'agente dovrebbe leggere il file da sé — cioè avere i verbi di
        lettura dei topic che il messaggero non ha, e aggirare il
        compartimento."""
        tool = next(t for t in self.modulo.TOOL_SPECS if t["name"] == "send_file")
        schema = tool["inputSchema"]
        self.assertEqual(schema["required"], [self.decl["path_arg"]])
        for arg in ("bytes_arg", "name_arg", "tier_arg", "topic_arg", "chat_arg"):
            with self.subTest(arg=arg):
                self.assertIn(self.decl[arg], schema["properties"])

    def test_senza_consegna_non_si_spedisce_un_allegato_vuoto(self) -> None:
        """Gli argomenti dei byte li riempie il gateway: vuoti significano che
        la consegna non è avvenuta, non che il file era vuoto."""
        with self.assertRaises(ValueError):
            self.modulo.send_file({"chat_id": "-100", "path": "local/x"})


class IlRelayRiconosceUnJoinTests(unittest.TestCase):
    """Cosa `updates` deve riportare perché il gateway sappia fare il suo pezzo."""

    def setUp(self) -> None:
        self.modulo = _core()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.dict(os.environ, {
            "CLODIA_TELEGRAM_STATE": str(Path(self.tmp.name) / "stato.json"),
            "TELEGRAM_BOT_TOKEN": "123:FINTO"})
        p.start()
        self.addCleanup(p.stop)

    def test_la_riga_porta_il_tipo_della_chat_e_la_risposta_il_bot(self) -> None:
        """Il join per link (`/start <token>`) lo intercetta il GATEWAY, che è
        l'unica porta da cui gli update entrano: `claim` accetta solo i gruppi,
        quindi senza il tipo un join in un gruppo verrebbe scartato e finirebbe
        nel topic come un messaggio qualunque. `bot_username` serve a
        riconoscere `/start@bot` e viaggia con la risposta per non costare una
        seconda chiamata a ogni poll."""
        upd = [{"update_id": 5, "message": {
            "message_id": 1, "text": "/start abcdefghijklmnop",
            "chat": {"id": -100, "title": "Gruppo", "type": "supergroup"},
            "from": {"id": 7, "username": "dadabit"}}}]
        Path(os.environ["CLODIA_TELEGRAM_STATE"]).write_text(json.dumps(
            {"offset": 0, "bot_username": "clodiabot", "chats": {}}), encoding="utf-8")
        with patch.object(self.modulo, "api_call", lambda *a, **k: upd):
            out = self.modulo.updates({"timeout": 0})
        self.assertEqual(out["bot_username"], "clodiabot")
        self.assertEqual(out["messages"][0]["chat_type"], "supergroup")
        self.assertEqual(out["messages"][0]["from_username"], "dadabit")



class IVerbiInterniNonSonoDellAgenteTests(unittest.TestCase):
    """`updates`/`download` sono la porta del relay, non verbi d'agente."""

    def test_chi_ha_il_namespace_li_ha_negati(self) -> None:
        """Un grant `telegram.*` senza una negazione esplicita raggiungerebbe
        `updates`/`download`, e un `getUpdates` dell'agente svuoterebbe la coda
        che il relay deve leggere — nessun errore, solo messaggi che non
        arrivano più nel topic. Il messaggero, che aveva quel grant, è stato
        ritirato (clodia-platform#531): vale per qualunque seed lo riprenda."""
        for f in sorted(SEEDS.glob("*/agent.yaml")):
            seed = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            grants = {str(x) for x in seed.get("tool_permissions") or []}
            if not grants & {"telegram.*", "*"}:
                continue
            negati = {str(x) for x in (seed.get("denied_tools") or [])}
            with self.subTest(seed=f.parent.name):
                self.assertIn("telegram.updates", negati)
                self.assertIn("telegram.download", negati)

    def test_il_manifest_non_li_dichiara_come_verbi(self) -> None:
        """Non sono destinazioni né letture da PDP: sono il trasporto. Se
        comparissero fra i `verbs:` sarebbero verbi d'agente a tutti gli
        effetti, concedibili a chiunque abbia il namespace."""
        c = _connector()
        dichiarati = {str(v["verb"]) for v in c.get("verbs") or []}
        self.assertNotIn("telegram.updates", dichiarati)
        self.assertNotIn("telegram.download", dichiarati)


if __name__ == "__main__":
    unittest.main()
