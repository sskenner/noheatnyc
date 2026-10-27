"""Post No Heat NYC updates to Bluesky.

Two kinds of post, both only during heat season (Oct 1 - May 31):

  weekly  The buildings with the most heat complaints in the past 7 days, from
          data/digest.json (written by build.py), each address linked to its page.
          Mondays and Thursdays (ROUNDUP_DAYS), never twice within 2 days.
  cold    A heat-law reminder when the National Weather Service forecasts a cold
          night for Central Park (low of COLD_F or below). At most once every
          COOLDOWN_DAYS, unless tonight is SNAP_F colder than the last alert.

Needs the BSKY_APP_PASSWORD environment variable (a Bluesky app password, not the
account password); without it, it prints the post and exits. Standard library only.

    python post_bsky.py weekly --dry-run   # show the post, send nothing
    python post_bsky.py weekly --force     # post now, skipping the day/season/repeat checks
    python post_bsky.py cold --dry-run     # show tonight's forecast and what would be posted
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from build import DIGEST, ROOT, nyc_today

PDS = "https://bsky.social/xrpc/"
LIMIT = 300  # Bluesky's post length cap (graphemes; len() over-counts only for combined emoji)

WEEKLY_HEADER = "🥶 Most NYC heat complaints this past week"
ROUNDUP_DAYS = {0: "Monday", 3: "Thursday"}  # date.weekday() numbers
COLD_HEADER = "❄️ Cold night ahead in NYC"
COLD_F = 40          # alert when tonight's forecast low is at or below this
COOLDOWN_DAYS = 7    # ...and no alert went out in this many days
SNAP_F = 8           # ...or tonight is this much colder than the last alert (and it was 2+ days ago)
NWS_FORECAST = "https://api.weather.gov/gridpoints/OKX/34,45/forecast"  # Central Park
NWS_HEADERS = {"User-Agent": "noheatnyc.com (411@noheatnyc.com)", "Accept": "application/geo+json"}


def in_season(day):
    return day.month in (10, 11, 12, 1, 2, 3, 4, 5)


def assemble(parts):
    """Join (text, target) parts into post text plus Bluesky facets; target is a URL or '#tag'."""
    text = "".join(t for t, _ in parts)
    facets, pos = [], 0
    for t, target in parts:
        size = len(t.encode("utf-8"))  # facet offsets are UTF-8 byte positions
        if target:
            feature = ({"$type": "app.bsky.richtext.facet#tag", "tag": target[1:]} if target.startswith("#")
                       else {"$type": "app.bsky.richtext.facet#link", "uri": target})
            facets.append({"index": {"byteStart": pos, "byteEnd": pos + size}, "features": [feature]})
        pos += size
    return text, facets


def site_link(site, path=""):
    url = site.rstrip("/") + "/" + path
    return url.split("//", 1)[1].rstrip("/"), url


def compose_weekly(digest, site):
    """Drops buildings from the bottom until the post fits."""
    for count in range(min(3, len(digest["top7"])), 0, -1):
        parts = [(WEEKLY_HEADER + ":\n\n", None)]
        for i, b in enumerate(digest["top7"][:count], 1):
            parts += [(f"{i}. ", None), (b["address"], b["url"]),
                      (f", {b['borough']}: {b['n7']:,}\n", None)]
        parts += [(f"\n{digest['last7']:,} heat & hot-water complaints citywide in 7 days. Check your building: ", None),
                  site_link(site), (" ", None), ("#NYC", "#NYC")]
        text, facets = assemble(parts)
        if len(text) <= LIMIT:
            break
    return text, facets


def compose_cold(low, site):
    return assemble([
        (f"{COLD_HEADER}: low around {low}°F tonight.\n\n", None),
        ("Landlords must keep apartments at least 62°F from 10 PM to 6 AM, and 68°F during the day "
         "when it's below 55°F outside.\n\nNo heat? Call 311. Know ", None),
        ("your rights", site.rstrip("/") + "/rights/"), (" and check your building: ", None),
        site_link(site), (" ", None), ("#NYC", "#NYC"),
    ])


def tonight_low():
    """Forecast low (°F) for the next night period at Central Park."""
    req = urllib.request.Request(NWS_FORECAST, headers=NWS_HEADERS)
    for attempt in range(3):  # the NWS API returns sporadic 500s
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                periods = json.load(resp)["properties"]["periods"]
            night = next(p for p in periods if not p["isDaytime"])
            return night["temperature"], night["name"]
        except urllib.error.HTTPError:
            if attempt == 2:
                raise


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


def recent_posts(token, did, header):
    """(date, text) of our recent posts that start with header, newest first."""
    feed = call("app.bsky.feed.getAuthorFeed", token, query={"actor": did, "limit": 30, "filter": "posts_no_replies"})
    return [(dt.date.fromisoformat(item["post"]["record"]["createdAt"][:10]), item["post"]["record"]["text"])
            for item in feed.get("feed", []) if item["post"]["record"].get("text", "").startswith(header)]


def cold_ok(history, today, low):
    if not history:
        return True
    last_day, last_text = history[0]
    days = (today - last_day).days
    match = re.search(r"low around (-?\d+)°F", last_text)
    colder = match and low <= int(match.group(1)) - SNAP_F
    return days >= COOLDOWN_DAYS or (days >= 2 and bool(colder))


def publish(cfg, token, did, text, facets):
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
    print(f"Posted: https://bsky.app/profile/{cfg['bluesky_handle']}/post/{result['uri'].rsplit('/', 1)[1]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kind", choices=["weekly", "cold"])
    parser.add_argument("--dry-run", action="store_true", help="print the post without sending it")
    parser.add_argument("--force", action="store_true", help="skip the schedule, threshold and repeat checks")
    args = parser.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    today = nyc_today()
    if args.kind == "weekly":
        digest = json.loads(DIGEST.read_text(encoding="utf-8"))
        text, facets = compose_weekly(digest, cfg["base_url"])
        header = WEEKLY_HEADER
        skip = (f"Roundups go out on {' and '.join(ROUNDUP_DAYS.values())}" if today.weekday() not in ROUNDUP_DAYS else
                "No complaints this week" if not digest["top7"] else None)
    else:
        try:
            low, period = tonight_low()
        except (urllib.error.URLError, StopIteration, KeyError) as e:
            print(f"Couldn't get the NWS forecast ({e}); not posting.")
            return
        print(f"NWS forecast for {period}: low {low}°F (alert at {COLD_F}°F or below)")
        text, facets = compose_cold(low, cfg["base_url"])
        header = COLD_HEADER
        skip = f"Tonight isn't cold enough ({low}°F)" if low > COLD_F else None
    if not in_season(today):
        skip = "Outside heat season"
    print(text, f"\n[{len(text)} chars, {len(facets)} links/tags]")

    password = os.environ.get("BSKY_APP_PASSWORD")
    if args.dry_run or not password:
        verdict = f"would skip: {skip}" if skip else "would post"
        print(f"Dry run ({verdict})." if args.dry_run else "BSKY_APP_PASSWORD not set; not posting.")
        return
    if skip and not args.force:
        print(skip + "; not posting.")
        return

    session = call("com.atproto.server.createSession",
                   body={"identifier": cfg["bluesky_handle"], "password": password})
    token, did = session["accessJwt"], session["did"]
    if not args.force:
        history = recent_posts(token, did, header)
        if args.kind == "weekly" and history and (today - history[0][0]).days < 2:
            print(f"Already posted a roundup on {history[0][0]}; not posting.")
            return
        if args.kind == "cold" and not cold_ok(history, today, low):
            print(f"Last cold-night alert was {history[0][0]}; not posting again yet.")
            return
    publish(cfg, token, did, text, facets)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        sys.exit(f"Bluesky API error {e.code}: {e.read().decode(errors='replace')[:500]}")
