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
#
# Also used for the codex and opencode runtimes (clodia-platform#471): as root
# they could read /proc/1/environ, i.e. the agent-server's own environment.
# Optional:
#   CLODIA_AGENT_GROUPS  comma-separated supplementary gids (codex: the group
#                        that owns the shared CODEX_HOME)
#   CLODIA_AGENT_UMASK   umask for the runtime (codex: 007, so a token refreshed
#                        by one spawn stays usable by the next)
set -eu

: "${CLODIA_AGENT_UID:?CLODIA_AGENT_UID mancante}"
: "${CLODIA_REAL_CLI:?CLODIA_REAL_CLI mancante}"
# gid = gruppo del SEED (famiglia). Se assente, usa l'uid (gruppo privato).
CLODIA_AGENT_GID="${CLODIA_AGENT_GID:-$CLODIA_AGENT_UID}"

if [ -n "${CLODIA_AGENT_UMASK:-}" ]; then
    umask "$CLODIA_AGENT_UMASK"
fi

if [ -n "${CLODIA_AGENT_GROUPS:-}" ]; then
    exec setpriv --reuid="$CLODIA_AGENT_UID" --regid="$CLODIA_AGENT_GID" \
         --groups="$CLODIA_AGENT_GROUPS" --inh-caps=-all "$CLODIA_REAL_CLI" "$@"
fi
exec setpriv --reuid="$CLODIA_AGENT_UID" --regid="$CLODIA_AGENT_GID" \
     --clear-groups --inh-caps=-all "$CLODIA_REAL_CLI" "$@"
