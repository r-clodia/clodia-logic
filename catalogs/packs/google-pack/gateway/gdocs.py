"""gdocs.* — Google Docs via MCP, sulla credenziale Workspace del vault.

Stessa credenziale di gdrive/gcalendar (scope `documents` già incluso). Verbi:
create, read (testo estratto), append_text, replace_text. Per creare dentro una
cartella specifica: create + gdrive.move. L'agente deve avere il grant Workspace.
"""
from __future__ import annotations

from typing import Optional

from . import gdrive_root
from . import google_io
from . import gdrive


def _docs(acct: str, method_path: str, **kw) -> dict:
    """One Docs v1 call for a resolved account, through the Google seam (#524)."""
    return google_io.call(acct, "docs", "v1", method_path, **kw)


def create(title: str, text: Optional[str] = None, account: Optional[str] = None) -> dict:
    acct = gdrive._resolve_account(account)
    doc = _docs(acct, "POST documents", body={"title": title})
    doc_id = doc.get("documentId")
    # L'API Docs crea sempre nella radice di «Il mio Drive»: se la credenziale è
    # confinata il file va adottato subito nella cartella consentita.
    folder = gdrive_root.adopt(account, doc_id, "gdocs.create")
    if text:
        _docs(acct, "POST documents/{documentId}:batchUpdate",
              params={"documentId": doc_id}, body={"requests": [
                  {"insertText": {"location": {"index": 1}, "text": text}}]})
    return {"account": acct, "document_id": doc_id, "title": title,
            "folder_id": folder,
            "url": f"https://docs.google.com/document/d/{doc_id}/edit"}


def read(document_id: str, account: Optional[str] = None) -> dict:
    acct = gdrive._resolve_account(account)
    gdrive_root.guard_id(account, document_id, "gdocs.read")
    doc = _docs(acct, "GET documents/{documentId}", params={"documentId": document_id})
    text = _extract_text(doc)
    return {"account": acct, "document_id": document_id,
            "title": doc.get("title"), "text": text, "chars": len(text)}


def append_text(document_id: str, text: str, account: Optional[str] = None) -> dict:
    """Aggiunge testo in fondo al documento."""
    acct = gdrive._resolve_account(account)
    gdrive_root.guard_id(account, document_id, "gdocs.append_text")
    doc = _docs(acct, "GET documents/{documentId}", params={"documentId": document_id})
    end = _end_index(doc)
    _docs(acct, "POST documents/{documentId}:batchUpdate",
          params={"documentId": document_id}, body={"requests": [
              {"insertText": {"location": {"index": max(1, end - 1)}, "text": text}}]})
    return {"account": acct, "document_id": document_id, "appended_chars": len(text), "ok": True}


def replace_text(document_id: str, find: str, replace: str,
                 match_case: bool = True, account: Optional[str] = None) -> dict:
    """Sostituisce TUTTE le occorrenze di `find` con `replace`."""
    acct = gdrive._resolve_account(account)
    gdrive_root.guard_id(account, document_id, "gdocs.replace_text")
    res = _docs(acct, "POST documents/{documentId}:batchUpdate",
                params={"documentId": document_id}, body={"requests": [
                    {"replaceAllText": {"containsText": {"text": find, "matchCase": match_case},
                                        "replaceText": replace}}]})
    occ = (res.get("replies", [{}])[0].get("replaceAllText", {}) or {}).get("occurrencesChanged", 0)
    return {"account": acct, "document_id": document_id, "occurrences": occ, "ok": True}


def _extract_text(doc: dict) -> str:
    out = []
    for el in doc.get("body", {}).get("content", []):
        para = el.get("paragraph")
        if not para:
            continue
        for pe in para.get("elements", []):
            tr = pe.get("textRun")
            if tr and tr.get("content"):
                out.append(tr["content"])
    return "".join(out)


def _end_index(doc: dict) -> int:
    content = doc.get("body", {}).get("content", [])
    return content[-1].get("endIndex", 1) if content else 1
