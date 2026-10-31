# Raceday Windows - Lead Alert Webhook - Full Transfer Pack
Copy-paste this whole document into Gemini to transfer all info.

## Project goal
When a new lead submits the Raceday Windows Facebook lead form, send an instant SMS alert to the business owner so he can call within 30 minutes. SMS goes to the owner only - never an auto-reply to the lead, never marketing to consumers.

## People and numbers
- Agency owner: Austin Trigueiro, austin@theresalemint.com, phone (903) 569-7757
- Client: Grayson Weber, Raceday Window Cleaning, phone (903) 565-9207 (alert recipient for live leads)
- Test recipient FIRST: Austin at (903) 569-7757. Grayson only after Austin confirms his test.
- Twilio sender number bought: (681) 266-6539, ends 666539 = MONKEY, Keyser, WV, Local, $1.15/month
- Twilio account: Full access, pay-as-you-go, $20 starting balance, auto-recharge on
- Twilio brand: Austin Trigueiro, SOLE_PROPRIETOR, Approved
- Twilio messaging service: Raceday Lead Alerts
- Twilio campaign: Sole Proprietor use case, NOT yet submitted. Fee: $15 one-time vetting + $2/month. Balance $16.00. Sample messages below. Campaign is in Draft in Twilio Console.
- GitHub repo created by Austin: https://github.com/Austin-cmd-del/race-day-webhook - Public. Files still need uploading (see below).
- Render: Austin signed in via Google, dashboard "Austin's workspace", New Web Service flow on Public Git Repository step, URL field empty, nothing deployed yet. Free tier only.

## Meta / Facebook campaign (live, do not change without Austin's go)
- Ad account: raceday, ID 1386970390075903, ACTIVE via partner access
- Campaign: Raceday Windows - Leads, ID 120259258103390093
- Ad set: Raceday Windows - Leads Ad Set, ID 120259258103400093, $10/day, 18+, Feeds only, maximize leads, no cost cap, locations +10mi, Chapel Hill town-only
- Ads: Story 120259258103380093, Stay Off the Ladder 120259258494150093, Stop Fighting Streaks 120259258647850093
- Live form: Raceday Windows - Get a Quote - House Size v2-copy, Form ID 2120733161884717
- Form fields: house size (Under 1,500 / 1,500-2,199 / 2,200-3,299 / 3,300+ sq ft), price check Yes/No, full name, phone, street address. Q2 No = disqualified, not a lead.
- Status Oct 31 morning: $26.54 spent total, 2 leads, CPL $13.27, campaign/ad set Active, no restrictions.

## SMS alert text (final, must match Twilio Sample Message #1)
Attention, we got you a lead!

New Lead for Raceday Windows
Lead Name: [Name]
Phone: [Phone]
Address: [Address]
House Size: [size]

Please call as soon as possible - within 30 minutes if between 8am-8pm.

Disclaimer: Only Sigmas Allowed to Call

Twilio Sample Message #2 (enrollment confirmation):
Raceday Lead Alerts: You are enrolled to receive SMS lead alerts at this number. Reply STOP to cancel. Msg & data rates may apply.

## Privacy Policy and Terms (built, need permanent public HTML host)
Privacy Policy must: be titled Privacy Policy, describe data collected/how used, contain "We do not sell or share your SMS opt-in data or personal information with third parties for marketing purposes", mention Austin Trigueiro.
Terms must: be titled Terms & Conditions, include SMS Terms section, include "message and data rates may apply", mention Austin Trigueiro.
Temporary Muse file links EXPIRED / NOT ACCEPTED by Twilio checker (serve as download, not HTML, and expire Nov 2, 2026):
- Privacy (old, do not use for Twilio): https://muse.ai/files/1388254881030222/946652861621329/kujgrsm0wv1t3h49n7f5dhgg/privacy-policy.html
- Terms (old, do not use for Twilio): https://muse.ai/files/1388254881030222/2361090567958533/fgk0xb6gmiikjbn5ddsvwt2q/terms-and-conditions.html
Permanent solution: serve them from the webhook host at /privacy-policy and /terms-and-conditions as real HTML, then use those URLs in Twilio.

## Remaining steps, in order
1. Upload 5 files to https://github.com/Austin-cmd-del/race-day-webhook (Public)
2. Paste repo URL into Render, deploy free-tier Docker web service, verify /health, /privacy-policy, /terms-and-conditions
3. In Twilio, replace Privacy/Terms URLs with the Render URLs, re-check campaign for errors, submit, pay $15 vetting fee
4. Set webhook server env vars (never in chat, never in repo): META_VERIFY_TOKEN, META_PAGE_ACCESS_TOKEN (Page admin Grayson, leads_retrieval, long-lived), TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER=(681) 266-6539, ALERT_TO_NUMBER=+19035659207 for live, +19035697757 for test first, EXPECTED_FORM_ID=2120733161884717
5. Meta app: create app, Webhooks product, callback https://YOUR-RENDER-DOMAIN/webhook, verify token, subscribe Raceday Page for leadgen
6. Test: Meta test button / test lead, SMS to Austin (903) 569-7757 first, confirm, then switch ALERT_TO to Grayson
7. Move Privacy/Terms to permanent host before any temporary links expire (already planned via Render routes)

## Files to upload to GitHub (full contents below)

### File: app.py
```
"""
Raceday Windows - Meta Lead Ads webhook -> Twilio SMS
=====================================================
Receives Meta leadgen webhook events in real time, fetches the lead
details from the Graph API, and texts Grayson at (903) 565-9207.

NO secrets are stored in this file. All credentials come from
environment variables (see .env.example). Never commit a real .env.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env   # then fill it in, do NOT commit it
    python app.py

Meta webhook verification (GET /webhook):
    Meta calls: /webhook?hub.mode=subscribe
                     &hub.verify_token=<VERIFY_TOKEN>
                     &hub.challenge=<challenge>
    We return the challenge only if the token matches.

Lead event (POST /webhook):
    Meta sends: {"entry": [{"changes": [{"field": "leadgen",
        "value": {"leadgen_id": "...", "form_id": "...",
                  "page_id": "..."}}]}]}
    We fetch the lead via Graph API, format a short SMS, and send it
    via Twilio. Each leadgen_id is processed once (seen set on disk).

SMS format (kept short, one segment if possible):
    New Raceday lead: <Name> <Phone> <Street> House: <size> Price OK: <Yes/No>
"""

import json
import os
import time
from pathlib import Path

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "")
PAGE_ACCESS_TOKEN = os.environ.get("META_PAGE_ACCESS_TOKEN", "")
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "")
ALERT_TO = os.environ.get("ALERT_TO_NUMBER", "+19035659207")
# Only alert for this form; blank = any form on the subscribed Page.
EXPECTED_FORM_ID = os.environ.get("EXPECTED_FORM_ID", "2120733161884717")
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v21.0")

SEEN_FILE = Path(__file__).parent / "seen_leadgen_ids.json"


def load_seen():
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except Exception:
        return set()


def save_seen(seen):
    try:
        SEEN_FILE.write_text(json.dumps(sorted(seen)))
    except Exception:
        pass


def fetch_lead(leadgen_id):
    """Fetch a lead's field values from the Graph API."""
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{leadgen_id}"
    r = requests.get(url, params={"access_token": PAGE_ACCESS_TOKEN}, timeout=15)
    r.raise_for_status()
    data = r.json()
    fields = {}
    for item in data.get("field_data", []):
        name = item.get("name", "")
        values = item.get("values") or []
        fields[name] = values[0] if values else ""
    return fields


def format_sms(fields):
    name = fields.get("full_name", "")
    phone = fields.get("phone_number", "")
    street = fields.get("street_address", "")
    # House size / price-check fields vary by form version; include
    # whatever is present, labelled generically.
    extra = []
    for key in ("house_size", "how_big_is_your_house", "price_check"):
        if fields.get(key):
            extra.append(str(fields[key]))
    house = " / ".join(extra) if extra else ""
    lines = [
        "Attention, we got you a lead!",
        "",
        "New Lead for Raceday Windows",
        f"Lead Name: {name}",
        f"Phone: {phone}",
        f"Address: {street}",
        f"House Size: {house}",
        "",
        "Please call as soon as possible - within 30 minutes if between 8am-8pm.",
        "",
        "Disclaimer: Only Sigmas Allowed to Call",
    ]
    # Multi-line format: ~2 SMS segments, still only ~$0.026 per lead.
    return "\n".join(lines)


def send_sms(body):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    r = requests.post(
        url,
        auth=(TWILIO_SID, TWILIO_TOKEN),
        data={"From": TWILIO_FROM, "To": ALERT_TO, "Body": body},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "raceday-lead-webhook"})


@app.route("/privacy-policy", methods=["GET"])
def privacy_policy():
    # Served as real HTML (text/html) so Twilio's checker accepts it.
    return Path(__file__).parent.joinpath("privacy-policy.html").read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/terms-and-conditions", methods=["GET"])
def terms_and_conditions():
    return Path(__file__).parent.joinpath("terms-and-conditions.html").read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification token mismatch", 403


@app.route("/webhook", methods=["POST"])
def receive():
    payload = request.get_json(silent=True) or {}
    seen = load_seen()
    results = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "leadgen":
                continue
            value = change.get("value", {})
            leadgen_id = value.get("leadgen_id")
            form_id = str(value.get("form_id", ""))
            if not leadgen_id or leadgen_id in seen:
                continue
            if EXPECTED_FORM_ID and form_id and form_id != EXPECTED_FORM_ID:
                continue
            seen.add(leadgen_id)
            try:
                fields = fetch_lead(leadgen_id)
                sms = format_sms(fields)
                sent = send_sms(sms)
                results.append({"leadgen_id": leadgen_id, "sms_sid": sent.get("sid")})
            except Exception as exc:  # noqa: BLE001 - log and keep 200 for Meta
                results.append({"leadgen_id": leadgen_id, "error": str(exc)})
    save_seen(seen)
    # Always return 200 quickly so Meta does not retry duplicates.
    return jsonify({"received": True, "results": results})


if __name__ == "__main__":
    missing = [
        name
        for name, val in [
            ("META_VERIFY_TOKEN", VERIFY_TOKEN),
            ("META_PAGE_ACCESS_TOKEN", PAGE_ACCESS_TOKEN),
            ("TWILIO_ACCOUNT_SID", TWILIO_SID),
            ("TWILIO_AUTH_TOKEN", TWILIO_TOKEN),
            ("TWILIO_FROM_NUMBER", TWILIO_FROM),
        ]
        if not val
    ]
    if missing:
        print(f"WARNING: missing env vars (server will still run, "
              f"but sends will fail): {', '.join(missing)}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
```

### File: requirements.txt
```
Flask>=3.0
requests>=2.31
```

### File: Dockerfile
```
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY app.py .
# Secrets come from the host's environment, never baked into the image.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
```

### File: privacy-policy.html
```
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy</title>
</head>
<body>
<h1>Privacy Policy</h1>
<p>Last updated: October 31, 2026</p>
<p>This Privacy Policy describes how <strong>Austin Trigueiro</strong> collects, uses, and protects personal information in connection with SMS lead alert notifications.</p>

<h2>What data is collected</h2>
<p>When a business owner requests lead alerts, we collect:</p>
<ul>
<li>The business owner's name, phone number, and email address, provided when they request the alerts.</li>
<li>Lead information submitted through a Facebook lead form for that business, including the lead's name, phone number, street address, and house size / price-check answer, which is used only to compose the alert message.</li>
</ul>
<p>We do not collect any other personal information through the SMS alert service.</p>

<h2>How data is used</h2>
<p>Collected data is used only to:</p>
<ul>
<li>Send SMS lead alerts to the business owner who requested them, so they can promptly contact a new lead;</li>
<li>Operate, maintain, and troubleshoot the alert service;</li>
<li>Comply with applicable law.</li>
</ul>

<h2>SMS opt-in data</h2>
<p>We do not sell or share your SMS opt-in data or personal information with third parties for marketing purposes.</p>
<p>SMS opt-in data is shared only with the messaging provider needed to deliver the alerts (Twilio) and with the business owner receiving the alert, and only for the purpose of delivering that alert. It is not shared with any other third party for marketing.</p>

<h2>Data retention</h2>
<p>Lead alert data is retained only as long as needed to deliver the alert and to troubleshoot delivery, and is then deleted or anonymized.</p>

<h2>Your choices</h2>
<p>A business owner receiving alerts may opt out at any time by replying STOP to an alert message. Reply HELP for help. Message frequency varies based on the number of leads received. Message and data rates may apply.</p>

<h2>Contact</h2>
<p>Questions about this policy: Austin Trigueiro, austin@theresalemint.com.</p>
</body>
</html>
```

### File: terms-and-conditions.html
```
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terms &amp; Conditions</title>
</head>
<body>
<h1>Terms &amp; Conditions</h1>
<p>Last updated: October 31, 2026</p>
<p>These Terms &amp; Conditions govern the SMS lead alert service provided by <strong>Austin Trigueiro</strong>.</p>

<h2>SMS Terms</h2>
<p>By requesting SMS lead alerts, you agree to receive SMS messages from Austin Trigueiro about new leads for your business.</p>
<p>Message frequency varies based on the number of leads received. Message and data rates may apply.</p>
<p>To opt out, reply STOP to any alert message. To get help, reply HELP, or contact Austin Trigueiro at austin@theresalemint.com.</p>
<p>Alerts are sent only to the business owner who requested them. Alerts are not marketing messages to consumers, and no message is sent to a lead through this service. The business owner is responsible for contacting leads in compliance with applicable law.</p>
<p>Carriers are not liable for delayed or undelivered messages.</p>

<h2>Acceptable use</h2>
<p>You agree not to use the alert service for unlawful purposes, and not to share alert messages containing a lead's personal information with any third party except as needed to contact that lead.</p>

<h2>Changes</h2>
<p>We may update these terms from time to time. Continued use of the service after a change constitutes acceptance of the updated terms.</p>

<h2>Contact</h2>
<p>Austin Trigueiro, austin@theresalemint.com.</p>
</body>
</html>
```
