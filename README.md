# ASO Hub

One private page for how your apps are found and doing in the stores:
downloads, proceeds, impressions, page views, deletions, rankings, competitors,
keyword coverage, ratings, and one list of what to do next. It reads
[AsoBeast](https://github.com/AsoBeast/asobeast) (rankings; it does not rebuild
them), App Store Connect and Google Play, saves everything it reads, and shows
it on a single self-hosted page. It can post new to-dos and a weekly crash
digest to Discord.

<img src="assets/icon/aso-hub-icon-180.png" alt="ASO Hub icon" width="90">

Shared as-is under the MIT license. Issues are welcome; there is no promise of
support or a roadmap. It was built for one small team's apps, so expect rough
edges outside that setup.

The example `config.toml` uses made-up apps (Lumen, Kettle, Tandem, Atlas,
Beacon); replace them with yours.

![Overview page with sample data](docs/screenshot-overview.png)

Screenshots show made-up numbers from `demo/seed.py`, which fills a throwaway
database so you can look around without any keys:
`python demo/seed.py /tmp/demo.db`, then
`DATABASE_PATH=/tmp/demo.db python -m aso_hub serve`.

## Pages

Every page takes a time range: 7 days, 30 days, 3 months or 12 months
(`?days=7|30|90|365`), compared with the range before it.

The top bar shows each app with its store icon (from AsoBeast) and the open
to-do count; light and dark follow the device setting. Style and script links
carry a fingerprint of their contents, so a deploy never leaves a browser on an
old stylesheet.

- **Overview**: per app, downloads for the range with a trend line, the
  latest day's downloads (and the US share), proceeds, the rating, crashes for
  Android, the biggest keyword moves of
  the week in the app's first market, and open to-dos and FYIs.
- **App page**: funnel (impressions → page views → downloads), downloads,
  proceeds, deletions, charts of downloads, impressions, page views and
  proceeds over the range (one measure per chart, hover or tab to a column for
  its value), and a table by day (up to 30 days), week (3 months) or
  month (12 months). Then actions, ratings and recent reviews, and per market a
  keyword table (position, 7-day move, popularity, difficulty, competitors
  ahead, where it is in the listing) and the listing text with character counts.
- **Actions**: "To do" (things to act on: hub rules and AsoBeast's Action
  Center, with a link to AsoBeast to snooze or dismiss) and "Worth knowing"
  (nothing to do about it directly, like low downloads).

Every page footer says when each source last updated, and a failed pull shows
a banner with the reason.

## Where the numbers come from

| Number | Source | Notes |
|---|---|---|
| First-time downloads | Sales and Trends daily report **and** the App Downloads analytics report | The larger of the two each day, as ASO Atlas does: each misses some on some days. Sales units are product types 1, 1F, 1T, F1 and the 1E variants; updates and in-app purchases are left out. |
| Impressions, page views | App Store Discovery and Engagement report | Pages show total counts (what App Store Connect and ASO Atlas show). The page-view spike rule uses unique devices, so one person reloading the page isn't a spike. |
| Deletions | App Store Installation and Deletion report | Can end a day earlier than the impressions report. |
| Proceeds | App Store Purchases report, `Proceeds in USD` | Apple's own dollar figure. ASO Atlas differs by a few cents on some days ($2.83 vs $2.78), probably its own currency rates. |
| Conversion | downloads ÷ impressions | Same as App Store Connect and ASO Atlas. |
| Android installs, uninstalls, devices | Play report files, `stats/installs/..._overview.csv` (and `_country.csv` for US) | "Installs" is Daily User Installs (new people, not devices). About 4 days behind. UTF-16 files. |
| Android store listing visitors, installed | `stats/store_performance/..._country.csv` | Visitors and "acquisitions" (visitors who installed), summed over countries; Google lumps small numbers into "Other", so a small app has no country or search-term split yet. About 6 days behind. |
| Android in-app purchases | `sales/salesreport_YYYYMM.zip` | Orders counted per day ("Charged"); refunds counted separately. In the buyer's currency, so the hub counts them rather than converting. |
| Android earnings | `earnings/earnings_YYYYMM_*.zip` | Monthly, in US dollars after Google's fee, published early the next month. |
| Android crashes and ANRs | Play Developer Reporting API, `errorIssues:search` | Google computes no daily crash or ANR rate for an app with few users (a small app's comes back empty), so the hub reads the issues themselves. Daily rates lag about 9 days when they do exist. |
| Android crash and ANR counts by version (weekly digest) | Play Developer Reporting API, `errorCountMetricSet:query` | Report counts per Pacific day and version code. Unlike rates, these come back for small apps. About a day behind; asking past Google's latest day is refused, so the digest stops there and says so. |
| iOS crashes by version (weekly digest) | App Crashes analytics report | See "Weekly crash digest" for what Apple leaves out. |

**Google Play** is read with a Google Cloud service account that you add as a
user in Play Console with two account permissions: "View app information and
download bulk reports (read-only)" and "View financial data". The reports
bucket (`gs://pubsite_prod_<id>`, shown under Download reports in Play Console)
holds installs, store visitors, sales and earnings; Google can take a day or
two to open it to a new account. Put the bucket name in `PLAY_BUCKET`.

Checked against ASO Atlas for one ten-day stretch: downloads, impressions,
page views and deletions matched every day.

Things learned building it:

- **Apple restates each day in the next day's analytics file.** The hub keeps
  every file and counts only the newest one covering a day (`asc_current` in
  `db.py`). Adding them up double counts.
- **Apple keeps about 31 days of daily analytics files** and a year of daily
  sales reports, so the hub's database is the only long-term copy. Nothing is
  deleted. The first pull reads the whole year of sales (about 4 minutes).
- **The Sales-role key is enough.** It lists and reads the ongoing report
  requests ASO Atlas created; no Admin key. The hub never creates requests (a
  second create fails with 409).
- Report files download from Amazon S3 (`*.s3.us-west-2.amazonaws.com`), not
  from Apple's API host.

## AsoBeast

Read over HTTP (`ASOBEAST_URL`; inside a Docker network if AsoBeast runs on the
same host) with a read-only token, built against AsoBeast 1.13.0.
Each pull saves the answers as that day's snapshots (`asobeast_snapshots`), all
at once, so a failed pull leaves the last good day. Apps are matched by store id.

- **Hidden keywords**: AsoBeast re-adds single words from the title, subtitle
  and keyword field every day (`source` TITLE, SUBTITLE or KEYWORD_FIELD). The
  keyword tables hide those unless listed in `keep_single_words` in
  `config.toml`; "show all" brings them back. Keywords you added (MANUAL)
  always show.
- **"In the listing"**: AsoBeast counts a phrase as covered only when one field
  holds all of it, but Apple combines words across the title, subtitle and
  keyword field. So when no field holds the phrase, the hub checks each word
  against the fields the store searches (AsoBeast's `indexed` flag; the App
  Store doesn't search the description) plus the keyword fields of the other
  languages the market reads (the US store also reads Spanish (Mexico)), and
  names any missing word.
- **Visibility for the Monday post**: `/portfolio` and `/portfolio/insights`
  give each app's visibility and listing audit score with their 7-day change,
  and the same visibility for a linked group (one app on both stores).
  `report-snapshot` prints them as JSON for a daily-report script of your own.

## Rules

Thresholds live in `config.toml` . An action opens the first
pull a rule fires, keeps its "since" date while it keeps firing, and resolves
the first pull it doesn't.

To do:
- A Google Play crash or ANR reported in the last 7 days (high if it hit 3+
  users), with a link to it in Play Console.
- A keyword whose usual spot the week before (the middle of its 7 checks) was
  in the top 50 sits 15+ places below that on each of the last 3 checks (hidden
  single words don't count). The usual spot, not the best day, so a search that
  swings 40 places a day doesn't raise a false alarm.
- Deletions above 50% of first-time downloads over 14 days (needs 10 downloads).
- A review from the last 7 days with no reply (says where to reply).

Worth knowing:
- US first-time downloads average below a per-app minimum over 7 days (example: 3
  for one app), with any US keywords that fell 10+ places in the same days as a likely cause.
- Page viewers at least 3x normal (and 20+) with no matching rise in downloads.
- A new rating in the last 3 days.

**Discord:** after each pull, new to-dos (hub rules and open AsoBeast items)
post to the Discord webhook saved as `discord-webhook` (one message, or more if
it runs past Discord's length limit), with a link to the Actions page. Each
posts once, and again only if it resolves and comes back. "Worth knowing" and snoozed AsoBeast items don't post. A failed
post is retried on the next pull. With no webhook saved, nothing posts.

## Weekly crash digest

Run weekly (`deploy/asohub-crashes.timer`, `python -m aso_hub crash-digest`),
one message per app listed under `[crash_digest]` in `config.toml`, to a
Discord channel of your choice per app. It covers the last full week
(Monday to Sunday) against the week before: crashes by app version on iOS,
crashes and freezes (ANRs) by version code on Android, and the week's top
Android issues with a link to each in Play Console. A week with nothing says
"No crashes reported", so a Monday without a message means the job didn't run.
iOS and Android of the same app share a message.

**What Apple's numbers include (checked Nov 2026).** The only crash source
the hub's Sales-role key can read is the App Crashes analytics report, in the
ongoing report request each app already has. Apple fills it only from people
who share analytics with developers (in one app's App Opt In report, 5 of 7
downloading users), and only once at least 5 of them crash, so a
small app's report is usually empty: none of the four iOS apps we tried had a single
App Crashes file in Apple's 31-day window. A day can still grow for 5
days after it ends. The report has no crash causes or logs; those are in Xcode's
Organizer (Window > Organizer > Crashes), which has no API. The other Apple
sources were tried with the same key: the performance metrics
(`/v1/apps/{id}/perfPowerMetrics`) answer but are empty for apps this size, and
build diagnostics (hangs, slow launches) and TestFlight crash feedback answer
403 to a Sales key. Google counts every crash its users' phones report, with no
minimum.

**Where it posts.** Each app's webhook is a file in the secrets folder named
`alerts-<app>` (`alerts-lumen`, `alerts-tandem`), saved with
`./set-secret.sh alerts-lumen`. If a pull
fails, the app's message says so in place of that store's numbers; that, a
failed post, or a listed app with no webhook saved also posts to
`alerts-admin` (a channel for failures), and the run exits with code 3. Any other failure (the container restarting,
say) is retried every 10 minutes, up to 5 tries; if the container
stays down past that, the missing Monday message is the only sign.
An iOS app with no analytics report request yet gets a note, not a failure.
Each app posts once per week; `crash-digest --again` posts the week again, and
`crash-digest --dry-run` prints the messages without posting.

## Running it locally

Keep settings and secrets outside the repo, for example in `~/.config/aso-hub/`
(a `hub.env` with the ids and key paths, the `.p8` key, `google-play.json`, and
`asobeast-token` saved with `./set-secret.sh asobeast-token`).

```sh
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
set -a; . ~/.config/aso-hub/hub.env; set +a
.venv/bin/python -m aso_hub collect          # pull everything (or: collect asc)
.venv/bin/python -m aso_hub serve            # http://127.0.0.1:8690
.venv/bin/ruff check . && .venv/bin/pytest -q
```

Environment variables: `ASC_ISSUER_ID`, `ASC_KEY_ID`, `ASC_VENDOR_NUMBER`,
`ASC_KEY_PATH` (App Store Connect, a key with the Sales role), `GOOGLE_KEY_PATH`
and `PLAY_BUCKET` (Google Play), `ASOBEAST_URL`, `ASOBEAST_TOKEN_FILE` and
`ASOBEAST_WEB_URL` (your AsoBeast address, for links), `DISCORD_WEBHOOK_FILE`,
`ALERTS_WEBHOOK_DIR`, `ASO_HUB_URL` (the hub's own address, for links in
Discord posts). The hub has no sign-in of its own: keep it on a trusted network
or behind an authenticating proxy. If you use Cloudflare Access, set
`CF_ACCESS_AUD` and `CF_ACCESS_TEAM_DOMAIN` and the hub also checks Access's
signed pass (`access.py`).

## On a server

`deploy/compose.yaml` is an example for any Docker host, with data and secrets
folders mounted from the host. `deploy/asohub.timer` and `asohub-crashes.timer`
(with their `.service` files) are systemd units that run the pull twice a day
and the crash digest weekly through `docker exec`; copy them to
`/etc/systemd/system/`, set the times and your user, and enable the timers.

Build the image on the host or anywhere that matches its CPU, then point
`ASO_HUB_VERSION` in `.env` (next to `deploy/compose.yaml`) at the tag and run
`docker compose -f deploy/compose.yaml up -d`:

```sh
tag=$(git rev-parse --short HEAD)
docker build -t aso-hub:$tag --build-arg VERSION=$tag .
```

## Checks

- `security.yml`: gitleaks over the full history, pip-audit and OSV-Scanner on
  `requirements.txt`, on every push and weekly.
- `tests.yml`: ruff and pytest on every push.
- Dependabot update pull requests (`dependabot.yml`).
- `requirements.txt` pins every library, indirect ones too, so the image ships
  exactly what was scanned.

## License

MIT, see `LICENSE`.
