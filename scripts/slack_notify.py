"""Send the durable notification outbox as the configured Slack app.
Only GitHub Actions reads the webhook secret. Never print HTTP exception URLs.
"""
import base64
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

LEDGER_PATH = "notifications/deliveries.json"

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def api(method, path, data=None):
    url = "https://api.github.com/repos/" + os.environ["GITHUB_REPOSITORY"] + path
    payload = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=payload, method=method, headers={
        "Authorization": "Bearer " + os.environ["GH_TOKEN"],
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("GitHub API returned HTTP " + str(exc.code)) from None
    except urllib.error.URLError:
        raise RuntimeError("GitHub API could not be reached") from None

def read_json(path):
    result = api("GET", "/contents/" + path + "?ref=main")
    return json.loads(base64.b64decode(result["content"])), result["sha"]

def write_ledger(ledger, sha):
    result = api("PUT", "/contents/" + LEDGER_PATH, {
        "branch": "main",
        "sha": sha,
        "message": "Record Slack bot notification delivery [skip ci]",
        "content": base64.b64encode((json.dumps(ledger, ensure_ascii=False, indent=2) + "\n").encode()).decode(),
    })
    return result["content"]["sha"]

def send(webhook, text):
    req = urllib.request.Request(webhook, data=json.dumps({
        "text": text,
        "unfurl_links": False,
        "unfurl_media": False,
        "link_names": False,
    }).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status == 200 and response.read().decode().strip() == "ok", "response"
    except urllib.error.HTTPError as exc:
        return False, "http_" + str(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        # Delivery may have happened; do not resend automatically.
        return False, "ambiguous"

def main():
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    parsed = urllib.parse.urlparse(webhook)
    if parsed.scheme != "https" or parsed.hostname != "hooks.slack.com" or not parsed.path.startswith("/services/"):
        raise RuntimeError("SLACK_WEBHOOK_URL is missing or invalid")
    outbox, _ = read_json("notifications/outbox.json")
    ledger, sha = read_json(LEDGER_PATH)
    state, _ = read_json("automation-state.json")
    known = {item["key"]: item for item in state["known_competitions"]}
    entries = ledger.setdefault("messages", {})
    seen = set()
    failures = []
    for message in outbox.get("messages", []):
        ident = message["id"]
        text = message["text"]
        if not isinstance(ident, str) or ident in seen or not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise RuntimeError("Invalid or duplicate outbox message")
        seen.add(ident)
        previous = entries.get(ident, {}).get("status")
        if previous in ("sent", "skipped_expired"):
            continue
        if previous in ("sending", "ambiguous"):
            failures.append(ident + ": uncertain previous delivery; check Slack before retry")
            continue
        kind = message.get("kind")
        if kind == "test":
            if text != "INFORMUOSIU APIE NAUJUS KONKURSUS" or ident != "setup-test-2026-10-19":
                raise RuntimeError("Unexpected setup test")
        elif kind == "competition":
            key = message["competition_key"]
            if key not in known or known[key]["notification"] not in ("pending", "queued", "sent"):
                raise RuntimeError("Competition is not in the verified notification queue")
            deadline = dt.date.fromisoformat(message["deadline"])
            if (deadline - dt.datetime.now(ZoneInfo("Europe/Vilnius")).date()).days < 28:
                entries[ident] = {"status": "skipped_expired", "updated_at": now()}
                sha = write_ledger(ledger, sha)
                continue
        else:
            raise RuntimeError("Unsupported notification kind")
        entries[ident] = {"status": "sending", "updated_at": now()}
        sha = write_ledger(ledger, sha)  # Durable reservation before posting.
        ok, detail = send(webhook, text)
        if ok:
            entries[ident] = {"status": "sent", "updated_at": now()}
        else:
            status = "ambiguous" if detail == "ambiguous" else "failed"
            entries[ident] = {"status": status, "updated_at": now(), "detail": detail}
            failures.append(ident + ": " + status)
        sha = write_ledger(ledger, sha)
        print(("Sent: " if ok else "Not confirmed: ") + ident)
    if failures:
        raise RuntimeError("; ".join(failures))
    print("Notification outbox processed.")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Explicit messages above contain no secrets; unexpected exceptions are redacted.
        print(str(exc) if isinstance(exc, RuntimeError) else "Notification processing failed", file=sys.stderr)
        sys.exit(1)
