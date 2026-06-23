"""Mini-client Trello per il lane consumer.

Riusa la stessa convenzione di credenziali del tool `tools/system/trello/`:
key e token letti da file in `secrets/`, mai da CLI / env. Esposto solo il
sottoinsieme di endpoint che servono al lane consumer.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

import httpx

from ..config import workspace_path

LOG = logging.getLogger("agent-server.agents.trello")

BASE_URL = "https://api.trello.com/1"
# secrets è mappato come bundle path (/clodia/secrets nel container, secrets/
# locale), non come datadir path. Coerente col resto del codice esistente.
SECRETS_DIR = workspace_path("secrets")


def _creds() -> tuple[str, str]:
    """Credenziali Trello dell'agente.

    Convenzione del bundle: `secrets/trello-apikey` e `secrets/trello-token`
    contengono le credenziali dell'account Trello collegato all'agency.
    Le azioni Trello firmate da questi agenti appariranno quindi come
    scritte dall'agent, non dall'owner.
    """
    key = (SECRETS_DIR / "trello-apikey").read_text().strip()
    token = (SECRETS_DIR / "trello-token").read_text().strip()
    return key, token


def _auth() -> dict:
    k, t = _creds()
    return {"key": k, "token": t}


def list_cards(list_id: str) -> list[dict]:
    """Cards aperte di una lane, ordinate FIFO (più vecchia per prima).

    Trello ordina per `pos` (drag&drop), non per data; ma `id` è un
    ObjectId che inizia con il timestamp di creazione, quindi sort
    per id ascendente = creazione cronologica = FIFO.
    """
    r = httpx.get(
        f"{BASE_URL}/lists/{list_id}/cards",
        params={**_auth(), "filter": "open", "fields": "id,name,desc,idList,idMembers,labels,due,dateLastActivity"},
        timeout=15.0,
    )
    r.raise_for_status()
    cards = r.json()
    return sorted(cards, key=lambda c: c["id"])


def get_list_id_by_name(board_id: str, name: str) -> Optional[str]:
    r = httpx.get(
        f"{BASE_URL}/boards/{board_id}/lists",
        params={**_auth(), "filter": "open", "fields": "id,name"},
        timeout=15.0,
    )
    r.raise_for_status()
    for lst in r.json():
        if lst["name"] == name:
            return lst["id"]
    return None


def list_lists(board_id: str) -> list[dict]:
    """Lane aperte di una board (id + name)."""
    r = httpx.get(
        f"{BASE_URL}/boards/{board_id}/lists",
        params={**_auth(), "filter": "open", "fields": "id,name"},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def list_lanes_with_card_counts(board_id: str) -> list[dict]:
    """Lane aperte con conteggio card aperte per lane.

    Usato dal skill_consumer per il polling delle board di processo.
    Ritorna [{id, name, count}] in ordine di posizione Trello.
    """
    r = httpx.get(
        f"{BASE_URL}/boards/{board_id}/lists",
        params={**_auth(), "filter": "open", "fields": "id,name,pos",
                "cards": "open", "card_fields": "id"},
        timeout=15.0,
    )
    r.raise_for_status()
    lanes = r.json()
    result = []
    for lane in sorted(lanes, key=lambda l: l.get("pos", 0)):
        result.append({
            "id": lane["id"],
            "name": lane["name"],
            "count": len(lane.get("cards", [])),
        })
    return result


def get_board(board_id: str) -> dict:
    """Metadati base di una board (name, url)."""
    r = httpx.get(
        f"{BASE_URL}/boards/{board_id}",
        params={**_auth(), "fields": "id,name,url"},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def create_card(list_id: str, name: str, desc: str = "", pos: str = "bottom") -> dict:
    """Crea una card su una lane (usata dal fork per delegation)."""
    r = httpx.post(
        f"{BASE_URL}/cards",
        params={**_auth(), "idList": list_id, "name": name, "desc": desc, "pos": pos},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def add_comment(card_id: str, text: str) -> dict:
    r = httpx.post(
        f"{BASE_URL}/cards/{card_id}/actions/comments",
        params={**_auth(), "text": text},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def update_card_desc(card_id: str, new_desc: str) -> dict:
    """Riscrive il body della desc di una card."""
    r = httpx.put(
        f"{BASE_URL}/cards/{card_id}",
        params={**_auth()},
        data={"desc": new_desc},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def move_card(card_id: str, target_list_id: str) -> dict:
    r = httpx.put(
        f"{BASE_URL}/cards/{card_id}",
        params={**_auth(), "idList": target_list_id, "pos": "bottom"},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def archive_card(card_id: str) -> dict:
    r = httpx.put(
        f"{BASE_URL}/cards/{card_id}",
        params={**_auth(), "closed": "true"},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def attach_file(card_id: str, file_path: Path, name: Optional[str] = None) -> dict:
    if not file_path.is_file():
        raise FileNotFoundError(file_path)
    with file_path.open("rb") as f:
        files = {"file": (name or file_path.name, f)}
        r = httpx.post(
            f"{BASE_URL}/cards/{card_id}/attachments",
            params=_auth(),
            files=files,
            timeout=60.0,
        )
    r.raise_for_status()
    return r.json()


def list_comments(card_id: str, limit: int = 1000) -> list[dict]:
    """Commenti di una card in ordine cronologico (più vecchio per primo).

    Trello ritorna gli action `commentCard` dal più recente al più vecchio:
    invertiamo prima di ritornare per coerenza con la lettura naturale.
    """
    r = httpx.get(
        f"{BASE_URL}/cards/{card_id}/actions",
        params={**_auth(), "filter": "commentCard", "limit": min(limit, 1000)},
        timeout=15.0,
    )
    r.raise_for_status()
    actions = r.json()
    return list(reversed(actions))


def get_card_attachments(card_id: str) -> list[dict]:
    r = httpx.get(
        f"{BASE_URL}/cards/{card_id}/attachments",
        params={**_auth(), "fields": "id,name,url,mimeType,bytes"},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def download_attachment(url: str, dest: Path) -> Path:
    """Scarica un attachment Trello. Richiede header auth."""
    k, t = _creds()
    headers = {"Authorization": f'OAuth oauth_consumer_key="{k}", oauth_token="{t}"'}
    with httpx.stream("GET", url, headers=headers, timeout=60.0, follow_redirects=True) as r:
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return dest


# ── Provisioning CAP (spec §7-fase, §10): board/lane da pipeline ────


def create_board(name: str, desc: str = "") -> dict:
    """Crea una board Trello vuota (senza lane di default).

    Usata dal Pipeline Registry al provisioning: ogni pipeline → una
    board, ogni step → una lane (vedi colony/provisioning.py).
    """
    r = httpx.post(
        f"{BASE_URL}/boards/",
        params={
            **_auth(), "name": name, "desc": desc,
            "defaultLists": "false", "prefs_permissionLevel": "private",
        },
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def create_list(board_id: str, name: str, pos: str = "bottom") -> dict:
    """Crea una lane su una board (in coda: l'ordine di chiamata = ordine step)."""
    r = httpx.post(
        f"{BASE_URL}/lists",
        params={**_auth(), "idBoard": board_id, "name": name, "pos": pos},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def close_board(board_id: str) -> dict:
    """Archivia (chiude) una board — reversibile da UI Trello, NON eliminazione."""
    r = httpx.put(
        f"{BASE_URL}/boards/{board_id}",
        params={**_auth(), "closed": "true"},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def add_board_member(board_id: str, username: str, member_type: str = "admin") -> dict:
    """Aggiunge un membro a una board (provisioning CAP: invito owner)."""
    r = httpx.put(
        f"{BASE_URL}/boards/{board_id}/members/{username}",
        params={**_auth(), "type": member_type},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()
