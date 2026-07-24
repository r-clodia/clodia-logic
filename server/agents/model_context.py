"""Finestra di contesto (token) per famiglia di modello — best-effort.

Serve alla UI per il "termometro" di contesto di ogni agente in un topic. I valori
sono APPROSSIMATI (finestra dichiarata dai provider, per famiglia): bastano per un
indicatore verde/arancione/rosso, non per calcoli esatti. Match per sottostringa,
il primo che combacia vince → mettere le voci PIÙ SPECIFICHE prima. Modello ignoto
→ None (la UI nasconde il termometro invece di mostrare un dato falso).
"""
from __future__ import annotations

# (sottostringa_lower, finestra_token). Specifiche prima delle generiche.
_WINDOWS: list[tuple[str, int]] = [
    ("gpt-4.1", 1_000_000),
    ("gpt-4o", 128_000),
    ("gpt-5.4", 1_000_000),
    ("gpt-5", 400_000),
    ("gpt-oss", 128_000),
    ("codex", 200_000),
    ("o1", 200_000),
    ("o3", 200_000),
    ("o4", 200_000),
    # Claude (Opus/Sonnet/Haiku, incl. 4.x/5): 200k
    ("opus", 200_000),
    ("sonnet", 200_000),
    ("haiku", 200_000),
    ("claude", 200_000),
    # Altri provider (Scaleway/OpenCode ecc.)
    ("glm", 128_000),
    ("gemma", 128_000),
    ("mistral", 128_000),
    ("llama", 128_000),
    ("qwen", 128_000),
]


def model_context_window(model: str | None) -> int | None:
    """Finestra di contesto approssimata per un id/nome modello, o None se ignoto."""
    if not model:
        return None
    m = str(model).lower()
    for key, win in _WINDOWS:
        if key in m:
            return win
    return None
