"""Keuzeregel voor /add met tekst (fase 2). Zie docs/ARCHITECTUUR.md 3.4.

Doet zelf geen requests: werkt op de database (woordenboek, historie) en de
gecachte Favorieten. Volgorde:

1. woordenboek (aliases) van het huishouden;
2. kandidaten uit historie (laatste 18 maanden) en Favorieten waarvan de naam
   alle woorden van de term bevat, of van een vertaling uit terms.json;
3. toevoegen zonder vragen alleen bij precies één kandidaat, of als de beste
   minstens 80 procent van de recente aankopen van alle kandidaten heeft;
4. anders terugvragen met maximaal 4 opties.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

TERMS = json.loads((Path(__file__).parent / "terms.json").read_text(encoding="utf-8"))

HISTORY_DAYS = 548  # 18 maanden
RECENT_DAYS = 365
WIN_SHARE = 0.8
MAX_OPTIONS = 4

_STOPWORDS = {
    "de", "het", "een", "wat", "nog", "paar", "graag", "ook", "even",
    "the", "a", "an", "some", "more", "of", "please", "also",
}
_KEEP = re.compile(r"[^\w\s'&+-]", re.UNICODE)


def normalize(term: str) -> str:
    """Kleine letters, geen leestekens of stopwoorden, enkele spaties."""
    words = _KEEP.sub(" ", (term or "").lower()).split()
    kept = [w for w in words if w not in _STOPWORDS]
    return " ".join(kept or words)


def term_variants(term: str, lang: str) -> list[list[str]]:
    """De term zelf plus vertalingen, elk als lijst van woorden.

    Vertalingen werken ook op een deel van de term: in "havermelk barista" wordt
    "havermelk" vervangen door "haverdrink", zodat "Oatly haverdrink barista" matcht.
    De langste bekende woordgroep gaat voor ("oat milk" in "barista oat milk").
    """
    words = term.split()
    tables = [TERMS.get(lang, {}), TERMS.get("nl", {})]
    variants = [words]
    for size in range(len(words), 0, -1):
        found = False
        for start in range(len(words) - size + 1):
            phrase = " ".join(words[start:start + size])
            for table in tables:
                for synonym in table.get(phrase, []):
                    variant = words[:start] + synonym.lower().split() + words[start + size:]
                    if variant not in variants:
                        variants.append(variant)
                        found = True
        if found:
            break
    return [v for v in variants if v]


_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)
_DIMINUTIVE = ("tjes", "jes", "tje", "je")


def _tokens(name: str) -> list[str]:
    return _TOKEN.findall(name.lower())


def _word_in(word: str, tokens: list[str]) -> bool:
    """Woord hoort bij een naam als een woord uit de naam ermee begint of eindigt.

    Begint: "chocola" in "chocolonely", "olijv" in "olijven". Eindigt: Nederlandse
    samenstellingen, "melk" in "karnemelk", ook na verkleinwoord ("snackworteltjes").
    Midden in een woord telt niet: "cola" hoort niet bij "chocolade".
    """
    forms = {word}
    if len(word) > 4 and word.endswith("s"):
        forms.add(word[:-1])
    if len(word) > 5 and word.endswith("en"):
        forms.add(word[:-2])
    for token in tokens:
        stems = {token}
        for suffix in _DIMINUTIVE:
            if token.endswith(suffix) and len(token) > len(suffix) + 2:
                stems.add(token[: -len(suffix)])
        for form in forms:
            if token.startswith(form) or any(stem.endswith(form) for stem in stems):
                return True
    return False


def name_matches(name: str | None, variants: list[list[str]]) -> bool:
    tokens = _tokens(name or "")
    return any(all(_word_in(w, tokens) for w in variant) for variant in variants)


def exact_match(name: str | None, variants: list[list[str]]) -> bool:
    """Alle woorden staan als los woord in de naam, niet alleen in een samenstelling."""
    tokens = set(_tokens(name or ""))
    for variant in variants:
        if all({w, w[:-1] if w.endswith("s") else w, w[:-2] if w.endswith("en") else w} & tokens for w in variant):
            return True
    return False


def euro(cents: int | None) -> str:
    if cents is None:
        return ""
    return "€ " + f"{cents / 100:.2f}".replace(".", ",")


@dataclass
class Candidate:
    article_id: str
    name: str
    unit_quantity: str | None = None
    price_cents: int | None = None
    recent_count: int = 0
    total_count: int = 0
    last_bought: str | None = None
    favorite: bool = False
    exact: bool = False
    label: str = ""
    short_label: str = ""

    def as_dict(self) -> dict:
        return {
            "article_id": self.article_id,
            "name": self.name,
            "unit_quantity": self.unit_quantity,
            "price_cents": self.price_cents,
            "times_bought": self.total_count,
            "times_bought_last_year": self.recent_count,
            "favorite": self.favorite,
            "label": self.short_label or self.label,
            "full_label": self.label,
        }


@dataclass
class Decision:
    kind: str  # alias | winner | choose | none
    term: str
    article: Candidate | None = None
    options: list[Candidate] = field(default_factory=list)
    reason: str = ""


def _unique(labels: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result = []
    for label in labels:
        seen[label] = seen.get(label, 0) + 1
        result.append(label if seen[label] == 1 else f"{label} ({seen[label]})")
    return result


def _labels(candidates: list[Candidate]) -> None:
    """Volledig label (naam, eenheid, prijs) en een kort label om uit te spreken.

    Het korte label laat weg wat alle opties gemeen hebben: bij "Oatly haverdrink vol"
    en "Oatly haverdrink barista" blijft "vol" en "barista" over. Zijn korte labels
    gelijk, dan komt de eenheid erbij ("barista, 500 ml"), en zo nodig de prijs.
    """
    full = []
    for c in candidates:
        parts = [c.name]
        if c.unit_quantity:
            parts.append(c.unit_quantity)
        if c.price_cents is not None:
            parts.append(euro(c.price_cents))
        full.append(", ".join(parts))
    for c, label in zip(candidates, _unique(full)):
        c.label = label

    if len(candidates) == 1:
        candidates[0].short_label = candidates[0].name
        return
    word_sets = [{w.lower() for w in c.name.split()} for c in candidates]
    common = set.intersection(*word_sets)
    shorts = [" ".join(w for w in c.name.split() if w.lower() not in common) or c.name for c in candidates]
    for extra in ("unit_quantity", "price"):
        counts = {s: shorts.count(s) for s in shorts}
        if all(n == 1 for n in counts.values()):
            break
        for i, c in enumerate(candidates):
            if counts[shorts[i]] > 1:
                value = c.unit_quantity if extra == "unit_quantity" else euro(c.price_cents)
                if value:
                    shorts[i] = f"{shorts[i]}, {value}"
    for c, short in zip(candidates, _unique(shorts)):
        c.short_label = short


def spoken_options(options: list[Candidate], lang: str) -> str:
    """"vol of barista" / "whole or barista"."""
    labels = [o.short_label or o.label for o in options]
    joiner = " of " if lang == "nl" else " or "
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + joiner + labels[-1]


def decide(term_raw: str, lang: str, store, now: datetime | None = None) -> Decision:
    now = now or datetime.now(timezone.utc)
    term = normalize(term_raw)
    alias = store.get_alias(term)
    if alias:
        cand = Candidate(alias["article_id"], alias["name"] or alias["article_id"], alias["unit_quantity"])
        known = store.article(alias["article_id"])
        if known:
            cand.price_cents = known["last_unit_cents"]
            cand.total_count = known["deliveries_count"] or 0
        _labels([cand])
        return Decision("alias", term, article=cand, reason="woordenboek")

    variants = term_variants(term, lang)
    history_since = (now - timedelta(days=HISTORY_DAYS)).isoformat()
    recent_since = (now - timedelta(days=RECENT_DAYS)).isoformat()

    candidates: dict[str, Candidate] = {}
    for row in store.purchase_stats(recent_since):
        if (row["last_bought"] or "") < history_since or not name_matches(row["name"], variants):
            continue
        candidates[row["article_id"]] = Candidate(
            row["article_id"], row["name"], row["unit_quantity"], row["price_cents"],
            row["recent_count"] or 0, row["deliveries_count"] or 0, row["last_bought"],
        )
    for fav in store.list_favorites():
        if not name_matches(fav["name"], variants):
            continue
        cand = candidates.get(fav["article_id"])
        if cand is None:
            known = store.article(fav["article_id"])
            cand = Candidate(
                fav["article_id"], fav["name"], fav["unit_quantity"], fav["price_cents"],
                total_count=(known["deliveries_count"] or 0) if known else 0,
                last_bought=known["last_bought"] if known else None,
            )
            candidates[fav["article_id"]] = cand
        cand.favorite = True
        if fav["price_cents"] is not None:
            cand.price_cents = fav["price_cents"]

    if not candidates:
        return Decision("none", term, reason="geen kandidaten in historie of Favorieten")

    for cand in candidates.values():
        cand.exact = exact_match(cand.name, variants)
    # Losse woorden gaan voor samenstellingen ("kaas" boven "pindakaas"), daarna recente aankopen
    ranked = sorted(
        candidates.values(),
        key=lambda c: (not c.exact, -c.recent_count, -c.total_count, not c.favorite, c.name),
    )
    _labels(ranked)
    # De winnaar gaat puur op recente aankopen; de volgorde hierboven is alleen voor de keuzelijst
    top = max(ranked, key=lambda c: (c.recent_count, c.total_count))
    total_recent = sum(c.recent_count for c in ranked)
    if len(ranked) == 1:
        return Decision("winner", term, article=top, reason="enige kandidaat")
    if total_recent and top.recent_count / total_recent >= WIN_SHARE:
        share = round(100 * top.recent_count / total_recent)
        return Decision("winner", term, article=top, reason=f"{share} procent van de recente aankopen")
    return Decision("choose", term, options=ranked[:MAX_OPTIONS], reason=f"{len(ranked)} kandidaten, geen duidelijke winnaar")


def options_from_search(term: str, items: list[dict], store) -> Decision:
    """Zoekresultaten als keuzelijst; nooit automatisch toevoegen."""
    options = []
    for item in items[:MAX_OPTIONS]:
        known = store.article(item["article_id"])
        options.append(Candidate(
            item["article_id"], item["name"], item.get("unit_quantity"), item.get("price_cents"),
            total_count=(known["deliveries_count"] or 0) if known else 0,
        ))
    _labels(options)
    if not options:
        return Decision("none", term, reason="ook niets gevonden bij Picnic")
    return Decision("choose", term, options=options, reason="nooit eerder gekocht, zoekresultaten")
