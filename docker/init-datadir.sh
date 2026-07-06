#!/usr/bin/env bash
# Inizializza una datadir vuota per un'installazione pristine di Clodia.
# Uso: bash docker/init-datadir.sh /path/to/clodia-data
#
# Lo schema dei DB (logica) sta nel bundle (docker/schema/).
# I dati dell'istanza (righe) stanno nella datadir.
set -euo pipefail

DATADIR="${1:-$HOME/clodia-data}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "Inizializzazione datadir: $DATADIR"
# Dir del modello seed/job/spawn: agents (seed vivi), jobs (file-per-job),
# spawns (esecuzioni vive, persistenti per il resume), sessions (cronologia chat
# per agent), pki/providers. (data/boot/codex-home/claude-home/agent-* restano
# per ora per compatibilità con compose/codice; la loro rimozione è il cleanup
# successivo dopo migrazione vault/MCP/home-effimere.)
mkdir -p "$DATADIR"/{secrets,data,topics,boot/retrospectives,daemon-state/{whatsapp,telegram,check-mail},claude-home,codex-home,agents,jobs,spawns,sessions,pki,providers,agent-workspaces,agent-state,agency-shared,skills-catalog,rules-catalog}

# (rimosso 6 lug 2026) contacts.db NON viene più creato al bootstrap: era un
# residuo del CRM di Clodia Primal. Le edizioni cliente integrano il PROPRIO
# CRM via MCP (pagina Integrations / pack). Le istanze esistenti che hanno un
# contacts.db lo conservano: nulla lo tocca.

# VIOLATION.md deve esistere come file
touch "$DATADIR/boot/VIOLATION.md"

# pipes.yaml (CAP pipeline registry) deve esistere come FILE prima del primo
# `up`: il bind single-file di docker-compose altrimenti lo crea come directory.
[ -f "$DATADIR/pipes.yaml" ] || printf 'pipelines: {}\n' > "$DATADIR/pipes.yaml"

# Keystore: depositario unico credenziali + policy grant (default deny)
mkdir -p "$DATADIR/secrets/keystore"
[ -f "$DATADIR/keystore-policy.yaml" ] || printf 'credentials: {}\n' > "$DATADIR/keystore-policy.yaml"

# Seed agent: installa gli agent NATIVI della piattaforma da catalogs/agents-seed
# (clodia, ophelia — super; wainston — admin; mercuria — messaggero). Sono il
# genoma clonato con ogni istanza. Eventuali agent aggiuntivi dell'istanza vivono
# in CLODIA_DATA/agents/ e non stanno nel repo. Copia solo se manca, per non
# sovrascrivere editing locale.
# Terraformazione (Modular Distro): se esiste $DATADIR/native-seeds (un nome
# per riga), vengono seminati SOLO i seed elencati (anche uno solo). File
# assente = tutti (comportamento storico). Il file lo scrive clodia-build.
for seed in "$BUNDLE_ROOT"/catalogs/agents-seed/*; do
    [ -d "$seed" ] || continue
    name="$(basename "$seed")"
    if [ -f "$DATADIR/native-seeds" ] && ! grep -qx "$name" "$DATADIR/native-seeds"; then
        echo "Seed agent SALTATO (native-seeds): $name"
        continue
    fi
    target="$DATADIR/agents/$name"
    if [ ! -e "$target" ]; then
        cp -R "$seed" "$target"
        mkdir -p "$target/memory"
        echo "Seed agent installato: $name"
    fi
done

# trusted.json per WhatsApp (vuoto — da popolare con il LID di owner)
echo '{}' > "$DATADIR/daemon-state/whatsapp/trusted.json"

echo ""
echo "Struttura creata:"
find "$DATADIR" -not -path '*/.git/*' | sort

echo ""
echo "Prossimo passo: crea .env nella root del bundle con:"
echo "  CLODIA_DATA=$DATADIR"
echo "  ANTHROPIC_API_KEY=sk-ant-..."
echo "  TELEGRAM_BOT_TOKEN=..."
echo ""
echo "Per agenti agent_sdk=codex, il worker usa @openai/codex installato"
echo "nell'immagine e la subscription auth persistita in codex-home:"
echo "  CODEX_HOME=$DATADIR/codex-home codex login"
