"""The 24 Google verbs as the agents see them — schemas and descriptions.

Copied VERBATIM from clodia-tools `server/main.py` (`_GDRIVE_TOOLS`,
`_GCALENDAR_TOOLS`, `_GDOCS_TOOLS`, `_GSHEETS_TOOLS`, gateway 2.108.0) when the
verbs moved into google-pack's gateway component (clodia-platform#558): an
agent must see the same tool list in both positions of the switch, so the
descriptions keep their original wording. `test_558_google_component.py`
(clodia-logic) and `test_558_google_component_parity.py` (clodia-tools, with
CLODIA_LOGIC_SRC) compare the two.
"""
from __future__ import annotations

from mcp.types import Tool

_GDRIVE_TOOLS: list[Tool] = [
    Tool(name="gdrive.list",
         description=("Elenca file/cartelle di Google Drive. folder_id per il contenuto di "
                      "una cartella; query per una query Drive (es. \"name contains 'x'\")."),
         inputSchema={"type": "object", "properties": {
             "folder_id": {"type": "string"}, "query": {"type": "string"},
             "limit": {"type": "integer"}, "account": {"type": "string"}}}),
    Tool(name="gdrive.search",
         description="Cerca file/cartelle Drive per nome (match parziale).",
         inputSchema={"type": "object", "properties": {
             "name": {"type": "string"}, "limit": {"type": "integer"},
             "account": {"type": "string"}}, "required": ["name"]}),
    Tool(name="gdrive.mkdir",
         description="Crea una cartella Drive (riusa una omonima nello stesso parent se esiste).",
         inputSchema={"type": "object", "properties": {
             "name": {"type": "string"}, "parent_id": {"type": "string"},
             "account": {"type": "string"}}, "required": ["name"]}),
    Tool(name="gdrive.upload",
         description=("Carica un file su Drive. src = path di un file nello scratch dell'agent "
                      "(prepara con topic.fetch). name = nome su Drive; folder_id = cartella."),
         inputSchema={"type": "object", "properties": {
             "src": {"type": "string"}, "name": {"type": "string"},
             "folder_id": {"type": "string"}, "account": {"type": "string"}},
             "required": ["src"]}),
    Tool(name="gdrive.update",
         description=("Sostituisce il CONTENUTO di un file Drive ESISTENTE con src (path nello "
                      "scratch): stesso file, stesso id, stesso link, stesse condivisioni, e "
                      "Drive tiene la revisione precedente. Usalo per correggere un documento "
                      "già trasmesso invece di caricarne una copia. Su un Google Doc/Sheet/"
                      "Slides nativo il file (es. .docx, .xlsx, .pptx, .md, .txt) viene "
                      "convertito nel documento."),
         inputSchema={"type": "object", "properties": {
             "file_id": {"type": "string"}, "src": {"type": "string"},
             "account": {"type": "string"}}, "required": ["file_id", "src"]}),
    Tool(name="gdrive.download",
         description=("Scarica un file Drive in dest (path scratch dell'agent; poi usa topic.put "
                      "per metterlo nel topic). I Google-doc nativi sono esportati (PDF/xlsx)."),
         inputSchema={"type": "object", "properties": {
             "file_id": {"type": "string"}, "dest": {"type": "string"},
             "account": {"type": "string"}}, "required": ["file_id", "dest"]}),
    Tool(name="gdrive.share",
         description="Condivide un file/cartella Drive con un'email. role: writer (editor, default)|reader|commenter.",
         inputSchema={"type": "object", "properties": {
             "file_id": {"type": "string"}, "email": {"type": "string"},
             "role": {"type": "string"}, "account": {"type": "string"}},
             "required": ["file_id", "email"]}),
    Tool(name="gdrive.rename",
         description="Rinomina un file/cartella Drive (anche sui Shared Drive).",
         inputSchema={"type": "object", "properties": {
             "file_id": {"type": "string"}, "new_name": {"type": "string"},
             "account": {"type": "string"}},
             "required": ["file_id", "new_name"]}),
    Tool(name="gdrive.move",
         description=("Sposta un file/cartella in un'altra cartella Drive "
                      "(folder_id di destinazione; anche sui Shared Drive)."),
         inputSchema={"type": "object", "properties": {
             "file_id": {"type": "string"}, "folder_id": {"type": "string"},
             "account": {"type": "string"}},
             "required": ["file_id", "folder_id"]}),
]

_GCALENDAR_TOOLS: list[Tool] = [
    Tool(name="gcalendar.list_calendars",
         description="Elenca i calendari accessibili con l'account Workspace.",
         inputSchema={"type": "object", "properties": {"account": {"type": "string"}}}),
    Tool(name="gcalendar.list_events",
         description=("Elenca eventi di un calendario in una finestra temporale. "
                      "time_min/time_max ISO8601; query = testo libero opzionale."),
         inputSchema={"type": "object", "properties": {
             "calendar_id": {"type": "string", "description": "default 'primary'"},
             "time_min": {"type": "string"}, "time_max": {"type": "string"},
             "query": {"type": "string"}, "limit": {"type": "integer"},
             "account": {"type": "string"}}}),
    Tool(name="gcalendar.create_event",
         description=("Crea un evento. start/end ISO8601 (dateTime) o date (YYYY-MM-DD "
                      "se all_day=true). attendees = lista di email."),
         inputSchema={"type": "object", "properties": {
             "summary": {"type": "string"}, "start": {"type": "string"},
             "end": {"type": "string"}, "calendar_id": {"type": "string"},
             "description": {"type": "string"}, "location": {"type": "string"},
             "attendees": {"type": "array", "items": {"type": "string"}},
             "all_day": {"type": "boolean"}, "account": {"type": "string"}},
             "required": ["summary", "start", "end"]}),
    Tool(name="gcalendar.update_event",
         description="Modifica un evento esistente (solo i campi passati).",
         inputSchema={"type": "object", "properties": {
             "event_id": {"type": "string"}, "calendar_id": {"type": "string"},
             "summary": {"type": "string"}, "start": {"type": "string"},
             "end": {"type": "string"}, "description": {"type": "string"},
             "location": {"type": "string"}, "account": {"type": "string"}},
             "required": ["event_id"]}),
    Tool(name="gcalendar.delete_event",
         description="Elimina un evento dal calendario.",
         inputSchema={"type": "object", "properties": {
             "event_id": {"type": "string"}, "calendar_id": {"type": "string"},
             "account": {"type": "string"}}, "required": ["event_id"]}),
    Tool(name="gcalendar.freebusy",
         description="Ritorna gli intervalli occupati (busy) in una finestra temporale.",
         inputSchema={"type": "object", "properties": {
             "time_min": {"type": "string"}, "time_max": {"type": "string"},
             "calendar_id": {"type": "string"}, "account": {"type": "string"}},
             "required": ["time_min", "time_max"]}),
]

_GDOCS_TOOLS: list[Tool] = [
    Tool(name="gdocs.create",
         description="Crea un Google Doc (opz. con testo iniziale). Ritorna id + url.",
         inputSchema={"type": "object", "properties": {
             "title": {"type": "string"}, "text": {"type": "string"},
             "account": {"type": "string"}}, "required": ["title"]}),
    Tool(name="gdocs.read",
         description="Legge il testo di un Google Doc (estratto plain-text).",
         inputSchema={"type": "object", "properties": {
             "document_id": {"type": "string"}, "account": {"type": "string"}},
             "required": ["document_id"]}),
    Tool(name="gdocs.append_text",
         description="Aggiunge testo in fondo a un Google Doc.",
         inputSchema={"type": "object", "properties": {
             "document_id": {"type": "string"}, "text": {"type": "string"},
             "account": {"type": "string"}}, "required": ["document_id", "text"]}),
    Tool(name="gdocs.replace_text",
         description="Sostituisce tutte le occorrenze di `find` con `replace` nel Doc.",
         inputSchema={"type": "object", "properties": {
             "document_id": {"type": "string"}, "find": {"type": "string"},
             "replace": {"type": "string"}, "match_case": {"type": "boolean"},
             "account": {"type": "string"}},
             "required": ["document_id", "find", "replace"]}),
]

_GSHEETS_TOOLS: list[Tool] = [
    Tool(name="gsheets.list_tabs",
         description=("Tab di un Google Sheet: titolo, id, posizione e dimensioni. "
                      "Da chiamare prima di leggere o scrivere, per sapere i nomi."),
         inputSchema={"type": "object", "properties": {
             "spreadsheet_id": {"type": "string"}, "account": {"type": "string"}},
             "required": ["spreadsheet_id"]}),
    Tool(name="gsheets.read",
         description=("Legge i valori di un range A1 (es. 'Foglio1!A1:D20') oppure di "
                      "un'intera tab. Senza range né tab legge la PRIMA tab. "
                      "formulas=true restituisce il TESTO delle formule invece del "
                      "valore calcolato: usalo se devi riprodurre il foglio, "
                      "altrimenti una formula ti torna come numero."),
         inputSchema={"type": "object", "properties": {
             "spreadsheet_id": {"type": "string"}, "range": {"type": "string"},
             "tab": {"type": "string"}, "formulas": {"type": "boolean"},
             "account": {"type": "string"}},
             "required": ["spreadsheet_id"]}),
    Tool(name="gsheets.add_tab",
         description=("Aggiunge una tab a un Google Sheet ESISTENTE lasciando intatte "
                      "le altre (mutazione, non riscrittura del file). Errore "
                      "azionabile se il titolo è già in uso."),
         inputSchema={"type": "object", "properties": {
             "spreadsheet_id": {"type": "string"}, "title": {"type": "string"},
             "index": {"type": "integer"}, "account": {"type": "string"}},
             "required": ["spreadsheet_id", "title"]}),
    Tool(name="gsheets.append_rows",
         description=("Aggiunge righe DOPO l'ultima riga popolata di una tab: non "
                      "sovrascrive nulla. È la scrittura da preferire. I valori "
                      "entrano come se digitati (le formule restano formule)."),
         inputSchema={"type": "object", "properties": {
             "spreadsheet_id": {"type": "string"}, "tab": {"type": "string"},
             "rows": {"type": "array", "items": {"type": "array"}},
             "account": {"type": "string"}},
             "required": ["spreadsheet_id", "tab", "rows"]}),
    Tool(name="gsheets.write_range",
         description=("Scrive in un range A1 esplicito SOVRASCRIVENDO le celle "
                      "esistenti. Nessun default e nessuna scorciatoia su tab intera: "
                      "per aggiungere dati usa append_rows."),
         inputSchema={"type": "object", "properties": {
             "spreadsheet_id": {"type": "string"}, "range": {"type": "string"},
             "values": {"type": "array", "items": {"type": "array"}},
             "account": {"type": "string"}},
             "required": ["spreadsheet_id", "range", "values"]}),
]

#: Every verb the component registers, in the gateway's order.
TOOLS: list[Tool] = _GDRIVE_TOOLS + _GCALENDAR_TOOLS + _GDOCS_TOOLS + _GSHEETS_TOOLS
