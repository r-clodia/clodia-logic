"""Bare acknowledgements do not start agent turns (clodia-platform#551).

«ok», «ricevuto», «grazie», «👍» carry no request. Before this module every
one of them opened a turn: a human «grazie» woke the coordinator to say
«prego», and two agents that tag each other with «@clodia Ok / Ricevuto /
resto disponibile» — «Nessuna azione richiesta» burned the 15 delegation hops
of a chain (SEAL-1/software-house).

A message is a bare acknowledgement when ALL of the following hold:

- it has no attachment (a file is content, whatever the text says);
- it has no question mark (``ricevuto?`` asks something);
- once the ``@mentions`` are removed, what remains is short (``max_chars``)
  and not empty (a lone ``@clodia`` is a summons, not a thank-you);
- every clause of that remainder — split on ``, . ; : ! / - –`` and newlines —
  is either a curated phrase, matches a curated regex, or is made only of
  acknowledgement emoji (👍 👌 ✅ 🙏 …, skin tones allowed).

So ``ok, grazie!`` and ``@clodia ok`` are acknowledgements, while
``ok, procedi con il preventivo``, ``ok fai X`` and ``ricevuto?`` are not:
one clause that is not on the list is a request, and it routes as before.

The message is stored and shown as usual; only the turn is not started.

Configuration: the ``acknowledgements`` section of ``catalogs/router.yaml``,
overridable per instance in ``CLODIA_DATA/routing/router.yaml`` (same files,
same live reload as the rest of the router config). Keys: ``enabled``,
``max_chars``, ``phrases``, ``patterns``, ``emoji``. A list given in the
instance file REPLACES the default list, it does not extend it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re
import unicodedata

from . import mentions, router_config

LOG = logging.getLogger("agent-server.acknowledgement")

# Affirmative ANSWERS («sì», «yes», «certo», «procedi», «done»/«fatto») are
# deliberately absent: after «Procedo?» they are the go-ahead, and swallowing
# them would stall the work they authorise.
DEFAULT_PHRASES: tuple[str, ...] = (
    # Italian
    "ok", "okay", "ok grazie", "va bene", "d'accordo", "daccordo",
    "perfetto", "perfetta", "ottimo", "benissimo", "bene",
    "ricevuto", "ricevuta", "ricevuti", "ricevute", "ricevuto grazie",
    "grazie", "grazie mille", "mille grazie", "grazie ancora", "grazie a te",
    "chiaro", "tutto chiaro", "capito", "inteso", "preso nota", "ne prendo nota",
    "nota presa", "a posto", "tutto a posto", "nessun problema", "figurati",
    "di nulla", "prego", "buon lavoro", "resto disponibile",
    "resto a disposizione", "resto in attesa", "nessuna azione richiesta",
    # English
    "got it", "thanks", "thank you", "thanks a lot", "many thanks", "noted",
    "understood", "roger", "roger that", "ack", "acknowledged", "received",
    "sounds good", "great", "perfect", "cool", "nice", "all good", "no worries",
    "no problem", "cheers", "will do", "standing by", "no action needed",
    "no action required",
)

DEFAULT_PATTERNS: tuple[str, ...] = (
    r"o+k+(?:a+y+|e+i+)?",       # ok, okk, okay, okei
    r"k{1,2}",                   # k, kk
    r"grazie+",                  # grazieee
    r"thx|tnx|ty|tks",
)

DEFAULT_EMOJI: tuple[str, ...] = (
    "👍", "👌", "✅", "🙏", "👏", "🆗", "✔", "☑", "🤝", "🙌",
)

DEFAULT_MAX_CHARS = 60

# Everything that may decorate an emoji without changing what it says: skin
# tones, variation selectors, zero-width joiner.
_EMOJI_DECORATION = re.compile("[\U0001F3FB-\U0001F3FF︎️‍]")
_CLAUSE_SPLIT = re.compile(r"[,.;:!/\n\r–—\-…]+")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class AckConfig:
    enabled: bool = True
    max_chars: int = DEFAULT_MAX_CHARS
    phrases: frozenset = field(default_factory=lambda: frozenset(DEFAULT_PHRASES))
    patterns: tuple = field(default_factory=lambda: tuple(
        re.compile(p) for p in DEFAULT_PATTERNS))
    emoji: frozenset = field(default_factory=lambda: frozenset(DEFAULT_EMOJI))


_CACHE_KEY: tuple | None = None
_CACHE = AckConfig()


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = text.replace("’", "'").replace("`", "'")
    return _SPACES.sub(" ", text).strip()


def _validated(raw) -> AckConfig:
    if raw is None:
        return AckConfig()
    if not isinstance(raw, dict):
        raise ValueError("acknowledgements must be a mapping")
    d = AckConfig()
    enabled = bool(raw.get("enabled", d.enabled))
    max_chars = int(raw.get("max_chars", d.max_chars))
    if not 1 <= max_chars <= 500:
        raise ValueError("acknowledgements.max_chars must be between 1 and 500")

    def _strings(key: str, default):
        val = raw.get(key)
        if val is None:
            return default
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            raise ValueError(f"acknowledgements.{key} must be a list of strings")
        return val

    phrases = frozenset(_norm(p) for p in _strings("phrases", d.phrases) if _norm(p))
    try:
        patterns = tuple(re.compile(p) if isinstance(p, str) else p
                         for p in _strings("patterns", d.patterns))
    except re.error as exc:
        raise ValueError(f"acknowledgements.patterns: invalid regex ({exc})") from exc
    emoji = frozenset(_EMOJI_DECORATION.sub("", e)
                      for e in _strings("emoji", d.emoji) if e.strip())
    return AckConfig(enabled, max_chars, phrases, patterns, emoji)


def load() -> AckConfig:
    """The live configuration: versioned defaults, then the instance override.

    Section-level merge, like the rest of the router config: an instance file
    that defines ``acknowledgements.phrases`` replaces the default phrases and
    keeps the default patterns and emoji.
    """
    global _CACHE_KEY, _CACHE
    key = (router_config._signature(router_config._REPO_PATH),
           router_config._signature(router_config._LIVE_PATH))
    if key == _CACHE_KEY:
        return _CACHE
    merged: dict = {}
    for path in (router_config._REPO_PATH, router_config._LIVE_PATH):
        section = router_config._read(path).get("acknowledgements")
        if isinstance(section, dict):
            merged.update(section)
        elif section is not None:
            LOG.warning("router config %s: acknowledgements is not a mapping, ignored",
                        path)
    try:
        config = _validated(merged)
    except (TypeError, ValueError) as exc:
        LOG.warning("acknowledgements config invalid (%s): using defaults", exc)
        config = AckConfig()
    _CACHE_KEY = key
    _CACHE = config
    return config


def _emoji_only(clause: str, cfg: AckConfig) -> bool:
    rest = _EMOJI_DECORATION.sub("", clause).replace(" ", "")
    if not rest:
        return False
    for e in sorted(cfg.emoji, key=len, reverse=True):
        rest = rest.replace(e, "")
    return rest == ""


def _clause_is_ack(clause: str, cfg: AckConfig) -> bool:
    if _emoji_only(clause, cfg):
        return True
    # A trailing emoji on a phrase («grazie 🙏») is still the phrase.
    words = clause
    for e in cfg.emoji:
        words = words.replace(e, " ")
    words = _SPACES.sub(" ", _EMOJI_DECORATION.sub("", words)).strip()
    if not words:
        return False
    if words in cfg.phrases:
        return True
    return any(p.fullmatch(words) for p in cfg.patterns)


def is_bare_acknowledgement(text: str | None, attachments=None, *,
                            config: AckConfig | None = None) -> bool:
    """True when ``text`` only acknowledges and must not start a turn."""
    cfg = config or load()
    if not cfg.enabled or attachments:
        return False
    if not text or "?" in text or "？" in text:
        return False
    remainder = _norm(mentions._MENTION_RE.sub(" ", text))
    if not remainder or len(remainder) > cfg.max_chars:
        return False
    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(remainder)]
    clauses = [c for c in clauses if c]
    if not clauses:
        return False
    return all(_clause_is_ack(c, cfg) for c in clauses)
