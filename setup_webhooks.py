"""
Rinkel Webhook Registratie
===========================
Voer dit script éénmalig uit om de twee benodigde webhooks in Rinkel te registreren.

Gebruik:
  python setup_webhooks.py

Vereisten:
  - .env gevuld met RINKEL_API_KEY en WEBHOOK_BASE_URL
"""

import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

RINKEL_API_KEY   = os.getenv("RINKEL_API_KEY")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL")   # bijv. https://mijnserver.nl


def subscribe(event: str, url: str) -> bool:
    """Registreer één webhook in Rinkel."""
    resp = requests.post(
        f"https://api.rinkel.com/v1/webhooks/{event}",
        headers={
            "x-rinkel-api-key": RINKEL_API_KEY,
            "Content-Type":     "application/json",
            "Accept":           "application/json",
        },
        json={
            "url":         url,
            "contentType": "application/json",
            "active":      True,
            "description": f"Salesforce integratie – {event}",
        },
        timeout=10,
    )
    if resp.status_code in (200, 201):
        print(f"  ✅  '{event}' geregistreerd → {url}")
        return True
    else:
        print(f"  ❌  '{event}' mislukt  ({resp.status_code}): {resp.text}")
        return False


def list_webhooks():
    """Toon alle bestaande webhooks in Rinkel."""
    resp = requests.get(
        "https://api.rinkel.com/v1/webhooks",
        headers={"x-rinkel-api-key": RINKEL_API_KEY, "Accept": "application/json"},
        timeout=10,
    )
    if resp.ok:
        items = resp.json().get("data", [])
        if items:
            print("\nHuidige webhooks in Rinkel:")
            for w in items:
                status = "actief" if w.get("active") else "inactief"
                print(f"  [{status}] {w.get('event')} → {w.get('url')}")
        else:
            print("\nGeen webhooks gevonden in Rinkel.")
    else:
        print(f"\nKon webhooks niet ophalen: {resp.status_code}")


def main():
    print("=" * 55)
    print("  Rinkel → Salesforce  |  Webhook registratie")
    print("=" * 55)

    if not RINKEL_API_KEY:
        print("❌  RINKEL_API_KEY niet ingesteld in .env")
        sys.exit(1)

    if not WEBHOOK_BASE_URL:
        print("❌  WEBHOOK_BASE_URL niet ingesteld in .env")
        print("    Voorbeeld: WEBHOOK_BASE_URL=https://mijnserver.nl")
        sys.exit(1)

    events = {
        "callEnd":      f"{WEBHOOK_BASE_URL.rstrip('/')}/webhook/callend",
        "callInsights": f"{WEBHOOK_BASE_URL.rstrip('/')}/webhook/callinsights",
    }

    print(f"\nBase URL: {WEBHOOK_BASE_URL}\n")
    success = 0
    for event, url in events.items():
        if subscribe(event, url):
            success += 1

    list_webhooks()

    print(f"\n{success}/{len(events)} webhooks succesvol geregistreerd.")


if __name__ == "__main__":
    main()
