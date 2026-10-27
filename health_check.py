"""Daily health check for the live site: is it up, is the data from today, do the money links work.

Checks the live site (config.json → base_url), not dist/. Prints a report, adds it to the
GitHub Actions job summary when run there, and exits 1 if any check fails, so a failed
run emails you. Standard library only.

    python health_check.py                  # check the live site
    python health_check.py --max-age-days 1 # allow yesterday's data (e.g. before the morning build)

With the BUTTONDOWN_API_KEY environment variable it also reports the alert-list size.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

from build import ROOT, nyc_today
from email_alerts import call

MIN_BUILDINGS = 1000  # a normal build has ~11–40k; far fewer means a dataset came back short
CERT_WARN_DAYS = 14   # GitHub Pages renews the certificate itself; this catches a renewal that stalled


def get(url):
    """(final URL, status, body), following redirects; status 0 if it couldn't connect."""
    req = urllib.request.Request(url, headers={"User-Agent": "noheatnyc-health-check"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.url, resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return url, e.code, ""
    except (urllib.error.URLError, OSError) as e:
        return url, 0, str(e)


def cert_days_left(host):
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=30) as sock, ctx.wrap_socket(sock, server_hostname=host) as tls:
        expires = dt.datetime.fromtimestamp(ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"]), dt.timezone.utc)
    return (expires - dt.datetime.now(dt.timezone.utc)).days


def affiliate_problems(body, cfg):
    """What's wrong with a page's Amazon links: wrong count, or links missing the tracking ID."""
    links = [urllib.parse.unquote(html.unescape(h)) for h in re.findall(r'href="(https://www\.amazon\.com/[^"]+)"', body)]
    want = len(cfg.get("amazon_products", []))
    problems = []
    if len(links) != want:
        problems.append(f"{len(links)} Amazon links, expected {want}")
    untagged = [l for l in links if f"tag={cfg['amazon_tag']}" not in l]
    if untagged:
        problems.append(f"{len(untagged)} without tag={cfg['amazon_tag']}")
    return problems


def run(cfg, max_age):
    base = cfg["base_url"].rstrip("/") + "/"
    host = urllib.parse.urlparse(base).hostname
    today = nyc_today()
    results, numbers = [], {}

    def check(name, ok, detail=""):
        results.append((name, ok, detail))

    # home page: up, and showing today's data
    _, status, home = get(base)
    check("Home page loads", status == 200 and cfg["site_name"] in home, f"HTTP {status}")
    shown = re.search(r"updated ([A-Z][a-z]{2} \d{1,2}, \d{4})", home)
    if shown:
        shown_date = dt.datetime.strptime(shown.group(1), "%b %d, %Y").date()
        age = (today - shown_date).days
        check('"Data updated" date is current', age <= max_age, f"shows {shown.group(1)} ({age} days old)")
    else:
        check('"Data updated" date is current', False, "no date on the home page")
    problems = affiliate_problems(home, cfg)
    check("Home page Amazon links tagged", not problems, "; ".join(problems) or f"tag={cfg['amazon_tag']}")

    # data files from the morning build
    counts = search = None
    _, status, body = get(base + "counts.json")
    try:
        counts = json.loads(body)
        age = (today - dt.date.fromisoformat(counts["built"])).days
        check("counts.json is from today's build", age <= max_age, f"built {counts['built']} ({age} days old)")
        n = len(counts["n"])
        check("Building count looks normal", n >= MIN_BUILDINGS, f"{n:,} buildings (min {MIN_BUILDINGS:,})")
        numbers["Buildings with pages"] = f"{n:,}"
        numbers["Heat complaints this season"] = f"{sum(counts['n'].values()):,} (since {counts['season']})"
    except (ValueError, KeyError):
        check("counts.json is from today's build", False, f"HTTP {status}, unreadable")
    _, status, body = get(base + "search.json")
    try:
        search = json.loads(body)
    except ValueError:
        check("search.json loads", False, f"HTTP {status}, unreadable")

    # the most-complained-about building's page: up, with the alert form and tagged links
    if search:
        address, _, _, path, n = search[0]
        numbers["Top building"] = f"{address} ({n:,} complaints)"
        _, status, page = get(base + path)
        check("Top building page loads", status == 200 and address in page, f"HTTP {status}, /{path}")
        if cfg.get("buttondown_username"):
            check("Email alert form on building page", "buttondown.com/api/emails/embed-subscribe" in page)
        problems = affiliate_problems(page, cfg)
        check("Building page Amazon links tagged", not problems, "; ".join(problems) or "OK")

    for path in ("rights/", "about/", "privacy/"):
        _, status, _ = get(base + path)
        check(f"/{path} loads", status == 200, f"HTTP {status}")
    _, status, body = get(base + "sitemap.xml")
    check("sitemap.xml loads", status == 200 and "<url>" in body, f"HTTP {status}, {body.count('<url>'):,} URLs")

    final, status, _ = get(f"https://www.{host}/")
    check("www redirects to the site", status == 200 and urllib.parse.urlparse(final).hostname == host,
          f"HTTP {status}, ended at {final}")
    try:
        days = cert_days_left(host)
        check("HTTPS certificate valid", days >= CERT_WARN_DAYS, f"expires in {days} days")
    except (OSError, ssl.SSLError) as e:
        check("HTTPS certificate valid", False, str(e))

    key = os.environ.get("BUTTONDOWN_API_KEY")
    if key:
        try:
            numbers["Email alert subscribers"] = f"{call('subscribers?page_size=1', key)['count']:,}"
        except (urllib.error.URLError, ValueError, KeyError) as e:
            numbers["Email alert subscribers"] = f"couldn't read ({e})"
    return today, results, numbers


def report(cfg, today, results, numbers):
    failed = [r for r in results if not r[1]]
    lines = [f"## {cfg['site_name']} health check, {today:%a %b} {today.day}",
             "",
             f"**{'All ' + str(len(results)) + ' checks passed' if not failed else str(len(failed)) + ' of ' + str(len(results)) + ' checks FAILED'}**",
             "",
             "| | Check | Detail |", "|---|---|---|"]
    lines += [f"| {'✅' if ok else '❌'} | {name} | {detail} |" for name, ok, detail in results]
    if numbers:
        lines += ["", "| Today's numbers | |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in numbers.items()]
    lines += ["", "Not checked here (no API): Amazon Associates clicks and earnings. Look at the Associates dashboard."]
    return "\n".join(lines) + "\n", failed


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-age-days", type=int, default=0, help="how many days old the data may be (default 0: today)")
    args = parser.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    text, failed = report(cfg, *run(cfg, args.max_age_days))
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    if failed:
        sys.exit(f"{len(failed)} check(s) failed: " + ", ".join(name for name, _, _ in failed))


if __name__ == "__main__":
    main()
