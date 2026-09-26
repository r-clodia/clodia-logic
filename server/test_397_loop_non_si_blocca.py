"""Nessuna coroutine fa I/O sincrono sul thread dell'event loop (#397 §1).

`core/loop_lag.py` misura i blocchi del loop e il 26 set 2026 ne ha registrati
quattro da 45-46s di fila. Un blocco così non è «carico»: è una chiamata
sincrona eseguita sullo stesso thread, che per la sua durata ferma OGNI timer
asincrono del processo — watchdog di turno e timeout di raccolta compresi, cioè
proprio i meccanismi che dovrebbero accorgersi del guasto.

Questo modulo non misura: **impedisce**. Cammina l'AST del server e fallisce se
una coroutine chiama direttamente una funzione bloccante nota. È il controllo
che, il giorno in cui qualcuno ne reintroduce una, diventa rosso prima del
deploy invece di diventare una notte di log persi.

SHORTCUT: guarda le chiamate DIRETTE dentro una coroutine, più una lista
          nominata di helper sincroni del repo già noti come bloccanti. Un
          terzo livello (helper che chiama helper che fa rete) gli sfugge.
          Regge perché i casi veri trovati finora sono tutti di primo livello;
          quando non basterà, la salita è costruire il grafo delle chiamate
          per modulo e propagare il flag «bloccante» sui sync raggiungibili.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest import TestCase

SERVER = Path(__file__).resolve().parent

#: Moduli il cui uso sincrono blocca il thread: rete o attesa di un processo.
_BLOCKING_ATTR = {
    "requests": None,                     # qualunque verbo HTTP sincrono
    "subprocess": {"run", "call", "check_call", "check_output", "Popen.wait"},
    "time": {"sleep"},
    "socket": {"create_connection"},
}
#: Funzioni bloccanti riconosciute dal solo nome finale.
_BLOCKING_NAMES = {"urlopen", "urlretrieve"}
#: Helper SINCRONI di questo repo che fanno I/O di rete o subprocess al loro
#: interno: chiamarli da una coroutine blocca il loop esattamente come farlo
#: in linea. Si aggiungono qui quando se ne scopre uno, non si tolgono.
_BLOCKING_HELPERS = {"rebuild_topic_index"}

#: Eccezioni consentite, con la ragione. Vuota di proposito: se ne serve una,
#: la si scrive qui insieme al motivo per cui quel blocco è accettabile.
_ALLOWED: dict[tuple[str, str, str], str] = {}


def _is_blocking(name: str) -> bool:
    ultimo = name.split(".")[-1]
    if ultimo in _BLOCKING_NAMES or ultimo in _BLOCKING_HELPERS:
        return True
    radice = name.split(".")[0]
    consentiti = _BLOCKING_ATTR.get(radice, "assente")
    if consentiti == "assente":
        return False
    return consentiti is None or ultimo in consentiti


def _offenders(path: Path) -> list[tuple[str, str, int]]:
    """(coroutine, chiamata, riga) per ogni chiamata bloccante diretta.

    Una `def` o una `lambda` annidata dentro la coroutine NON conta: è
    esattamente la forma che `asyncio.to_thread` esegue fuori dal loop.
    """
    try:
        albero = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
        return []
    trovati: list[tuple[str, str, int]] = []

    class Visitatore(ast.NodeVisitor):
        def __init__(self) -> None:
            self.dentro: list[tuple[bool, str]] = []

        def _scendi(self, nodo, asincrona: bool, nome: str) -> None:
            self.dentro.append((asincrona, nome))
            self.generic_visit(nodo)
            self.dentro.pop()

        def visit_AsyncFunctionDef(self, nodo):  # noqa: N802
            self._scendi(nodo, True, nodo.name)

        def visit_FunctionDef(self, nodo):  # noqa: N802
            self._scendi(nodo, False, nodo.name)

        def visit_Lambda(self, nodo):  # noqa: N802
            self._scendi(nodo, False, "<lambda>")

        def visit_Call(self, nodo):  # noqa: N802
            if self.dentro and self.dentro[-1][0]:
                nome = ast.unparse(nodo.func)
                if _is_blocking(nome):
                    trovati.append((self.dentro[-1][1], nome, nodo.lineno))
            self.generic_visit(nodo)

    Visitatore().visit(albero)
    return trovati


def _moduli() -> list[Path]:
    return [p for p in sorted(SERVER.rglob("*.py"))
            if not p.name.startswith("test_")]


class NienteIoSincronoNelLoopTest(TestCase):
    def test_nessuna_coroutine_blocca_il_thread(self):
        colpe = []
        for path in _moduli():
            rel = path.relative_to(SERVER.parent).as_posix()
            for coroutine, chiamata, riga in _offenders(path):
                if (rel, coroutine, chiamata) in _ALLOWED:
                    continue
                colpe.append(f"{rel}:{riga} — {coroutine}() chiama {chiamata}()")
        self.assertEqual(
            [], colpe,
            "I/O sincrono dentro una coroutine: per la sua durata l'event loop "
            "è fermo, watchdog compreso (#397 §1). Avvolgi in "
            "`await asyncio.to_thread(...)`.\n  " + "\n  ".join(colpe),
        )

    def test_il_controllo_sa_fallire(self):
        # Un controllo che non ha mai visto rosso non dimostra di poterlo
        # diventare: qui la prova, su un sorgente scritto apposta.
        rotto = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        brutto = rotto / "brutto.py"
        brutto.write_text(
            "import requests, subprocess, asyncio, time\n"
            "async def cattiva():\n"
            "    requests.get('http://x')\n"
            "    subprocess.run(['git', 'pull'])\n"
            "    time.sleep(1)\n"
            "async def buona():\n"
            "    await asyncio.to_thread(requests.get, 'http://x')\n"
            "    await asyncio.to_thread(lambda: subprocess.run(['git']))\n"
            "    def dentro():\n"
            "        return requests.get('http://x')\n"
            "    await asyncio.to_thread(dentro)\n"
            "def sincrona():\n"
            "    requests.get('http://x')\n",
            encoding="utf-8",
        )
        trovati = _offenders(brutto)
        self.assertEqual(
            [("cattiva", "requests.get", 3),
             ("cattiva", "subprocess.run", 4),
             ("cattiva", "time.sleep", 5)],
            trovati,
        )

    def test_time_monotonic_non_e_un_blocco(self):
        # `time.time()`/`time.monotonic()` non aspettano nulla: se finissero
        # nella rete del controllo, il rumore lo renderebbe inutile.
        self.assertFalse(_is_blocking("time.monotonic"))
        self.assertFalse(_is_blocking("time.time"))
        self.assertTrue(_is_blocking("time.sleep"))

    def test_gli_helper_bloccanti_noti_sono_sorvegliati(self):
        self.assertTrue(_is_blocking("rebuild_topic_index"))
        self.assertTrue(_is_blocking("topics.rebuild_topic_index"))
