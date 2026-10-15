"""I segreti non stanno su un filesystem che ignora i permessi (#544).

Su terra la datadir è il bind mount di una directory macOS attraverso virtiofs,
che **non applica i bit di permesso**: misurato dentro `clodia-agent-server-1`,
l'uid di sandbox di uno spawn (60123) leggeva 44 file mode 600 di root sotto
`/datadir/secrets`. L'isolamento per-uid degli spawn — l'unico che esista per i
runtime codex/opencode — lì non vale niente.

Il rimedio è nel compose, ed è una proprietà del FILE: `secrets` arriva da un
volume nominato (filesystem Linux del VM, dove i bit valgono) e non da un bind
della directory host. Questi test lo leggono dal `docker-compose.yml` vero,
perché è quel file a decidere, e perché un ripristino del bind — in un merge, o
per abitudine — altrimenti non lo vedrebbe nessuno fino al prossimo incidente.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "docker-compose.yml"

#: Montaggio della directory dei segreti dentro i container.
SECRETS_TARGET = "/datadir/secrets"

#: Secondo montaggio, storico, della STESSA directory: tolto con #544 (un solo
#: posto da cui leggere i segreti, e nessun file da tenere allineato).
LEGACY_TARGET = "/clodia/secrets"

#: File che il deploy esegue davvero: se uno di loro cita ancora il mount
#: legacy, il mount tolto dal compose diventa un guasto a runtime.
DEPLOY_FILES = ("docker/entrypoint.sh", "config.yaml")


def compose() -> dict:
    """Il compose come lo legge docker (ancore e alias già risolti)."""
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def mounts(service: dict) -> list[tuple[str, str]]:
    """`(sorgente, destinazione)` di ogni volume del servizio, forma corta.

    Il compose usa la forma `src:dst[:mode]`; `dst` è il secondo campo anche
    quando la sorgente contiene `${CLODIA_DATA}` o due punti non ce ne sono
    (volume anonimo), e in quel caso la sorgente è vuota.
    """
    out: list[tuple[str, str]] = []
    for row in service.get("volumes") or []:
        if isinstance(row, dict):  # forma lunga
            out.append((str(row.get("source") or ""), str(row.get("target") or "")))
            continue
        parts = str(row).split(":")
        out.append((parts[0], parts[1]) if len(parts) >= 2 else ("", parts[0]))
    return out


def named_volumes() -> set[str]:
    return set((compose().get("volumes") or {}).keys())


class SecretsStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conf = compose()
        self.services = self.conf.get("services") or {}
        # I due servizi che montano la datadir. `base`/`bundle` sono helper di
        # build (profile build-only) e non montano niente.
        self.serving = {n: s for n, s in self.services.items()
                        if s.get("volumes")}
        self.assertEqual(set(self.serving), {"agent-server", "clodia-tools"},
                         "cambiato l'insieme dei servizi che montano la datadir: "
                         "questo test va riletto, non aggiornato d'ufficio")

    def test_secrets_never_come_from_a_host_bind_mount(self):
        """`${CLODIA_DATA}/secrets` è il bind che su terra non protegge niente."""
        for name, service in self.serving.items():
            for source, target in mounts(service):
                with self.subTest(service=name, target=target):
                    self.assertFalse(
                        source.startswith("${CLODIA_DATA}")
                        and source.rstrip("/").endswith("/secrets"),
                        f"{name} monta i segreti dalla directory host ({source}): "
                        "su virtiofs i bit di permesso non valgono e l'uid di "
                        "sandbox legge tutto (clodia-platform#544)")

    def test_secrets_come_from_a_declared_named_volume(self):
        declared = named_volumes()
        for name, service in self.serving.items():
            sources = [s for s, t in mounts(service) if t == SECRETS_TARGET]
            with self.subTest(service=name):
                self.assertEqual(len(sources), 1,
                                 f"{name} deve montare {SECRETS_TARGET} una volta sola")
                self.assertIn(sources[0], declared,
                              f"{name}: {SECRETS_TARGET} deve arrivare da un volume "
                              f"nominato dichiarato in `volumes:` (trovato "
                              f"{sources[0]!r}), così il filesystem è quello Linux "
                              f"del VM e i bit di permesso valgono")

    def test_the_legacy_second_mount_of_the_same_directory_is_gone(self):
        for name, service in self.serving.items():
            with self.subTest(service=name):
                self.assertNotIn(LEGACY_TARGET,
                                 [t for _, t in mounts(service)],
                                 f"{name} rimonta i segreti in {LEGACY_TARGET}: "
                                 "due strade per lo stesso file, e una sola "
                                 "messa in sicurezza")

    def test_no_deploy_file_still_reads_from_the_legacy_mount(self):
        """Il mount tolto dal compose e un path lasciato in uno script sono lo
        stesso guasto, ma il secondo si manifesta solo al boot del container."""
        for rel in DEPLOY_FILES:
            testo = (REPO / rel).read_text(encoding="utf-8")
            codice = "\n".join(r for r in testo.splitlines()
                               if not r.lstrip().startswith("#"))
            with self.subTest(file=rel):
                self.assertNotIn(LEGACY_TARGET, codice,
                                 f"{rel} legge ancora da {LEGACY_TARGET}, che "
                                 "non è più montato")

    def test_both_containers_know_the_sandbox_uid(self):
        """Il self-check di isolamento del gateway (clodia-tools, #544) prova a
        leggere un sentinella 0600 con l'uid DAVVERO usato dagli spawn: se la
        variabile non arriva anche al gateway, quel probe userebbe un uid
        inventato e la risposta non direbbe niente sull'istanza."""
        for name, service in self.serving.items():
            with self.subTest(service=name):
                self.assertIn("CLODIA_AGENT_SANDBOX_UID",
                              service.get("environment") or {},
                              f"{name} non riceve CLODIA_AGENT_SANDBOX_UID")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
