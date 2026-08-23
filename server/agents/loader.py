"""Discovery + parsing degli agent.yaml in `clodia-data/agents/`.

La directory `agents/` vive sotto la datadir (`CLODIA_DATA/agents`), così
gli agenti persistono indipendentemente dai rebuild dell'immagine.
"""
from __future__ import annotations
import logging
import re
from pathlib import Path
from typing import Iterator, Optional

import yaml
from pydantic import ValidationError

from ..config import data_path
from .models import AgentSpec

LOG = logging.getLogger("agent-server.agents")

AGENTS_DIR = data_path("agents")


def _incoerenze(spec) -> list[str]:
    """Dichiarazioni che si contraddicono, o che mancano dove servirebbero.

    Non solleva e non corregge: SEGNALA. Un'incoerenza fra due campi di un seed
    non è una ragione per fermare la colonia, ma tacerla è come il difetto è
    arrivato in produzione (clodia-platform#227).

    Il caso che ha prodotto questa funzione: l'allowlist dei tool nativi è andata
    in enforcement mentre nessun seed dei pack la dichiarava, quindi tutti sono
    caduti sul pavimento dell'arciseed — che non contiene `Bash`. `fullstack-dev`
    aveva otto comandi in `allow_shell_cmds` e nessun modo di eseguirne uno: la
    sandbox autorizzava una stanza e niente ne dava la porta. Nessuna eccezione,
    nessun log, nessuno stato degradato — se n'è accorto un umano, notando che un
    agente sviluppatore non poteva far girare `pytest`.

    Chi non ha runtime non viene interrogato: un `human` non è eseguito, e un
    `proxy` NON PUÒ dichiarare strumenti nativi (`models.py` rifiuta lo spec).
    Consigliare a un proxy di dichiarare `[]` sarebbe consigliargli un file che
    non carica — e un avviso impossibile da agire, ripetuto a ogni load per ogni
    persona e ogni proxy della colonia, affoga l'unico che va letto.
    """
    if getattr(spec, "type", None) in ("human", "proxy"):
        return []
    fuori: list[str] = []
    nativi = getattr(spec, "native_tools", None)
    sandbox = getattr(spec, "sandbox", None)
    shell = list(getattr(sandbox, "allow_shell_cmds", None) or []) if sandbox else []
    ha_bash = any(str(t).split("(", 1)[0] == "Bash" for t in (nativi or []))

    if nativi is None:
        # `[]` invece è una DICHIARAZIONE («solo il pavimento») e non si segnala:
        # la differenza fra decisione e dimenticanza è tutto il punto.
        fuori.append(
            "`native_tools` non dichiarato: l'allowlist è in enforcement, quindi "
            "questo seed riceve SOLO il pavimento dell'arciseed (niente `Bash`, "
            "niente `Grep`). Dichiara `[]` se è ciò che vuoi")
    if shell and not ha_bash:
        fuori.append(
            f"`allow_shell_cmds` autorizza {len(shell)} comandi ({', '.join(shell[:4])}"
            f"{'…' if len(shell) > 4 else ''}) ma `native_tools` non concede `Bash`: "
            "comandi che l'agente non ha modo di eseguire")
    if ha_bash and not shell:
        fuori.append(
            "`native_tools` concede `Bash` ma `allow_shell_cmds` è vuoto: una "
            "porta su una stanza senza niente dentro")
    return fuori


#: `#tag# ...` — riga firmata da chi l'ha neutralizzata. Il marker parte per
#: forza con un alfanumerico, così `####` di un titolo o `#` di prosa non
#: passano per firme.
_RIGA_MARCATA = re.compile(r"^\s*#(?P<marker>[A-Za-z0-9][A-Za-z0-9_.-]*)#\s*(?P<corpo>.*)$")
#: `# chiave: valore` — un'assegnazione commentata, senza firma.
_CAMPO_COMMENTATO = re.compile(r"^\s*#\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*\S")


def _dichiarazioni_inerti(testo: str, raw: object) -> list[str]:
    """Campi che il seed scrive e nessuno applica, perché sono commentati.

    Il caso che ha prodotto questa funzione (clodia-platform#211): qualcosa ha
    riscritto i seed installati prefissando `#v7compat#` a sette campi VALIDI di
    quattro agenti — fra cui i `gated_tools`, il cui unico scopo è aggiungere un
    consenso umano, e gli `stacks`, senza i quali un agente perdeva il modello
    che il suo pack gli assegna. Nessun codice, nessun log, nessuna traccia: il
    file caricava, l'istanza girava, e girava meno le dichiarazioni. Se n'è
    accorto un umano un mese dopo.

    Non solleva e non ripristina: SEGNALA, sullo stesso canale di `_incoerenze`.
    Ripristinare d'ufficio sarebbe riaccendere per conto di nessuno campi che
    qualcuno ha spento per una ragione scritta da nessuna parte — e su
    `gated_tools` significherebbe cambiare da sé chi deve dare un consenso.

    Due forme, perché la firma è una cortesia e non una garanzia:
    - riga MARCATA (`#v7compat# ...`): si segnala qualunque cosa segua, perché
      ciò che viene spento non è sempre un campo — una voce di lista commentata
      («un `gsheets.write_range`») toglie un permesso allo stesso modo;
    - assegnazione commentata senza firma: si segnala solo se la chiave è un
      campo che lo schema CONOSCE e che il file non dichiara altrove. I seed dei
      pack sono scritti con pagine di prosa commentata in italiano, e un
      rilevatore che segnala la prosa è un rilevatore che nessuno rilegge.
    """
    dichiarati = set(raw.keys()) if isinstance(raw, dict) else set()
    noti = set(AgentSpec.model_fields)
    marcate: dict[str, list[str]] = {}
    fuori: list[str] = []
    for n, riga in enumerate((testo or "").splitlines(), 1):
        m = _RIGA_MARCATA.match(riga)
        if m:
            corpo = m.group("corpo").strip()
            marcate.setdefault(m.group("marker"), []).append(
                f"riga {n} `{corpo[:80]}{'…' if len(corpo) > 80 else ''}`")
            continue
        m = _CAMPO_COMMENTATO.match(riga)
        chiave = m.group("key") if m else None
        if chiave and chiave in noti and chiave not in dichiarati:
            fuori.append(
                f"riga {n}: `{chiave}` è dichiarato solo dentro un commento. Il "
                "campo esiste nello schema, quindi qui non fa niente e niente lo "
                "dice: decommentalo o togli la riga")
    for marker, righe in marcate.items():
        quante = (f"{len(righe)} righe neutralizzate" if len(righe) > 1
                  else "1 riga neutralizzata")
        fuori.append(
            f"{quante} dal marker `{marker}` — "
            "dichiarazioni inerti: quello che il pack dichiara e questo seed non "
            f"applica più (clodia-platform#211). {', '.join(righe)}")
    return fuori


def _messaggio_errore(e: Exception) -> str:
    """Il testo che finisce in `errors()`, con l'alternativa quando serve.

    Un campo ignoto fa fallire il load, ed è il default giusto: `extra="forbid"`
    fallisce CHIUSO, e un seed che dichiara ciò che il codice non implementa è
    meglio fermo che a metà. Ma è anche l'esatto punto in cui qualcuno, davanti
    a un agente che non carica, commenta la riga e tira avanti — e allora il
    load torna verde mentendo. Il messaggio deve nominare questa strada per
    escluderla, dove viene letta.
    """
    msg = str(e)
    if isinstance(e, ValidationError) and any(
            err.get("type") == "extra_forbidden" for err in e.errors()):
        msg += (
            "\n\nNOTA: un campo che lo schema non conosce va RIMOSSO dal seed o "
            "aggiunto allo schema. Non commentarlo: un campo commentato produce "
            "un seed che carica e non fa più ciò che il suo pack dichiara, senza "
            "che niente lo segnali (clodia-platform#211).")
    return msg


class AgentRegistry:
    """Cache in memoria degli agenti definiti. Ricaricabile a runtime
    (utile in dev: edit dell'agent.yaml + POST /api/agents/reload).
    """

    def __init__(self, base_dir: Path = AGENTS_DIR) -> None:
        self.base_dir = base_dir
        self._agents: dict[str, AgentSpec] = {}
        self._errors: dict[str, str] = {}
        #: Seed che CARICANO ma si contraddicono: nome → avvisi. Canale distinto
        #: da `_errors` (dove lo spec non carica affatto) perché sono due esiti
        #: diversi dello stesso load, e fonderli farebbe sparire dalla lista un
        #: agente funzionante — o leggere un errore come una nota a margine.
        self._warnings: dict[str, list[str]] = {}

    def discover(self) -> Iterator[Path]:
        """Yields i path agent.yaml trovati sotto base_dir."""
        if not self.base_dir.is_dir():
            return
        for child in sorted(self.base_dir.iterdir()):
            if not child.is_dir():
                continue
            spec_file = child / "agent.yaml"
            if spec_file.is_file():
                yield spec_file

    def load(self) -> None:
        """Ricarica tutta la registry dal filesystem."""
        self._agents.clear()
        self._errors.clear()
        self._warnings.clear()
        for spec_file in self.discover():
            agent_dir = spec_file.parent
            try:
                testo = spec_file.read_text()
                raw = yaml.safe_load(testo) or {}
                spec = AgentSpec.model_validate(raw)
                spec.agent_dir = str(agent_dir)
                if spec.name != agent_dir.name:
                    raise ValueError(
                        f"agent.name '{spec.name}' non corrisponde alla cartella "
                        f"'{agent_dir.name}' — devono coincidere"
                    )
                # Description derivata dinamicamente da system prompt +
                # capabilities + rules. Sovrascrive la description statica
                # nell'agent.yaml (che resta come seed/fallback).
                derived = _derive_description(spec, agent_dir)
                if derived:
                    spec.description = derived
                if spec.skills:
                    LOG.warning(
                        "agent '%s': campo `skills` DEPRECATO (AgentSpec v2) — "
                        "migrare i file a una skill del data catalog e usare "
                        "`capabilities`", spec.name)
                if spec.can_delegate_to:
                    LOG.warning(
                        "agent '%s': campo `can_delegate_to` DEPRECATO "
                        "(AgentSpec v2) — la delega è il movimento di card",
                        spec.name)
                # `_incoerenze` interroga lo spec CARICATO e salta chi non ha
                # runtime; `_dichiarazioni_inerti` legge il FILE, perché ciò che
                # è commentato non arriva mai allo spec, e un campo spento è
                # muto su qualunque tipo, umani e proxy compresi.
                avvisi = _incoerenze(spec) + _dichiarazioni_inerti(testo, raw)
                for avviso in avvisi:
                    LOG.warning("agent '%s': %s", spec.name, avviso)
                if avvisi:
                    self._warnings[spec.name] = avvisi
                self._agents[spec.name] = spec
                LOG.info("Caricato agent '%s' da %s", spec.name, spec_file)
            except (ValidationError, ValueError, yaml.YAMLError) as e:
                self._errors[agent_dir.name] = _messaggio_errore(e)
                LOG.warning("Errore parsing agent '%s': %s", agent_dir.name, e)

    def get(self, name: str) -> AgentSpec:
        return self._agents[name]

    def list(self) -> list[AgentSpec]:
        return list(self._agents.values())

    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    def warnings(self) -> dict[str, list[str]]:
        """Nome → incoerenze del seed caricato. Assente = niente da dire.

        L'agente resta caricato e usabile: questo canale serve a farlo VEDERE
        (clodia-platform#227), non a impedirlo.
        """
        return {n: list(v) for n, v in self._warnings.items()}

    def get_by_name(self, name: str) -> Optional[AgentSpec]:
        return self._agents.get(name)

    def get_by_telegram(self, handle: Optional[str]) -> Optional[AgentSpec]:
        """Lookup inverso `handle/chat_id Telegram → principal HUMAN registrato`.

        Ritorna lo spec dell'human con `telegram` corrispondente, o None (→ il
        mittente è un proxy, non registrato). Match tollerante: ignora un '@'
        iniziale, gli spazi e il case. Solo `type == "human"`: gli AI non sono
        committenti-umani di un canale."""
        if handle is None:
            return None
        want = str(handle).lstrip("@").strip().lower()
        if not want:
            return None
        for spec in self._agents.values():
            if spec.type != "human" or not spec.telegram:
                continue
            if str(spec.telegram).lstrip("@").strip().lower() == want:
                return spec
        return None


# Singleton globale; caricata al primo accesso, ricaricabile via API.
registry = AgentRegistry()


# ── description derivation ─────────────────────────────────────────


def _first_sentence(text: str, max_len: int = 220) -> str:
    """Prima frase di senso compiuto da `text`: salta righe vuote, header
    markdown, blocchi frontmatter `---`. Ritorna massimo `max_len` char,
    troncando alla fine della prima frase se possibile."""
    in_frontmatter = False
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if line.startswith("#") or line.startswith(">") or line.startswith("```"):
            continue
        # Prendo fino al primo terminatore di frase, altrimenti tronco
        for sep in (". ", "! ", "? "):
            i = line.find(sep)
            if 0 < i <= max_len:
                return line[: i + 1].strip()
        return line[:max_len].rstrip() + ("…" if len(line) > max_len else "")
    return ""


def _derive_description(spec: AgentSpec, agent_dir) -> str:
    """Compone una description sintetica da: prima frase del system-prompt,
    capabilities (skip `kanban-operations` base presente su tutti), rules.
    Pattern deterministico, no LLM call."""
    parts: list[str] = []
    # I principal `human` non hanno system_prompt (non eseguiti).
    if spec.system_prompt:
        sp_path = agent_dir / spec.system_prompt
        if sp_path.is_file():
            sent = _first_sentence(sp_path.read_text())
            if sent:
                parts.append(sent)
    caps = [c for c in (spec.capabilities or []) if c != "kanban-operations"]
    if caps:
        parts.append("Skill: " + ", ".join(caps) + ".")
    if spec.rules:
        parts.append("Rules: " + ", ".join(spec.rules) + ".")
    return " ".join(parts)


registry.load()
