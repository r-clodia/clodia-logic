"""Nessun client HTTP bloccante dentro un handler `async def` (#106).

Il difetto non vive in una funzione: vive nella COMBINAZIONE fra un client
sincrono (`requests`, timeout fino a 30s) e un chiamante `async def`. Ogni
chiamata cosi' fatta ferma l'event loop dell'agent-server per tutta la durata
della richiesta al gateway: health check, SSE e ogni altra API si accodano, e il
sistema appare morto pur essendo vivo. E' l'incidente del 17 lug, due blocchi
totali dello stack personal.

Un test per singolo call-site sarebbe inutile: sono oltre cento e ne nasce uno
ogni volta che qualcuno scrive un endpoint nuovo. Qui si asserisce sull'INVENTARIO
— quanti ne restano, e dove — cosi' che:

  * chi ne aggiunge uno nuovo vede il test diventare rosso subito;
  * chi ne migra un blocco e' costretto ad abbassare il budget, cioe' a
    dichiarare il progresso invece di lasciarlo implicito.

Il budget e' un debito residuo, non un permesso: scende e basta. Quando arriva a
zero, questo file diventa un semplice `assertEqual(inventario, {})` e l'issue si
chiude.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

#: Moduli client che parlano col gateway via `requests` (bloccanti per costruzione).
BLOCKING_MODULES = frozenset({
    "topics_client", "provider_store", "gateway_admin", "git_client",
    "connectors_client", "imagegen_client", "telegram_client", "gateway_pdp",
    "agent_registry", "telegram_bindings_client",
})

#: Nomi importati direttamente (`from .gateway_pdp import require_authz`): senza
#: questi l'inventario sottostima, ed e' esattamente il caso che ha nascosto due
#: call-site in channels.py.
BLOCKING_NAMES = frozenset({"require_authz", "gw_authorize", "gw_tool", "forward"})

#: Debito residuo per file, misurato su main. SOLO IN DISCESA.
#: Migrando un call-site si scrive `await <client>.<verbo>_async(...)` e si
#: abbassa il numero qui sotto; un file che arriva a zero si toglie dal dizionario.
BUDGET: dict[str, int] = {
    "server/api/channels.py": 65,
    "server/api/topics.py": 13,
    "server/api/packs.py": 1,          # gateway_admin.flow_allow
    "server/api/channel_relay.py": 6,
    "server/hooks/api.py": 5,
    "server/api/connectors.py": 2,
    "server/api/topic_signals.py": 1,
}

_ROOT = pathlib.Path(__file__).resolve().parents[2]


class _Audit(ast.NodeVisitor):
    """Raccoglie le chiamate bloccanti che stanno DENTRO un `async def`.

    Lo stack tiene conto delle funzioni annidate: una `def` sincrona definita
    dentro una coroutine non blocca il loop finche' e' il loop a non eseguirla,
    e va contata dove viene chiamata, non dove e' scritta.
    """

    def __init__(self) -> None:
        self.stack: list[bool] = []
        self.found: list[tuple[int, str]] = []

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.stack.append(True)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(False)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.stack.append(False)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if self.stack and self.stack[-1]:
            name = self._blocking_name(node.func)
            if name:
                self.found.append((node.lineno, name))
        self.generic_visit(node)

    @staticmethod
    def _blocking_name(func: ast.expr) -> str | None:
        if isinstance(func, ast.Attribute):
            owner = func.value
            if isinstance(owner, ast.Name) and owner.id in BLOCKING_MODULES:
                attr = func.attr
                if attr.endswith("_async") or attr.startswith("async_"):
                    return None  # gia' migrato
                return f"{owner.id}.{attr}"
            return None
        if isinstance(func, ast.Name) and func.id in BLOCKING_NAMES:
            return func.id
        return None


def _inventory() -> dict[str, list[tuple[int, str]]]:
    out: dict[str, list[tuple[int, str]]] = {}
    for path in sorted((_ROOT / "server").rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — file non parsabile, non e' compito nostro
            continue
        audit = _Audit()
        audit.visit(tree)
        if audit.found:
            out[path.relative_to(_ROOT).as_posix()] = audit.found
    return out


class NoBlockingCallsInAsyncTests(unittest.TestCase):
    def test_residual_debt_matches_the_declared_budget(self):
        found = _inventory()
        counts = {f: len(v) for f, v in found.items()}

        nuovi = sorted(set(counts) - set(BUDGET))
        self.assertEqual(
            nuovi, [],
            "file con chiamate bloccanti in handler async non previsti dal "
            f"budget: {nuovi}. Usa `await <client>.<verbo>_async(...)`.")

        for file, atteso in BUDGET.items():
            reale = counts.get(file, 0)
            self.assertLessEqual(
                reale, atteso,
                f"{file}: {reale} chiamate bloccanti in handler async, il budget "
                f"ne ammette {atteso}. Ogni nuova chiamata sincrona dentro un "
                "`async def` blocca l'event loop dell'intero agent-server (#106).")
            self.assertEqual(
                reale, atteso,
                f"{file}: {reale} chiamate bloccanti, budget {atteso}. Hai "
                "migrato dei call-site: abbassa il budget in BUDGET, altrimenti "
                "il debito residuo smette di dire la verita'.")

    def test_the_authz_guard_no_longer_blocks_the_loop(self):
        """Il pezzo migrato da questa PR, verificato per nome e non per conteggio:
        un budget che scende potrebbe farlo per la ragione sbagliata."""
        found = _inventory()
        colpevoli = [
            (file, line, name)
            for file, items in found.items()
            for line, name in items
            if name in {"require_authz", "gateway_pdp.require_authz"}
        ]
        self.assertEqual(colpevoli, [],
                         "require_authz e' tornato dentro un handler async: "
                         "usa require_authz_async")


if __name__ == "__main__":
    unittest.main()
