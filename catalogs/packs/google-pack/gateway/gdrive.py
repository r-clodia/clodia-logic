"""Google Drive tool exposed via MCP — export/import di file fra i topic e una
cartella Drive condivisa.

Riusa le credenziali OAuth Google Workspace già nel vault (`gworkspace_<account>`,
depositate dal flusso "Connetti Google Workspace": client_id/secret/refresh_token
+ scope `drive` pieno). Il segreto resta nel gateway: every API call goes through
`google_io.call` (clodia-platform#524), which refreshes the access token there;
it never reaches the model.

Moved into google-pack's gateway component (clodia-platform#558): the vault is
reached through the host's scoped credentials (`_host`), and the whitelist
check of the verb is the dispatcher's (`call_tool`), done before the handler
runs — exactly where it is done for a native verb.

Trasferimento file via **scratch** dell'agent (come topic.fetch/put): i byte non
transitano in base64 nel contesto. Flusso tipico orchestrato da clodia:
  export:  topic.fetch → gdrive.upload
  import:  gdrive.download → topic.put
"""
from __future__ import annotations

from typing import Optional

from . import _host
from . import gdrive_root
from . import google_io

_FOLDER_MIME = "application/vnd.google-apps.folder"
# Export di default per i Google-native doc (non scaricabili con get_media).
_GOOGLE_EXPORT = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet":
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
}
# shortcutDetails è obbligatorio: gdrive_root decide sulla riga, e senza
# questo campo una scorciatoia passerebbe il filtro degli elenchi.
_FIELDS = ("id, name, mimeType, modifiedTime, size, webViewLink, parents, "
           "md5Checksum, shortcutDetails")
# Shared Drive (Team Drive): senza questi flag l'API vede solo il "My Drive".
# supportsAllDrives → opera anche su file in shared drive; includeItemsFromAllDrives
# + corpora=allDrives → list/search includono i contenuti dei shared drive.
_ALL_DRIVES = {"supportsAllDrives": True, "includeItemsFromAllDrives": True}


def _drive_cred(account: str) -> str:
    """Nome credenziale per Drive/Docs/Calendar: preferisci la Google UNIFICATA
    (google_<account>, che include gli scope Workspace); fallback al legacy."""
    return (f"google_{account}" if _host.has_credential(f"google_{account}")
            else f"gworkspace_{account}")


def gworkspace_accounts() -> list[str]:
    """Account Workspace disponibili = credenziali google_<account> (unificata,
    ha gli scope Drive/Docs/Calendar) o legacy gworkspace_<account> nel vault."""
    accts = {n[len("google_"):] for n in _host.names("google_")}
    accts |= {n[len("gworkspace_"):] for n in _host.names("gworkspace_")}
    return sorted(accts)


def _resolve_account(account: Optional[str]) -> str:
    accts = gworkspace_accounts()
    if not accts:
        raise RuntimeError(
            "nessun account Google Workspace nel vault: connettilo da "
            "Tools → Google Workspace")
    if account:
        if account not in accts:
            raise ValueError(f"account '{account}' sconosciuto; disponibili: {accts}")
        return account
    return accts[0]


def _drive(account: str, method_path: str, **kw):
    """One Drive v3 call for a resolved `account`, through the Google seam."""
    return google_io.call(account, "drive", "v3", method_path, **kw)


def _clean(f: dict) -> dict:
    out = {k: f.get(k) for k in ("id", "name", "mimeType", "modifiedTime",
                                 "size", "webViewLink") if f.get(k) is not None}
    # md5Checksum esposto come "md5" → simmetrico a topic.files, per il verbo `same`.
    if f.get("md5Checksum"):
        out["md5"] = f["md5Checksum"]
    return out


# ── Tool esposti via MCP ─────────────────────────────────────────────────────

def list_files(folder_id: Optional[str] = None, query: Optional[str] = None,
               limit: int = 50, account: Optional[str] = None) -> dict:
    """Elenca file/cartelle di Drive. `folder_id` per il contenuto di una cartella;
    `query` per una query Drive arbitraria (es. \"name contains 'report'\")."""
    acct = _resolve_account(account)
    clauses = ["trashed = false"]
    if folder_id:
        clauses.append(f"'{folder_id}' in parents")
    if query:
        clauses.append(f"({query})")
    res = _drive(acct, "GET files", params={
        "q": " and ".join(clauses), "pageSize": max(1, min(int(limit), 1000)),
        "fields": f"files({_FIELDS})", "orderBy": "folder,name",
        "corpora": "allDrives", **_ALL_DRIVES})
    # Il confinamento sta nel FILTRO degli id, non in una clausola aggiunta a
    # `query`: quella stringa la controlla il chiamante.
    rows = gdrive_root.keep_inside(acct, res.get("files", []))
    return {"account": acct, "files": [_clean(f) for f in rows]}


def search(name: str, limit: int = 20, account: Optional[str] = None) -> dict:
    """Cerca per nome (match parziale, case-insensitive). Include i Shared Drive."""
    if not name:
        raise ValueError("'name' non può essere vuoto")
    acct = _resolve_account(account)
    safe = name.replace("'", "\\'")
    res = _drive(acct, "GET files", params={
        "q": f"name contains '{safe}' and trashed = false",
        "pageSize": max(1, min(int(limit), 1000)),
        "fields": f"files({_FIELDS})", "orderBy": "folder,name",
        "corpora": "allDrives", **_ALL_DRIVES})
    rows = gdrive_root.keep_inside(acct, res.get("files", []))
    return {"account": acct, "files": [_clean(f) for f in rows]}


def mkdir(name: str, parent_id: Optional[str] = None,
          account: Optional[str] = None) -> dict:
    """Crea una cartella (idempotenza non garantita: due chiamate → due cartelle).
    Cerca prima una cartella omonima nello stesso parent e la riusa se esiste."""
    if not name:
        raise ValueError("'name' non può essere vuoto")
    acct = _resolve_account(account)
    parent_id = gdrive_root.assert_writable_parent(acct, parent_id, "gdrive.mkdir")
    safe = name.replace("'", "\\'")
    q = (f"name = '{safe}' and mimeType = '{_FOLDER_MIME}' and trashed = false"
         + (f" and '{parent_id}' in parents" if parent_id else ""))
    existing = _drive(acct, "GET files", params={
        "q": q, "fields": f"files({_FIELDS})", "corpora": "allDrives",
        **_ALL_DRIVES}).get("files", [])
    if existing:
        return {"account": acct, "reused": True, **_clean(existing[0])}
    body = {"name": name, "mimeType": _FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    f = _drive(acct, "POST files", body=body,
               params={"fields": _FIELDS, "supportsAllDrives": True})
    return {"account": acct, "reused": False, **_clean(f)}


def upload(src: str, name: Optional[str] = None, folder_id: Optional[str] = None,
           account: Optional[str] = None) -> dict:
    """Carica un file su Drive. `src` è un path locale (scratch dell'agent, già
    validato dal dispatch). `name` default = nome del file; `folder_id` = cartella
    di destinazione."""
    import os
    acct = _resolve_account(account)
    folder_id = gdrive_root.assert_writable_parent(acct, folder_id, "gdrive.upload")
    fname = name or os.path.basename(src)
    body = {"name": fname}
    if folder_id:
        body["parents"] = [folder_id]
    # Multipart upload (metadata + bytes), mimetype guessed from the name.
    f = _drive(acct, "POST files", body=body, media=google_io.Media(src),
               params={"fields": _FIELDS, "supportsAllDrives": True})
    return {"account": acct, "uploaded": True, **_clean(f)}


#: Formati che Drive sa importare in un Google Doc/Sheet/Slides nativo: con
#: questi, `update` su un file nativo ne sostituisce il contenuto convertendo.
_IMPORT_INTO_NATIVE = {
    "application/vnd.google-apps.document": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword", "application/rtf", "text/plain", "text/html",
        "application/vnd.oasis.opendocument.text", "text/markdown"},
    "application/vnd.google-apps.spreadsheet": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel", "text/csv",
        "application/vnd.oasis.opendocument.spreadsheet"},
    "application/vnd.google-apps.presentation": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint"},
}


def update(file_id: str, src: str, account: Optional[str] = None) -> dict:
    """Sostituisce il CONTENUTO di un file Drive esistente con `src` (path nello
    scratch, già validato dal dispatch), lasciando lo stesso file: stesso id,
    stesso link, stesse condivisioni, e Drive ne tiene la revisione precedente.

    Esiste perché senza, per correggere un documento già trasmesso, l'unica via
    era caricarne una COPIA nuova (clodia-platform#427, 28 set 2026: «serve il
    verbo per modificare i gdoc esistenti senza creare nuove copie»).

    Su un Google Doc/Sheet/Slides NATIVO il file caricato viene convertito nel
    formato del documento (es. un .docx dentro un Google Doc): è l'import di
    Drive. Un formato che Drive non sa importare in quel tipo è rifiutato prima
    di scrivere.
    """
    import mimetypes as _mt
    import os
    if not os.path.isfile(src):
        raise ValueError(f"gdrive.update: file sorgente non trovato: {src}")
    acct = _resolve_account(account)
    gdrive_root.assert_inside(acct, file_id, "gdrive.update")
    meta = _drive(acct, "GET files/{fileId}", params={
        "fileId": file_id, "fields": "id, name, mimeType", "supportsAllDrives": True})
    target = meta.get("mimeType", "")
    src_mime = _mt.guess_type(src)[0] or "application/octet-stream"
    if src.lower().endswith(".md"):
        src_mime = "text/markdown"
    if target == "application/vnd.google-apps.folder":
        raise ValueError("gdrive.update: l'id è una cartella, non un file")
    if target.startswith("application/vnd.google-apps."):
        ammessi = _IMPORT_INTO_NATIVE.get(target)
        if not ammessi:
            raise ValueError(f"gdrive.update: tipo Google non aggiornabile: {target}")
        if src_mime not in ammessi:
            raise ValueError(
                f"gdrive.update: {os.path.basename(src)} ({src_mime}) non si può importare "
                f"in un {target.rsplit('.', 1)[-1]}: usa uno di {sorted(ammessi)}")
    # Media-only upload (no metadata): the content of the same file is replaced.
    f = _drive(acct, "PATCH files/{fileId}", media=google_io.Media(src, src_mime),
               params={"fileId": file_id, "fields": _FIELDS, "supportsAllDrives": True})
    return {"account": acct, "updated": True, "same_file": f.get("id") == file_id, **_clean(f)}


def download(file_id: str, dest: str, account: Optional[str] = None) -> dict:
    """Scarica un file di Drive in `dest` (path scratch dell'agent, già validato
    dal dispatch). I Google-native doc (Docs/Sheets/Slides) vengono esportati
    (PDF/xlsx)."""
    acct = _resolve_account(account)
    gdrive_root.assert_inside(acct, file_id, "gdrive.download")
    meta = _drive(acct, "GET files/{fileId}", params={
        "fileId": file_id, "fields": "id, name, mimeType", "supportsAllDrives": True})
    mime = meta.get("mimeType", "")
    if mime in _GOOGLE_EXPORT:
        export_mime, _ext = _GOOGLE_EXPORT[mime]
        data = _drive(acct, "GET files/{fileId}/export", download=True,
                      params={"fileId": file_id, "mimeType": export_mime})
    elif mime.startswith("application/vnd.google-apps"):
        raise ValueError(f"tipo Google non esportabile: {mime}")
    else:
        data = _drive(acct, "GET files/{fileId}", download=True,
                      params={"fileId": file_id, "supportsAllDrives": True})
    with open(dest, "wb") as fh:
        fh.write(data)
    return {"account": acct, "downloaded": True, "file_id": file_id,
            "name": meta.get("name"), "mimeType": mime, "local_path": dest,
            "size": len(data)}


def share(file_id: str, email: str, role: str = "writer",
          account: Optional[str] = None) -> dict:
    """Condivide un file/cartella con un'email. role: writer (editor, default),
    reader, commenter. Notifica l'utente via email."""
    if "@" not in (email or ""):
        raise ValueError(f"email non valida: '{email}'")
    if role not in ("writer", "reader", "commenter"):
        raise ValueError("role deve essere writer | reader | commenter")
    acct = _resolve_account(account)
    gdrive_root.assert_inside(acct, file_id, "gdrive.share")
    perm = _drive(acct, "POST files/{fileId}/permissions",
                  body={"type": "user", "role": role, "emailAddress": email},
                  params={"fileId": file_id, "sendNotificationEmail": True,
                          "supportsAllDrives": True, "fields": "id"})
    meta = _drive(acct, "GET files/{fileId}", params={
        "fileId": file_id, "fields": "id, name, webViewLink", "supportsAllDrives": True})
    return {"account": acct, "shared": True, "permission_id": perm.get("id"),
            "email": email, "role": role, **_clean(meta)}


def rename(file_id: str, new_name: str, account: Optional[str] = None) -> dict:
    """Rinomina un file/cartella Drive (files.update; funziona anche sui
    Shared Drive via supportsAllDrives)."""
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("'new_name' non può essere vuoto")
    acct = _resolve_account(account)
    gdrive_root.assert_inside(acct, file_id, "gdrive.rename")
    f = _drive(acct, "PATCH files/{fileId}", body={"name": new_name},
               params={"fileId": file_id, "fields": _FIELDS, "supportsAllDrives": True})
    return {"account": acct, "renamed": True, **_clean(f)}


def move(file_id: str, folder_id: str, account: Optional[str] = None) -> dict:
    """Sposta un file/cartella in un'altra cartella Drive: rimpiazza i parent
    correnti con `folder_id` (files.update addParents/removeParents,
    supportsAllDrives → ok anche sui Shared Drive)."""
    if not (folder_id or "").strip():
        raise ValueError("'folder_id' non può essere vuoto")
    acct = _resolve_account(account)
    # Entrambi gli estremi: senza il controllo sulla destinazione uno
    # spostamento porterebbe il file FUORI dal perimetro, che è esfiltrazione
    # per rilocazione — non serve leggere niente per riuscirci.
    gdrive_root.assert_inside(acct, file_id, "gdrive.move")
    gdrive_root.assert_inside(acct, folder_id.strip(), "gdrive.move (destinazione)")
    cur = _drive(acct, "GET files/{fileId}", params={
        "fileId": file_id, "fields": "parents", "supportsAllDrives": True})
    prev = ",".join(cur.get("parents") or [])
    f = _drive(acct, "PATCH files/{fileId}", params={
        "fileId": file_id, "addParents": folder_id.strip(), "removeParents": prev or None,
        "fields": _FIELDS, "supportsAllDrives": True})
    return {"account": acct, "moved": True, "from": prev or None,
            "to": folder_id.strip(), **_clean(f)}
