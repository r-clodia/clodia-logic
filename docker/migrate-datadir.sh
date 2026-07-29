#!/usr/bin/env bash
# Migrazioni idempotenti del datadir ESISTENTE, eseguite ad OGNI avvio
# dall'entrypoint (a differenza di init-datadir.sh, che gira solo su datadir
# vuoto). Ogni migrazione DEVE essere idempotente e string-based sui .yaml, per
# preservare commenti/formato ed editing locale.
#
# Uso: bash docker/migrate-datadir.sh /path/to/clodia-data
set -euo pipefail
DATADIR="${1:-/datadir}"

[ -d "$DATADIR/agents" ] || exit 0

# ── base-pack diet (issue #51) ────────────────────────────────────────────
# I seed non-immutabili non vengono ri-sincronizzati dal bundle, quindi su un
# datadir esistente le loro capabilities resterebbero appese a skill spostate in
# editorial-pack/comms-pack → skill perse (es. messaggero perde check-email,
# clodia/ophelia perdono editoriale/comms). Rewrite idempotente:
#  1) ri-referenzia le skill spostate al nuovo pack;
#  2) chi ha `base-pack/*` riceve anche `editorial-pack/*` + `comms-pack/*`, così
#     mantiene il comportamento pre-diet (base-pack/* prima le includeva tutte).
python3 - "$DATADIR/agents" <<'PYMIG'
import glob, os, sys
base = sys.argv[1]
MOVED = {
    "check-email": "comms-pack", "telegram-1to1": "comms-pack", "helpdesk": "comms-pack",
    "article-spec": "editorial-pack", "editorial-review": "editorial-pack",
    "fact-check": "editorial-pack",
}
for f in glob.glob(os.path.join(base, "*", "agent.yaml")):
    txt = open(f, encoding="utf-8").read()
    orig = txt
    for skill, pack in MOVED.items():
        txt = txt.replace(f"base-pack/{skill}", f"{pack}/{skill}")
    if "base-pack/*" in txt and "editorial-pack/*" not in txt:
        for q in ('"base-pack/*"', "'base-pack/*'"):
            if q in txt:
                ch = q[0]
                repl = (f"{ch}base-pack/*{ch}, {ch}editorial-pack/*{ch}, "
                        f"{ch}comms-pack/*{ch}")
                txt = txt.replace(q, repl, 1)
                break
    if txt != orig:
        open(f, "w", encoding="utf-8").write(txt)
        print(f"[migrate] capabilities (base-pack diet): "
              f"{os.path.basename(os.path.dirname(f))}")
PYMIG
