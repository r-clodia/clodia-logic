"""Nessun client HTTP sincrono dentro un handler `async def` (#106).

Incidente del 17 lug: due blocchi totali dello stack. I client interni
(`topics_client`, `gateway_pdp`, `provider_store`, …) parlano col gateway con
`requests`, che è bloccante; chiamati dritti da un handler `async def` fermano
l'**event loop** dell'agent-server — non solo quella richiesta. Basta un gateway
lento perché health check, SSE e tutte le API si accodino: il processo è vivo e
il sistema sembra morto.

Questo test è il guard-rail, non la cura: percorre l'AST di tutto `server/`,
trova le funzioni che fanno HTTP sincrono e verifica che nessuna sia chiamata
dentro un `async def`. Dalla parte async si passa dai wrapper (`async_*` /
`*_async`) che spostano l'attesa in un thread.

Quando fallisce, il rimedio non è aggiungere un'eccezione qui: è chiamare il
wrapper async — o crearlo, se manca.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

SERVER = pathlib.Path(__file__).resolve().parent.parent


def _moduli() -> list[pathlib.Path]:
    return [p for p in sorted(SERVER.rglob("*.py")) if not p.name.startswith("test_")]


def _funzioni_bloccanti(tree: ast.Module) -> set[str]:
    """Le funzioni sincrone del modulo che finiscono in una chiamata HTTP.

    Bloccante è chi tocca `requests.*` o il proxy `GatewayHTTP` del modulo, più —
    per chiusura transitiva — chi chiama una di quelle: un helper che avvolge la
    POST blocca esattamente quanto la POST.
    """
    importa_requests = any(
        (isinstance(n, ast.Import) and any(a.name.split(".")[0] == "requests" for a in n.names))
        or (isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "requests")
        for n in ast.walk(tree))
    if not importa_requests:
        return set()

    proxy = {"requests"} | {
        t.id
        for n in tree.body if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name) and isinstance(n.value, ast.Call)
        and getattr(n.value.func, "id", None) == "GatewayHTTP"}

    funzioni = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    bloccanti = {
        nome for nome, node in funzioni.items()
        if any(isinstance(s, ast.Attribute) and isinstance(s.value, ast.Name)
               and s.value.id in proxy for s in ast.walk(node))}

    cambiato = True
    while cambiato:  # SHORTCUT: punto fisso ingenuo, O(funzioni²) sul modulo
        cambiato = False  #          più grande (~30 funzioni): sotto il ms.
        for nome, node in funzioni.items():
            if nome in bloccanti:
                continue
            for s in ast.walk(node):
                if isinstance(s, ast.Call):
                    f = s.func
                    chiamata = (f.id if isinstance(f, ast.Name)
                                else f.attr if isinstance(f, ast.Attribute) else None)
                    if chiamata in bloccanti:
                        bloccanti.add(nome)
                        cambiato = True
                        break
    return bloccanti


class _CercaChiamate(ast.NodeVisitor):
    """Raccoglie le chiamate bloccanti che stanno nel corpo di un `async def`."""

    def __init__(self, alias: dict[str, str], diretti: dict[str, tuple[str, str]],
                 registro: dict[str, set[str]], helper: set[str]):
        self.alias, self.diretti, self.registro = alias, diretti, registro
        self.helper = helper
        self.trovate: list[tuple[int, str]] = []
        self.dentro_async = 0

    def visit_FunctionDef(self, node):
        # Una `def` sincrona annidata dentro un async def non gira sull'event
        # loop: è il bersaglio tipico di `asyncio.to_thread`.
        if not self.dentro_async:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self.dentro_async += 1
        for figlio in node.body:
            self.generic_visit(figlio)
        self.dentro_async -= 1

    def visit_Call(self, node):
        if self.dentro_async:
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                modulo = self.alias.get(f.value.id)
                if modulo and f.attr in self.registro.get(modulo, ()):
                    self.trovate.append((node.lineno, f"{modulo}.{f.attr}"))
            elif isinstance(f, ast.Name) and f.id in self.diretti:
                modulo, funzione = self.diretti[f.id]
                self.trovate.append((node.lineno, f"{modulo}.{funzione}"))
            elif isinstance(f, ast.Name) and f.id in self.helper:
                # Un helper sincrono del modulo che finisce in una chiamata al
                # gateway: blocca l'event loop esattamente come la chiamata che
                # avvolge, solo un salto più in là.
                self.trovate.append((node.lineno, f"{f.id}() [helper sincrono]"))
        self.generic_visit(node)


def _importazioni(tree: ast.Module, registro: dict[str, set[str]]):
    """(alias di modulo, nomi importati direttamente) usati nel file."""
    alias: dict[str, str] = {}
    diretti: dict[str, tuple[str, str]] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name in registro:                      # from . import topics_client
                    alias[a.asname or a.name] = a.name
                else:                                       # from .gateway_pdp import require_authz
                    modulo = (n.module or "").split(".")[-1]
                    if modulo in registro and a.name in registro[modulo]:
                        diretti[a.asname or a.name] = (modulo, a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                modulo = a.name.split(".")[-1]
                if modulo in registro:
                    alias[a.asname or modulo] = modulo
    return alias, diretti


def _helper_sincroni(tree: ast.Module, alias: dict[str, str], diretti: dict,
                     registro: dict[str, set[str]]) -> set[str]:
    """Le `def` sincrone DEL MODULO che finiscono in una chiamata al gateway.

    Il difetto non sparisce mettendo una funzione in mezzo: `_topic_title()` fa
    una `open_topic` sincrona, e chiamarla da un handler async blocca il loop
    quanto la `open_topic` scritta a mano.
    """
    funzioni = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def tocca_il_gateway(node) -> bool:
        for s in ast.walk(node):
            if not isinstance(s, ast.Call):
                continue
            f = s.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                modulo = alias.get(f.value.id)
                if modulo and f.attr in registro.get(modulo, ()):
                    return True
            elif isinstance(f, ast.Name) and f.id in diretti:
                return True
        return False

    helper = {n for n, node in funzioni.items() if tocca_il_gateway(node)}
    cambiato = True
    while cambiato:
        cambiato = False
        for n, node in funzioni.items():
            if n in helper:
                continue
            for s in ast.walk(node):
                if isinstance(s, ast.Call) and isinstance(s.func, ast.Name) and s.func.id in helper:
                    helper.add(n)
                    cambiato = True
                    break
    return helper


def chiamate_bloccanti() -> list[str]:
    """`file:riga  modulo.funzione` per ogni chiamata sincrona in un async def."""
    alberi = {p: ast.parse(p.read_text()) for p in _moduli()}
    registro = {p.stem: b for p, tree in alberi.items() if (b := _funzioni_bloccanti(tree))}

    trovate: list[str] = []
    for p, tree in alberi.items():
        alias, diretti = _importazioni(tree, registro)
        helper = _helper_sincroni(tree, alias, diretti, registro)
        cerca = _CercaChiamate(alias, diretti, registro, helper)
        cerca.visit(tree)
        trovate += [f"{p.relative_to(SERVER.parent)}:{riga}  {nome}"
                    for riga, nome in cerca.trovate]
    return trovate


class NessunHTTPSincronoNegliHandlerAsyncTests(unittest.TestCase):
    def test_il_registro_dei_client_bloccanti_non_e_vuoto(self):
        """Se l'euristica smettesse di riconoscere i client, il test principale
        passerebbe sempre — verde per cecità, non per assenza di difetti."""
        alberi = {p: ast.parse(p.read_text()) for p in _moduli()}
        registro = {p.stem: b for p, tree in alberi.items() if (b := _funzioni_bloccanti(tree))}
        self.assertIn("topics_client", registro)
        self.assertIn("open_topic", registro["topics_client"])
        self.assertIn("post_message", registro["topics_client"])
        self.assertIn("require_authz", registro.get("gateway_pdp", set()))

    def test_riconosce_anche_gli_helper_sincroni_del_modulo(self):
        """L'altra metà della copertura: il difetto a un salto di distanza.

        Su `main` prima del fix erano 152 chiamate in tutto, 38 delle quali
        passavano da un helper sincrono del modulo — invisibili a un `grep` sui
        nomi dei client."""
        canali = SERVER / "api" / "channels.py"
        tree = ast.parse(canali.read_text())
        alberi = {p: ast.parse(p.read_text()) for p in _moduli()}
        registro = {p.stem: b for p, t in alberi.items() if (b := _funzioni_bloccanti(t))}
        alias, diretti = _importazioni(tree, registro)
        helper = _helper_sincroni(tree, alias, diretti, registro)
        self.assertIn("_topic_title", helper)       # → topics_client.open_topic
        self.assertIn("_topic_agents_md", helper)   # → topics_client.get_agents_md

    def test_nessun_client_sincrono_dentro_un_handler_async(self):
        trovate = chiamate_bloccanti()
        self.assertEqual(
            trovate, [],
            "chiamate HTTP sincrone dentro un `async def`: bloccano l'event loop "
            "di tutto il processo. Usa il wrapper async del client "
            "(`topics_client.async_*`, `gateway_pdp.require_authz_async`, …) e "
            "mettilo in await.\n  " + "\n  ".join(trovate))


if __name__ == "__main__":
    unittest.main()
