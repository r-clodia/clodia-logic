FROM python:3.12-slim

ARG OPENAI_CODEX_NPM_VERSION=0.137.0
ARG OPENCODE_NPM_VERSION=1.15.13
# Claude Code is pinned too (clodia-platform#535): an unpinned install takes
# whatever npm serves on the day of the rebuild. 2.1.197 is the version the
# production agent-server was running when the pin was introduced (21 Nov 2026),
# the same value as clodia-platform's `docker/base.Dockerfile`. Bump it in a PR
# after a test turn of an `agent_sdk: claude` agent on the new version.
ARG ANTHROPIC_CLAUDE_CODE_NPM_VERSION=2.1.197

# Node.js 20 LTS + pandoc (toolchain docx: la skill docx usa pandoc per
# leggere/estrarre il contenuto dei .docx; senza, l'agente degrada a metodi lenti)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl bash git sqlite3 rsync pandoc \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# CLI agentici spawnabili dai bot Node e dai tool Python.
# Codex e' pinnato: gli agent `agent_sdk=codex` girano dentro il worker
# agent-server e devono trovare un binario stabile a build-time.
# `docx` (docx-js): usato dalla skill docx per CREARE nuovi documenti Word.
RUN npm install -g @anthropic-ai/claude-code@${ANTHROPIC_CLAUDE_CODE_NPM_VERSION} \
    @openai/codex@${OPENAI_CODEX_NPM_VERSION} \
    opencode-ai@${OPENCODE_NPM_VERSION} mcp-remote@0.16.7 docx

# Verifica installazione
RUN claude --version
RUN codex --version
RUN opencode --version

ENV CLODIA_DATA=/datadir
WORKDIR /clodia
