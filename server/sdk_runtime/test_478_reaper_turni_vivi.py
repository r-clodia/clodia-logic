"""clodia-platform#478 — il reaper non tocca il processo di un turno vivo.

L'1 ott 2026 il CLI di `clodia-365` è stato terminato con SIGTERM mentre il
turno era in corso (query inviata alle 09:09:44, kill alle 09:16:01, exit 143 e
nessuna risposta in chat). Il bersaglio era un processo VIVO: la sola prova di
proprietà che il reaper usava era la cwd risolta da `/proc`, e quando quella
inferenza non combacia col set `live_cwds` una sessione viva diventa un orfano.

Qui si prova il contrario su tre fronti:
- il pid dichiarato da una sessione viva non è un bersaglio, a nessuna età e
  qualunque cosa risolva la sua cwd (il caso dell'incidente);
- una cwd illeggibile non è una condanna (scelta prudente: meglio un orfano vero
  che sopravvive fino al restart che un secondo #478);
- il tick in `main.py` passa davvero i pid protetti — se qualcuno rimuove quel
  parametro il reaper torna a decidere sulla sola cwd e il difetto riapre.
"""
from __future__ import annotations

import ast
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from . import process_reaper
from .session import ChatManager, runtime_process_of


def _proc_dir(root: Path, pid: int, ppid: int, started: int, cwd: Path | None,
              command: str) -> None:
    d = root / str(pid)
    d.mkdir()
    # stat_tail: ppid in posizione 1, starttime in 19.
    tail = ["S", str(ppid)] + ["0"] * 17 + [str(started)]
    (d / "stat").write_text(f"{pid} (runtime worker) " + " ".join(tail))
    (d / "statm").write_text("100 10")
    (d / "cmdline").write_bytes(command.replace(" ", "\0").encode() + b"\0")
    if cwd is not None:
        (d / "cwd").symlink_to(cwd)
    else:
        # cwd che non risolve: è ciò che `/proc` mostra quando la dir di lavoro
        # è stata cancellata o il link non è leggibile → `RuntimeProcess.cwd=None`.
        (d / "cwd").symlink_to(root / "sparita")


class TurnoVivoNonUcciso(unittest.TestCase):
    def test_pid_di_sessione_viva_non_e_un_bersaglio(self):
        """L'incidente: processo vecchio, cwd non nel live set, ma turno in corso."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "uptime").write_text("10000 0")
            altro = root / "altro"
            altro.mkdir()
            # 2 = il CLI della sessione viva: età oltre il TTL e cwd che NON
            # combacia con nessuna voce di live_cwds (la divergenza di #478).
            _proc_dir(root, 2, 1, 100, altro, "/usr/bin/claude chat")
            # 3 = suo figlio: protetto per eredità, uccidere un figlio del turno
            # è lo stesso incidente.
            _proc_dir(root, 3, 2, 100, altro, "/usr/bin/claude mcp")
            killed: list[int] = []

            stats = process_reaper.sweep_orphan_runtime_processes(
                set(), 100, protected_pids={2}, proc_root=root, root_pid=1,
                kill=lambda pid, sig: killed.append(pid),
            )

            self.assertEqual(killed, [])
            self.assertEqual(stats["orphan_processes"], 0)
            self.assertEqual(stats["protected_processes"], 2)

    def test_orfano_vero_resta_un_bersaglio(self):
        """La protezione per pid non deve spegnere il reaper: senza rivendicazione
        e con cwd leggibile fuori dal live set, il kill resta."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "uptime").write_text("10000 0")
            orfano = root / "orfano"
            orfano.mkdir()
            _proc_dir(root, 7, 1, 100, orfano, "/usr/bin/claude chat")
            killed: list[int] = []

            stats = process_reaper.sweep_orphan_runtime_processes(
                set(), 100, protected_pids={2}, proc_root=root, root_pid=1,
                kill=lambda pid, sig: killed.append(pid),
            )

            self.assertEqual(killed, [7])
            self.assertEqual(stats["reaped"], 1)

    def test_cwd_illeggibile_non_e_una_condanna(self):
        """Scelta consapevole: un cwd che non risolve non prova nulla, quindi non
        si uccide. Un orfano vero con la spawn dir già cancellata resta fino al
        restart — costa meno di un turno vivo ucciso."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "uptime").write_text("10000 0")
            _proc_dir(root, 4, 1, 100, None, "/usr/bin/claude chat")
            killed: list[int] = []

            stats = process_reaper.sweep_orphan_runtime_processes(
                set(), 100, proc_root=root, root_pid=1,
                kill=lambda pid, sig: killed.append(pid),
            )

            self.assertEqual(killed, [])
            self.assertEqual(stats["orphan_processes"], 0)

    def test_stats_portano_gli_input_della_decisione(self):
        """Il log del tick deve poter stampare in base a COSA ha deciso: #478 non
        è diagnosticabile dai log perché usciva solo il conteggio dei kill."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "uptime").write_text("10000 0")
            viva = root / "viva"
            orfano = root / "orfano"
            viva.mkdir()
            orfano.mkdir()
            _proc_dir(root, 2, 1, 100, viva, "/usr/bin/claude chat")
            _proc_dir(root, 3, 1, 100, orfano, "/usr/bin/claude chat")

            stats = process_reaper.sweep_orphan_runtime_processes(
                {str(viva)}, 100, proc_root=root, root_pid=1,
                kill=lambda pid, sig: None,
            )

            self.assertEqual(stats["live_cwds"], [str(viva.resolve())])
            self.assertEqual([o["pid"] for o in stats["orphans"]], [3])


class PidDichiaratiDalleSessioni(unittest.TestCase):
    """`live_runtime_pids` è l'unica fonte dei pid protetti: se non li trova, la
    protezione è una dichiarazione vuota."""

    def _manager(self, chats: dict) -> ChatManager:
        m = ChatManager()
        m._chats.update(chats)
        return m

    def test_legge_il_pid_delle_tre_classi_di_sessione(self):
        claude = SimpleNamespace(_client=SimpleNamespace(
            _transport=SimpleNamespace(_process=SimpleNamespace(pid=11, returncode=None))))
        codex = SimpleNamespace(_proc=SimpleNamespace(pid=22, returncode=None))
        self.assertEqual(
            self._manager({"a": claude, "b": codex}).live_runtime_pids(), {11, 22})

    def test_sessione_senza_processo_non_contribuisce(self):
        spenta = SimpleNamespace(_client=None)
        fra_due_turni = SimpleNamespace(_proc=None, _client=None)  # codex
        self.assertIsNone(runtime_process_of(spenta))
        self.assertEqual(
            self._manager({"a": spenta, "b": fra_due_turni}).live_runtime_pids(), set())

    def test_una_sessione_rotta_non_priva_le_altre_della_protezione(self):
        class Esplosiva:
            @property
            def _proc(self):
                raise RuntimeError("runtime impuntato")

        buona = SimpleNamespace(_proc=SimpleNamespace(pid=33, returncode=None))
        self.assertEqual(
            self._manager({"a": Esplosiva(), "b": buona}).live_runtime_pids(), {33})


class TickDelReaper(unittest.TestCase):
    """Guard AST su `main.py`: la chiamata al reaper deve passare i pid vivi.

    Statico e non comportamentale apposta: il tick è dentro `_lifespan`, un loop
    infinito non montabile in un test, e la cosa che si vuole impedire è una
    riga cancellata per distrazione in un refactor."""

    SORGENTE = Path(__file__).resolve().parents[1] / "main.py"

    @staticmethod
    def _chiamate_al_reaper(sorgente: str) -> list[ast.Call]:
        trovate = []
        for nodo in ast.walk(ast.parse(sorgente)):
            if not isinstance(nodo, ast.Call):
                continue
            nomi = [a for a in nodo.args if isinstance(a, ast.Name)]
            diretto = isinstance(nodo.func, ast.Name) and \
                nodo.func.id == "sweep_orphan_runtime_processes"
            # `asyncio.to_thread(sweep_orphan_runtime_processes, ...)`
            indiretto = any(n.id == "sweep_orphan_runtime_processes" for n in nomi)
            if diretto or indiretto:
                trovate.append(nodo)
        return trovate

    def test_il_tick_passa_i_pid_protetti(self):
        chiamate = self._chiamate_al_reaper(self.SORGENTE.read_text(encoding="utf-8"))
        self.assertEqual(len(chiamate), 1, "il tick del reaper non è più uno solo")
        kw = {k.arg for k in chiamate[0].keywords}
        self.assertIn("protected_pids", kw,
                      "il tick deve passare i pid delle sessioni vive: senza, la "
                      "sola prova di proprietà torna a essere la cwd (#478)")

    def test_il_guard_sa_fallire(self):
        rotto = ("import asyncio\n"
                 "async def tick():\n"
                 "    await asyncio.to_thread(sweep_orphan_runtime_processes, live, ttl)\n")
        chiamate = self._chiamate_al_reaper(rotto)
        self.assertEqual(len(chiamate), 1)
        self.assertNotIn("protected_pids", {k.arg for k in chiamate[0].keywords})


if __name__ == "__main__":
    unittest.main()
