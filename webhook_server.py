import os
import time
import logging
import requests
import jwt as pyjwt

from flask import Flask, request, jsonify
from simple_salesforce import Salesforce

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ââ Salesforce JWT config ââââââââââââââââââââââââââââââââââââââââââââââââââ
SF_CONSUMER_KEY  = os.environ["SF_CONSUMER_KEY"]
SF_USERNAME      = os.environ["SF_USERNAME"]
SF_PRIVATE_KEY   = os.environ["SF_PRIVATE_KEY"].replace("\\n", "\n")
SF_DOMAIN        = os.environ.get("SF_DOMAIN", "login")   # "login" = productie

# Salesforce token endpoint
SF_TOKEN_URL = f"https://{SF_DOMAIN}.salesforce.com/services/oauth2/token"

# ââ WebOrder config ââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
SF_WEBORDER_OBJECT      = os.environ.get("SF_WEBORDER_OBJECT", "WebOrder__c")
SF_WEBORDER_PHONE_FIELD = os.environ.get("SF_WEBORDER_PHONE_FIELD", "Telefoonnummer__c")

# ââ Rinkel config ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
RINKEL_API_KEY  = os.environ["RINKEL_API_KEY"]
RINKEL_API_BASE = "https://api.rinkel.com/v1"


def enrich_data_from_cdr(call_id):
    """Haal volledige CDR op van Rinkel API en retourneer dict met verrijkte velden.
    Rinkel maakt de CDR soms later aan dan de webhook: probeer 3x met pauze.
    De callEnd webhook bevat alleen id/datetime/cause/callRecordingUrl;
    alle andere info (beller, duur, agent, richting) komt uit de CDR API."""
    url = f"{RINKEL_API_BASE}/call-detail-records/by-call-id/{call_id}"
    for poging in range(3):
        try:
            wait_secs = 2 if poging == 0 else 4
            time.sleep(wait_secs)  # wacht zodat Rinkel de CDR kan aanmaken
            resp = requests.get(
                url,
                headers={"x-rinkel-api-key": RINKEL_API_KEY},
                timeout=5,
            )
            resp.raise_for_status()
            cdr = resp.json().get("data", {})
            ext = cdr.get("externalNumber", {})

            # Wacht tot CDR volledig beschikbaar is
            if not ext.get("e164") and not ext.get("anonymous") and poging < 2:
                logger.info(f"CDR nog niet volledig bij poging {poging + 1}, opnieuw proberen...")
                continue

            result = {}

            # Bellernummer (localized formaat zoals "06 53233740")
            if ext.get("anonymous"):
                result["callerNumber"] = "anoniem"
            else:
                result["callerNumber"] = ext.get("localized") or ext.get("e164") or ""

            # Richting en duur
            result["direction"] = cdr.get("direction", "inbound")
            result["duration"]  = cdr.get("duration", 0)

            # Agent (medewerker)
            user = cdr.get("user") or {}
            if user:
                result["agentName"] = user.get("fullName", "")

            # Intern nummer (gebeld)
            internal = cdr.get("internalNumber") or {}
            if internal:
                result["calleeNumber"] = (
                    internal.get("localizedNumber") or internal.get("number", "")
                )

            # Opname URL
            recording = cdr.get("callRecording") or {}
            if recording:
                result["recordingUrl"] = recording.get("playUrl", "")

            logger.info(
                f"CDR opgehaald: beller={result.get('callerNumber')}, "
                f"duur={result.get('duration')}s, agent={result.get('agentName', '')}"
            )
            return result

        except Exception as e:
            logger.warning(f"CDR ophalen mislukt (poging {poging + 1}): {e}")
    return {}

def get_sf_connection():
    """Maak Salesforce verbinding via JWT Bearer Flow."""
    payload = {
        "iss": SF_CONSUMER_KEY,
        "sub": SF_USERNAME,
        "aud": f"https://{SF_DOMAIN}.salesforce.com",
        "exp": int(time.time()) + 300,
    }
    token = pyjwt.encode(payload, SF_PRIVATE_KEY, algorithm="RS256")
    resp = requests.post(SF_TOKEN_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": token,
    })
    resp.raise_for_status()
    data = resp.json()
    instance_url = data["instance_url"]
    access_token = data["access_token"]
    subdomain = instance_url.replace("https://", "").split(".")[0]
    return Salesforce(
        instance_url=instance_url,
        session_id=access_token,
        domain=SF_DOMAIN,
    )


def find_weborders_by_phone(sf, phone):
    """Zoek alle WebOrders op basis van telefoonnummer. Geeft lijst van IDs terug."""
    phone_clean = phone.strip().replace(" ", "")
    variants = [phone_clean]
    if phone_clean.startswith("+31"):
        variants.append("0" + phone_clean[3:])
    elif phone_clean.startswith("0"):
        variants.append("+31" + phone_clean[1:])
    seen_ids = set()
    weborder_ids = []
    for variant in variants:
        escaped = variant.replace("'", "\'")
        query = (
            f"SELECT Id, Name FROM {SF_WEBORDER_OBJECT} "
            f"WHERE {SF_WEBORDER_PHONE_FIELD} = '{escaped}' "
            f"ORDER BY CreatedDate DESC LIMIT 200"
        )
        result = sf.query(query)
        for record in result.get("records", []):
            if record["Id"] not in seen_ids:
                seen_ids.add(record["Id"])
                weborder_ids.append(record["Id"])
    return weborder_ids

def find_tasks_by_rinkel_id(sf, rinkel_call_id):
    """Zoek alle Tasks op basis van Rinkel call-ID (opgeslagen in CallObject)."""
    escaped = rinkel_call_id.replace("'", "\'")
    result = sf.query(
        f"SELECT Id FROM Task WHERE CallObject = '{escaped}' LIMIT 200"
    )
    return [r["Id"] for r in result.get("records", [])]

CAUSE_LABELS = {
    "OUTSIDE_OPERATION_TIMES": "Buiten openingstijden",
    "NO_ANSWER"              : "Niet opgenomen",
    "BUSY"                   : "In gesprek",
    "REJECTED"               : "Geweigerd",
    "VOICEMAIL"              : "Voicemail",
}


def build_task(call_data, weborder_id):
    """Bouw Task-dict op basis van Rinkel callEnd-data."""
    direction   = call_data.get("direction", "inbound")
    duration    = call_data.get("duration") or call_data.get("callDuration") or call_data.get("call_duration", 0)
    caller      = call_data.get("callerNumber") or call_data.get("caller_number", "onbekend")
    callee      = call_data.get("calleeNumber") or call_data.get("callee_number", "")
    rinkel_id   = call_data.get("id") or call_data.get("callId") or call_data.get("call_id", "")
    recording   = call_data.get("recordingUrl") or call_data.get("recording_url", "")
    agent       = call_data.get("agentName") or call_data.get("agent_name", "")
    cause       = call_data.get("cause", "")
    richting_nl = "Inkomend" if direction == "inbound" else "Uitgaand"
    minuten     = duration // 60
    seconden    = duration % 60
    duur_str    = f"{minuten}m {seconden}s"
    if cause == "OUTSIDE_OPERATION_TIMES":
        subject = f"Gemist (buiten openingstijden) - {caller}"
    elif cause in CAUSE_LABELS:
        subject = f"Gemist gesprek - {caller}"
    else:
        subject = f"Gesprek {richting_nl} – Beantwoord"
    omschrijving_regels = [
        f"Richting: {richting_nl}",
        f"Nummer: {caller}",
    ]
    if callee and callee not in ("onbekend", ""):
        omschrijving_regels.append(f"Gebeld: {callee}")
    omschrijving_regels.append(f"Duur: {duur_str}")
    if cause and cause in CAUSE_LABELS:
        omschrijving_regels.append(f"Reden: {CAUSE_LABELS.get(cause, cause)}")
    if agent:
        omschrijving_regels.append(f"Medewerker: {agent}")
    if recording:
        omschrijving_regels.append(f"Opname: {recording}")
    omschrijving_regels.append(f"Rinkel ID: {rinkel_id}")
    task = {
        "Subject"             : subject,
        "Description"         : "\n".join(omschrijving_regels),
        "Status"              : "Voltooid",
        "CallDurationInSeconds": duration,
        "CallObject"          : rinkel_id,
        "TaskSubtype"         : "Call",
    }
    if weborder_id:
        task["WhatId"] = weborder_id
    return task

def _insights_lines(insights):
    """Formatteer AI-insights naar tekstregels."""
    lines = []
    if insights.get("summary"):
        lines.append(f"\n--- AI Samenvatting ---\n{insights['summary']}")
    if insights.get("sentiment"):
        lines.append(f"Sentiment: {insights['sentiment']}")
    if insights.get("topics"):
        topics = ", ".join(insights["topics"]) if isinstance(insights["topics"], list) else insights["topics"]
        lines.append(f"Onderwerpen: {topics}")
    return "\n".join(lines)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/webhook/callend", methods=["POST"])
def webhook_callend():
    data = request.get_json(force=True) or {}
    logger.info(f"callEnd ontvangen: {data}")
    if request.headers.get("X-Rinkel-Token") != RINKEL_API_KEY:
        logger.warning("Ongeldige API-key")
    # De callEnd webhook bevat alleen id/datetime/cause/callRecordingUrl.
    # Haal alle overige info (beller, duur, agent, richting) op via de CDR API.
    call_id = data.get("id") or data.get("callId") or data.get("call_id", "")
    if call_id:
        cdr_data = enrich_data_from_cdr(call_id)
        if cdr_data:
            data.update(cdr_data)
    # Fallback: gebruik callRecordingUrl als recordingUrl nog niet gezet is
    if not data.get("recordingUrl") and data.get("callRecordingUrl"):
        data["recordingUrl"] = data["callRecordingUrl"]
    phone = data.get("callerNumber", "")
    try:
        sf = get_sf_connection()
        weborder_ids = find_weborders_by_phone(sf, phone) if phone else []
        if not weborder_ids:
            logger.warning(f"Geen WebOrder gevonden voor nummer: {phone}")
            task = build_task(data, None)
            result = sf.Task.create(task)
            logger.info(f"Task aangemaakt (geen WebOrder): {result}")
            return jsonify({"status": "ok", "task_ids": [result.get("id")]}), 201
        task_ids = []
        for wo_id in weborder_ids:
            task = build_task(data, wo_id)
            result = sf.Task.create(task)
            logger.info(f"Task aangemaakt voor WebOrder {wo_id}: {result}")
            task_ids.append(result.get("id"))
        return jsonify({"status": "ok", "task_ids": task_ids}), 201
    except Exception as e:
        logger.error(f"Fout bij callEnd: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/webhook/callinsights", methods=["POST"])
def webhook_callinsights():
    data = request.get_json(force=True) or {}
    logger.info(f"callInsights ontvangen: {data}")
    rinkel_call_id = data.get("callId") or data.get("call_id", "")
    insights       = data.get("insights") or data
    try:
        sf = get_sf_connection()
        task_ids = find_tasks_by_rinkel_id(sf, rinkel_call_id)
        if not task_ids:
            logger.warning(f"Geen Task gevonden voor Rinkel ID: {rinkel_call_id}")
            return jsonify({"status": "not_found"}), 404
        extra_tekst = _insights_lines(insights)
        for task_id in task_ids:
            task_record = sf.Task.get(task_id)
            huidige_beschrijving = task_record.get("Description") or ""
            nieuwe_beschrijving  = huidige_beschrijving + extra_tekst
            sf.Task.update(task_id, {"Description": nieuwe_beschrijving})
            logger.info(f"Task {task_id} bijgewerkt met AI-insights")
        return jsonify({"status": "ok", "updated": len(task_ids)}), 200
    except Exception as e:
        logger.error(f"Fout bij callInsights: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
