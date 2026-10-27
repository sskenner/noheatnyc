# No Heat NYC

An automated static site built from NYC Open Data, in the spirit of isthelrunning.com:

- **Home page:** a big live YES / NO / NOT YET answer to "Is your landlord required to turn on the heat right now?",
  worked out in the browser from New York time and the current Central Park temperature (National Weather Service).
- **One page per building** (~11–40k, depending on the time of season) with heat and hot-water 311 complaints this season vs. last,
  open HPD violations, rat inspections and bedbug filings.
- **One page per ZIP code** ranking its buildings, plus a "your rights" page, about, and privacy pages.
- **Money:** AdSense slots, Amazon affiliate links (thermometer logger, space heater, blankets…), and slots for referral links
  (renters insurance, apartment search). These only render once you fill in `config.json`.

## Run it

```sh
python build.py             # fetch live data (~8 min) and write dist/
python build.py --offline   # rebuild from data/snapshot.json (~1 min)
python -m http.server 8765 --directory dist   # preview at http://localhost:8765
```

Standard library only, Python 3.9+. Set `SOCRATA_APP_TOKEN` (free, from data.cityofnewyork.us → Developer Settings) to avoid throttling.

## Data sources

| Dataset | ID | Used for |
|---|---|---|
| 311 Service Requests | `erm2-nwe9` | `HEAT/HOT WATER` complaints, by building (BBL) |
| HPD Housing Maintenance Code Violations | `wvxf-dwi5` | open violations, class B / C |
| DOHMH Rodent Inspection | `p937-wjvj` | inspections and "Rat Activity" results, past 12 months |
| HPD Bedbug Reporting | `wz6d-d3jb` | owners' annual infestation filings, past 2 years |
| NWS KNYC observations | api.weather.gov | live Central Park temperature (in the browser) |

Pages are generated for every building with a complaint this season, plus buildings with at least
`min_prev_season_complaints` last season. Raise that number to make fewer pages. Cloudflare Pages' free plan allows 20,000 files per deploy.

## Deploy (GitHub Pages, rebuilt daily)

Live at **https://noheatnyc.com** (repo `sskenner/noheatnyc`).

1. Repo → Settings → Pages → Source: **GitHub Actions**. Custom domain: `noheatnyc.com`, then tick **Enforce HTTPS** once the certificate is issued.
2. Namecheap → Domain List → noheatnyc.com → Advanced DNS. Delete the default parking records, then add:

   | Type | Host | Value |
   |---|---|---|
   | A | `@` | `185.199.108.153` |
   | A | `@` | `185.199.109.153` |
   | A | `@` | `185.199.110.153` |
   | A | `@` | `185.199.111.153` |
   | CNAME | `www` | `sskenner.github.io.` |

3. Optional: add a `SOCRATA_APP_TOKEN` repo secret.

`domain`, `base_url` and `base_path` in `config.json` control the canonical URLs, the sitemap and the `CNAME` file.
`.github/workflows/build.yml` rebuilds every morning and redeploys.

## Weekly Bluesky post

Every Monday in heat season (Oct 1 – May 31), after the morning build, the workflow posts the
three buildings with the most heat complaints that week to [@noheatnyc.com](https://bsky.app/profile/noheatnyc.com),
each address linked to its building page. It needs a Bluesky **app password** saved as the
`BSKY_APP_PASSWORD` repository secret; without it the job just prints the post.

- Preview the post: `python post_bsky.py --dry-run` (after a build)
- Post right away: Actions → Build and deploy → Run workflow → tick "Post the weekly Bluesky roundup now"

## Building email alerts

Each building page can show a sign-up form: "Get an email when this building gets new heat complaints."
Sign-ups go to [Buttondown](https://buttondown.com), which handles double opt-in, storage and unsubscribes.
Each subscriber gets the tag `bbl-<BBL>` for every building they follow.

Each build publishes `counts.json` (complaints per building). Before building, the workflow saves the
live copy. After the deploy, `email_alerts.py` compares the two. For each followed building whose count went up,
it sends one email to that building's subscribers only, so there's at most one alert per building per deploy.

1. Create a Buttondown newsletter. Tags need the **Basic** plan or higher.
2. Put its username in `config.json` → `buttondown_username`. The forms and privacy-policy text appear on the next build.
3. Save the API key (Settings → API) as the `BUTTONDOWN_API_KEY` repository secret. Without it the job just prints the alerts.

- Preview the alerts: `python email_alerts.py --save-previous`, then `python build.py`, then `python email_alerts.py --dry-run`

## Turning on revenue

Edit `config.json`:

- `adsense_client` (`ca-pub-…`) plus `adsense_slot_top` / `adsense_slot_inline`. This also writes `ads.txt`.
  Apply for AdSense after the site has been live on its own domain for a few weeks. The rights, about and privacy pages are there for the review.
- `amazon_tag`: your Associates tracking ID, added to every product search link. You must make 3 qualifying sales within 180 days or the account is closed.
- `referrals[].url`: links from renters insurance or apartment-listing programs (e.g. through Impact or CJ). Items without a URL are hidden.
- `ga4_id`: optional Google Analytics.

## Growth ideas

- Post the weekly "worst buildings" list to neighborhood subreddits and X/Bluesky (it's on the home page and ZIP pages).
- Grow the building-alert list into a newsletter. It's the best way past Amazon's 3-sale rule.
- Reuse the same pipeline for noise (`Noise - Residential`), rats and parking (see the strategy notes).
