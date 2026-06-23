#!/usr/bin/env bash
# Pre-installa le dipendenze Python note per velocizzare i restart.
# Se il requirements.txt cambia dopo il build, l'entrypoint può reinstallare.
set -euo pipefail

# agent-server (post-refactor v4: requirements.txt alla root del repo)
if [ -f /clodia/requirements.txt ]; then
    pip install --no-cache-dir -q -r /clodia/requirements.txt
fi
