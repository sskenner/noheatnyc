#!/usr/bin/env python3
"""Build the No Heat NYC static site from NYC Open Data.

Pulls heat/hot-water 311 complaints, open HPD violations, rat inspections and
bedbug filings, aggregates them per building (BBL), and writes a static site to
dist/: a home page with a live "is heat required right now?" answer, one page per
building, one page per ZIP code, a search index and a sitemap.

Usage:
    python build.py              # fetch fresh data and build
    python build.py --offline    # rebuild from the last saved snapshot

Set SOCRATA_APP_TOKEN to raise the API rate limit (free at data.cityofnewyork.us).
Only the Python standard library is used.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
SNAPSHOT = ROOT / "data" / "snapshot.json"
API = "https://data.cityofnewyork.us/resource/{}.json"
PAGE_SIZE = 50000

DS_311 = "erm2-nwe9"
DS_HPD_VIOLATIONS = "wvxf-dwi5"
DS_RATS = "p937-wjvj"
DS_BEDBUGS = "wz6d-d3jb"

BOROUGHS = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn", "4": "Queens", "5": "Staten Island"}
HEAT = "complaint_type='HEAT/HOT WATER'"


# ---------------------------------------------------------------- fetching

def soql(dataset, params):
    """Run a SoQL query, following pages until the result is exhausted."""
    headers = {"User-Agent": "noheatnyc-builder"}
    if os.environ.get("SOCRATA_APP_TOKEN"):
        headers["X-App-Token"] = os.environ["SOCRATA_APP_TOKEN"]
    rows, offset = [], 0
    while True:
        query = dict(params, **{"$limit": PAGE_SIZE, "$offset": offset})
        url = API.format(dataset) + "?" + urllib.parse.urlencode(query)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300) as resp:
                    batch = json.load(resp)
                break
            except Exception as exc:  # network hiccups and 5xx are common on big group-bys
                if attempt == 3:
                    raise
                print(f"  retry {dataset} ({exc})", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def seasons(today):
    """Current heat season start, previous season bounds, and last year's same date."""
    year = today.year if today.month >= 10 else today.year - 1
    cur_start = dt.date(year, 10, 1)
    prev_start = dt.date(year - 1, 10, 1)
    prev_end = dt.date(year, 6, 1)
    try:
        prev_same_day = today.replace(year=today.year - 1)
    except ValueError:  # Feb 29
        prev_same_day = today.replace(year=today.year - 1, day=28)
    return cur_start, prev_start, prev_end, prev_same_day


def fetch(today):
    cur_start, prev_start, prev_end, prev_same_day = seasons(today)
    week_ago = today - dt.timedelta(days=7)
    year_ago = today - dt.timedelta(days=365)
    two_years_ago = today - dt.timedelta(days=730)

    print("Fetching heat complaints, this season...")
    current = soql(DS_311, {
        "$select": "bbl, count(*) as n,"
                   f" sum(case(created_date >= '{week_ago}', 1, true, 0)) as n7,"
                   " sum(case(descriptor = 'ENTIRE BUILDING', 1, true, 0)) as whole,"
                   " sum(case(descriptor_2 like '%HEAT%', 1, true, 0)) as noheat,"
                   " sum(case(descriptor_2 like '%HOT WATER%', 1, true, 0)) as nohw,"
                   " max(created_date) as last, max(incident_address) as addr, max(incident_zip) as zip",
        "$where": f"{HEAT} AND created_date >= '{cur_start}' AND bbl IS NOT NULL",
        "$group": "bbl", "$order": "bbl",
    })
    print("Fetching heat complaints, last season...")
    previous = soql(DS_311, {
        "$select": "bbl, count(*) as n,"
                   f" sum(case(created_date < '{prev_same_day}', 1, true, 0)) as to_date,"
                   " max(incident_address) as addr, max(incident_zip) as zip",
        "$where": f"{HEAT} AND created_date >= '{prev_start}' AND created_date < '{prev_end}' AND bbl IS NOT NULL",
        "$group": "bbl", "$order": "bbl",
    })
    print("Fetching citywide daily totals...")
    daily = soql(DS_311, {
        "$select": "date_trunc_ymd(created_date) as day, count(*) as n",
        "$where": f"{HEAT} AND created_date >= '{cur_start}'",
        "$group": "day", "$order": "day",
    })
    prev_to_date = soql(DS_311, {
        "$select": "count(*) as n",
        "$where": f"{HEAT} AND created_date >= '{prev_start}' AND created_date < '{prev_same_day}'",
    })
    print("Fetching open HPD violations...")
    violations = soql(DS_HPD_VIOLATIONS, {
        "$select": "bbl, count(*) as open,"
                   " sum(case(class = 'C', 1, true, 0)) as c,"
                   " sum(case(class = 'B', 1, true, 0)) as b",
        "$where": "violationstatus = 'Open' AND bbl IS NOT NULL",
        "$group": "bbl", "$order": "bbl",
    })
    print("Fetching rat inspections...")
    rats = soql(DS_RATS, {
        "$select": "bbl, count(*) as inspections,"
                   " sum(case(result = 'Rat Activity', 1, true, 0)) as activity,"
                   " max(inspection_date) as last",
        "$where": f"inspection_date >= '{year_ago}' AND bbl IS NOT NULL",
        "$group": "bbl", "$order": "bbl",
    })
    print("Fetching bedbug filings...")
    bedbugs = soql(DS_BEDBUGS, {
        "$select": "bbl, max(infested_dwelling_unit_count) as infested,"
                   " max(of_dwelling_units) as units, max(filing_date) as last",
        "$where": f"filing_date >= '{two_years_ago}' AND bbl IS NOT NULL",
        "$group": "bbl", "$order": "bbl",
    })
    return {
        "built": today.isoformat(),
        "current": current, "previous": previous, "daily": daily,
        "prev_to_date": int(prev_to_date[0]["n"]) if prev_to_date else 0,
        "violations": violations, "rats": rats, "bedbugs": bedbugs,
    }


# ---------------------------------------------------------------- shaping

SUFFIXES = {"ST": "Street", "AVE": "Avenue", "AV": "Avenue", "RD": "Road", "PL": "Place", "BLVD": "Boulevard",
            "PKWY": "Parkway"}
DIRECTIONS = {"E": "East", "W": "West", "N": "North", "S": "South"}
KEEP_CASE = {"ST": "St"}  # "ST NICHOLAS AVENUE" is Saint, not Street


def clean_address(raw):
    words = re.sub(r"\s+", " ", (raw or "").strip()).split(" ")
    out = []
    for i, w in enumerate(words):
        last = i == len(words) - 1
        if last and w in SUFFIXES:
            out.append(SUFFIXES[w])
        elif not last and w in DIRECTIONS:
            out.append(DIRECTIONS[w])
        elif w in KEEP_CASE:
            out.append(KEEP_CASE[w])
        else:
            out.append(w if w[:1].isdigit() else w.capitalize())
    return " ".join(out)


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def valid_bbl(bbl):
    return bool(bbl) and len(bbl) == 10 and bbl[0] in BOROUGHS and bbl.isdigit()


def shape(raw, cfg):
    by_bbl = lambda rows: {r["bbl"]: r for r in rows if valid_bbl(r.get("bbl"))}
    cur, prev = by_bbl(raw["current"]), by_bbl(raw["previous"])
    viol, rats, bugs = by_bbl(raw["violations"]), by_bbl(raw["rats"]), by_bbl(raw["bedbugs"])

    keep = set(cur) | {b for b, r in prev.items() if to_int(r["n"]) >= cfg["min_prev_season_complaints"]}
    buildings = []
    for bbl in keep:
        c, p = cur.get(bbl, {}), prev.get(bbl, {})
        address = clean_address(c.get("addr") or p.get("addr"))
        if not address:
            continue
        v, r, bb = viol.get(bbl, {}), rats.get(bbl, {}), bugs.get(bbl, {})
        buildings.append({
            "bbl": bbl,
            "address": address,
            "zip": (c.get("zip") or p.get("zip") or "")[:5],
            "borough": BOROUGHS[bbl[0]],
            "path": f"b/{bbl}-{slugify(address)}/",
            "n": to_int(c.get("n")), "n7": to_int(c.get("n7")),
            "whole": to_int(c.get("whole")), "noheat": to_int(c.get("noheat")), "nohw": to_int(c.get("nohw")),
            "last": (c.get("last") or "")[:10],
            "prev": to_int(p.get("n")), "prev_to_date": to_int(p.get("to_date")),
            "open": to_int(v.get("open")), "class_c": to_int(v.get("c")), "class_b": to_int(v.get("b")),
            "rat_inspections": to_int(r.get("inspections")), "rat_activity": to_int(r.get("activity")),
            "bedbug_infested": to_int(bb.get("infested")), "units": to_int(bb.get("units")),
            "bedbug_filed": (bb.get("last") or "")[:10],
        })

    # citywide percentile and ZIP rank, this season
    ranked = sorted((b for b in buildings if b["n"]), key=lambda b: -b["n"])
    total = len(ranked)
    for i, b in enumerate(ranked):
        b["rank"] = i + 1
        b["percentile"] = min(99, int(100 * (total - i - 1) / max(total - 1, 1)))
    zips = {}
    for b in buildings:
        if b["zip"]:
            zips.setdefault(b["zip"], []).append(b)
    for members in zips.values():
        members.sort(key=lambda b: (-b["n"], -b["prev"]))
        for i, b in enumerate(members):
            b["zip_rank"] = i + 1
    return buildings, zips


# ---------------------------------------------------------------- html helpers

esc = html.escape


def fmt_date(iso):
    if not iso:
        return "—"
    d = dt.date.fromisoformat(iso[:10])
    return f"{d:%b} {d.day}, {d.year}"


def plural(n, word, many=None):
    return f"{n:,} {word if n == 1 else (many or word + 's')}"


class Site:
    def __init__(self, cfg, today):
        self.cfg, self.today = cfg, today
        self.base = cfg["base_path"].rstrip("/") + "/"
        self.urls = []

    def url(self, path=""):
        return self.base + path

    def ad(self, slot_key):
        client, slot = self.cfg.get("adsense_client"), self.cfg.get(slot_key)
        if not (client and slot):
            return ""
        return (f'<div class="ad"><ins class="adsbygoogle" style="display:block" data-ad-client="{esc(client)}"'
                f' data-ad-slot="{esc(slot)}" data-ad-format="auto" data-full-width-responsive="true"></ins>'
                '<script>(adsbygoogle = window.adsbygoogle || []).push({});</script></div>')

    def shop(self, heading="Stay warm while you wait"):
        tag = self.cfg.get("amazon_tag")
        items = []
        for p in self.cfg.get("amazon_products", []):
            q = {"k": p["query"]}
            if tag:
                q["tag"] = tag
            href = "https://www.amazon.com/s?" + urllib.parse.urlencode(q)
            items.append(f'<li><a href="{esc(href)}" rel="sponsored nofollow noopener" target="_blank">'
                         f'{esc(p["label"])}</a><span>{esc(p["note"])}</span></li>')
        for r in self.cfg.get("referrals", []):
            if r.get("url"):
                items.append(f'<li><a href="{esc(r["url"])}" rel="sponsored nofollow noopener" target="_blank">'
                             f'{esc(r["label"])}</a><span>{esc(r["note"])}</span></li>')
        if not items:
            return ""
        # Amazon's Operating Agreement requires this exact sentence wherever Associates links appear
        amazon = " As an Amazon Associate I earn from qualifying purchases." if tag else ""
        return (f'<aside class="card shop"><h2>{esc(heading)}</h2><ul>{"".join(items)}</ul>'
                '<p class="fine">We may earn a commission from these links. It doesn\'t change what you pay.'
                f'{amazon}</p></aside>')

    def page(self, path, title, description, body, extra_head=""):
        cfg = self.cfg
        canonical = cfg["base_url"].rstrip("/") + "/" + path
        head_scripts = ""
        if cfg.get("adsense_client"):
            head_scripts += (f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js'
                             f'?client={esc(cfg["adsense_client"])}" crossorigin="anonymous"></script>')
        if cfg.get("ga4_id"):
            gid = esc(cfg["ga4_id"])
            head_scripts += (f'<script async src="https://www.googletagmanager.com/gtag/js?id={gid}"></script>'
                             "<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments)}"
                             f"gtag('js',new Date());gtag('config','{gid}');</script>")
        social = "".join(f' · <a href="{esc(s["url"])}" rel="me">{esc(s["label"])}</a>'
                         for s in cfg.get("social", []))
        doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{esc(canonical)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:image" content="{esc(cfg['base_url'].rstrip('/'))}/static/share.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="{self.url('favicon.ico')}" sizes="any">
<link rel="icon" href="{self.url('static/icon.svg')}" type="image/svg+xml">
<link rel="apple-touch-icon" href="{self.url('static/apple-touch-icon.png')}">
<link rel="stylesheet" href="{self.url('static/style.css')}">
{head_scripts}{extra_head}
</head>
<body data-base="{self.base}">
<header class="top"><a class="brand" href="{self.url()}">{esc(cfg['site_name'])}</a>
<nav><a href="{self.url('#search')}">Find a building</a><a href="{self.url('rights/')}">Your rights</a></nav></header>
<main>
{body}
</main>
<footer>
<p>Data from <a href="https://opendata.cityofnewyork.us/">NYC Open Data</a> (311, HPD, DOHMH), updated {fmt_date(self.today.isoformat())}.
311 complaints are reports from the public, not confirmed violations. This site is not affiliated with the City of New York and is not legal advice.</p>
<p><a href="{self.url('about/')}">About &amp; disclosures</a> · <a href="{self.url('privacy/')}">Privacy</a> · <a href="mailto:{esc(cfg['contact_email'])}">Contact</a>{social}</p>
</footer>
<script src="{self.url('static/app.js')}" defer></script>
</body>
</html>
"""
        out = DIST / path / "index.html" if not path.endswith(".html") else DIST / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(doc, encoding="utf-8")
        self.urls.append(canonical)


# ---------------------------------------------------------------- chart

def daily_chart(daily, today):
    """Server-rendered SVG column chart of citywide complaints; weekly bins past 60 days."""
    days = [(r["day"][:10], to_int(r["n"])) for r in daily]
    if not days:
        return ""
    unit = "day"
    if len(days) > 60:
        unit, weeks = "week", {}
        for d, n in days:
            day = dt.date.fromisoformat(d)
            start = (day - dt.timedelta(days=day.weekday())).isoformat()
            weeks[start] = weeks.get(start, 0) + n
        days = sorted(weeks.items())

    W, H, left, right, top, bottom = 720, 240, 44, 8, 12, 28
    plot_w, plot_h = W - left - right, H - top - bottom
    peak = max(n for _, n in days)
    step = next(s for s in (10, 20, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 50000)
                if peak / s <= 4)
    ymax = step * -(-peak // step)
    slot = plot_w / len(days)
    bar = min(24, slot - 2)
    parts = []
    for i in range(0, ymax + 1, step):
        y = top + plot_h - plot_h * i / ymax
        label = f"{i // 1000}K" if i >= 1000 and i % 1000 == 0 else f"{i / 1000:.1f}K" if i >= 1000 else str(i)
        parts.append(f'<line class="grid" x1="{left}" x2="{W - right}" y1="{y:.1f}" y2="{y:.1f}"/>'
                     f'<text class="tick" x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{label}</text>')
    for i, (d, n) in enumerate(days):
        x = left + i * slot + (slot - bar) / 2
        h = plot_h * n / ymax
        y = top + plot_h - h
        r = min(4, bar / 2, h)
        label = (f"Week of {fmt_date(d)}" if unit == "week" else fmt_date(d)) + f": {n:,} complaints"
        path = (f"M{x:.1f},{top + plot_h:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
                f"H{x + bar - r:.1f}Q{x + bar:.1f},{y:.1f} {x + bar:.1f},{y + r:.1f}V{top + plot_h:.1f}Z")
        parts.append(f'<g class="col" data-tip="{esc(label)}"><rect class="hit" x="{left + i * slot:.1f}" y="{top}"'
                     f' width="{slot:.1f}" height="{plot_h}"/><path class="bar" d="{path}"/></g>')
    for i in sorted({0, len(days) // 2, len(days) - 1}):
        x = left + i * slot + slot / 2
        d = dt.date.fromisoformat(days[i][0])
        parts.append(f'<text class="tick" x="{x:.1f}" y="{H - 8}" text-anchor="middle">{d:%b} {d.day}</text>')
    rows = "".join(f"<tr><td>{fmt_date(d)}</td><td>{n:,}</td></tr>" for d, n in days)
    return f"""<figure class="card chart">
<h2>Heat &amp; hot-water complaints per {unit}, citywide</h2>
<div class="chart-wrap"><svg viewBox="0 0 {W} {H}" role="img" aria-label="Column chart of NYC heat complaints per {unit} this season">{"".join(parts)}</svg>
<div class="tip" hidden></div></div>
<details><summary>Show as table</summary><table><thead><tr><th>{unit.title()}</th><th>Complaints</th></tr></thead><tbody>{rows}</tbody></table></details>
</figure>"""


# ---------------------------------------------------------------- pages

RULES = """<ul class="rules">
<li><b>Heat season:</b> October 1 – May 31.</li>
<li><b>6 AM – 10 PM:</b> if it's below 55°F outside, it must be at least 68°F inside.</li>
<li><b>10 PM – 6 AM:</b> it must be at least 62°F inside, whatever the weather.</li>
<li><b>Hot water:</b> at least 120°F at the tap, all year.</li>
</ul>"""

ACTIONS = """<ol class="steps">
<li>Tell your landlord or super in writing (text or email is fine) and keep a copy.</li>
<li>Record the temperature inside, with the date and time, several times a day.</li>
<li>File a complaint with <a href="https://portal.311.nyc.gov/article/?kanumber=KA-01045" rel="noopener">311</a> online, in the 311 app, or by calling 311. HPD may call the landlord or send an inspector.</li>
<li>If heat stays off, you can bring an HP action in Housing Court. Free help: <a href="https://www.nyc.gov/site/hra/legal-services/tenant-legal-services.page" rel="noopener">NYC Tenant Legal Services</a>.</li>
</ol>"""


def building_sentence(b, cur_start):
    since = fmt_date(cur_start.isoformat())
    if b["n"]:
        s = (f"Tenants at {b['address']} have filed {plural(b['n'], 'heat or hot-water complaint')} with 311 "
             f"since {since}")
        s += f", including {b['n7']:,} in the past week." if b["n7"] else "."
        if b.get("rank") == 1:
            s += " That's the most of any building in New York City this season."
        elif b.get("percentile", 0) >= 90:
            s += f" That's more than {b['percentile']}% of NYC buildings with complaints this season"
            s += f" and the most in ZIP {b['zip']}." if b.get("zip_rank") == 1 and b["zip"] else "."
        elif b.get("zip_rank") and b["zip"]:
            s += f" It ranks #{b['zip_rank']} in ZIP {b['zip']} so far this season."
    else:
        s = (f"No heat complaints have been filed for {b['address']} since {since}, but tenants filed "
             f"{plural(b['prev'], 'complaint')} last season.")
    return s


def trend_text(b):
    if not b["prev_to_date"]:
        return "No complaints by this date last season." if b["n"] else ""
    diff = b["n"] - b["prev_to_date"]
    if diff == 0:
        return f"Same as this point last season ({b['prev_to_date']:,})."
    word = "more" if diff > 0 else "fewer"
    return f"{abs(diff):,} {word} than at this point last season ({b['prev_to_date']:,})."


def stat(value, label, sub=""):
    return (f'<div class="stat"><div class="v">{value}</div><div class="l">{label}</div>'
            + (f'<div class="s">{sub}</div>' if sub else "") + "</div>")


def build_building(site, b, cur_start):
    other = []
    if b["open"]:
        other.append(f"<li><b>{plural(b['open'], 'open HPD violation')}</b>, including "
                     f"{b['class_c']:,} immediately hazardous (class C) and {b['class_b']:,} hazardous (class B).</li>")
    else:
        other.append("<li>No open HPD housing-code violations on record.</li>")
    if b["rat_inspections"]:
        other.append(f"<li><b>Rats:</b> {plural(b['rat_inspections'], 'city rat inspection')} in the past year, "
                     + (f"{b['rat_activity']:,} found signs of active rats.</li>" if b["rat_activity"]
                        else "none found signs of active rats.</li>"))
    if b["bedbug_filed"]:
        units = f" of {b['units']:,}" if b["units"] else ""
        other.append(f"<li><b>Bedbugs:</b> the owner's latest annual filing ({fmt_date(b['bedbug_filed'])}) reported "
                     f"{b['bedbug_infested']:,}{units} units with bedbugs.</li>")
    split = ""
    if b["n"]:
        split = (f"<p>{b['whole']:,} of this season's complaints were about the <b>entire building</b>; "
                 f"{b['noheat']:,} mention no heat and {b['nohw']:,} mention no hot water.</p>")
    zip_link = (f'<a href="{site.url("zip/" + b["zip"] + "/")}">ZIP {b["zip"]}</a>' if b["zip"] else "")
    body = f"""<p class="crumbs"><a href="{site.url()}">Home</a> › {b['borough']}{' › ' + zip_link if zip_link else ''}</p>
<h1>{esc(b['address'])}<span class="sub">{b['borough']}, NY {b['zip']}</span></h1>
<p class="lede">{esc(building_sentence(b, cur_start))}</p>
<div class="stats">
{stat(f"{b['n']:,}", "complaints this season", esc(trend_text(b)))}
{stat(f"{b['n7']:,}", "in the last 7 days", "Last: " + fmt_date(b['last']) if b['last'] else "")}
{stat(f"{b['prev']:,}", "all last season", "Oct–May")}
</div>
{split}
{site.ad("adsense_slot_inline")}
<section class="card"><h2>Other conditions on record</h2><ul>{"".join(other)}</ul></section>
<section class="card"><h2>No heat in your apartment? What to do</h2>{ACTIONS}<h3>The legal minimums</h3>{RULES}</section>
{site.shop()}
<p class="fine">Building ID (BBL) {b['bbl']}. Complaints are counted per 311 report, so one outage can produce many reports.
Sources: 311 Service Requests, HPD Housing Maintenance Code Violations, DOHMH Rodent Inspections, HPD Bedbug Reporting.</p>"""
    title = f"{b['address']}, {b['borough']}: heat complaints & building report"
    desc = (f"{b['address']} ({b['borough']} {b['zip']}): {b['n']:,} heat/hot-water complaints this season, "
            f"{b['prev']:,} last season, {b['open']:,} open HPD violations.")
    site.page(b["path"], title, desc, body)


def table(site, rows, show_zip=True):
    trs = "".join(
        f'<tr><td><a href="{site.url(b["path"])}">{esc(b["address"])}</a>'
        + (f'<span class="muted"> · {b["borough"]} {b["zip"]}</span>' if show_zip else "")
        + f'</td><td class="num">{b["n"]:,}</td><td class="num">{b["n7"]:,}</td><td class="num">{b["prev"]:,}</td></tr>'
        for b in rows)
    return ('<div class="scroll"><table class="rank"><thead><tr><th>Building</th><th class="num">This season</th>'
            '<th class="num">Last 7 days</th><th class="num">Last season</th></tr></thead>'
            f"<tbody>{trs}</tbody></table></div>")


def build_zip(site, z, members, cur_start):
    n = sum(b["n"] for b in members)
    boro = members[0]["borough"]
    body = f"""<p class="crumbs"><a href="{site.url()}">Home</a> › {boro} › ZIP {z}</p>
<h1>Heat complaints in ZIP {z}<span class="sub">{boro}</span></h1>
<p class="lede">{plural(n, 'heat or hot-water complaint')} across {plural(sum(1 for b in members if b['n']), 'building')}
in {z} since {fmt_date(cur_start.isoformat())}. Buildings are ranked by complaints this season.</p>
{site.ad("adsense_slot_inline")}
{table(site, members[:200], show_zip=False)}
{site.shop()}"""
    site.page(f"zip/{z}/", f"Heat complaints in {z} ({boro}) – buildings ranked",
              f"Which buildings in ZIP {z} have the most heat and hot-water complaints this season.", body)
    return n


def build_home(site, raw, buildings, zips, cur_start, prev_same_day):
    daily_total = sum(to_int(r["n"]) for r in raw["daily"])
    week_ago = (site.today - dt.timedelta(days=7)).isoformat()
    last7 = sum(to_int(r["n"]) for r in raw["daily"] if r["day"][:10] >= week_ago)
    prev = raw["prev_to_date"]
    vs = ""
    if prev:
        pct = round(100 * (daily_total - prev) / prev)
        vs = f"{abs(pct)}% {'more' if pct >= 0 else 'fewer'} than by {fmt_date(prev_same_day.isoformat())}"
    top = sorted((b for b in buildings if b["n"]), key=lambda b: -b["n"])[:25]
    zip_totals = sorted(((z, sum(b["n"] for b in m), m[0]["borough"]) for z, m in zips.items()), key=lambda t: -t[1])[:12]
    zip_list = "".join(f'<li><a href="{site.url("zip/" + z + "/")}">{z}</a> <span class="muted">{boro} · {n:,}</span></li>'
                       for z, n, boro in zip_totals if n)
    body = f"""<section class="hero" id="answer" data-season-start="10-01" data-season-end="05-31">
<p class="q">{esc(site.cfg['tagline'])}</p>
<p class="a" aria-live="polite">…</p>
<p class="why">Checking the time and the Central Park temperature.</p>
<noscript><p class="why">Turn on JavaScript for a live answer. The rules are below.</p></noscript>
</section>
<div class="stats">
{stat(f"{daily_total:,}", "heat complaints this season", vs)}
{stat(f"{last7:,}", "in the last 7 days")}
{stat(f"{sum(1 for b in buildings if b['n']):,}", "buildings with complaints", "since " + fmt_date(cur_start.isoformat()))}
</div>
<section class="card search" id="search"><h2>Look up your building</h2>
<input type="search" id="q" placeholder="e.g. 1225 Boston Rd" autocomplete="off" aria-label="Search by street address">
<ul id="results"></ul>
<p class="fine">Lists buildings with heat complaints this season or many last season.</p></section>
{site.ad("adsense_slot_top")}
{daily_chart(raw["daily"], site.today)}
<section class="card"><h2>Most complaints this season</h2>{table(site, top)}</section>
<section class="card"><h2>ZIP codes with the most complaints</h2><ul class="zips">{zip_list}</ul></section>
<section class="card"><h2>The rules</h2>{RULES}<p><a href="{site.url('rights/')}">What to do if your heat is off →</a></p></section>
{site.shop()}"""
    site.page("", f"{site.cfg['site_name']}: is heat required right now?",
              "Live answer to whether NYC landlords must provide heat right now, plus heat-complaint history for every building.",
              body)


def build_static_pages(site):
    site.page("rights/", "No heat in NYC? Your rights and what to do",
              "NYC heat season rules, minimum temperatures, and how to get your landlord to fix the heat.",
              f"""<h1>No heat? Your rights in NYC</h1>
<section class="card"><h2>The legal minimums</h2>{RULES}
<p>These come from the NYC Housing Maintenance Code (§27-2029 and §27-2031). Hot water must be available 24 hours a day.</p></section>
<section class="card"><h2>What to do</h2>{ACTIONS}</section>
{site.ad("adsense_slot_inline")}
<section class="card"><h2>Staying safe</h2><ul>
<li>Never use the oven or stove to heat your apartment. It can cause carbon monoxide poisoning.</li>
<li>Plug space heaters straight into the wall, not an extension cord, and keep them 3 feet from anything that burns.</li>
<li>Check that your smoke and carbon monoxide alarms work.</li></ul></section>
{site.shop()}""")
    site.page("about/", f"About {site.cfg['site_name']}", "How this site works, where the data comes from, and how it is funded.",
              f"""<h1>About</h1>
<section class="card"><p>{esc(site.cfg['site_name'])} rebuilds itself every day from public NYC Open Data:
311 heat and hot-water complaints, HPD housing-code violations, health department rat inspections and owners' bedbug filings.
It is independent and not affiliated with the City of New York.</p>
<p>311 complaints are reports from the public. They show where tenants are reporting problems, not that a violation was found.
One outage often produces many reports from the same building.</p>
<h2>How the site is funded</h2>
<p>The site shows ads and includes affiliate links (for example to Amazon). If you buy through one, we may earn a commission
at no cost to you. As an Amazon Associate I earn from qualifying purchases.</p>
<p>Spotted an error? Email <a href="mailto:{esc(site.cfg['contact_email'])}">{esc(site.cfg['contact_email'])}</a>.</p></section>""")
    site.page("privacy/", "Privacy policy", "What this site collects and how ads and analytics use cookies.",
              f"""<h1>Privacy policy</h1>
<section class="card"><p>This site has no accounts and doesn't ask for personal information. Address searches run in your browser and are not sent to us.</p>
<p>The home page asks the National Weather Service (api.weather.gov) for the current Central Park temperature.</p>
<p>We use Google AdSense to show ads and may use Google Analytics to count visits. Google and its partners use cookies to serve ads
based on your visits to this and other sites. You can opt out of personalized ads at
<a href="https://adssettings.google.com" rel="noopener">Google Ad Settings</a> or <a href="https://www.aboutads.info" rel="noopener">aboutads.info</a>.
See <a href="https://policies.google.com/technologies/partner-sites" rel="noopener">how Google uses data from partner sites</a>.</p>
<p>Affiliate links may set cookies on the retailer's site so the purchase can be credited.</p>
<p>Questions: <a href="mailto:{esc(site.cfg['contact_email'])}">{esc(site.cfg['contact_email'])}</a>. Last updated {fmt_date(site.today.isoformat())}.</p></section>""")


def build(raw, cfg):
    today = dt.date.fromisoformat(raw["built"])
    cur_start, _, _, prev_same_day = seasons(today)
    buildings, zips = shape(raw, cfg)

    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.copytree(ROOT / "static", DIST / "static")
    # browsers request /favicon.ico at the site root regardless of <link> tags
    shutil.copy(ROOT / "static" / "favicon.ico", DIST / "favicon.ico")
    site = Site(cfg, today)

    build_home(site, raw, buildings, zips, cur_start, prev_same_day)
    build_static_pages(site)
    for b in buildings:
        build_building(site, b, cur_start)
    for z, members in zips.items():
        build_zip(site, z, members, cur_start)

    index = [[b["address"], b["zip"], b["borough"][:2], b["path"], b["n"]]
             for b in sorted(buildings, key=lambda b: (-b["n"], -b["prev"]))]
    (DIST / "search.json").write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")

    lastmod = today.isoformat()
    urls = "".join(f"<url><loc>{esc(u)}</loc><lastmod>{lastmod}</lastmod></url>" for u in site.urls)
    (DIST / "sitemap.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        encoding="utf-8")
    (DIST / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {cfg['base_url'].rstrip('/')}/sitemap.xml\n")
    if cfg.get("adsense_client"):
        pub = cfg["adsense_client"].replace("ca-", "")
        (DIST / "ads.txt").write_text(f"google.com, {pub}, DIRECT, f08c47fec0942fa0\n")
    (DIST / ".nojekyll").write_text("")
    if cfg.get("domain"):
        (DIST / "CNAME").write_text(cfg["domain"] + "\n")
    print(f"Built {len(site.urls):,} pages ({len(buildings):,} buildings, {len(zips):,} ZIPs) into {DIST}")


def nyc_today():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:  # Windows without the tzdata package; EST is close enough for a date
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)).date()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="rebuild from data/snapshot.json")
    args = parser.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    if args.offline:
        raw = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    else:
        raw = fetch(nyc_today())
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(raw), encoding="utf-8")
    build(raw, cfg)


if __name__ == "__main__":
    main()
