"""Tuning dell'operating point del routing dai voti 👍/👎.

Per ogni voto abbiamo: chosen, verdict, scores dei candidati. Data una coppia
(THRESHOLD T, MARGIN M), la regola `decide()` avrebbe SCELTO il top specialista sse
`top>=T` e `top-second>=M`, altrimenti FALLBACK. Cerchiamo (T,M) che massimizza
l'accordo coi voti:
  • 👍 su una scelta per rilevanza  → l'operating point dovrebbe ancora SCEGLIERE;
  • 👎 su una scelta per rilevanza  → dovrebbe fare FALLBACK (o comunque non quella).
I voti su tag/rango/delega (non 'relevance') sono ignorati: lì soglia/margine non
c'entrano.

Uso:  docker compose exec agent-server python3 -m server.api.routing_tune
"""
from __future__ import annotations

from . import routing_feedback


def _top_two(scores: list[dict]) -> tuple[float, float]:
    vals = sorted((float(s.get("score") or 0.0) for s in scores), reverse=True)
    top = vals[0] if vals else 0.0
    second = vals[1] if len(vals) > 1 else 0.0
    return top, second


def tune(votes: list[dict] | None = None) -> dict:
    votes = votes if votes is not None else routing_feedback.load()
    # solo i voti dove soglia/margine decidono (routing per rilevanza)
    rel = [v for v in votes if (v.get("mode") == "relevance") and v.get("scores")]
    labeled = []
    for v in rel:
        top, second = _top_two(v["scores"])
        labeled.append((top, second, v.get("verdict")))
    if not labeled:
        return {"note": "nessun voto utile (serve mode=relevance con scores)",
                "total_votes": len(votes)}

    best = None
    T = 0.70
    while T <= 0.92:
        M = 0.005
        while M <= 0.06:
            correct = 0
            for top, second, verdict in labeled:
                picks = (top >= T) and ((top - second) >= M)
                # 👍 → giusto se picks; 👎 → giusto se NON picks (fallback)
                if (verdict == "up" and picks) or (verdict == "down" and not picks):
                    correct += 1
            acc = correct / len(labeled)
            if best is None or acc > best["accuracy"]:
                best = {"threshold": round(T, 3), "margin": round(M, 4),
                        "accuracy": round(acc, 3), "correct": correct, "n": len(labeled)}
            M += 0.005
        T += 0.01
    return {"total_votes": len(votes), "labeled": len(labeled),
            "best_operating_point": best,
            "stats": routing_feedback.stats()}


if __name__ == "__main__":
    import json
    print(json.dumps(tune(), ensure_ascii=False, indent=2))
