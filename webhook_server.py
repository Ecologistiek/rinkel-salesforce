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


def get_caller_from_rinkel_api(call_id):
    """Haal bellernummer op van Rinkel API via call-ID (bijv. bij OUTSIDE_OPERATION_TIMES).
    Rinkel maakt de CDR soms later aan dan de webhook: probeer 3x met pauze."""
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
            if ext.get("anonymous"):
                return "anoniem"
            phone = ext.get("e164") or ext.get("localized") or ""
            if phone:
                return phone
            logger.info(f"Rinkel API: nog geen nummer bij poging {poging + 1}, wacht...")
        except Exception as e:
            logger.warning(f"Kon bellernummer niet ophalen van Rinkel API (poging {poging + 1}): {e}")
    return ""


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


def find_weborder_by_phone(sf, phone):
    """Zoek WebOrder op basis van telefoonnummer."""
    phone_clean = phone.strip().replace(" ", "")
    variants = [phone_clean]
    if phone_clean.startswith("+31"):
        variants.append("0" + phone_clean[3:])
    elif phone_clean.startswith("0"):
        variants.append("+31" + phone_clean[1:])
    for variant in variants:
        escaped = variant.replace("'", "\\'")
        query = (
            f"SELECT Id, Name FROM {SF_WEBORDER_OBJECT} "
            f"WHERE {SF_WEBORDER_PHONE_FIELD} = '{escaped}' "
            f"ORDER BY CreatedDate DESC LIMIT 1"
        )
        result = sf.query(query)
        if result["totalSize"] > 0:
            return result["records"][0]["Id"]
    return None


def find_task_by_rinkel_id(sf, rinkel_call_id):
    """Zoek bestaande Task op basis van Rinkel call-ID (opgeslagen in CallObject)."""
    escaped = rinkel_call_id.replace("'", "\\'")
    result = sf.query(
        f"SELECT Id FROM Task WHERE CallObject = '{escaped}' LIMIT 1"
    )
    if result["totalSize"] > 0:
        return result["records"][0]["Id"]
    return None


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
    duration    = call_data.get("duration", 0)
    caller      = call_data.get("callerNumber") or call_data.get("caller_number", "onbekend")
    callee      = call_data.get("calleeNumber") or call_data.get("callee_number", "onbekend")
    rinkel_id   = call_data.get("callId") or call_data.get("call_id", "")
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
        subject = f"Gesprek {richting_nl} - {caller}"

    omschrijving_regels = [
        f"Richting: {richting_nl}",
        f"Beller: {caller}",
        f"Gebeld: {callee}",
        f"Duur: {duur_str}",
    ]
    if cause:
        omschrijving_regels.append(f"Reden: {CAUSE_LABELS.get(cause, cause)}")
    if agent:
        omschrijving_regels.append(f"Agent: {agent}")
    if recording:
        omschrijving_regels.append(f"Opname: {recording}")
    omschrijving_regels.append(f"Rinkel ID: {rinkel_id}")
    task = {
        "Subject"      : subject,
        "Description"  : "\n".join(omschrijving_regels),
        "Status"       : "Voltooid",
        "CallDurationInSeconds": duration,
        "CallObject"   : rinkel_id,
        "TaskSubtype"  : "Call",
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
    phone = (
        data.get("callerNumber")
        or data.get("caller_number")
        or data.get("calleeNumber")
        or data.get("callee_number")
        or ""
    )
    # Bij OUTSIDE_OPERATION_TIMES zit er geen nummer in de webhook payload.
    # Haal het op via de Rinkel API.
    if not phone:
        rinkel_call_id = data.get("id") or data.get("callId") or data.get("call_id", "")
        if rinkel_call_id:
            phone = get_caller_from_rinkel_api(rinkel_call_id)
            if phone:
                logger.info(f"Bellernummer opgehaald via Rinkel API: {phone}")
                data["callerNumber"] = phone  # zodat build_task het meepakt
    try:
        sf = get_sf_connection()
        weborder_id = find_weborder_by_phone(sf, phone) if phone else None
        if not weborder_id:
            logger.warning(f"Geen WebOrder gevonden voor nummer: {phone}")
        task = build_task(data, weborder_id)
        result = sf.Task.create(task)
        logger.info(f"Task aangemaakt: {result}")
        return jsonify({"status": "ok", "task_id": result.get("id")}), 201
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
        task_id = find_task_by_rinkel_id(sf, rinkel_call_id)
        if not task_id:
            logger.warning(f"Geen Task gevonden voor Rinkel ID: {rinkel_call_id}")
            return jsonify({"status": "not_found"}), 404
        extra_tekst = _insights_lines(insights)
        task_record = sf.Task.get(task_id)
        huidige_beschrijving = task_record.get("Description") or ""
        nieuwe_beschrijving  = huidige_beschrijving + extra_tekst
        sf.Task.update(task_id, {"Description": nieuwe_beschrijving})
        logger.info(f"Task {task_id} bijgewerkt met AI-insights")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Fout bij callInsights: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
