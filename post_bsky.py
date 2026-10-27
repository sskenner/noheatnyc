"""Post the weekly No Heat NYC roundup to Bluesky.

Reads data/digest.json (written by build.py) and posts the buildings with the most
heat complaints in the past 7 days, each address linked to its building page, with
the site's share card attached. Standard library only.

By default it posts only on Mondays during heat season (Oct 1 - May 31), and never
twice within 6 days. Needs the BSKY_APP_PASSWORD environment variable (a Bluesky
app password, not the account password); without it, it prints the post and exits.

    python post_bsky.py --dry-run     # show the post, send nothing
    python post_bsky.py --force       # post now, ignoring the Monday/season/repeat checks
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from build import DIGEST, ROOT, nyc_today

PDS = "https://bsky.social/xrpc/"
HEADER = "🥶 Most NYC heat complaints this past week"
LIMIT = 300  # Bluesky's post length cap (graphemes; len() over-counts only for combined emoji)


def compose(digest, site):
    """Return (text, facets). Drops buildings from the bottom until the post fits."""
    for count in range(min(3, len(digest["top7"])), 0, -1):
        rows = digest["top7"][:count]
        parts = [(HEADER + ":\n\n", None)]
        for i, b in enumerate(rows, 1):
            parts += [(f"{i}. ", None), (b["address"], b["url"]),
                      (f", {b['borough']}: {b['n7']:,}\n", None)]
        parts += [(f"\n{digest['last7']:,} heat & hot-water complaints citywide in 7 days. Check your building: ", None),
                  (site.split("//", 1)[1].rstrip("/"), site), (" ", None), ("#NYC", "#NYC")]
        text = "".join(t for t, _ in parts)
        if len(text) <= LIMIT:
            break
    facets, pos = [], 0
    for t, target in parts:
        size = len(t.encode("utf-8"))  # facet offsets are UTF-8 byte positions
        if target:
            feature = ({"$type": "app.bsky.richtext.facet#tag", "tag": target[1:]} if target.startswith("#")
                       else {"$type": "app.bsky.richtext.facet#link", "uri": target})
            facets.append({"index": {"byteStart": pos, "byteEnd": pos + size}, "features": [feature]})
        pos += size
    return text, facets


def call(method, token=None, body=None, raw=None, content_type="application/json", query=None):
    url = PDS + method + ("?" + urllib.parse.urlencode(query) if query else "")
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data)
    if data is not None:
        req.add_header("Content-Type", content_type)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def posted_recently(token, did, today):
    feed = call("app.bsky.feed.getAuthorFeed", token, query={"actor": did, "limit": 20, "filter": "posts_no_replies"})
    for item in feed.get("feed", []):
        record = item["post"]["record"]
        if record.get("text", "").startswith(HEADER):
            when = dt.date.fromisoformat(record["createdAt"][:10])
            if (today - when).days < 6:
                return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the post without sending it")
    parser.add_argument("--force", action="store_true", help="skip the Monday, season and repeat checks")
    args = parser.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    digest = json.loads(DIGEST.read_text(encoding="utf-8"))
    today = nyc_today()
    text, facets = compose(digest, cfg["base_url"])
    print(text, f"\n[{len(text)} chars, {len(facets)} links/tags]")

    password = os.environ.get("BSKY_APP_PASSWORD")
    if args.dry_run or not password:
        print("Dry run." if args.dry_run else "BSKY_APP_PASSWORD not set; not posting.")
        return
    if not args.force:
        if today.weekday() != 0:
            print("Not Monday in New York; not posting.")
            return
        if today.month not in (10, 11, 12, 1, 2, 3, 4, 5):
            print("Outside heat season; not posting.")
            return
    if not digest["top7"]:
        print("No complaints this week; not posting.")
        return

    session = call("com.atproto.server.createSession",
                   body={"identifier": cfg["bluesky_handle"], "password": password})
    token, did = session["accessJwt"], session["did"]
    if not args.force and posted_recently(token, did, today):
        print("Already posted this week; not posting.")
        return

    thumb = call("com.atproto.repo.uploadBlob", token,
                 raw=(ROOT / "static" / "share.png").read_bytes(), content_type="image/png")["blob"]
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "facets": facets,
        "langs": ["en"],
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "embed": {"$type": "app.bsky.embed.external", "external": {
            "uri": cfg["base_url"].rstrip("/") + "/",
            "title": cfg["site_name"],
            "description": cfg["tagline"],
            "thumb": thumb,
        }},
    }
    result = call("com.atproto.repo.createRecord", token,
                  body={"repo": did, "collection": "app.bsky.feed.post", "record": record})
    post_id = result["uri"].rsplit("/", 1)[1]
    print(f"Posted: https://bsky.app/profile/{cfg['bluesky_handle']}/post/{post_id}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        sys.exit(f"Bluesky API error {e.code}: {e.read().decode(errors='replace')[:500]}")
