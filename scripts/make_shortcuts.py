#!/usr/bin/env python3
"""Maakt de iOS-opdrachten voor picnic-service en ondertekent ze (alleen op macOS).

Gebruik:  python3 scripts/make_shortcuts.py [--base-url http://192.168.1.20:8085] [--no-sign]

Schrijft naar data/shortcuts/ (gitignored). Het serveradres en de API-sleutel
staan niet vast in het bestand: de opdracht vraagt er bij het importeren om.
Met --base-url wordt het adres alvast als standaardantwoord ingevuld.
Ondertekenen (nodig om te importeren) kan alleen op macOS.

Opdrachten:
- "Picnic" (Nederlands, lang=nl) en "Picnic EN" (Engels, lang=en): vraagt wat
  erbij moet, POST /add, bij twijfel een keuzelijst, spreekt het antwoord uit.
- "Picnic terug" en "Picnic undo": POST /undo.
"""

import argparse
import plistlib
import subprocess
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "shortcuts"

TEXTS = {
    "nl": {
        "name": "Picnic",
        "ask": "Wat moet erbij?",
        "key_question": "Plak de API_KEY uit .env op de server",
        "url_question": "Adres van de server, bijvoorbeeld http://192.168.1.20:8085",
        "undo_name": "Picnic terug",
    },
    "en": {
        "name": "Picnic EN",
        "ask": "What should I add?",
        "key_question": "Paste the API_KEY from .env on the server",
        "url_question": "Server address, for example http://192.168.1.20:8085",
        "undo_name": "Picnic undo",
    },
}

OBJ = "￼"  # plaatshouder voor een variabele in een tekst


# ---- bouwstenen voor het plist-formaat van Opdrachten ----

def text(value: str) -> dict:
    return {"Value": {"string": value, "attachmentsByRange": {}}, "WFSerializationType": "WFTextTokenString"}


def var_ref(name: str) -> dict:
    return {"Type": "Variable", "VariableName": name}


def output_ref(action_uuid: str, output_name: str) -> dict:
    return {"Type": "ActionOutput", "OutputUUID": action_uuid, "OutputName": output_name}


def token(ref: dict) -> dict:
    """Tekstveld dat alleen uit een variabele bestaat."""
    return {"Value": {"string": OBJ, "attachmentsByRange": {"{0, 1}": ref}}, "WFSerializationType": "WFTextTokenString"}


def attachment(ref: dict) -> dict:
    return {"Value": ref, "WFSerializationType": "WFTextTokenAttachment"}


def dictionary(items: list[tuple[str, dict]]) -> dict:
    return {
        "Value": {
            "WFDictionaryFieldValueItems": [
                {"WFItemType": 0, "WFKey": text(key), "WFValue": value} for key, value in items
            ]
        },
        "WFSerializationType": "WFDictionaryFieldValue",
    }


def action(identifier: str, **params) -> dict:
    return {"WFWorkflowActionIdentifier": identifier, "WFWorkflowActionParameters": params}


def set_variable(name: str, source: dict) -> dict:
    return action("is.workflow.actions.setvariable", WFVariableName=name, WFInput=attachment(source))


def endpoint(path: str) -> dict:
    """URL-veld: variabele Server gevolgd door het pad, bijvoorbeeld Server + "/add"."""
    return {
        "Value": {"string": OBJ + path, "attachmentsByRange": {"{0, 1}": var_ref("Server")}},
        "WFSerializationType": "WFTextTokenString",
    }


def post(path: str, body: list[tuple[str, dict]]) -> tuple[dict, str]:
    uid = str(uuid.uuid4()).upper()
    return (
        action(
            "is.workflow.actions.downloadurl",
            UUID=uid,
            WFURL=endpoint(path),
            WFHTTPMethod="POST",
            WFHTTPBodyType="JSON",
            ShowHeaders=True,
            WFHTTPHeaders=dictionary([("X-Api-Key", token(var_ref("Sleutel"))), ("Content-Type", text("application/json"))]),
            WFJSONValues=dictionary(body),
        ),
        uid,
    )


def get_value(key: str, source: dict) -> tuple[dict, str]:
    uid = str(uuid.uuid4()).upper()
    return (
        action("is.workflow.actions.getvalueforkey", UUID=uid, WFDictionaryKey=key, WFInput=attachment(source)),
        uid,
    )


def setup_actions() -> list[dict]:
    """Twee tekstacties, ingevuld bij import: API-sleutel in Sleutel (actie 0), serveradres in Server (actie 2)."""
    key_uid = str(uuid.uuid4()).upper()
    url_uid = str(uuid.uuid4()).upper()
    return [
        action("is.workflow.actions.gettext", UUID=key_uid, WFTextActionText=text("")),
        set_variable("Sleutel", output_ref(key_uid, "Text")),
        action("is.workflow.actions.gettext", UUID=url_uid, WFTextActionText=text("")),
        set_variable("Server", output_ref(url_uid, "Text")),
    ]


def workflow(actions: list[dict], lang: str, base_url: str) -> dict:
    return {
        "WFWorkflowActions": actions,
        "WFWorkflowClientVersion": "2605.0.5",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4292093695, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowImportQuestions": [
            {
                "ActionIndex": 0,
                "Category": "Parameter",
                "DefaultValue": "",
                "ParameterKey": "WFTextActionText",
                "Text": TEXTS[lang]["key_question"],
            },
            {
                "ActionIndex": 2,
                "Category": "Parameter",
                "DefaultValue": base_url,
                "ParameterKey": "WFTextActionText",
                "Text": TEXTS[lang]["url_question"],
            },
        ],
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": ["Watch"],
        "WFQuickActionSurfaces": [],
    }


# ---- de opdrachten ----

def add_shortcut(base_url: str, lang: str) -> dict:
    t = TEXTS[lang]
    group = str(uuid.uuid4()).upper()
    actions = setup_actions()

    ask_uid = str(uuid.uuid4()).upper()
    actions.append(action("is.workflow.actions.ask", UUID=ask_uid, WFAskActionPrompt=t["ask"], WFInputType="Text"))
    actions.append(set_variable("Term", output_ref(ask_uid, "Provided Input")))

    first, first_uid = post("/add", [("q", token(var_ref("Term"))), ("lang", text(lang))])
    actions.append(first)
    actions.append(set_variable("Antwoord", output_ref(first_uid, "Contents of URL")))

    # Alleen een keuzevraag (status needs_choice) heeft "labels". "Heeft een waarde"
    # (WFCondition 100) vraagt geen vergelijkingswaarde; die bleef bij import leeg.
    labels, labels_uid = get_value("labels", var_ref("Antwoord"))
    actions.append(labels)
    actions.append(set_variable("Opties", output_ref(labels_uid, "Dictionary Value")))
    actions.append(action(
        "is.workflow.actions.conditional",
        GroupingIdentifier=group,
        WFControlFlowMode=0,
        WFCondition=100,
        WFInput={"Type": "Variable", "Variable": attachment(var_ref("Opties"))},
    ))
    question, question_uid = get_value("speech", var_ref("Antwoord"))
    actions.append(question)
    actions.append(set_variable("Vraag", output_ref(question_uid, "Dictionary Value")))
    choose_uid = str(uuid.uuid4()).upper()
    actions.append(action(
        "is.workflow.actions.choosefromlist",
        UUID=choose_uid,
        WFInput=attachment(var_ref("Opties")),
        WFChooseFromListActionPrompt=token(var_ref("Vraag")),
    ))
    second, second_uid = post(
        "/add",
        [("q", token(var_ref("Term"))), ("choice", token(output_ref(choose_uid, "Chosen Item"))), ("lang", text(lang))],
    )
    actions.append(second)
    actions.append(set_variable("Antwoord", output_ref(second_uid, "Contents of URL")))
    actions.append(action("is.workflow.actions.conditional", GroupingIdentifier=group, WFControlFlowMode=2))

    speech, speech_uid = get_value("speech", var_ref("Antwoord"))
    actions.append(speech)
    actions.append(action("is.workflow.actions.speaktext", WFText=token(output_ref(speech_uid, "Dictionary Value"))))
    return workflow(actions, lang, base_url)


def undo_shortcut(base_url: str, lang: str) -> dict:
    actions = setup_actions()
    request, request_uid = post("/undo", [("lang", text(lang))])
    actions.append(request)
    speech, speech_uid = get_value("speech", output_ref(request_uid, "Contents of URL"))
    actions.append(speech)
    actions.append(action("is.workflow.actions.speaktext", WFText=token(output_ref(speech_uid, "Dictionary Value"))))
    return workflow(actions, lang, base_url)


def write_and_sign(name: str, data: dict, sign: bool) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    unsigned = OUT / f"{name} (unsigned).shortcut"
    with unsigned.open("wb") as fh:
        plistlib.dump(data, fh, fmt=plistlib.FMT_BINARY)
    if not sign:
        return unsigned
    signed = OUT / f"{name}.shortcut"
    result = subprocess.run(
        ["shortcuts", "sign", "--mode", "anyone", "--input", str(unsigned), "--output", str(signed)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Ondertekenen van {name} mislukt: {result.stderr.strip()}", file=sys.stderr)
        return unsigned
    unsigned.unlink()
    return signed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="", help="standaardantwoord voor het serveradres bij import")
    parser.add_argument("--no-sign", action="store_true")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    for lang in ("nl", "en"):
        t = TEXTS[lang]
        print(write_and_sign(t["name"], add_shortcut(base, lang), not args.no_sign))
        print(write_and_sign(t["undo_name"], undo_shortcut(base, lang), not args.no_sign))
    return 0


if __name__ == "__main__":
    sys.exit(main())
