"""Parser periodicità in linguaggio naturale → espressione cron a 5 campi.

Copre i casi comuni in italiano e inglese:
  "ogni 15 minuti", "ogni ora", "ogni 2 ore", "ogni giorno alle 9",
  "tutti i giorni alle 18:30", "ogni lunedì alle 9", "il lunedì e venerdì alle 8",
  "nei giorni feriali alle 7", "ogni mese il primo alle 9", "ogni settimana", ...

`parse(text)` → (cron_expr, descrizione_it). Solleva ValueError se non comprende.
"""
from __future__ import annotations

import re

_DOW = {
    "domenica": 0, "sunday": 0, "sun": 0, "dom": 0,
    "lunedì": 1, "lunedi": 1, "monday": 1, "mon": 1, "lun": 1,
    "martedì": 2, "martedi": 2, "tuesday": 2, "tue": 2, "mar": 2,
    "mercoledì": 3, "mercoledi": 3, "wednesday": 3, "wed": 3, "mer": 3,
    "giovedì": 4, "giovedi": 4, "thursday": 4, "thu": 4, "gio": 4,
    "venerdì": 5, "venerdi": 5, "friday": 5, "fri": 5, "ven": 5,
    "sabato": 6, "saturday": 6, "sat": 6, "sab": 6,
}
_DOW_LABEL = {0: "domenica", 1: "lunedì", 2: "martedì", 3: "mercoledì",
              4: "giovedì", 5: "venerdì", 6: "sabato"}


def _parse_time(text: str) -> tuple[int, int] | None:
    """Estrae un orario: 'alle 9', 'alle 9:30', 'ore 18.00', 'at 9am', '9:30'."""
    m = re.search(r"(?:alle|at|ore|h)\s*(\d{1,2})(?:[:.h](\d{2}))?\s*(am|pm)?", text)
    if not m:
        m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", text)
        if not m:
            return None
        return int(m.group(1)) % 24, int(m.group(2)) % 60
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    ap = m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h % 24, mi % 60


def _hhmm(h: int, mi: int) -> str:
    return f"{h:02d}:{mi:02d}"


def parse(text: str) -> tuple[str, str]:
    t = (text or "").strip().lower()
    if not t:
        raise ValueError("periodicità vuota")

    # 1) intervallo a minuti: "ogni 15 minuti"
    m = re.search(r"(?:ogni|every)\s+(\d+)\s*(?:min\b|minut|minute)", t)
    if m:
        n = int(m.group(1))
        if not 1 <= n <= 59:
            raise ValueError("i minuti devono essere tra 1 e 59")
        return f"*/{n} * * * *", f"ogni {n} minuti"
    if re.search(r"ogni\s+minuto|every\s+minute|al\s+minuto", t):
        return "* * * * *", "ogni minuto"

    # 2) intervallo a ore: "ogni 2 ore"
    m = re.search(r"(?:ogni|every)\s+(\d+)\s*(?:or[ae]\b|hour)", t)
    if m:
        n = int(m.group(1))
        if not 1 <= n <= 23:
            raise ValueError("le ore devono essere tra 1 e 23")
        return f"0 */{n} * * *", f"ogni {n} ore"
    if re.search(r"ogni\s+ora|every\s+hour|hourly|orari[ao]", t):
        return "0 * * * *", "ogni ora"

    # orario (per i pattern giornalieri/settimanali/mensili); default 09:00
    hm = _parse_time(t)
    h, mi = hm if hm else (9, 0)
    at = f"alle {_hhmm(h, mi)}"

    # 3) giorni feriali / weekend
    if re.search(r"feriali|lavorativ|weekday", t):
        return f"{mi} {h} * * 1-5", f"nei giorni feriali {at}"
    if re.search(r"weekend|fine\s*settimana", t):
        return f"{mi} {h} * * 0,6", f"nel weekend {at}"

    # 4) giorni della settimana espliciti (anche più d'uno)
    days = []
    for word, num in _DOW.items():
        if re.search(rf"\b{re.escape(word)}\b", t) and num not in days:
            days.append(num)
    if days:
        days.sort()
        dow = ",".join(str(d) for d in days)
        label = " e ".join(_DOW_LABEL[d] for d in days)
        return f"{mi} {h} * * {dow}", f"ogni {label} {at}"

    # 5) mensile
    if re.search(r"mensil|monthly|ogni\s+mese|del\s+mese|primo\s+del\s+mese|primo\s+giorno", t):
        dom = 1
        md = re.search(r"\bil\s+(\d{1,2})\b", t)  # "il 15 del mese"
        if md and re.search(r"mese|month", t):
            dom = max(1, min(28, int(md.group(1))))
        return f"{mi} {h} {dom} * *", f"ogni mese il giorno {dom} {at}"

    # 6) settimanale generico → lunedì
    if re.search(r"settiman|weekly|ogni\s+settimana", t):
        return f"{mi} {h} * * 1", f"ogni settimana (lunedì) {at}"

    # 7) giornaliero / solo orario → ogni giorno
    if hm or re.search(r"ogni\s+giorno|tutti\s+i\s+giorni|giornalier|daily|every\s+day|al\s+giorno", t):
        return f"{mi} {h} * * *", f"ogni giorno {at}"

    raise ValueError(
        "non ho capito la periodicità — prova es. 'ogni giorno alle 9', "
        "'ogni lunedì alle 18', 'ogni 15 minuti', 'ogni mese il primo alle 8'")
