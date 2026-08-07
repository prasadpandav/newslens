"""Deterministic ticker resolution.

The LLM never authors a symbol. It names a company as the article named it, and
this module decides whether that name maps to a ticker. Anything not in
tickers.yaml comes back unresolved, with the name preserved — the failure mode
is "we know the company, not the symbol", never a confident wrong symbol.

Loaded once at import (same pattern as agents.PROMPTS and Verifier's
sources.yaml) so a 40-entity story costs no file I/O.
"""
import re

import yaml

from .. import config
from ..schemas.finance import EntityType, Ticker

# Legal suffixes stripped before matching, so "Reliance Industries Ltd." and
# "Reliance Industries" are one name without needing an alias for each form.
_SUFFIXES = (
    "ltd", "limited", "inc", "incorporated", "corp", "corporation", "plc",
    "llc", "llp", "lp", "co", "company", "pvt", "private", "nv", "sa", "ag",
    "spa", "gmbh", "holdings", "holding", "group", "the",
)

# Institutions that are never tickers but must be typed correctly, or the graph
# ends up with "SEBI" as an organisation that could in principle be acquired.
REGULATORS = {
    "reserve bank of india": "RBI", "rbi": "RBI",
    "securities and exchange board of india": "SEBI", "sebi": "SEBI",
    "insurance regulatory and development authority of india": "IRDAI",
    "irdai": "IRDAI",
    "competition commission of india": "CCI", "cci": "CCI",
    "national company law tribunal": "NCLT", "nclt": "NCLT",
    "enforcement directorate": "ED",
    "income tax department": "Income Tax Department",
    "goods and services tax council": "GST Council", "gst council": "GST Council",
    "securities and exchange commission": "SEC", "sec": "SEC",
    "federal reserve": "Federal Reserve", "fed": "Federal Reserve",
    "european central bank": "ECB", "ecb": "ECB",
    "bank of england": "Bank of England",
    "international monetary fund": "IMF", "imf": "IMF",
    "world bank": "World Bank",
    "financial conduct authority": "FCA",
    "federal trade commission": "FTC",
    "department of justice": "DOJ",
}


def normalise(name):
    """Lowercase, drop punctuation, drop legal suffixes, collapse whitespace.

    Ampersands are kept as a word ("and") rather than deleted: "L&T" and
    "Larsen & Toubro" must not collapse onto "lt" and "larsen toubro"
    inconsistently."""
    s = (name or "").casefold().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    words = [w for w in s.split() if w]
    while words and words[-1] in _SUFFIXES:
        words.pop()
    while words and words[0] in _SUFFIXES:
        words.pop(0)
    return " ".join(words)


def _load():
    """{normalised name: (symbol, exchange, kind)}.

    A name that two entries claim is dropped from the map entirely. Ambiguity
    resolved by picking one is how a wrong symbol gets stored; ambiguity
    resolved by resolving nothing is merely incomplete.
    """
    doc = yaml.safe_load(config.TICKERS_FILE.read_text()) or {}
    index, clashes = {}, set()
    for kind, rows in (("company", doc.get("companies") or []),
                       ("index", doc.get("indices") or [])):
        for row in rows:
            sym = str(row.get("symbol") or "").strip()
            if not sym:
                continue
            exch = str(row.get("exchange") or "").strip()
            names = [row.get("name") or ""] + list(row.get("aliases") or [])
            for n in names:
                key = normalise(n)
                if not key:
                    continue
                if key in index and index[key][0] != sym:
                    clashes.add(key)
                index[key] = (sym, exch, kind)
    for key in clashes:
        index.pop(key, None)
    return index, clashes


_INDEX, _CLASHES = _load()


def resolve(name):
    """A Ticker for `name`, resolved or not. Never raises: an unresolvable name
    is a normal outcome that the caller records in EntityTable.unresolved."""
    key = normalise(name)
    hit = _INDEX.get(key)
    if not hit:
        return Ticker(name=(name or "").strip() or "unknown", resolved=False)
    sym, exch, _kind = hit
    return Ticker(name=(name or "").strip(), symbol=sym, exchange=exch,
                  resolved=True, resolver="tickers.yaml")


def type_of(name, proposed=None):
    """Best entity type for `name`, preferring what we can prove.

    A name in the regulator table is a REGULATOR whatever the model said; a
    name in the ticker map is an ORGANIZATION or INDEX. Only when neither knows
    the name does the model's proposal stand — and an unrecognised proposal
    falls back to ORGANIZATION, which is the common case in business copy."""
    key = normalise(name)
    if key in REGULATORS or (name or "").casefold() in REGULATORS:
        return EntityType.REGULATOR
    hit = _INDEX.get(key)
    if hit:
        return EntityType.INDEX if hit[2] == "index" else EntityType.ORGANIZATION
    if isinstance(proposed, EntityType):
        return proposed
    try:
        return EntityType(str(proposed).strip().casefold())
    except ValueError:
        return EntityType.ORGANIZATION


def canonical(name):
    """The canonical spelling for a name, so the graph doesn't hold "Reliance",
    "RIL" and "Reliance Industries Ltd" as three separate nodes. Falls back to
    the trimmed input when we have no opinion."""
    key = normalise(name)
    if key in REGULATORS:
        return REGULATORS[key]
    hit = _INDEX.get(key)
    if hit:
        return hit[0]                    # the symbol IS the canonical id
    return (name or "").strip()


def stats():
    """For the admin surface / --unresolved report."""
    return {"names_indexed": len(_INDEX),
            "symbols": len({v[0] for v in _INDEX.values()}),
            "ambiguous_dropped": sorted(_CLASHES),
            "regulators": len(set(REGULATORS.values()))}
