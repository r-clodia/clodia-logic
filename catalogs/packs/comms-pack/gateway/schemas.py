"""The eight `email.*` verbs as the agents see them — schemas and descriptions.

Copied VERBATIM from clodia-tools `server/main.py` (`_EMAIL_TOOLS`, gateway
2.109.0) when the verbs moved into comms-pack's gateway component
(clodia-platform#558): an agent must see the same tool list in both positions
of the switch `connectors: {mail: native|pack}`, so the descriptions keep their
original wording (Italian included). `server/agents/test_558_mail_component.py`
(clodia-logic, with CLODIA_TOOLS_SRC) and `server/test_558_mail_component.py`
(clodia-tools, with CLODIA_LOGIC_SRC) compare the two.
"""
from __future__ import annotations

from mcp.types import Tool


TOOLS: list[Tool] = [
    Tool(
        name="email.send",
        description=(
            "Send an email. Specify the sender mailbox via `account` (the account "
            "name, e.g. as shown by email.folders or your instructions). "
            "Plain-text body, optional CC and local file attachments."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "recipient email address"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "account": {
                    "type": "string",
                    "description": "sender mailbox account name (required if you have more than one)",
                },
                "cc": {"type": "string", "description": "optional CC address"},
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "optional local file paths to attach",
                },
                "topic_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("allegati PER RIFERIMENTO: path di file del topic "
                                    "(es. 'files/contratto.pdf'). Il gateway li legge e "
                                    "li allega senza che il contenuto entri nel tuo "
                                    "contesto — non serve scaricarli prima, e non "
                                    "consumano token."),
                },
                "tier": {"type": "string", "description": ("tier del topic per topic_files. "
                          "OPZIONALE: senza, si usa il topic del canale in cui stai "
                          "lavorando")},
                "name": {"type": "string", "description": "nome del topic (opzionale, vedi tier)"},
            },
            "required": ["to", "subject", "body"],
        },
    ),
    Tool(
        name="email.folders",
        description="List the IMAP folders of an account (pass the account name in `account`).",
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string",
                            "description": "account name to inspect"},
            },
        },
    ),
    Tool(
        name="email.list",
        description="List messages of a folder (default INBOX) for a configured account.",
        inputSchema={
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "folder": {"type": "string", "description": "IMAP folder, default INBOX"},
                "limit": {"type": "integer", "description": "max messages, default 10"},
            },
        },
    ),
    Tool(
        name="email.read",
        description="Read a single message by its IMAP id from a folder (default INBOX).",
        inputSchema={
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "IMAP message id"},
                "account": {"type": "string"},
                "folder": {"type": "string", "description": "IMAP folder, default INBOX"},
            },
            "required": ["email_id"],
        },
    ),
    Tool(
        name="email.get_attachment",
        description=("Contenuto di un allegato di un messaggio, in base64 (per nome file). "
                     "SOLO per allegati piccoli/testuali: per PDF, immagini e binari usa "
                     "email.save_attachment (il base64 non passa dal modello). "
                     "Usa email.read per scoprire i nomi degli allegati."),
        inputSchema={
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "IMAP message id"},
                "filename": {"type": "string", "description": "nome esatto dell'allegato (da email.read)"},
                "account": {"type": "string"},
                "folder": {"type": "string", "description": "IMAP folder, default INBOX"},
            },
            "required": ["email_id", "filename"],
        },
    ),
    Tool(
        name="email.save_attachment",
        description=("Scarica un allegato senza che i byte passino dal contesto del "
                     "modello — usa QUESTO per PDF, immagini e binari. Due "
                     "destinazioni, ne serve una: `tier`+`name` per archiviarlo "
                     "DIRETTAMENTE nei file di un topic (il gateway lo scrive, "
                     "provenienza `untrusted` e il canale risulta contaminato: non "
                     "serve topic.put), oppure `dest` per un path nel tuo scratch se "
                     "devi lavorarlo. Usa email.read per scoprire i nomi degli "
                     "allegati."),
        inputSchema={
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "IMAP message id"},
                "filename": {"type": "string", "description": "nome esatto dell'allegato (da email.read)"},
                "dest": {"type": "string", "description": ("path assoluto nel tuo "
                          "scratch. Alternativa a tier+name: se devi solo archiviare "
                          "l'allegato, usa tier+name e i byte non passano da te")},
                "tier": {"type": "string", "description": ("tier del topic in cui archiviare. "
                          "OPZIONALE: senza `dest` e senza tier/name l'allegato va nel "
                          "topic del canale in cui stai lavorando")},
                "name": {"type": "string", "description": "nome del topic (opzionale, vedi tier)"},
                "account": {"type": "string"},
                "folder": {"type": "string", "description": "IMAP folder, default INBOX"},
            },
            # `dest` non è più obbligatorio: serve UNA delle due destinazioni, e il
            # dispatch lo verifica con un errore che dice quali sono. Uno schema che
            # imponesse entrambe costringerebbe a passare un path finto per
            # archiviare in un topic.
            "required": ["email_id", "filename"],
        },
    ),
    Tool(
        name="email.search",
        description="Search messages with an IMAP query (e.g. FROM \"x@y.it\") in a folder.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "IMAP search query"},
                "account": {"type": "string"},
                "folder": {"type": "string", "description": "IMAP folder, default INBOX"},
                "limit": {"type": "integer", "description": "max results, default 20"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="email.reply",
        description="Reply to a message keeping the thread (plain-text body, optional CC and local attachments).",
        inputSchema={
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "id of the message to reply to"},
                "body": {"type": "string"},
                "account": {"type": "string"},
                "folder": {"type": "string", "description": "IMAP folder, default INBOX"},
                "cc": {"type": "string", "description": "optional CC address"},
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "optional local file paths to attach",
                },
            },
            "required": ["email_id", "body"],
        },
    ),
]
