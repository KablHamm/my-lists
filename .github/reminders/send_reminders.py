"""Send due My Lists reminders as Web Push notifications. Run every few minutes by .github/workflows/reminders.yml.

Reads reminders.json from the private data repo. Every item there holds a send time and a notification that the
app ALREADY ENCRYPTED for one device, so this script never sees any task text: it only signs the request (VAPID)
and forwards the encrypted bytes to the device's push service, then removes what it sent.
Logs only counts (this repo is public, so its Actions logs are too).

Environment:
  DATA_TOKEN         fine-grained token: Contents read and write on the data repo (Actions secret)
  VAPID_PRIVATE_KEY  base64url P-256 private key (Actions secret)
  DATA_REPO          owner/name of the private data repo
  REMINDERS_PATH     path of reminders.json in that repo (default reminders.json)
  VAPID_SUBJECT      contact URL for push services (default: the site URL)
  GH_TOKEN, WORKFLOW_REPO   optional: once a day, re-enable this workflow so GitHub does not pause it
  GITHUB_API, REM_NOW       test overrides
"""
import base64, json, os, sys, time, urllib.error, urllib.request
from urllib.parse import urlsplit
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

API = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
TOKEN = os.environ.get("DATA_TOKEN", "").strip()
REPO = os.environ.get("DATA_REPO", "").strip()
PATH = os.environ.get("REMINDERS_PATH", "reminders.json").strip()
SUBJECT = os.environ.get("VAPID_SUBJECT", "https://kablhamm.github.io/my-lists/")
NOW = int(os.environ.get("REM_NOW") or time.time() * 1000)
DAY = 24 * 3600 * 1000


def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def ub64u(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


if not TOKEN or not REPO or not os.environ.get("VAPID_PRIVATE_KEY"):
    sys.exit("Missing DATA_TOKEN, DATA_REPO or VAPID_PRIVATE_KEY (add them as Actions secrets).")
PRIV = ec.derive_private_key(int.from_bytes(ub64u(os.environ["VAPID_PRIVATE_KEY"].strip()), "big"), ec.SECP256R1())
PUB = b64u(PRIV.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))


def vapid(endpoint):
    u = urlsplit(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": SUBJECT}).encode())
    r, s = decode_dss_signature(PRIV.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"vapid t={head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}, k={PUB}"


def gh(method, path, body=None, token=TOKEN):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "my-lists-reminders",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def load():
    code, j = gh("GET", f"/repos/{REPO}/contents/{PATH}")
    if code == 404:
        return None, None
    if code != 200:
        sys.exit(f"Could not read reminders file (HTTP {code}). Check the DATA_TOKEN secret.")
    return json.loads(base64.b64decode(j["content"])), j["sha"]


def send(dev, item):
    req = urllib.request.Request(dev["endpoint"], method="POST", data=base64.b64decode(item["payload"]), headers={
        "Authorization": vapid(dev["endpoint"]), "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        "TTL": "86400", "Urgency": "high"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def keepalive():
    """GitHub pauses scheduled workflows in repos with no activity for 60 days; re-enabling once a day avoids that."""
    tok, repo = os.environ.get("GH_TOKEN"), os.environ.get("WORKFLOW_REPO")
    t = time.gmtime(NOW / 1000)
    if tok and repo and t.tm_hour == 3 and t.tm_min < 5:
        code, _ = gh("PUT", f"/repos/{repo}/actions/workflows/reminders.yml/enable", token=tok)
        print(f"keepalive: HTTP {code}")


def main():
    keepalive()
    data, sha = load()
    if not data:
        print("No reminders file yet.")
        return 0
    due = [i for i in data.get("items", []) if i.get("at", 0) <= NOW]
    if not due:
        print(f"Nothing due ({len(data.get('items', []))} scheduled).")
        return 0
    done, gone, sent, failed = set(), set(), 0, 0
    for it in due:
        dev = data.get("devices", {}).get(it.get("dev"))
        if NOW - it["at"] > DAY or not dev:          # too old, or the device was removed
            done.add(it["id"]); continue
        code = send(dev, it)
        if 200 <= code < 300:
            done.add(it["id"]); sent += 1
        elif code in (404, 410):                    # subscription expired / unsubscribed
            done.add(it["id"]); gone.add(it["dev"])
        else:
            failed += 1                             # keep it; try again next run
    for _ in range(5):
        data["items"] = [i for i in data.get("items", []) if i["id"] not in done and i.get("dev") not in gone]
        for g in gone:
            data.get("devices", {}).pop(g, None)
        if gone:
            data["fp"] = ""                         # make the app rebuild its list next time
        body = {"message": "Reminders sent", "content": base64.b64encode(json.dumps(data, indent=1).encode()).decode(), "sha": sha}
        code, _ = gh("PUT", f"/repos/{REPO}/contents/{PATH}", body)
        if code in (200, 201):
            break
        if code != 409:
            print(f"Could not update reminders file (HTTP {code}).")
            return 1
        data, sha = load()                          # someone saved in between: re-read and drop what we sent again
        if not data:
            break
    print(f"Sent {sent}, failed {failed}, dropped {len(done) - sent}, removed devices {len(gone)}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
