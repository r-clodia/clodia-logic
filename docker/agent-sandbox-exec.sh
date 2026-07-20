#!/bin/sh
# Contenimento runtime dell'agente (M3, fase perms-based).
#
# L'SDK invoca questo script come `cli_path` al posto del CLI `claude` bundled.
# Qui scendiamo a un utente NON-root e poi exec-hiamo il CLI reale: così il
# subprocess dell'agente (incluso qualunque `bash` che il modello lanci) gira
# senza privilegi e NON può leggere i file root-only della datadir — ca.key,
# identity.key, vault (già 600/700 root). Le chiavi restano leggibili solo
# dall'orchestrator (root), che è l'unico a coniare i token.
#
# Attivato solo quando l'orchestrator passa CLODIA_AGENT_UID + CLODIA_REAL_CLI
# (vedi sdk_runtime/session.py, opt-in per-kind). Senza, non viene mai usato.
set -eu

: "${CLODIA_AGENT_UID:?CLODIA_AGENT_UID mancante}"
: "${CLODIA_REAL_CLI:?CLODIA_REAL_CLI mancante}"
# gid = gruppo del SEED (famiglia). Se assente, usa l'uid (gruppo privato).
CLODIA_AGENT_GID="${CLODIA_AGENT_GID:-$CLODIA_AGENT_UID}"

exec setpriv --reuid="$CLODIA_AGENT_UID" --regid="$CLODIA_AGENT_GID" \
     --clear-groups --inh-caps=-all "$CLODIA_REAL_CLI" "$@"
