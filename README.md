# flight-watch

A daily price watch for one flight route. You pick a destination, one or more
airports to leave from, and a window of dates. Each run it checks every date
combination on Google Flights, keeps every price in a CSV, and sends you **one
report**:

```
Subject: ✈️ London $412 from JFK (Mar 4 → Mar 16) ▼ NEW LOW

Cheapest to London right now: $412 from JFK
  out 2027-03-04  ·  back 2027-03-16
  ▼ The cheapest it has ever been (previous best $431).

FROM       BEST  DATES             vs LAST    ALLTIME     TARGET
----------------------------------------------------------------
JFK        $412  Mar 4 → Mar 16        -19       $431       $450 **
EWR        $438  Mar 2 → Mar 19         -5       $443       $450 **

** = at or below your target for that airport.

TARGET HIT, worth acting on:
  JFK $412 <= $450  ·  Mar 4 → Mar 16
  EWR $438 <= $450  ·  Mar 2 → Mar 19

Trend, cheapest fare over the last 10 runs:
  2027-01-18       $438 JFK  (Mar 1 → Mar 15)
  2027-01-19       $433 JFK  (Mar 1 → Mar 15)
  2027-01-20       $431 JFK  (Mar 1 → Mar 15)

Grid: Mar 1–Mar 7 outbound × Mar 15–Mar 19 return, 35 date combos per airport, economy, 1 adult(s).
```

It is built for deciding **when to book**, not for alarms. It reports every
run even when nothing moved, because "still flat for a week" is exactly what
you want to know before you pay.

## Why a date grid

Fares for the same trip swing by hundreds depending on which day you leave and
come back. Instead of watching one itinerary, flight-watch prices every
outbound date against every return date in your window and reports the
cheapest pair. Seven flexible days out and five back is 35 searches per
airport, per run.

Adding a second airport shows you whether leaving from somewhere else is
worth the trip to get there.

## Setup

Needs Python 3.11 or newer (tested on 3.12, 3.13 and 3.14).

```bash
git clone https://github.com/maxwraae/flight-watch.git
cd flight-watch
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml
```

Edit `config.toml`: destination, origins, dates, and how you want the report
delivered. Every option is explained inline. Then:

```bash
.venv/bin/python flight_watch.py --dry-run      # real prices, prints the report, saves nothing
.venv/bin/python flight_watch.py --test-notify  # checks that delivery reaches you
.venv/bin/python flight_watch.py                # the real run: log + report
.venv/bin/python flight_watch.py --history      # every run so far and the all-time lows
```

## Delivery

Set `notify.method` in the config:

| method     | what it does | needs |
|------------|--------------|-------|
| `print`    | writes the report to stdout | nothing |
| `smtp`     | sends email through any SMTP server | `[notify.smtp]` settings, and the password in the env var `FLIGHT_WATCH_SMTP_PASSWORD` |
| `mail_app` | sends email through Apple Mail | macOS, with an account already set up in Mail |

For Gmail over SMTP, create an [app password](https://myaccount.google.com/apppasswords)
and use `smtp.gmail.com`, port 587.

## Running it daily

It is a single command, so any scheduler works. Once a day is plenty;
fares rarely move faster than that, and it keeps load on Google light.

- **macOS:** [`examples/launchd.plist`](examples/launchd.plist)
- **Linux / macOS cron:** [`examples/crontab.txt`](examples/crontab.txt)

The run exits `0` on success, `1` if the report could not be delivered, and
`2` on a config or install problem (including a date window that has entirely
passed), so a scheduler can tell them apart.

On macOS, keep the repo out of Desktop, Documents, Downloads and iCloud Drive.
macOS privacy protection stops background jobs reading those folders unless
you grant extra access, so a scheduled run can fail there even though the same
command works in your terminal.

## Things to know

- **Prices come from Google Flights** via the unofficial
  [fast-flights](https://github.com/AWeirdDev/flights) library. There is no API
  key and no account, but Google can change its pages and break the scraper,
  and it throttles heavy use. The report flags **SCRAPE HEALTH** when too few
  dates came back; if that keeps happening, lower `max_workers` or raise the
  pauses in `[search]`.
- **Google's price is not always the airline's price.** `watch_airline` tracks
  the cheapest fare on one airline as Google shows it. Airline sites, and the
  same airline in another country's currency, can be cheaper or much more
  expensive. Check before you buy.
- **Missing is not zero.** A date that fails to price is logged empty and
  never counts as a low.
- **Dates that have passed are skipped**, so the grid shrinks as the trip gets
  close. Date pairs that would return before leaving are never searched.
- **History is per route.** Change the destination or remove an origin and
  the report ignores those old rows, but they stay in the CSV. The log does not
  record currency, cabin, passengers or one-way vs return, so if you change any
  of those, point `[storage] log` at a new file or the old prices will skew
  "all-time low" and "vs last".
- Your `config.toml` and price log are gitignored. Keep your SMTP password in
  the environment, not the file. A `password` in `[notify.smtp]` works as a
  fallback, but anything running as you can read it.
- **Report times are local, the log is UTC.** The CSV timestamps every run in
  UTC so the history is unambiguous; reports and `--history` convert to your
  timezone.

## Using an AI assistant to set it up

[`AGENTS.md`](AGENTS.md) is written for coding assistants like Claude Code,
Codex or Cursor. Point yours at this repo and say something like *"set up
flight-watch for my trip"*; it will ask for your route and dates, write the
config, test it, and schedule it.

## Tests

```bash
.venv/bin/python -m unittest discover tests
```

Offline, no network: config validation, the date grid, history, and the report.

## License

MIT
