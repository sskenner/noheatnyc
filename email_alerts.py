"""Email subscribers when a building they follow gets new heat complaints.

Building pages have a Buttondown sign-up form that tags each subscriber with the
building (ALERT_TAG + BBL). After each deploy, this compares the counts in the new
dist/counts.json with the ones the live site showed before the build, saved by
--save-previous, and for every followed building whose count went up sends one
email to that building's subscribers only. Standard library only.

Needs the BUTTONDOWN_API_KEY environment variable; without it, it prints the alerts
and exits.

    python email_alerts.py --save-previous   # before building: save the live site's counts
    python email_alerts.py --dry-run         # after building: show the alerts, send nothing
    python email_alerts.py                   # send them
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from build import ALERT_TAG, BOROUGHS, COUNTS, DIST, ROOT, fmt_date, plural

API = "https://api.buttondown.com/v1/"
PREVIOUS = ROOT / "data" / "previous_counts.json"


def save_previous(cfg):
    url = cfg["base_url"].rstrip("/") + "/counts.json"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            counts = json.load(resp)
    except (urllib.error.URLError, ValueError) as e:
        # without a baseline every building would look new, so the next run sends nothing
        PREVIOUS.unlink(missing_ok=True)
        print(f"Couldn't fetch {url} ({e}); no alerts will be sent after this build.")
        return
    PREVIOUS.parent.mkdir(exist_ok=True)
    PREVIOUS.write_text(json.dumps(counts, separators=(",", ":")), encoding="utf-8")
    print(f"Saved counts from the {counts['built']} build ({len(counts['n']):,} buildings).")


def new_complaints(previous, current):
    """{bbl: complaints added since the previous build}, for buildings whose count went up."""
    old = previous["n"] if previous["season"] == current["season"] else {}  # counts restart on Oct 1
    return {bbl: n - old.get(bbl, 0) for bbl, n in current["n"].items() if n > old.get(bbl, 0)}


def compose(b, added, cfg, season):
    base = cfg["base_url"].rstrip("/") + "/"
    subject = f"{plural(added, 'new heat complaint')} at {b['address']}"
    body = f"""Tenants at **{b['address']}**, {b['borough']} {b['zip']}, filed {plural(added, 'new heat or hot-water complaint')} with 311 since our last update. That makes {b['n']:,} since {fmt_date(season)}.

[See the building's full report]({base + b['path']})

Heat off in your apartment? [Here's what the law requires and what to do]({base}rights/).

You're getting this because you signed up for alerts about this building on {cfg['site_name']}. 311 complaints are reports from the public, not confirmed violations.
"""
    return subject, body


def call(path, key, body=None, method=None):
    url = path if path.startswith("https://") else API + path
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Authorization", "Token " + key)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def tag_ids(key):
    """{bbl: Buttondown tag ID} for every building someone has signed up for."""
    ids, url = {}, "tags?page_size=1000"
    while url:
        page = call(url, key)
        for tag in page["results"]:
            if tag["name"].startswith(ALERT_TAG):
                ids[tag["name"][len(ALERT_TAG):]] = tag["id"]
        url = page["next"]
    return ids


def send(key, tag_id, subject, body):
    audience = {"field": "subscriber.tags", "operator": "contains", "value": tag_id}
    draft = call("emails", key, {
        "subject": subject, "body": body, "status": "draft", "archival_mode": "disabled",
        "filters": {"predicate": "and", "groups": [], "filters": [audience]},
    })
    # an email without the filter would go to every subscriber, so check it stuck before sending
    if audience not in draft.get("filters", {}).get("filters", []):
        raise RuntimeError(f"draft {draft['id']} came back without the tag filter; left unsent")
    call("emails/" + draft["id"], key, {"status": "about_to_send"}, method="PATCH")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--save-previous", action="store_true", help="save the live site's counts, before building")
    parser.add_argument("--dry-run", action="store_true", help="print the alerts without sending them")
    args = parser.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    if args.save_previous:
        save_previous(cfg)
        return
    if not PREVIOUS.exists():
        print("No counts saved from before this build; not sending alerts.")
        return
    previous = json.loads(PREVIOUS.read_text(encoding="utf-8"))
    current = json.loads(COUNTS.read_text(encoding="utf-8"))
    added = new_complaints(previous, current)
    pages = {r[3][2:12]: r for r in json.loads((DIST / "search.json").read_text(encoding="utf-8"))}
    print(f"{len(added):,} buildings have new complaints since the {previous['built']} build.")

    key = os.environ.get("BUTTONDOWN_API_KEY")
    if key:
        tags = {bbl: tag for bbl, tag in tag_ids(key).items() if bbl in added}
        print(f"{len(tags):,} of them have alert subscribers.")
    else:
        tags = {bbl: None for bbl in list(added)[:3]}  # can't see who follows what without the API; show a sample
    sending = key and not args.dry_run
    if not sending:
        print("Dry run." if args.dry_run else "BUTTONDOWN_API_KEY not set; not sending.")

    failed = 0
    for bbl, tag in tags.items():
        address, zip_code, _, path, n = pages[bbl]
        b = {"address": address, "zip": zip_code, "borough": BOROUGHS[bbl[0]], "path": path, "n": n}
        subject, body = compose(b, added[bbl], cfg, current["season"])
        if not sending:
            print(f"\n--- {ALERT_TAG}{bbl}: {subject}\n{body}")
            continue
        try:
            send(key, tag, subject, body)
        except (urllib.error.HTTPError, RuntimeError) as e:
            failed += 1
            detail = e.read().decode(errors="replace")[:300] if isinstance(e, urllib.error.HTTPError) else e
            print(f"Failed for {ALERT_TAG}{bbl}: {detail}", file=sys.stderr)
        else:
            print(f"Sent: {subject}")
    if failed:
        sys.exit(f"{failed} of {len(tags)} alerts failed.")


if __name__ == "__main__":
    main()
