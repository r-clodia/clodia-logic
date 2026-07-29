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

# Seed agent: installa gli agent NATIVI della piattaforma dal base-pack
# (catalogs/packs/base-pack/agents/): clodia, ophelia — super; sysadmin — admin;
# messaggero; janitor. Sono il genoma clonato con ogni istanza. Eventuali agent
# aggiuntivi dell'istanza vivono in CLODIA_DATA/agents/ e non stanno nel repo.
# Copia solo se manca, per non sovrascrivere editing locale.
# Terraformazione (Modular Distro): se esiste $DATADIR/native-seeds (un nome
# per riga), vengono seminati SOLO i seed elencati (anche uno solo). File
# assente = tutti (comportamento storico). Il file lo scrive clodia-build.
for seed in "$BUNDLE_ROOT"/catalogs/packs/base-pack/agents/*; do
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
    elif grep -qE '^immutable:[[:space:]]*true' "$seed/agent.yaml" 2>/dev/null; then
        # Seed IMMUTABILE (super/system): il bundle è la fonte di verità.
        # Ri-sincronizza la DEFINIZIONE (agent.yaml, system-prompt.md, pfp)
        # ad ogni boot, così un update del seed via rebuild si propaga senza
        # intervento manuale. Preserva memory/ (stato runtime dell'agente).
        for f in agent.yaml system-prompt.md pfp.png; do
            [ -f "$seed/$f" ] && cp -f "$seed/$f" "$target/$f"
        done
        echo "Seed agent immutabile ri-sincronizzato: $name"
    fi
done

# Registra i manifest dei pack first-party bundled in CLODIA_DATA/packs/ (così
# compaiono nella view Packs con la loro versione e il confronto con la versione
# bundled abilita il tasto Update). Copia i pack.yaml del catalogo bundled.
for pack_manifest in "$BUNDLE_ROOT"/catalogs/packs/*/pack.yaml; do
    [ -f "$pack_manifest" ] || continue
    pack_name="$(basename "$(dirname "$pack_manifest")")"
    mkdir -p "$DATADIR/packs/$pack_name"
    cp -f "$pack_manifest" "$DATADIR/packs/$pack_name/pack.yaml"
    echo "Manifest $pack_name registrato in packs/"
done

# Migrazione capabilities (base-pack diet): sui datadir ESISTENTI gli agenti non
# immutabili non vengono ri-sincronizzati dal bundle, quindi le loro capabilities
# resterebbero appese a skill spostate in editorial-pack/comms-pack → skill perse
# (es. messaggero perde check-email, clodia/ophelia perdono editoriale/comms).
# Rewrite idempotente e string-based (preserva commenti/formato dei .yaml):
#  1) ri-referenzia le skill spostate al nuovo pack;
#  2) chi ha `base-pack/*` riceve anche `editorial-pack/*` + `comms-pack/*`, così
#     mantiene il comportamento pre-diet (base-pack/* prima le includeva tutte).
if [ -d "$DATADIR/agents" ]; then
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
                repl = q[0] + "base-pack/*" + q[0] + ", " + q[0] + "editorial-pack/*" \
                    + q[0] + ", " + q[0] + "comms-pack/*" + q[0]
                txt = txt.replace(q, repl, 1)
                break
    if txt != orig:
        open(f, "w", encoding="utf-8").write(txt)
        print(f"Capabilities migrate (base-pack diet): {os.path.basename(os.path.dirname(f))}")
PYMIG
fi

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
