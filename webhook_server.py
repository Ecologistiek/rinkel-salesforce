"""
Rinkel → Salesforce integratie
================================
Ontvangt Rinkel webhook events (callEnd, callInsights) en slaat
gesprekshistorie op als Activiteit (Task) op het Weborder object in Salesforce.

Endpoints:
  POST /webhook/callend       → Rinkel 'callEnd' event
  POST /webhook/callinsights  → Rinkel 'callInsights' event
  GET  /health                → Statuscheck
"""

import os
import re
import time
import logging
from flask import Flask, request, jsonify
import requests
from simple_salesforce import Salesforce
from dotenv import load_dotenv

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Configuratie (via .env) ───────────────────────────────────────────────────
RINKEL_API_KEY = os.getenv("RINKEL_API_KEY")

SF_USERNAME       = os.getenv("SF_USERNAME")
SF_PASSWORD       = os.getenv("SF_PASSWORD")
SF_SECURITY_TOKEN = os.getenv("SF_SECURITY_TOKEN")
SF_DOMAIN         = os.getenv("SF_DOMAIN", "login")   # 'login' = productie, 'test' = sandbox

# ── Salesforce veldnamen – PAS DEZE AAN als jouw API-namen anders zijn ────────
SF_WEBORDER_OBJECT      = os.getenv("SF_WEBORDER_OBJECT",      "Weborder__c")
SF_WEBORDER_PHONE_FIELD = os.getenv("SF_WEBORDER_PHONE_FIELD", "Eindklant_Telefoonnummer__c")
# Zoek alleen open/actieve weborders? Zet op "" om alle te zoeken.
SF_WEBORDER_STATUS_FILTER = os.getenv("SF_WEBORDER_STATUS_FILTER", "")


# ═══════════════════════════════════════════════════════════════════════════════
# Hulpfuncties
# ═══════════════════════════════════════════════════════════════════════════════

def get_sf() -> Salesforce:
    """Maak verbinding met Salesforce."""
    return Salesforce(
        username=SF_USERNAME,
        password=SF_PASSWORD,
        security_token=SF_SECURITY_TOKEN,
        domain=SF_DOMAIN,
    )


def get_rinkel_cdr(call_id: str) -> dict | None:
    """
    Haal een Call Detail Record op uit Rinkel via het callId
    (het ID dat in webhook-events wordt meegestuurd).
    """
    url = f"https://api.rinkel.com/v1/call-detail-records/by-call-id/{call_id}"
    headers = {
        "x-rinkel-api-key": RINKEL_API_KEY,
        "Accept": "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data")
    except requests.RequestException as e:
        logger.error(f"Rinkel CDR ophalen mislukt voor callId={call_id}: {e}")
        return None


def digits_only(value: str) -> str:
    """Strip alles behalve cijfers uit een string."""
    return re.sub(r"\D", "", value)


def phone_search_suffix(phone_e164: str) -> str:
    """Geeft de laatste 8 significante cijfers van een E.164-nummer."""
    return digits_only(phone_e164)[-8:]


def phones_match(rinkel_e164: str, stored_value: str) -> bool:
    """
    Vergelijkt een Rinkel E.164-nummer met een willekeurig opgeslagen waarde.
    Werkt ongeacht opmaak: spaties, koppeltekens, haakjes, tekst achteraan.
    """
    rinkel_digits = digits_only(rinkel_e164)
    stored_digits = digits_only(stored_value)

    if not rinkel_digits or not stored_digits:
        return False

    tail = min(9, len(rinkel_digits), len(stored_digits))
    return rinkel_digits[-tail:] == stored_digits[-tail:]


def find_weborder(sf: Salesforce, phone_e164: str) -> dict | None:
    """
    Zoek de meest recente Weborder op basis van telefoonnummer.
    Stap 1: SOQL LIKE op de laatste 8 cijfers
    Stap 2: Python-normalisatie op de teruggekregen records
    """
    suffix = phone_search_suffix(phone_e164)
    if len(suffix) < 6:
        logger.warning(f"Nummer te kort om op te zoeken: {phone_e164}")
        return None

    status_clause = (
        f"AND Status__c = '{SF_WEBORDER_STATUS_FILTER}'"
        if SF_WEBORDER_STATUS_FILTER else ""
    )

    query = f"""
        SELECT Id, Name, {SF_WEBORDER_PHONE_FIELD}
        FROM {SF_WEBORDER_OBJECT}
        WHERE {SF_WEBORDER_PHONE_FIELD} LIKE '%{suffix}%'
        {status_clause}
        ORDER BY CreatedDate DESC
        LIMIT 50
    """
    try:
        result = sf.query(query)
    except Exception as e:
        logger.error(f"Salesforce query mislukt: {e}")
        return None

    for record in result.get("records", []):
        stored = record.get(SF_WEBORDER_PHONE_FIELD) or ""
        if phones_match(phone_e164, stored):
            logger.info(
                f"Match: '{stored}' → WebOrder {record['Name']} "
                f"(gezocht op suffix '{suffix}')"
            )
            return record

    logger.info(
        f"Geen WebOrder gevonden voor {phone_e164} "
        f"(SOQL suffix '{suffix}' gaf {result.get('totalSize', 0)} kandidaten)"
    )
    return None


def find_task_by_rinkel_id(sf: Salesforce, rinkel_call_id: str) -> str | None:
    """Zoek een bestaande Task op via het Rinkel callId."""
    escaped = rinkel_call_id.replace("'", "\'")
    query = f"SELECT Id FROM Task WHERE CallObject = '{escaped}' LIMIT 1"
    try:
        result = sf.query(query)
        if result["totalSize"] > 0:
            return result["records"][0]["Id"]
    except Exception as e:
        logger.error(f"Task opzoeken mislukt: {e}")
    return None


def build_task(cdr: dict, weborder_id: str) -> dict:
    """Bouw een Salesforce Task-dict op vanuit een Rinkel CDR."""
    direction = cdr.get("direction", "inbound")
    status    = cdr.get("status", "ANSWERED")
    duration  = cdr.get("duration", 0)
    call_id   = cdr.get("callId", cdr.get("id", ""))

    ext         = cdr.get("externalNumber") or {}
    ext_number  = ext.get("localized") or ext.get("e164") or "Onbekend"

    user      = cdr.get("user") or {}
    user_name = user.get("fullName", "Onbekend") if user else "Onbekend"

    direction_label = "Inkomend" if direction == "inbound" else "Uitgaand"
    status_map = {
        "ANSWERED":         "Beantwoord",
        "MISSED":           "Gemist",
        "VOICEMAIL":        "Voicemail",
        "ANSWERING_SERVICE":"Antwoordservice",
    }
    status_label = status_map.get(status, status)

    mins, secs = divmod(duration, 60)

    lines = [
        f"Richting  : {direction_label}",
        f"Status    : {status_label}",
        f"Duur      : {mins}:{secs:02d} min",
        f"Nummer    : {ext_number}",
        f"Medewerker: {user_name}",
    ]

    recording = cdr.get("callRecording")
    if recording and recording.get("playUrl"):
        lines.append(f"\nOpname (beschikbaar tot {recording.get('availableUntil','?')[:10]}):")
        lines.append(recording["playUrl"])

    voicemail = cdr.get("voicemail")
    if voicemail and voicemail.get("playUrl"):
        lines.append(f"\nVoicemail (beschikbaar tot {voicemail.get('availableUntil','?')[:10]}):")
        lines.append(voicemail["playUrl"])

    lines += _insights_lines(cdr.get("insights"))

    task = {
        "WhatId":                weborder_id,
        "Subject":               f"Gesprek {direction_label} – {status_label}",
        "Status":                "Completed",
        "Description":           "\n".join(lines),
        "CallType":              "Inbound" if direction == "inbound" else "Outbound",
        "CallDurationInSeconds": duration,
        "CallObject":            call_id,
    }
    return task


def _insights_lines(insights: dict | None) -> list[str]:
    """Geeft de tekstregels terug voor AI-inzichten."""
    if not insights:
        return []

    sentiment_map = {
        "POSITIVE": "Positief",
        "NEGATIVE": "Negatief",
        "NEUTRAL":  "Neutraal",
    }

    if insights.get("status") == "AVAILABLE":
        lines = ["\n--- AI Inzichten ---"]
        summary = insights.get("summary") or insights.get("customSummary")
        if summary:
            lines.append(f"Samenvatting:\n{summary}")
        if insights.get("sentiment"):
            lines.append(f"Sentiment: {sentiment_map.get(insights['sentiment'], insights['sentiment'])}")
        if insights.get("topics"):
            lines.append(f"Onderwerpen: {', '.join(insights['topics'])}")
        return lines

    if insights.get("status") == "IN_PROGRESS":
        return ["\n--- AI Inzichten worden verwerkt... ---"]

    return []


# ═══════════════════════════════════════════════════════════════════════════════
# Webhook-endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/webhook/callend", methods=["POST"])
def webhook_call_end():
    """Verwerkt het Rinkel 'callEnd' event."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Geen data ontvangen"}), 400

    call_id = data.get("id")
    cause   = data.get("cause", "?")
    logger.info(f"callEnd ontvangen | callId={call_id} | cause={cause}")

    if not call_id:
        return jsonify({"error": "Geen call ID in payload"}), 400

    time.sleep(3)
    cdr = get_rinkel_cdr(call_id)

    if not cdr:
        logger.warning(f"CDR nog niet beschikbaar, tweede poging na 5s (callId={call_id})")
        time.sleep(5)
        cdr = get_rinkel_cdr(call_id)

    if not cdr:
        logger.error(f"CDR niet gevonden voor callId={call_id}")
        return jsonify({"error": "CDR niet gevonden"}), 404

    ext   = cdr.get("externalNumber") or {}
    phone = ext.get("e164")

    if not phone or ext.get("anonymous"):
        logger.info(f"Anoniem/geen nummer – overgeslagen (callId={call_id})")
        return jsonify({"status": "overgeslagen", "reden": "anoniem"}), 200

    sf       = get_sf()
    weborder = find_weborder(sf, phone)

    if not weborder:
        logger.info(f"Geen Weborder gevonden voor nummer {phone} (callId={call_id})")
        return jsonify({"status": "overgeslagen", "reden": "geen_weborder"}), 200

    task   = build_task(cdr, weborder["Id"])
    result = sf.Task.create(task)
    logger.info(f"Task aangemaakt: {result['id']} | Weborder: {weborder['Name']}")

    return jsonify({"status": "ok", "task_id": result["id"], "weborder": weborder["Name"]}), 201


@app.route("/webhook/callinsights", methods=["POST"])
def webhook_call_insights():
    """Verwerkt het Rinkel 'callInsights' event."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Geen data ontvangen"}), 400

    call_id = data.get("id")
    logger.info(f"callInsights ontvangen | callId={call_id}")

    if not call_id:
        return jsonify({"error": "Geen call ID in payload"}), 400

    cdr = get_rinkel_cdr(call_id)
    if not cdr:
        return jsonify({"error": "CDR niet gevonden"}), 404

    sf      = get_sf()
    task_id = find_task_by_rinkel_id(sf, call_id)

    if not task_id:
        logger.info(f"Geen bestaande Task gevonden, nieuwe aanmaken (callId={call_id})")
        ext   = cdr.get("externalNumber") or {}
        phone = ext.get("e164")
        if phone and not ext.get("anonymous"):
            weborder = find_weborder(sf, phone)
            if weborder:
                task   = build_task(cdr, weborder["Id"])
                result = sf.Task.create(task)
                logger.info(f"Task alsnog aangemaakt: {result['id']}")
                return jsonify({"status": "aangemaakt", "task_id": result["id"]}), 201
        return jsonify({"status": "overgeslagen"}), 200

    insights = cdr.get("insights") or {}
    if insights.get("status") != "AVAILABLE":
        return jsonify({"status": "geen_inzichten_beschikbaar"}), 200

    insight_lines = _insights_lines(insights)
    if not insight_lines:
        return jsonify({"status": "leeg"}), 200

    try:
        current = sf.query(f"SELECT Description FROM Task WHERE Id = '{task_id}'")
        current_desc = ""
        if current["totalSize"] > 0:
            current_desc = current["records"][0].get("Description") or ""

        current_desc = current_desc.replace("\n--- AI Inzichten worden verwerkt... ---", "")
        new_desc = current_desc.rstrip() + "\n" + "\n".join(insight_lines)
        sf.Task.update(task_id, {"Description": new_desc})
        logger.info(f"Task {task_id} bijgewerkt met AI-inzichten")
    except Exception as e:
        logger.error(f"Task bijwerken mislukt: {e}")
        return jsonify({"error": str(e)}), 500

    return jsonify({"status": "ok", "task_id": task_id}), 200


@app.route("/health", methods=["GET"])
def health():
    """Statuscheck – handig om te monitoren of de server draait."""
    return jsonify({"status": "ok"}), 200


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Rinkel–Salesforce webhook server gestart op poort {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
