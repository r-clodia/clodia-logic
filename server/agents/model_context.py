"""Finestra di contesto (token) per configurazione (SDK/harness, modello).

LEZIONE CHIAVE: la finestra EFFETTIVA non dipende solo dal modello ma dalla coppia
**(harness, modello)** — lo stesso modello ha finestre diverse a seconda della CLI
che lo comanda. Esempi (ricerca lug 2026):
  • gpt-5.5: nativo ~1.05M → dentro **Codex** cappato a 400k;
  • gpt-5-codex: dentro **Codex** = 200k;
  • Claude Code: default 200k, 1M solo sui modelli 1M-capable (Opus 4.6+/Sonnet 4.6+).
Per i modelli **opencode su Scaleway** la finestra può essere ridotta dal serving
(VRAM): i valori qui sono quelli NATIVI dei modelli — se Scaleway serve meno, vanno
abbassati (o dichiarati per-agent nel seed).

Match per sottostringa del modello, DENTRO il gruppo dell'SDK; il primo che combacia
vince → mettere le voci PIÙ SPECIFICHE prima. Ignoto → None (la UI nasconde la barra).
"""
from __future__ import annotations

# (agent_sdk) -> lista (sottostringa_modello_lower, finestra_token_EFFETTIVA per quell'harness)
_BY_SDK: dict[str, list[tuple[str, int]]] = {
    # Claude Code / claude agent SDK: default 200k; 1M sui modelli 1M-capable
    # (Opus 4.6+/Sonnet 4.6+/5), senza beta header, su piani che lo abilitano.
    "claude": [
        ("opus-4-6", 1_000_000),
        ("opus-4-7", 1_000_000),
        ("opus-4-8", 1_000_000),
        ("sonnet-4-6", 1_000_000),
        ("sonnet-5", 1_000_000),
        ("opus-4-5", 200_000),
        ("sonnet-4-5", 200_000),
        ("haiku", 200_000),
        ("opus", 200_000),      # fallback famiglia
        ("sonnet", 200_000),
        ("claude", 200_000),
    ],
    # OpenAI Codex CLI: cappa il contesto per-modello (indipendente dal nativo).
    "codex": [
        ("gpt-5.5", 400_000),      # nativo ~1.05M, in Codex 400k
        ("gpt-5.3", 128_000),      # codex-spark
        ("gpt-5-codex", 200_000),  # confermato
        ("gpt-5", 200_000),
        ("codex", 200_000),        # fallback harness
    ],
    # OpenCode (provider Scaleway per i nostri modelli): finestra NATIVA del modello
    # (Scaleway può ridurla per VRAM — verificare se emergono errori di context).
    "opencode": [
        ("glm-5.2", 1_000_000),    # nativo 1M (GLM-5.1 era 200k)
        ("glm-5.1", 200_000),
        ("glm-4.6", 200_000),
        ("gemma-4", 256_000),      # Gemma 4 flagship (26B/31B) 256k
        ("gpt-oss-120b", 128_000),
        ("gpt-oss", 128_000),
        ("gemma", 128_000),        # fallback famiglia gemma
        ("glm", 200_000),          # fallback famiglia glm
        ("qwen", 128_000),
        ("mistral", 128_000),
        ("llama", 128_000),
    ],
}

# Fallback per SDK ignoto: mappa sul solo modello (best-effort, harness-agnostica).
_GENERIC: list[tuple[str, int]] = [
    ("opus", 200_000), ("sonnet", 200_000), ("haiku", 200_000), ("claude", 200_000),
    ("gpt-5.5", 400_000), ("gpt-5", 200_000), ("codex", 200_000),
    ("glm-5.2", 1_000_000), ("glm", 200_000),
    ("gemma-4", 256_000), ("gemma", 128_000),
    ("gpt-oss", 128_000), ("qwen", 128_000), ("mistral", 128_000), ("llama", 128_000),
]


def model_context_window(model: str | None, sdk: str | None = None) -> int | None:
    """Finestra di contesto EFFETTIVA per la configurazione (sdk, modello), o None
    se ignota. `sdk` = agent_sdk dell'agente (claude|codex|opencode)."""
    if not model:
        return None
    m = str(model).lower()
    table = _BY_SDK.get((sdk or "").lower()) if sdk else None
    for key, win in (table or []):
        if key in m:
            return win
    for key, win in _GENERIC:
        if key in m:
            return win
    return None
