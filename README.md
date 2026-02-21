# Rinkel → Salesforce integratie

Logt automatisch belgeschiedenis van Rinkel als Activiteit (Task) op het **Weborder** object in Salesforce. Inclusief richting (in/outbound), status, duur, opname-link en AI-samenvatting.

---

## Hoe werkt het?

```
Gesprek eindigt
      ↓
Rinkel stuurt callEnd webhook → webhook_server.py
      ↓
Server haalt volledige gespreksdetails op via Rinkel API
      ↓
Zoekt Weborder in Salesforce op basis van telefoonnummer
      ↓
Maakt Activiteit (Task) aan op de Weborder

(Zodra AI-analyse klaar is)
Rinkel stuurt callInsights webhook → webhook_server.py
      ↓
Voegt samenvatting + sentiment toe aan bestaande Task
```

---

## Installatie

### 1. Vereisten
- Python 3.11+
- Een server met een publiek toegankelijke URL (bijv. VPS, Railway, Render)

### 2. Bestanden kopiëren
Kopieer alle bestanden naar je server.

### 3. Dependencies installeren
```bash
pip install -r requirements.txt
```

### 4. Configuratie
Kopieer `.env.example` naar `.env` en vul alle waarden in:
```bash
cp .env.example .env
nano .env
```

| Variabele | Wat invullen |
|---|---|
| `RINKEL_API_KEY` | Jouw Rinkel API-sleutel |
| `SF_USERNAME` | Salesforce gebruikersnaam |
| `SF_PASSWORD` | Salesforce wachtwoord |
| `SF_SECURITY_TOKEN` | Salesforce security token |
| `SF_WEBORDER_PHONE_FIELD` | API-naam van het telefoonnummerveld op Weborder |
| `WEBHOOK_BASE_URL` | Publieke URL van deze server |

### 5. Salesforce security token ophalen
1. Log in op Salesforce
2. Klik rechtsboven op je naam → **Instellingen**
3. Ga naar **Mijn persoonlijke info** → **Reset mijn beveiligingstoken**
4. Je ontvangt het token per e-mail

### 6. Server starten
```bash
python webhook_server.py
```

Test of de server draait:
```bash
curl http://localhost:5000/health
# → {"status": "ok"}
```

### 7. Webhooks registreren in Rinkel
Voer dit éénmalig uit zodra de server publiek bereikbaar is:
```bash
python setup_webhooks.py
```

---

## Wat verschijnt er in Salesforce?

Elke afgeronde oproep levert een **Activiteit** op de Weborder met:

```
Gesprek Inkomend – Beantwoord

Richting  : Inkomend
Status    : Beantwoord
Duur      : 4:32 min
Nummer    : 06 12345678
Medewerker: Jan de Vries

Opname (beschikbaar tot 2026-05-01):
https://api.rinkel.com/call-recordings/...

--- AI Inzichten ---
Samenvatting:
Klant belde over de levertijd van de salontafel. Medewerker heeft
bevestigd dat levering plaatsvindt op 28 februari. Klant tevreden.

Sentiment: Positief
Onderwerpen: levering, product
```

---

## Hosting-opties

| Optie | Kosten | Geschikt voor |
|---|---|---|
| **Railway** (railway.app) | ~$5/maand | Eenvoudig, aanbevolen |
| **Render** (render.com) | Gratis tier beschikbaar | Testen |
| **Eigen VPS** (Hetzner, DigitalOcean) | ~$5/maand | Volledige controle |

Voor productie: zorg voor HTTPS (verplicht voor Rinkel webhooks).

---

## Problemen oplossen

**Geen Weborder gevonden**
→ Controleer of het telefoonnummer in Salesforce overeenkomt met het bellers-nummer.
→ Controleer `SF_WEBORDER_PHONE_FIELD` in `.env`.

**Salesforce authenticatie mislukt**
→ Controleer of het security token juist is. Na wachtwoordwijziging verandert het token ook.
→ Zorg dat de gebruiker API-toegang heeft in Salesforce (profiel → API ingeschakeld).

**Rinkel geeft 403 terug bij webhook registratie**
→ Controleer of jouw Rinkel-abonnement integraties ondersteunt.
