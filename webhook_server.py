# deploy trigger 12
import os
import re
import time
import logging
import threading
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
from simple_salesforce import Salesforce

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# ── Salesforce config ────────────────────────────────────────────────────────
SF_USERNAME       = os.environ["SF_USERNAME"]
SF_PASSWORD       = os.environ["SF_PASSWORD"]
SF_SECURITY_TOKEN = os.environ["SF_SECURITY_TOKEN"]
SF_DOMAIN         = os.environ.get("SF_DOMAIN", "login")  # "login" = productie, "test" = sandbox

# ── WebOrder config ──────────────────────────────────────────────────────────
SF_WEBORDER_OBJECT       = os.environ.get("SF_WEBORDER_OBJECT", "WebOrder__c")
SF_WEBORDER_PHONE_FIELD  = os.environ.get("SF_WEBORDER_PHONE_FIELD", "Telefoonnummer__c")

# ── Rinkel config ────────────────────────────────────────────────────────────
RINKEL_API_KEY  = os.environ["RINKEL_API_KEY"]
RINKEL_API_BASE = "https://api.rinkel.com/v1"

AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

MAANDEN_NL = [
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
]


def format_datetime_nl(datetime_str):
    """Zet ISO datetime-string om naar Nederlandse notatie ('22 februari 20:58')."""
    if not datetime_str:
        return ""
    try:
        dt = datetime.fromisoformat(datetime_str.replace("Z", "+00:00"))
        dt_local = dt.astimezone(AMSTERDAM_TZ)
        return f"{dt_local.day} {MAANDEN_NL[dt_local.month - 1]} {dt_local.strftime('%H:%M')}"
    except Exception:
        return ""


def enrich_data_from_cdr(call_id):
    """Haal volledige CDR op van Rinkel API en retourneer dict met verrijkte velden."""
    url = f"{RINKEL_API_BASE}/call-detail-records/by-call-id/{call_id}"
    for poging in range(3):
        try:
            wait_secs = 2 if poging == 0 else 4
            time.sleep(wait_secs)
            resp = requests.get(
                url,
                headers={"x-rinkel-api-key": RINKEL_API_KEY},
                timeout=5,
            )
            resp.raise_for_status()
            cdr = resp.json().get("data", {})
            ext = cdr.get("externalNumber", {})

            if not ext.get("e164") and not ext.get("anonymous") and poging < 2:
                logger.info(f"CDR nog niet volledig bij poging {poging + 1}, opnieuw proberen...")
                continue

            result = {}

            if ext.get("anonymous"):
                result["callerNumber"] = "anoniem"
            else:
                result["callerNumber"] = ext.get("localized") or ext.get("e164") or ""

            result["direction"] = cdr.get("direction", "inbound")
            result["duration"]  = cdr.get("duration", 0)

            user = cdr.get("user") or {}
            if user:
                result["agentName"] = user.get("fullName", "")

            internal = cdr.get("internalNumber") or {}
            if internal:
                result["calleeNumber"] = (
                    internal.get("localizedNumber") or internal.get("number", "")
                )

            recording = cdr.get("callRecording") or {}
            if recording:
                result["recordingUrl"] = recording.get("playUrl", "")

            result["datetime_str"] = cdr.get("date", "")

            logger.info(
                f"CDR opgehaald: beller={result.get('callerNumber')}, "
                f"duur={result.get('duration')}s, agent={result.get('agentName', '')}"
            )
            return result

        except Exception as e:
            logger.warning(f"CDR ophalen mislukt (poging {poging + 1}): {e}")
    return {}


def get_sf_connection():
    """Maak Salesforce verbinding via username + password + security token."""
    return Salesforce(
        username=SF_USERNAME,
        password=SF_PASSWORD,
        security_token=SF_SECURITY_TOKEN,
        domain=SF_DOMAIN,
    )


def normalize_phone(phone):
    """Haal alleen cijfers uit een telefoonnummer en normaliseer naar NL-formaat."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("31") and len(digits) >= 11:
        digits = "0" + digits[2:]
    return digits


def build_like_pattern(phone_digits):
    """Bouw SOQL LIKE-patroon met wildcards tussen elke cijfer.
    Voorbeeld: '0655699265' -> '%6%5%5%6%9%9%2%6%5%'
    Dit matcht nummers ongeacht spaties, streepjes of andere tekens ertussen.
    Gebruik de laatste 9 cijfers (significant deel NL-nummer)."""
    significant = phone_digits[-9:] if len(phone_digits) >= 9 else phone_digits
    pattern = "%" + "%".join(significant) + "%"
    return pattern


def find_weborders_by_phone(sf, phone):
    """Zoek WebOrders op telefoonnummer met genormaliseerde cijfervergelijking.
    Strategie:
      1. Normaliseer het inkomende nummer naar alleen cijfers.
      2. Bouw SOQL LIKE-patroon met wildcards tussen elke cijfer,
         zodat spaties, streepjes, punten, haakjes etc. geen probleem zijn.
         Voorbeeld: '%6%5%5%6%9%9%2%6%5%' matcht '*31 6 - 55 6 99 26 5 (Kristel)'
      3. Vergelijk in Python op volledige genormaliseerde cijferreeks (exacte match).
    """
    phone_digits = normalize_phone(phone)
    if not phone_digits:
        return []

    like_pattern = build_like_pattern(phone_digits)
    escaped_pattern = like_pattern.replace("'", "\\'")

    query = (
        f"SELECT Id, Name, {SF_WEBORDER_PHONE_FIELD} FROM {SF_WEBORDER_OBJECT} "
        f"WHERE {SF_WEBORDER_PHONE_FIELD} LIKE '{escaped_pattern}' "
        f"ORDER BY CreatedDate DESC LIMIT 200"
    )
    logger.info(f"WebOrder-zoekopdracht: LIKE '{like_pattern}' (genorm. beller: {phone_digits})")

    result = sf.query(query)
    candidates = result.get("records", [])

    seen_ids     = set()
    weborder_ids = []
    for record in candidates:
        stored_raw    = record.get(SF_WEBORDER_PHONE_FIELD) or ""
        stored_digits = normalize_phone(stored_raw)
        if stored_digits == phone_digits:
            if record["Id"] not in seen_ids:
                seen_ids.add(record["Id"])
                weborder_ids.append(record["Id"])

    if not weborder_ids and candidates:
        logger.warning(
            f"Geen exacte match voor {phone} ({phone_digits}); "
            f"LIKE-kandidaten: {[r.get(SF_WEBORDER_PHONE_FIELD) for r in candidates[:5]]}"
        )

    return weborder_ids


def find_tasks_by_rinkel_id(sf, rinkel_call_id):
    """Zoek alle Tasks op basis van Rinkel call-ID (opgeslagen in CallObject)."""
    if not rinkel_call_id:
        logger.warning("find_tasks_by_rinkel_id: lege rinkel_call_id, geen query uitgevoerd")
        return []
    escaped = rinkel_call_id.replace("'", "\\'")
    result = sf.query(
        f"SELECT Id FROM Task WHERE CallObject = '{escaped}' LIMIT 200"
    )
    return [r["Id"] for r in result.get("records", [])]


CAUSE_LABELS = {
    "OUTSIDE_OPERATION_TIMES": "Buiten openingstijden",
    "NO_ANSWER" : "Niet opgenomen",
    "BUSY"      : "In gesprek",
    "REJECTED"  : "Geweigerd",
    "VOICEMAIL" : "Voicemail",
}


def build_task(call_data, weborder_id):
    """Bouw Task-dict op basis van Rinkel callEnd-data."""
    direction    = call_data.get("direction", "inbound")
    duration     = call_data.get("duration") or call_data.get("callDuration") or call_data.get("call_duration", 0)
    caller       = call_data.get("callerNumber") or call_data.get("caller_number", "onbekend")
    callee       = call_data.get("calleeNumber") or call_data.get("callee_number", "")
    rinkel_id    = call_data.get("id") or call_data.get("callId") or call_data.get("call_id", "")
    agent        = call_data.get("agentName") or call_data.get("agent_name", "")
    cause        = call_data.get("cause", "")
    datetime_str = call_data.get("datetime_str", "") or call_data.get("datetime", "")

    richting_nl = "Inkomend" if direction == "inbound" else "Uitgaand"
    minuten     = duration // 60
    seconden    = duration % 60
    duur_str    = f"{minuten}m {seconden}s"
    tijdstip    = format_datetime_nl(datetime_str)

    activity_date = None
    if datetime_str:
        try:
            dt = datetime.fromisoformat(datetime_str.replace("Z", "+00:00"))
            dt_local = dt.astimezone(AMSTERDAM_TZ)
            activity_date = dt_local.strftime("%Y-%m-%d")
        except Exception:
            pass

    if cause == "OUTSIDE_OPERATION_TIMES":
        subject = f"Gemist (buiten openingstijden) - {caller}"
        if tijdstip:
            subject += f" {tijdstip}"
    elif cause in CAUSE_LABELS:
        subject = f"Gemist gesprek - {caller}"
        if tijdstip:
            subject += f" {tijdstip}"
    else:
        subject = f"Gesprek {richting_nl} \u2013 Beantwoord"
        if tijdstip:
            subject += f" {tijdstip}"

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

    task = {
        "Subject"              : subject,
        "Description"          : "\n".join(omschrijving_regels),
        "Status"               : "Voltooid",
        "CallDurationInSeconds": duration,
        "CallObject"           : rinkel_id,
        "TaskSubtype"          : "Call",
    }
    if activity_date:
        task["ActivityDate"] = activity_date
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


def _process_callend(data):
    """Verwerk callEnd-data op de achtergrond (buiten de HTTP-request context)."""
    call_id = data.get("id") or data.get("callId") or data.get("call_id", "")
    if call_id:
        cdr_data = enrich_data_from_cdr(call_id)
        if cdr_data:
            data.update(cdr_data)

    if not data.get("recordingUrl") and data.get("callRecordingUrl"):
        data["recordingUrl"] = data["callRecordingUrl"]

    phone = data.get("callerNumber", "")
    try:
        sf           = get_sf_connection()
        weborder_ids = find_weborders_by_phone(sf, phone) if phone else []

        if not weborder_ids:
            logger.warning(f"Geen WebOrder gevonden voor nummer: {phone}")
            task   = build_task(data, None)
            result = sf.Task.create(task)
            logger.info(f"Task aangemaakt (geen WebOrder): {result}")
            return

        for wo_id in weborder_ids:
            task   = build_task(data, wo_id)
            result = sf.Task.create(task)
            logger.info(f"Task aangemaakt voor WebOrder {wo_id}: {result}")

    except Exception as e:
        logger.error(f"Fout bij verwerking callEnd: {e}", exc_info=True)


@app.route("/webhook/callend", methods=["POST"])
def webhook_callend():
    data = request.get_json(force=True) or {}
    logger.info(f"callEnd ontvangen: {data}")

    if request.headers.get("X-Rinkel-Token") != RINKEL_API_KEY:
        logger.warning("Ongeldige API-key")

    thread = threading.Thread(target=_process_callend, args=(data.copy(),), daemon=True)
    thread.start()

    return jsonify({"status": "ok", "message": "verwerking gestart"}), 200


@app.route("/webhook/callinsights", methods=["POST"])
def webhook_callinsights():
    data = request.get_json(force=True) or {}
    logger.info(f"callInsights ontvangen: {data}")

    rinkel_call_id = data.get("id") or data.get("callId") or data.get("call_id", "")
    insights       = data.get("insights") or data

    try:
        sf       = get_sf_connection()
        task_ids = find_tasks_by_rinkel_id(sf, rinkel_call_id)

        if not task_ids:
            logger.warning(f"Geen Task gevonden voor Rinkel ID: {rinkel_call_id}")
            return jsonify({"status": "not_found"}), 200

        extra_tekst = _insights_lines(insights)
        for task_id in task_ids:
            task_record          = sf.Task.get(task_id)
            huidige_beschrijving = task_record.get("Description") or ""
            nieuwe_beschrijving  = extra_tekst + ("\n\n" + huidige_beschrijving if huidige_beschrijving else "")
            sf.Task.update(task_id, {"Description": nieuwe_beschrijving})
            logger.info(f"Task {task_id} bijgewerkt met AI-insights")

        return jsonify({"status": "ok", "updated": len(task_ids)}), 200

    except Exception as e:
        logger.error(f"Fout bij callInsights: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
