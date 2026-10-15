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
CORE_FILE = PLUGIN / "mcp" / "telegram_core.py"
ENTRYPOINT = PLUGIN / "mcp" / "telegram_mcp.py"
MCP_JSON = PLUGIN / ".mcp.json"
MESSAGGERO = Path(workspace_path(
    "catalogs/packs/base-pack/agents/messaggero/agent.yaml"))


def _manifest() -> dict:
    return yaml.safe_load((PACK / "pack.yaml").read_text(encoding="utf-8")) or {}


def _connector(cid: str = "telegram") -> dict:
    for entry in _manifest().get("connectors") or []:
        if isinstance(entry, dict) and entry.get("id") == cid:
            return entry
    raise AssertionError(f"comms-pack non dichiara più il connettore '{cid}'")


def _core():
    """Il connettore, importato dal file del pack.

    È stdlib pura — l'involucro MCP è un altro file — quindi si importa qui
    senza avere `mcp`, e soprattutto senza avere la STESSA versione di `mcp`
    dell'immagine del gateway: un controllo che dipendesse da quello misurerebbe
    l'ambiente invece del connettore.
    """
    spec = importlib.util.spec_from_file_location("telegram_core_pack", CORE_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IlPackPortaIlCodiceTests(unittest.TestCase):
    """I file ci sono e dicono la stessa cosa del manifest."""

    def test_il_server_e_nel_pack_e_il_mount_lo_punta(self) -> None:
        self.assertTrue(CORE_FILE.is_file(), CORE_FILE)
        self.assertTrue(ENTRYPOINT.is_file(), ENTRYPOINT)
        mount = json.loads(MCP_JSON.read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(list(mount), ["telegram"],
                         "il backend deve chiamarsi come il namespace del "
                         "connettore: con un altro nome i verbi tornano come "
                         "`<altro>.send` e nessuna mappatura li descrive più")
        telegram = mount["telegram"]
        args = [str(x) for x in telegram.get("args") or []]
        self.assertEqual(len(args), 1, args)
        # Il percorso è quello in cui l'import copia `mcp/` del plugin
        # (`plugin_import`): il file deve esistere QUI col nome che sta LÌ.
        self.assertTrue(args[0].endswith(f"/{ENTRYPOINT.name}"), args[0])
        self.assertIn("/plugins/comms-pack/mcp/", args[0])

    def test_il_token_e_quello_dichiarato_dal_connettore(self) -> None:
        """Il mount inietta una credenziale del vault: dev'essere la voce che
        il connettore dichiara, non un'altra. Un placeholder diverso darebbe al
        processo del pack un segreto che il suo manifest non nomina — e che
        nessun controllo di namespace ha mai visto."""
        env = json.loads(MCP_JSON.read_text(encoding="utf-8")) \
            ["mcpServers"]["telegram"].get("env") or {}
        dichiarata = next(p["credential"]["vault_entry"]
                          for p in _connector()["providers"] if p.get("credential"))
        self.assertIn(f"${{VAULT:{dichiarata}}}", env.values())
        for valore in env.values():
            if "VAULT:" in str(valore):
                self.assertIn(f"VAULT:{dichiarata}", str(valore),
                              "il mount inietta una credenziale che il "
                              "connettore non dichiara")

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

    def test_ogni_tool_accetta_il_contesto_dichiarato(self) -> None:
        """Il contesto è per NAMESPACE: il gateway lo riempie su ogni verbo del
        connettore, quindi ogni tool del server deve dichiararlo nel proprio
        schema. Un tool che non lo fa riceve un argomento che non conosce."""
        modulo = _core()
        attesi = set(_connector().get("context") or {})
        self.assertTrue(attesi, "il connettore non dichiara più `context:`")
        for tool in modulo.TOOL_SPECS:
            with self.subTest(tool=tool["name"]):
                props = set((tool["inputSchema"] or {}).get("properties") or {})
                self.assertTrue(attesi <= props, sorted(attesi - props))

    def test_il_contesto_non_e_obbligatorio_nello_schema(self) -> None:
        """Lo mette il gateway, non il chiamante: pretenderlo `required` farebbe
        fallire la validazione del client prima che il gateway lo scriva."""
        modulo = _core()
        attesi = set(_connector().get("context") or {})
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
        try:
            spec = importlib.util.spec_from_file_location("telegram_mcp_pack", ENTRYPOINT)
            modulo = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(modulo)
        except Exception as e:  # noqa: BLE001 — `mcp` assente o di un'altra major
            self.skipTest(f"involucro non importabile qui ({type(e).__name__}): "
                          f"resta il controllo statico")
        self.assertEqual([t.name for t in modulo._TOOLS],
                         [s["name"] for s in _core().TOOL_SPECS])



class ILeaseSonoDellAgenteCheIlGatewayDichiaraTests(unittest.TestCase):
    """Il comportamento che dipende dal contesto, con l'API finta."""

    def setUp(self) -> None:
        self.modulo = _core()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.stato = base / "telegram-state.json"
        self.bindings = base / "channel-bindings.json"
        p = patch.dict(os.environ, {
            "CLODIA_TELEGRAM_STATE": str(self.stato),
            "CLODIA_CHANNEL_BINDINGS": str(self.bindings),
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

    def test_send_senza_chat_usa_la_chat_legata_al_canale(self) -> None:
        """Il caso del 23 set 2026: chi chiama non deve indovinare un nome di
        gruppo. Il canale lo dichiara il gateway, il binding lo scrive il
        gateway — l'agente non sceglie nessuno dei due."""
        self.bindings.write_text(json.dumps({"version": 1, "connectors": {
            "telegram": {"-100999": {"instance": "messaggero-1",
                                     "tier": "SEAL-1", "topic": "software-house"}}}}),
            encoding="utf-8")
        with self._api({"sendMessage": {"message_id": 7}}):
            out = self.modulo.send({"text": "ciao", "agent": "messaggero",
                                    "channel": "SEAL-1/software-house"})
        self.assertEqual(out["chat_id"], "-100999")
        self.assertEqual(self.chiamate[0][1]["chat_id"], -100999)

    def test_send_senza_chat_e_senza_binding_non_indovina(self) -> None:
        self.bindings.write_text(json.dumps({"version": 1, "connectors": {}}),
                                 encoding="utf-8")
        with self._api({"sendMessage": {"message_id": 7}}):
            with self.assertRaises(ValueError):
                self.modulo.send({"text": "ciao", "agent": "messaggero",
                                  "channel": "SEAL-1/altro-topic"})
        self.assertEqual(self.chiamate, [])

    def test_il_binding_di_un_altro_connettore_non_vale(self) -> None:
        """Il registro è neutro e tiene i binding di più connettori: leggere
        quelli di un altro manderebbe il messaggio alla chat sbagliata."""
        self.bindings.write_text(json.dumps({"version": 1, "connectors": {
            "whatsapp": {"-100999": {"instance": "m-1", "tier": "SEAL-1",
                                     "topic": "software-house"}}}}),
            encoding="utf-8")
        with self._api({"sendMessage": {"message_id": 7}}):
            with self.assertRaises(ValueError):
                self.modulo.send({"text": "ciao", "channel": "SEAL-1/software-house"})

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


class IVerbiInterniNonSonoDellAgenteTests(unittest.TestCase):
    """`updates`/`download` sono la porta del relay, non verbi d'agente."""

    def test_il_messaggero_li_ha_negati(self) -> None:
        """Il suo grant è `telegram.*`: senza una negazione esplicita il
        wildcard li raggiungerebbe, e un `getUpdates` dell'agente svuoterebbe la
        coda che il relay deve leggere — nessun errore, solo messaggi che non
        arrivano più nel topic."""
        seed = yaml.safe_load(MESSAGGERO.read_text(encoding="utf-8")) or {}
        negati = {str(x) for x in (seed.get("denied_tools") or [])}
        self.assertIn("telegram.*", {str(x) for x in seed.get("tool_permissions") or []})
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
