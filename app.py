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

# --- BEGIN drive-push addition: Google Drive deliverable upload alerts ---
# Adds the /drive/* routes from drive_watch.py (see README-DRIVE-PUSH.md).
# Wrapped defensively: if the add-on cannot load, the lead webhook must
# keep running exactly as before.
try:
    from drive_watch import register_drive_routes

    register_drive_routes(app)
except Exception as _drive_exc:  # noqa: BLE001
    print(f"WARNING: drive watch routes not registered: {_drive_exc}")
# --- END drive-push addition ---

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
