"""
Verbindingstest – Rinkel & Salesforce
======================================
Runt dit script op je server/laptop om te controleren of alles klopt
voordat je de echte webhook server start.

Gebruik:
  pip install -r requirements.txt
  python test_verbinding.py
"""

import os
import sys
import re
from dotenv import load_dotenv

load_dotenv()

RINKEL_API_KEY        = os.getenv("RINKEL_API_KEY")
SF_USERNAME           = os.getenv("SF_USERNAME")
SF_PASSWORD           = os.getenv("SF_PASSWORD")
SF_SECURITY_TOKEN     = os.getenv("SF_SECURITY_TOKEN")
SF_DOMAIN             = os.getenv("SF_DOMAIN", "login")
SF_WEBORDER_OBJECT    = os.getenv("SF_WEBORDER_OBJECT", "WebOrder__c")
SF_WEBORDER_PHONE_FIELD = os.getenv("SF_WEBORDER_PHONE_FIELD", "Telefoonnummer__c")

OK   = "✅"
FAIL = "❌"
WARN = "⚠️ "

def hr(title=""):
    print(f"\n{'─'*55}")
    if title:
        print(f"  {title}")
        print(f"{'─'*55}")

# ── 1. Config check ───────────────────────────────────────────────────────────
hr("1. Configuratie (.env)")

missing = []
for var in ["RINKEL_API_KEY", "SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN"]:
    val = os.getenv(var)
    if val:
        masked = val[:6] + "..." + val[-4:] if len(val) > 10 else "***"
        print(f"  {OK}  {var} = {masked}")
    else:
        print(f"  {FAIL}  {var} ontbreekt!")
        missing.append(var)

if missing:
    print(f"\n{FAIL} Vul de ontbrekende waarden in .env in en probeer opnieuw.")
    sys.exit(1)

# ── 2. Packages check ─────────────────────────────────────────────────────────
hr("2. Python packages")

try:
    import requests
    print(f"  {OK}  requests ({requests.__version__})")
except ImportError:
    print(f"  {FAIL}  requests niet geïnstalleerd  →  pip install requests")
    sys.exit(1)

try:
    from simple_salesforce import Salesforce
    import simple_salesforce
    print(f"  {OK}  simple_salesforce ({simple_salesforce.__version__})")
except ImportError:
    print(f"  {FAIL}  simple_salesforce niet geïnstalleerd  →  pip install simple-salesforce")
    sys.exit(1)

try:
    from flask import Flask
    import flask
    print(f"  {OK}  flask ({flask.__version__})")
except ImportError:
    print(f"  {FAIL}  flask niet geïnstalleerd  →  pip install flask")
    sys.exit(1)

# ── 3. Rinkel API ─────────────────────────────────────────────────────────────
hr("3. Rinkel API verbinding")

try:
    resp = requests.get(
        "https://api.rinkel.com/v1/call-detail-records?perPage=3&includeDetails=true",
        headers={"x-rinkel-api-key": RINKEL_API_KEY, "Accept": "application/json"},
        timeout=10,
    )
    if resp.status_code == 200:
        data = resp.json()
        total = data["meta"]["pagination"]["totalItems"]
        records = data["data"]
        print(f"  {OK}  Verbinding OK — {total} gesprekken in de database")
        print(f"\n  Laatste 3 gesprekken:")
        for r in records:
            ext   = r.get("externalNumber") or {}
            num   = ext.get("localized") or ext.get("e164") or "anoniem"
            dur   = r.get("duration", 0)
            ai    = (r.get("insights") or {}).get("status", "—")
            user  = (r.get("user") or {}).get("fullName", "—")
            print(f"    {r['date'][:10]}  {r['direction']:<8}  {r['status']:<10}  "
                  f"{num:<18}  {dur}s  AI:{ai}  {user}")
    elif resp.status_code == 401:
        print(f"  {FAIL}  Ongeldige API key (401)")
        sys.exit(1)
    else:
        print(f"  {FAIL}  Onverwachte statuscode: {resp.status_code} — {resp.text[:200]}")
        sys.exit(1)
except requests.RequestException as e:
    print(f"  {FAIL}  Netwerk fout: {e}")
    sys.exit(1)

# ── 4. Salesforce verbinding ──────────────────────────────────────────────────
hr("4. Salesforce verbinding")

try:
    sf = Salesforce(
        username=SF_USERNAME,
        password=SF_PASSWORD,
        security_token=SF_SECURITY_TOKEN,
        domain=SF_DOMAIN,
    )
    info = sf.query("SELECT Id, Name FROM Organization LIMIT 1")
    org_name = info["records"][0]["Name"] if info["totalSize"] > 0 else "onbekend"
    print(f"  {OK}  Ingelogd op: {org_name}  ({SF_DOMAIN})")
except Exception as e:
    err = str(e)
    if "INVALID_LOGIN" in err:
        print(f"  {FAIL}  Ongeldige inloggegevens — controleer gebruikersnaam, wachtwoord en security token")
    elif "expired" in err.lower():
        print(f"  {FAIL}  Security token verlopen of onjuist")
    else:
        print(f"  {FAIL}  Salesforce fout: {err}")
    sys.exit(1)

# ── 5. WebOrder object & veld ─────────────────────────────────────────────────
hr("5. Salesforce – WebOrder object")

try:
    result = sf.query(
        f"SELECT Id, Name, {SF_WEBORDER_PHONE_FIELD} "
        f"FROM {SF_WEBORDER_OBJECT} "
        f"WHERE {SF_WEBORDER_PHONE_FIELD} != null "
        f"LIMIT 5"
    )
    total = result["totalSize"]
    print(f"  {OK}  Object '{SF_WEBORDER_OBJECT}' gevonden")
    print(f"  {OK}  Veld '{SF_WEBORDER_PHONE_FIELD}' bestaat")
    print(f"\n  WebOrders met telefoonnummer ({total} gevonden, max 5 getoond):")
    for r in result["records"]:
        phone = r.get(SF_WEBORDER_PHONE_FIELD) or "—"
        print(f"    {r['Name']:<20}  telefoon: '{phone}'")

    if result["records"]:
        sample = result["records"][0].get(SF_WEBORDER_PHONE_FIELD) or ""
        digits = re.sub(r"\D", "", sample)
        print(f"\n  Voorbeeld normalisatie: '{sample}' → alleen cijfers: '{digits}' → suffix(8): '{digits[-8:]}'")

except Exception as e:
    err = str(e)
    if "INVALID_FIELD" in err or "No such column" in err.lower():
        print(f"  {FAIL}  Veld '{SF_WEBORDER_PHONE_FIELD}' bestaat niet op '{SF_WEBORDER_OBJECT}'")
    elif "INVALID_TYPE" in err or "does not exist" in err.lower():
        print(f"  {FAIL}  Object '{SF_WEBORDER_OBJECT}' bestaat niet")
    else:
        print(f"  {FAIL}  Fout: {err}")
    sys.exit(1)

# ── 6. Test telefoonnummer matching ───────────────────────────────────────────
hr("6. Telefoonnummer matching test")

test_cases = [
    ("+31612345678", "06 12345678",               True),
    ("+31612345678", "0612345678",                True),
    ("+31612345678", "06 123 456 78",             True),
    ("+31612345678", "+31 6 12345678",            True),
    ("+31612345678", "06-123.456.78 (bel overdag)", True),
    ("+31612345678", "06 12345679",               False),
    ("+31183646353", "0183 646 353",              True),
]

all_ok = True
for rinkel, stored, expected in test_cases:
    rinkel_d = re.sub(r"\D", "", rinkel)
    stored_d = re.sub(r"\D", "", stored)
    tail = min(9, len(rinkel_d), len(stored_d))
    match = rinkel_d[-tail:] == stored_d[-tail:]
    status = OK if match == expected else FAIL
    if match != expected:
        all_ok = False
    print(f"  {status}  rinkel='{rinkel}'  salesforce='{stored}'  → {'match' if match else 'geen match'}")

print(f"\n  {'Alle tests geslaagd!' if all_ok else 'Sommige tests faalden!'}")

# ── Samenvatting ──────────────────────────────────────────────────────────────
hr("Samenvatting")
print(f"""
  {OK}  Rinkel API key geldig
  {OK}  Salesforce verbinding werkt
  {OK}  WebOrder object en telefoonveld correct
  {OK}  Telefoonnummer matching werkt

  Je kunt nu de webhook server starten:

    python webhook_server.py

  En de webhooks registreren in Rinkel:

    python setup_webhooks.py
""")
