# Instructions for AI assistants

You are helping someone set up **flight-watch**, a daily flight price watch.
The whole program is `flight_watch.py`; all settings live in `config.toml`.
Your job is usually to configure, test and schedule it for their trip, not to
change the code.

## Setup, step by step

### 1. Ask for the trip

Get these from the user before writing anything. Ask in plain language and
translate to config yourself.

- **Destination.** Convert a city to its main IATA airport code (Copenhagen →
  CPH). If a city has several airports and it matters, ask.
- **Where they could leave from.** Usually their home airport. Offer to add
  nearby alternatives only if they would genuinely consider travelling to one.
- **Dates.** A window of days they could leave, and a window they could come
  back. No return window means one-way. Keep grids modest: outbound days ×
  return days is the number of searches per airport per run (pairs that
  would return before leaving are skipped). Stay around 60 or
  below; well past that, Google throttles and the results thin out.
- **Target price** per airport (optional): "tell me when it's under X".
- **Currency, passengers, cabin, nonstop only.** Defaults are USD, 1 adult,
  economy, any stops. Ask only if the user hints otherwise.
- **An airline they care about** (optional), for `watch_airline`.
- **How to get the report.** See step 3.

### 2. Install and write the config

```bash
python3 --version        # must be 3.11+ (tested on 3.12 to 3.14)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml
```

Edit `config.toml`. Every key is documented inline in `config.example.toml`.

### 3. Delivery

- `print`: fine for a first test, or when the scheduler captures stdout.
- `mail_app`: easiest on a Mac that already has Apple Mail set up. No
  passwords. Only works on macOS.
- `smtp`: works everywhere. The password goes in the environment variable
  `FLIGHT_WATCH_SMTP_PASSWORD`, **never in config.toml, never committed, never
  echoed back in chat logs**. (`password` under `[notify.smtp]` is read as a
  fallback, but do not put it there unless the user asks.) For Gmail the user needs an app password from
  https://myaccount.google.com/apppasswords (it requires 2-step verification);
  let them paste it into their own shell or scheduler config.

### 4. Test before scheduling

```bash
.venv/bin/python flight_watch.py --dry-run
```

This hits Google Flights for real and prints the report, but writes nothing.
It takes roughly `(number of combos × 0.5s) + fetch time` per airport.

- If an airport shows `whole grid empty`, the code is probably wrong or the
  route does not exist. Check the IATA code. Some small airports have no
  itineraries to some destinations at all.
- If many combos are empty, it is throttling. Retry later, or shrink the grid
  or lower `max_workers`.
- A `config error:` line explains exactly what is wrong. Fix and rerun.
- A `setup error:` line means the requirements are not installed for the
  Python that ran it. Use `.venv/bin/python`, or reinstall the requirements.

Then confirm delivery reaches them:

```bash
.venv/bin/python flight_watch.py --test-notify
```

With `mail_app`, the message can sit in Mail's outbox for several minutes
before it sends. That is normal, not a failure.

### 5. Schedule it

Once a day. Use absolute paths everywhere; schedulers have a minimal environment.
On macOS the repo must not live in Desktop, Documents, Downloads or iCloud
Drive: macOS privacy protection stops background jobs reading those folders, so
the job can fail even though the same command works in a terminal. Move it (for
example to `~/code`) first.

- **macOS:** fill in `examples/launchd.plist`, save to
  `~/Library/LaunchAgents/`, then
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<file>.plist`.
  Remove with `launchctl bootout gui/$(id -u)/<label>`.
  `mail_app` delivery needs permission to control Mail. Running
  `--test-notify` in a terminal grants it to the terminal app, not to the
  scheduled job, which asks again on its first run, when nobody may be there
  to click Allow. So right after bootstrapping, with the user at the Mac, run
  the job once with `launchctl kickstart gui/$(id -u)/<label>` (a real run: it
  logs prices and sends a report), have them approve the prompt, and check the
  `.err.log` for `[notify-error]`.
- **Linux, or cron on macOS:** see `examples/crontab.txt`.

Tell the user what you scheduled, when it runs, and exactly how to stop it.

### 6. When they book

Stop the schedule (step 5's remove command). Leave the CSV unless they ask you
to delete it.

## Changing things later

- New dates, target or airports: edit `config.toml`. No code change.
- Changing currency, cabin, adults or one-way vs return: also point
  `[storage] log` at a new file. The log doesn't record those settings, so old
  rows would make the comparisons wrong.
- Removing an origin hides its old rows from reports; it does not delete them.
- `--history` shows every logged run and the all-time lows.

## If you do change the code

- Run `.venv/bin/python -m unittest discover tests`. Tests are offline.
- Keep the one-report-per-run design. Don't add a second message for
  alerts or errors; fold them into the report.
- Never let a failed fetch count as a price. Missing stays empty.
- Don't raise concurrency to go faster. Google throttling is the main
  failure mode, and pacing is what keeps the grid complete.
