"""Poster opslag fra køen til Instagram, når deres tidspunkt er nået.

Køres hver time af GitHub Actions (.github/workflows/post.yml).
Læser config.json og queue/*/opslag.json. Skriver status tilbage i opslag.json.

Status-værdier i opslag.json:
  klar     – venter og bliver postet, når tidspunktet er nået
  stop     – bliver ikke postet (sæt den selv for at stoppe et opslag)
  postet   – er lagt på Instagram
  udløbet  – blev ikke postet i tide (fx under pause) og springes over
  fejl     – fejlede 3 gange; se "note"
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
API = f"{CONFIG.get('api_host', 'https://graph.facebook.com').rstrip('/')}/{CONFIG.get('api_version', 'v25.0')}"
TOKEN = os.environ.get("IG_TOKEN", "").strip()


def call(method, path, params=None):
    params = dict(params or {})
    params["access_token"] = TOKEN
    data = urllib.parse.urlencode(params).encode()
    if method == "GET":
        req = urllib.request.Request(f"{API}/{path}?{data.decode()}")
    else:
        req = urllib.request.Request(f"{API}/{path}", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Instagram svarede {e.code}: {body[:400]}") from None


def wait_ready(container_id):
    for _ in range(10):
        st = call("GET", container_id, {"fields": "status_code"}).get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Billedet kunne ikke behandles af Instagram (status {st})")
        time.sleep(30)
    raise RuntimeError("Instagram blev ikke færdig med at behandle billedet i tide")


def check_url(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as r:
            if r.status != 200:
                raise RuntimeError(f"Billedet svarer {r.status}: {url}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Billedet er ikke offentligt tilgængeligt endnu ({e.code}): {url}") from None


def publish(user_id, folder, post):
    base = CONFIG["pages_base_url"].rstrip("/")
    urls = [f"{base}/queue/{folder.name}/{img}" for img in post["billeder"]]
    for u in urls:
        check_url(u)
    caption = post["billedtekst"]
    if len(urls) == 1:
        c = call("POST", f"{user_id}/media", {"image_url": urls[0], "caption": caption})["id"]
    else:
        children = []
        for u in urls:
            cid = call("POST", f"{user_id}/media", {"image_url": u, "is_carousel_item": "true"})["id"]
            children.append(cid)
        for cid in children:
            wait_ready(cid)
        c = call("POST", f"{user_id}/media", {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption})["id"]
    wait_ready(c)
    return call("POST", f"{user_id}/media_publish", {"creation_id": c})["id"]


def find_existing(user_id, caption, since):
    """Finder et opslag på profilen med samme billedtekst, postet efter 'since'.

    Instagram kan svare med en fejl, selvom opslaget faktisk er kommet ud.
    Derfor tjekker vi profilen, før vi prøver igen, så der ikke opstår dubletter.
    """
    try:
        items = call("GET", f"{user_id}/media", {"fields": "id,caption,timestamp", "limit": "15"}).get("data", [])
    except Exception:  # noqa: BLE001
        return None
    key = (caption or "").strip()[:120]
    for it in items:
        ts = it.get("timestamp", "").replace("+0000", "+00:00")
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if t >= since and (it.get("caption") or "").strip()[:120] == key:
            return it["id"]
    return None


def is_block(err):
    s = str(err)
    return '"is_transient":false' in s or "2207051" in s or "Action is blocked" in s or '"code":4,' in s


def pause_all(reason):
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["pause"] = True
    cfg["pause_grund"] = reason
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    if CONFIG.get("pause"):
        print("Posting er sat på pause i config.json. Intet postes.")
        return 0
    if not TOKEN:
        print("Mangler hemmeligheden IG_TOKEN. Se OPSÆTNING.md.")
        return 1
    if not os.environ.get("IG_USER_ID", "").strip():
        print("Mangler hemmeligheden IG_USER_ID. Se OPSÆTNING.md.")
        return 1
    if not CONFIG.get("pages_base_url"):
        print("Mangler pages_base_url i config.json.")
        return 1

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        try:
            info = call("GET", os.environ["IG_USER_ID"].strip(), {"fields": "username"})
            print(f"Forbindelsen virker: forbundet til @{info.get('username')}.")
        except Exception as e:  # noqa: BLE001
            print(f"Forbindelsen virker ikke: {e}")
            return 1

    now = datetime.now(timezone.utc)
    max_late = float(CONFIG.get("max_forsinkelse_timer", 12)) * 3600
    user_id = os.environ.get("IG_USER_ID", "").strip()
    failed = False

    due = []
    for folder in (p for p in (ROOT / "queue").iterdir() if p.is_dir()):
        f = folder / "opslag.json"
        if not f.exists():
            continue
        post = json.loads(f.read_text(encoding="utf-8"))
        if post.get("status") != "klar":
            continue
        when = datetime.fromisoformat(post["tidspunkt"])
        if when <= now:
            due.append((when, folder, f, post))

    # Ældste tidspunkt først, så rækkefølgen på profilen følger planen
    for when, folder, f, post in sorted(due, key=lambda x: x[0]):
        if (now - when).total_seconds() > max_late:
            post["status"] = "udløbet"
            post["note"] = f"Ikke postet inden for {CONFIG.get('max_forsinkelse_timer', 12)} timer efter planlagt tidspunkt."
            print(f"{folder.name}: udløbet")
        else:
            since = when - timedelta(hours=1)
            existing = find_existing(user_id, post["billedtekst"], since)
            if existing:
                post["status"] = "postet"
                post["instagram_id"] = existing
                post["postet"] = post.get("postet") or datetime.now(timezone.utc).isoformat(timespec="seconds")
                post["note"] = "Fundet på profilen; blev ikke postet igen."
                print(f"{folder.name}: lå allerede på profilen ({existing})")
            else:
                try:
                    media_id = publish(user_id, folder, post)
                    post["status"] = "postet"
                    post["instagram_id"] = media_id
                    post["postet"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    post["note"] = ""
                    print(f"{folder.name}: postet ({media_id})")
                except Exception as e:  # noqa: BLE001
                    time.sleep(20)
                    existing = find_existing(user_id, post["billedtekst"], since)
                    if existing:
                        post["status"] = "postet"
                        post["instagram_id"] = existing
                        post["postet"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        post["note"] = f"Instagram meldte fejl, men opslaget kom ud: {str(e)[:200]}"
                        print(f"{folder.name}: postet trods fejlbesked ({existing}). Instagram meldte begrænsning, men opslaget kom ud, så posting fortsætter.")
                    else:
                        post["forsøg"] = post.get("forsøg", 0) + 1
                        post["note"] = str(e)
                        if post["forsøg"] >= 3:
                            post["status"] = "fejl"
                        print(f"{folder.name}: fejl ({e})")
                        failed = True
                    # Kun pause, hvis opslaget faktisk IKKE kom ud. Kom det ud trods fejlbesked, fortsætter vi.
                    if is_block(e) and not existing:
                        pause_all(f"Instagram begrænsede kontoen ({datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC). Sæt pause til false, når det er gået et par dage.")
                        print("Instagram begrænser kontoen. Al posting er sat på pause i config.json.")
                        f.write_text(json.dumps(post, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                        return 1
        f.write_text(json.dumps(post, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
