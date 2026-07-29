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
# clodia/ophelia perdono editoriale/comms). Rewrite idempotente e YAML-safe
# (gestisce sia le liste INLINE `[...]` sia quelle a BLOCCO `- item`):
#  1) ri-referenzia le skill spostate al nuovo pack;
#  2) chi ha `base-pack/*` riceve anche `editorial-pack/*` + `comms-pack/*`, così
#     mantiene il comportamento pre-diet (base-pack/* prima le includeva tutte).
python3 - "$DATADIR/agents" <<'PYMIG'
import glob, os, re, sys
base = sys.argv[1]
MOVED = {
    "check-email": "comms-pack", "telegram-1to1": "comms-pack", "helpdesk": "comms-pack",
    "article-spec": "editorial-pack", "editorial-review": "editorial-pack",
    "fact-check": "editorial-pack",
}
ADD = ["editorial-pack/*", "comms-pack/*"]
# item a blocco che è ESATTAMENTE base-pack/* (con o senza quote): "  - base-pack/*"
BLOCK_RE = re.compile(r'^(\s*)-\s*(["\']?)base-pack/\*\2\s*$')

def migrate(txt: str) -> str:
    # 1) skill spostate → nuovo pack (safe ovunque)
    for skill, pack in MOVED.items():
        txt = txt.replace(f"base-pack/{skill}", f"{pack}/{skill}")
    # 2) aggiungi editorial-pack/* + comms-pack/* accanto a base-pack/* (una volta)
    if "base-pack/*" in txt and "editorial-pack/*" not in txt:
        out, done = [], False
        for line in txt.splitlines(keepends=True):
            out.append(line)
            m = BLOCK_RE.match(line.rstrip("\n"))
            if m and not done:  # forma a BLOCCO → nuovi item con stesso indent/quote
                indent, q = m.group(1), m.group(2)
                nl = "\n" if line.endswith("\n") else ""
                for cap in ADD:
                    out.append(f"{indent}- {q}{cap}{q}{nl}")
                done = True
        txt = "".join(out)
        if not done:  # forma INLINE `[..., "base-pack/*", ...]`
            for q in ('"base-pack/*"', "'base-pack/*'"):
                if q in txt:
                    ch = q[0]
                    txt = txt.replace(
                        q, f"{ch}base-pack/*{ch}, {ch}editorial-pack/*{ch}, "
                           f"{ch}comms-pack/*{ch}", 1)
                    break
    return txt

for f in glob.glob(os.path.join(base, "*", "agent.yaml")):
    orig = open(f, encoding="utf-8").read()
    new = migrate(orig)
    if new != orig:
        open(f, "w", encoding="utf-8").write(new)
        print(f"[migrate] capabilities (base-pack diet): "
              f"{os.path.basename(os.path.dirname(f))}")
PYMIG
