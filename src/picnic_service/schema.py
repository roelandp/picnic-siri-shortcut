"""Schema-rapport uit de raw dumps: per endpoint paden, types, aantallen, voorbeeld.

Werkt alleen op bestanden in data/raw, doet zelf geen requests. Voorbeeldwaarden
worden gemaskeerd als ze naar persoonsgegevens ruiken: op sleutelnaam, op patroon
(e-mail, telefoon, postcode) en op de concrete waarden uit de get_user-dump.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

MAX_DEPTH = 12
MAX_PATHS = 300
MAX_FILES_PER_ENDPOINT = 40
EXAMPLE_LEN = 60
MASK = "***"

SENSITIVE_KEYS = {
    "firstname", "lastname", "first_name", "last_name", "full_name", "display_name",
    "street", "house_number", "house_number_ext", "postcode", "zipcode", "city",
    "phone", "phone_number", "contact_email", "email", "redacted_iban", "iban",
    "bank_id", "user_id", "customer_id", "latitude", "longitude", "lat", "lng",
    "auth_token", "token", "secret", "password", "adults", "children", "cats", "dogs",
}
# Sleutels die deze fragmenten bevatten zijn altijd persoonlijk (ook half gemaskeerd door Picnic)
SENSITIVE_KEY_PARTS = ("phone", "email", "iban", "account", "redacted", "mgm_code", "share_url")
# Alles onder deze padsegmenten gaat over de klant zelf, bijvoorbeeld profile-menu `user.name`
SENSITIVE_PATH_PARTS = {"user", "address"}
_PARTIAL_MASK = re.compile(r"[•*]{2,}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w.])(?:\+\d{2}|0)\d[\d \-]{7,}\d(?![\w.])")
_POSTCODE = re.compile(r"\b\d{4} ?[A-Z]{2}\b")
_ID_TAIL = re.compile(r"([-_][0-9A-Za-z]*\d[0-9A-Za-z]*)+$")


def personal_values(raw_dir: Path) -> set[str]:
    """Concrete persoonsgegevens uit de nieuwste get_user-dump, om op te maskeren."""
    files = sorted((raw_dir / "get_user").glob("*.json"))
    if not files:
        return set()
    body = json.loads(files[-1].read_text()).get("body")
    values: set[str] = set()
    if isinstance(body, dict):
        for key in ("firstname", "lastname", "phone", "contact_email"):
            if isinstance(body.get(key), str):
                values.add(body[key])
        address = body.get("address")
        if isinstance(address, dict):
            for key in ("street", "city", "postcode"):
                if isinstance(address.get(key), str):
                    values.add(address[key])
    return {v for v in values if len(v) >= 3}


def _segments(path: str) -> set[str]:
    return {s for s in re.split(r"[.\[\]]+", path.lower()) if s}


def mask_example(key: str, value: Any, personal: set[str], path: str = "") -> str:
    lowered = key.lower()
    if lowered in SENSITIVE_KEYS or any(part in lowered for part in SENSITIVE_KEY_PARTS):
        return MASK
    if _segments(path) & SENSITIVE_PATH_PARTS:
        return MASK
    if not isinstance(value, str):
        return json.dumps(value)
    text = value
    if _EMAIL.search(text) or _PHONE.search(text) or _POSTCODE.search(text) or _PARTIAL_MASK.search(text):
        return MASK
    if any(p.lower() in text.lower() for p in personal):
        return MASK
    text = text.replace("\n", " ").replace("|", "\\|")
    return text if len(text) <= EXAMPLE_LEN else text[: EXAMPLE_LEN - 1] + "…"


@dataclass
class PathInfo:
    types: Counter = field(default_factory=Counter)
    count: int = 0
    list_min: int | None = None
    list_max: int | None = None
    example: str | None = None


@dataclass
class EndpointSchema:
    name: str
    files: int
    statuses: Counter
    paths: dict[str, PathInfo]
    truncated: bool
    pml_types: Counter
    pml_ids: Counter


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    return {bool: "bool", int: "int", float: "float", str: "str", list: "list", dict: "dict"}.get(
        type(value), type(value).__name__
    )


def _is_pml(body: Any) -> bool:
    return isinstance(body, dict) and "body" in body and isinstance(body["body"], dict)


def _collect(node: Any, path: str, key: str, depth: int, schema: dict, state: dict, personal: set) -> None:
    if path not in schema:
        if len(schema) >= MAX_PATHS:
            state["truncated"] = True
            return
        schema[path] = PathInfo()
    info = schema[path]
    info.count += 1
    info.types[_type_name(node)] += 1
    if isinstance(node, list):
        n = len(node)
        info.list_min = n if info.list_min is None else min(info.list_min, n)
        info.list_max = n if info.list_max is None else max(info.list_max, n)
    elif not isinstance(node, dict) and node is not None and info.example is None:
        info.example = mask_example(key, node, personal, path)
    if depth >= MAX_DEPTH:
        if isinstance(node, (dict, list)) and node:
            state["truncated"] = True
        return
    if isinstance(node, dict):
        for k, v in node.items():
            _collect(v, f"{path}.{k}" if path else k, k, depth + 1, schema, state, personal)
    elif isinstance(node, list):
        for item in node:
            _collect(item, f"{path}[]", key, depth + 1, schema, state, personal)


def _walk_dicts(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_dicts(v)


def analyze_endpoint(directory: Path, personal: set[str]) -> EndpointSchema:
    files = sorted(directory.glob("*.json"))
    statuses: Counter = Counter()
    paths: dict[str, PathInfo] = {}
    state = {"truncated": False}
    pml_types: Counter = Counter()
    pml_ids: Counter = Counter()
    for f in files[-MAX_FILES_PER_ENDPOINT:]:
        envelope = json.loads(f.read_text())
        statuses[envelope.get("meta", {}).get("status")] += 1
        body = envelope.get("body")
        _collect(body, "", "", 0, paths, state, personal)
        if _is_pml(body):
            for node in _walk_dicts(body):
                if isinstance(node.get("type"), str):
                    pml_types[node["type"]] += 1
                if isinstance(node.get("id"), str):
                    pml_ids[_ID_TAIL.sub("-<id>", node["id"])] += 1
    return EndpointSchema(directory.name, len(files), statuses, paths, state["truncated"], pml_types, pml_ids)


def render(schemas: list[EndpointSchema], extra_sections: list[str] | None = None) -> str:
    out = [f"# Picnic API schema, {date.today().isoformat()}", ""]
    out.append("Automatisch gegenereerd uit `data/raw`. Persoonsgegevens gemaskeerd als `***`.")
    out.append(f"Per endpoint maximaal {MAX_FILES_PER_ENDPOINT} dumps, {MAX_PATHS} paden, diepte {MAX_DEPTH}.")
    out.append("")
    out.append("| endpoint | dumps | statuscodes |")
    out.append("|---|---|---|")
    for s in schemas:
        codes = ", ".join(f"{k}: {v}" for k, v in sorted(s.statuses.items(), key=lambda kv: str(kv[0])))
        out.append(f"| [{s.name}](#{s.name.lower()}) | {s.files} | {codes} |")
    out.append("")
    for s in schemas:
        out.append(f"## {s.name}")
        out.append("")
        if s.truncated:
            out.append("_Afgekapt op diepte of aantal paden._")
            out.append("")
        out.append("| pad | types | aantal | voorbeeld |")
        out.append("|---|---|---|---|")
        for path, info in s.paths.items():
            types = ", ".join(t for t, _ in info.types.most_common())
            if info.list_min is not None:
                types += f" [{info.list_min}..{info.list_max}]"
            example = f"`{info.example}`" if info.example not in (None, "") else ""
            out.append(f"| `{path or '(root)'}` | {types} | {info.count} | {example} |")
        out.append("")
        if s.pml_types:
            out.append("PML-widgettypes (meest voorkomend):")
            out.append("")
            out.append(", ".join(f"`{t}` {n}" for t, n in s.pml_types.most_common(25)))
            out.append("")
            out.append("PML-node-ids (ids ingekort tot prefix):")
            out.append("")
            out.append(", ".join(f"`{t}` {n}" for t, n in s.pml_ids.most_common(40)))
            out.append("")
    for section in extra_sections or []:
        out.append(section)
        out.append("")
    return "\n".join(out)


def build_schema(raw_dir: Path, extra_sections: list[str] | None = None) -> str:
    personal = personal_values(raw_dir)
    dirs = sorted(d for d in raw_dir.iterdir() if d.is_dir()) if raw_dir.exists() else []
    schemas = [analyze_endpoint(d, personal) for d in dirs]
    return render(schemas, extra_sections)
