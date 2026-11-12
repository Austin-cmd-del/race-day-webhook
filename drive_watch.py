"""
drive_watch.py - Google Drive push notifications for deliverable uploads
=========================================================================
Add-on for the Raceday lead webhook app (app.py). It watches client
deliverables folders in Google Drive and texts Austin the moment a new
file lands, instead of waiting for the next tracker check:

    client uploads -> Drive push ping -> POST /drive/notify
                   -> POST to the SMMA Deliverable Reminders Zapier hook
                   -> Twilio text from the MONKEY number to Austin

Design rules:
- NO new dependencies: plain `requests` + Flask, like app.py.
- NO secrets in code. Everything comes from environment variables.
- Nothing in here may ever break the lead webhook: every Drive call is
  wrapped in try/except, and app.py registers these routes defensively.
- The 4-hourly SMMA deliverables tracker check remains the backup: it
  still flips tracker rows to Received, sends the client thank-you text
  and runs the reminder chase. This module only adds the instant alert.

Environment variables (see README-DRIVE-PUSH.md for the full list):
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
    DRIVE_WATCH_FOLDERS   JSON array: [{"folderId": "...", "business": "..."}]
    DRIVE_ADMIN_KEY       gates /drive/renew and /drive/oauth/start
    PUBLIC_BASE_URL       public https base of this service (custom domain)
    DELIVERABLE_HOOK_URL  Zapier sender hook (texts from the MONKEY number)
    AUSTIN_ALERT_NUMBER   default "+19035697757"

State files (next to this file, written atomically):
    drive_seen.json       {folderId: [fileId, ...]} already-alerted files
    drive_channels.json   {folderId: {channelId, resourceId, expiration}}
"""

import html
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Response, jsonify, redirect, request

BASE_DIR = Path(__file__).parent
SEEN_PATH = BASE_DIR / "drive_seen.json"
CHANNELS_PATH = BASE_DIR / "drive_channels.json"

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

# Set once by the OAuth bootstrap (see /drive/oauth/callback); lets the app
# use a fresh refresh token before it has been pasted into the environment.
_MEMORY_REFRESH_TOKEN = ""

# Access-token cache: {"token": str, "expires_at": epoch seconds}
_TOKEN_CACHE = {"token": "", "expires_at": 0.0}


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------

def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _refresh_token():
    return _MEMORY_REFRESH_TOKEN or _env("GOOGLE_REFRESH_TOKEN")


def google_configured():
    """True when all three Google OAuth pieces are available."""
    return bool(_env("GOOGLE_CLIENT_ID") and _env("GOOGLE_CLIENT_SECRET")
                and _refresh_token())


def _registry():
    """Parse DRIVE_WATCH_FOLDERS defensively -> [{folderId, business}]."""
    raw = _env("DRIVE_WATCH_FOLDERS")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    folders = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("folderId"):
                folders.append({
                    "folderId": str(item["folderId"]),
                    "business": str(item.get("business") or "A client"),
                })
    return folders


def _business_for(folder_id):
    for entry in _registry():
        if entry["folderId"] == folder_id:
            return entry["business"]
    return "A client"


def _admin_ok(provided):
    key = _env("DRIVE_ADMIN_KEY")
    return bool(key) and bool(provided) and provided == key


def _not_configured():
    return jsonify({"ok": False, "error": "google_not_configured"}), 503


# --------------------------------------------------------------------------
# Small JSON state files (atomic writes: temp file + rename)
# --------------------------------------------------------------------------

def _load_json(path):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json(path, data):
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, path)
    except Exception:
        pass  # state loss is recoverable (re-seed / re-renew); never crash


# --------------------------------------------------------------------------
# Google OAuth2: refresh-token exchange with an in-memory access-token cache
# --------------------------------------------------------------------------

def get_access_token():
    """Return a valid Google access token, refreshing when needed."""
    now = time.time()
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["expires_at"] > now + 60:
        return _TOKEN_CACHE["token"]
    r = requests.post(TOKEN_URL, data={
        "client_id": _env("GOOGLE_CLIENT_ID"),
        "client_secret": _env("GOOGLE_CLIENT_SECRET"),
        "refresh_token": _refresh_token(),
        "grant_type": "refresh_token",
    }, timeout=15)
    r.raise_for_status()
    payload = r.json()
    _TOKEN_CACHE["token"] = payload["access_token"]
    _TOKEN_CACHE["expires_at"] = now + int(payload.get("expires_in", 3600))
    return _TOKEN_CACHE["token"]


def _drive_headers():
    return {"Authorization": f"Bearer {get_access_token()}"}


# --------------------------------------------------------------------------
# Drive helpers
# --------------------------------------------------------------------------

def list_folder_files(folder_id):
    """Newest files in a folder (id, name, createdTime, mimeType)."""
    r = requests.get(
        f"{DRIVE_API}/files",
        headers=_drive_headers(),
        params={
            "q": f"'{folder_id}' in parents and trashed=false",
            "orderBy": "createdTime desc",
            "pageSize": 25,
            "fields": "files(id,name,createdTime,mimeType)",
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("files", [])


def _parse_iso(ts):
    """Parse a Drive createdTime ('...Z') to epoch seconds, or None."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _ms_to_iso(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def send_upload_alert(business, file_name):
    """POST one alert through the Zapier sender hook (MONKEY number)."""
    hook = _env("DELIVERABLE_HOOK_URL")
    if not hook:
        return False
    body = (f"📦 Received: {business} just uploaded '{file_name}'. "
            f"It's in their deliverables folder.")
    r = requests.post(hook, data={
        "to": _env("AUSTIN_ALERT_NUMBER", "+19035697757"),
        "body": body,
    }, timeout=15)
    r.raise_for_status()
    return True


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def register_drive_routes(app):
    """Attach the /drive/* routes to the Flask app. Called once by app.py."""

    @app.route("/drive/notify", methods=["POST"])
    def drive_notify():
        # Drive push receiver. Must answer fast and ALWAYS 200 once Google
        # is configured, or Drive retries and eventually drops the channel.
        if not google_configured():
            return _not_configured()
        channel_id = request.headers.get("X-Goog-Channel-ID", "")
        state = request.headers.get("X-Goog-Resource-State", "")
        folder_id = request.headers.get("X-Goog-Channel-Token", "")
        if state == "sync":
            return jsonify({"ok": True, "sync": True, "channel": channel_id})
        if not folder_id:
            return jsonify({"ok": True, "ignored": "no_channel_token"})
        try:
            files = list_folder_files(folder_id)
            seen_map = _load_json(SEEN_PATH)
            current_ids = [f.get("id") for f in files if f.get("id")]
            if folder_id not in seen_map:
                # First sight of this folder: seed silently so a fresh
                # deploy never re-alerts on everything already in there.
                seen_map[folder_id] = current_ids
                _save_json(SEEN_PATH, seen_map)
                return jsonify({"ok": True, "seeded": len(current_ids)})
            seen = set(seen_map.get(folder_id) or [])
            business = _business_for(folder_id)
            now = time.time()
            alerted = []
            for f in files:
                fid = f.get("id")
                if not fid or fid in seen:
                    continue
                created = _parse_iso(f.get("createdTime", ""))
                # Recency guard: after a state reset, files that were
                # seeded-then-deleted edge cases can't spam old alerts.
                if created is not None and created >= now - 3600:
                    try:
                        if send_upload_alert(business, f.get("name", "a file")):
                            alerted.append(fid)
                    except Exception:
                        pass  # an alert failure must not break the ping
                seen.add(fid)
            seen_map[folder_id] = sorted(seen)
            _save_json(SEEN_PATH, seen_map)
            return jsonify({"ok": True, "new": len(alerted), "alerted": alerted})
        except Exception as exc:  # noqa: BLE001 - still 200 for Drive
            return jsonify({"ok": False, "error": str(exc)})

    @app.route("/drive/renew", methods=["POST"])
    def drive_renew():
        # Admin-gated. (Re)creates one watch channel per registry folder.
        # Triggered weekly by cron (see README) because Drive channels
        # expire after at most 7 days.
        if not _admin_ok(request.headers.get("X-Admin-Key", "")):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if not google_configured():
            return _not_configured()
        base = _env("PUBLIC_BASE_URL").rstrip("/")
        if not base:
            return jsonify({"ok": False,
                            "error": "public_base_url_not_configured"}), 503
        channels = _load_json(CHANNELS_PATH)
        results = []
        for entry in _registry():
            folder_id = entry["folderId"]
            result = {"folderId": folder_id, "business": entry["business"]}
            try:
                old = channels.get(folder_id) or {}
                if old.get("channelId") and old.get("resourceId"):
                    try:  # best effort: stop the previous channel
                        requests.post(
                            f"{DRIVE_API}/channels/stop",
                            headers=_drive_headers(),
                            json={"id": old["channelId"],
                                  "resourceId": old["resourceId"]},
                            timeout=15,
                        )
                    except Exception:
                        pass
                expiration_ms = int((time.time() + 7 * 24 * 3600) * 1000)
                channel_id = str(uuid.uuid4())
                r = requests.post(
                    f"{DRIVE_API}/files/{folder_id}/watch",
                    headers=_drive_headers(),
                    json={
                        "id": channel_id,
                        "type": "web_hook",
                        "address": f"{base}/drive/notify",
                        "token": folder_id,
                        "expiration": str(expiration_ms),
                    },
                    timeout=20,
                )
                r.raise_for_status()
                data = r.json()
                actual_ms = int(data.get("expiration") or expiration_ms)
                channels[folder_id] = {
                    "channelId": data.get("id", channel_id),
                    "resourceId": data.get("resourceId", ""),
                    "expiration": actual_ms,
                }
                result.update({"ok": True,
                               "channelId": channels[folder_id]["channelId"],
                               "expiration": _ms_to_iso(actual_ms)})
            except Exception as exc:  # noqa: BLE001 - report per folder
                result.update({"ok": False, "error": str(exc)})
            results.append(result)
        _save_json(CHANNELS_PATH, channels)
        return jsonify({"ok": True, "results": results})

    @app.route("/drive/health", methods=["GET"])
    def drive_health():
        # Always answers (even when unconfigured) so the weekly renewal
        # cron can tell "not set up yet" apart from "set up and lapsed".
        channels = _load_json(CHANNELS_PATH)
        listing = []
        for entry in _registry():
            rec = channels.get(entry["folderId"]) or {}
            listing.append({
                "folderId": entry["folderId"],
                "business": entry["business"],
                "channelId": rec.get("channelId", ""),
                "expiration": _ms_to_iso(rec["expiration"])
                if rec.get("expiration") else "",
            })
        return jsonify({
            "ok": True,
            "service": "drive-watch",
            "configured": google_configured(),
            "folders": len(_registry()),
            "channels": listing,
        })

    @app.route("/drive/oauth/start", methods=["GET"])
    def drive_oauth_start():
        # One-time bootstrap, step 1: send the admin to Google's consent
        # screen. Gated by the admin key so the link is not public.
        if not _admin_ok(request.args.get("key", "")):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        base = _env("PUBLIC_BASE_URL").rstrip("/")
        if not _env("GOOGLE_CLIENT_ID") or not base:
            return _not_configured()
        params = {
            "client_id": _env("GOOGLE_CLIENT_ID"),
            "redirect_uri": f"{base}/drive/oauth/callback",
            "response_type": "code",
            "scope": DRIVE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }
        qs = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}"
                      for k, v in params.items())
        return redirect(f"{AUTH_URL}?{qs}", code=302)

    @app.route("/drive/oauth/callback", methods=["GET"])
    def drive_oauth_callback():
        # One-time bootstrap, step 2: Google redirects here with a code
        # (no admin header is possible on a browser redirect; the code is
        # single-use and short-lived). We exchange it and show the
        # refresh token ONCE so it can be pasted into the environment.
        global _MEMORY_REFRESH_TOKEN
        err = request.args.get("error", "")
        code = request.args.get("code", "")
        if err or not code:
            return Response(
                f"Google OAuth failed: {html.escape(err or 'no code returned')}",
                status=400, mimetype="text/plain")
        try:
            base = _env("PUBLIC_BASE_URL").rstrip("/")
            r = requests.post(TOKEN_URL, data={
                "code": code,
                "client_id": _env("GOOGLE_CLIENT_ID"),
                "client_secret": _env("GOOGLE_CLIENT_SECRET"),
                "redirect_uri": f"{base}/drive/oauth/callback",
                "grant_type": "authorization_code",
            }, timeout=15)
            r.raise_for_status()
            token = r.json().get("refresh_token", "")
            if not token:
                raise RuntimeError(
                    "Google returned no refresh_token; retry via "
                    "/drive/oauth/start (prompt=consent is required).")
            _MEMORY_REFRESH_TOKEN = token
            _TOKEN_CACHE["token"] = ""  # force a fresh access token next call
            page = f"""<!DOCTYPE html>
<html><head><title>Drive watch - token ready</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 40px auto;">
<h2>Google Drive connected</h2>
<p>Copy this refresh token <b>now</b> (it is shown only once) and paste it
into the Render environment variable <code>GOOGLE_REFRESH_TOKEN</code>,
then restart/redeploy the service:</p>
<pre style="background:#f4f4f4; padding:12px; word-break:break-all;">{html.escape(token)}</pre>
<p>The app can already use the token in memory until the next restart,
but it only survives restarts once it is saved in the environment.</p>
</body></html>"""
            resp = Response(page, mimetype="text/html")
            resp.headers["Cache-Control"] = "no-store"
            return resp
        except Exception as exc:  # noqa: BLE001 - show the failure plainly
            return Response(f"Google OAuth exchange failed: {html.escape(str(exc))}",
                            status=502, mimetype="text/plain")
