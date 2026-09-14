#!/usr/bin/env python3
"""
flight-watch: a daily price watch for one flight route, over a grid of dates.

You give it a destination, one or more departure airports, and a window of
outbound (and optionally return) dates. Each run it prices every date
combination on Google Flights, logs every price to a CSV, and sends you ONE
report: the cheapest fare and its dates, the move since the last run, the
all-time low, and the recent trend.

It is a buying-decision tool, not an alarm. It reports every run, even when
nothing changed, because "still flat" is exactly what you need to see when
deciding whether to book. Hitting your target price or a failed scrape changes
the subject line and adds a section; it never sends a second message.

    python flight_watch.py                  price the grid, log it, send the report
    python flight_watch.py --dry-run        price the grid, print the report, write nothing
    python flight_watch.py --history        print every logged run and the all-time lows
    python flight_watch.py --test-notify    send a test report to check delivery works
    python flight_watch.py --config PATH    use a config other than ./config.toml

All settings live in config.toml (copy config.example.toml). See README.md.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import os
import smtplib
import ssl
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

HEADER = ["ts", "origin", "destination", "out_date", "back_date",
          "overall_price", "airline_price"]
SMTP_PASSWORD_ENV = "FLIGHT_WATCH_SMTP_PASSWORD"


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    pass


@dataclass
class Origin:
    code: str
    city: str
    alert: int | None = None       # "at or below this is worth acting on"


@dataclass
class Config:
    path: Path
    dest_code: str
    dest_city: str
    origins: list[Origin]
    out_dates: list[str]
    back_dates: list[str] | None   # None means one-way
    currency: str = "USD"
    adults: int = 1
    seat: str = "economy"
    max_stops: int | None = None
    watch_airline: str = ""        # also track the cheapest fare on this airline
    results_per_query: int = 12
    max_workers: int = 4
    stagger: float = 0.5
    origin_pause: float = 15
    retry_pause: float = 25
    min_success_frac: float = 0.75
    trend_runs: int = 10
    notify_method: str = "print"
    notify_to: str = ""
    smtp: dict = field(default_factory=dict)
    log_path: Path = Path("flight_watch_log.csv")
    # outbound dates before this are skipped: Google can't price a past flight,
    # and counting those as failed fetches would read as throttling
    today: str = field(default_factory=lambda: date.today().isoformat())

    @property
    def one_way(self) -> bool:
        return self.back_dates is None

    @property
    def combos(self) -> list[tuple[str, str | None]]:
        """Every date pair worth pricing. Pairs that return before they leave are
        skipped: Google still quotes a price for them, but nobody can fly it."""
        outs = [o for o in self.out_dates if o >= self.today]
        if self.one_way:
            return [(o, None) for o in outs]
        return [(o, b) for o in outs for b in self.back_dates if b >= o]


def _iso(v) -> date:
    # TOML reads a quoted "2027-03-01" as text and an unquoted 2027-03-01 as a
    # date. Take both.
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    return date.fromisoformat(v)


def _date_range(start, end, what: str) -> list[str]:
    try:
        s, e = _iso(start), _iso(end)
    except (TypeError, ValueError):
        raise ConfigError(f"{what}: dates must be YYYY-MM-DD, got {start!r} and {end!r}")
    if e < s:
        raise ConfigError(f"{what}: end date {end} is before start date {start}")
    out, d = [], s
    while d <= e:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _table(parent: dict, key: str, name: str) -> dict:
    v = parent.get(key) or {}
    if not isinstance(v, dict):
        raise ConfigError(f"{name} must be a section, [{name}], not a single value")
    return v


_KINDS = {str: "text in quotes", int: "a whole number", float: "a number"}


def _opt(table: dict, key: str, default, name: str, lo=None, hi=None):
    """table[key], or default when absent. It must be the same type as the
    default (a whole number is fine where a number is expected), within lo..hi,
    so a wrong value is a config error and not a traceback mid-run."""
    v = table.get(key, default)
    kind = type(default)
    if isinstance(v, bool) or not isinstance(v, (int, float) if kind is float else kind):
        raise ConfigError(f"{name}: {key} must be {_KINDS[kind]}, got {v!r}")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        bound = f"at least {lo}" if hi is None else f"between {lo} and {hi}"
        raise ConfigError(f"{name}: {key} must be {bound}, got {v!r}")
    return float(v) if kind is float else v


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"no config at {path}. Copy config.example.toml to config.toml "
                          "and fill it in.")
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}")
    except OSError as exc:
        # e.g. macOS blocking a background job from ~/Documents
        raise ConfigError(f"can't read {path}: {exc}")

    dest = _table(raw, "destination", "destination")
    if not dest.get("code") or not isinstance(dest["code"], str):
        raise ConfigError("[destination] needs a code, e.g. code = \"LHR\"")

    raw_origins = raw.get("origins") or []
    if not isinstance(raw_origins, list) or not all(isinstance(o, dict) for o in raw_origins):
        raise ConfigError("write each origin as its own [[origins]] section, with double brackets")
    origins = []
    for o in raw_origins:
        if not o.get("code") or not isinstance(o["code"], str):
            raise ConfigError("every [[origins]] entry needs a code, e.g. code = \"JFK\"")
        code = o["code"].upper()
        where = f"origin {code}"
        alert = _opt(o, "alert", 0, where, lo=1) if "alert" in o else None
        origins.append(Origin(code, _opt(o, "city", code, where), alert))
    if not origins:
        raise ConfigError("add at least one [[origins]] entry")

    dates = _table(raw, "dates", "dates")
    for k in ("outbound_from", "outbound_to"):
        if k not in dates:
            raise ConfigError(f"[dates] is missing {k}")
    out_dates = _date_range(dates["outbound_from"], dates["outbound_to"], "outbound")
    has_from, has_to = "return_from" in dates, "return_to" in dates
    if has_from != has_to:
        raise ConfigError("[dates] needs both return_from and return_to, or neither "
                          "(neither means a one-way watch)")
    back_dates = (_date_range(dates["return_from"], dates["return_to"], "return")
                  if has_from else None)
    if back_dates and back_dates[0] < out_dates[0]:
        raise ConfigError("the return window starts before the outbound window")

    search = _table(raw, "search", "search")
    notify = _table(raw, "notify", "notify")
    storage = _table(raw, "storage", "storage")
    dest_code = dest["code"].upper()

    cfg = Config(
        path=path,
        dest_code=dest_code,
        dest_city=_opt(dest, "city", dest_code, "destination"),
        origins=origins,
        out_dates=out_dates,
        back_dates=back_dates,
        currency=_opt(search, "currency", "USD", "search"),
        adults=_opt(search, "adults", 1, "search", lo=1, hi=9),  # Google's limit is 9
        seat=_opt(search, "seat", "economy", "search"),
        max_stops=(_opt(search, "max_stops", 0, "search", lo=0)
                   if "max_stops" in search else None),
        watch_airline=_opt(search, "watch_airline", "", "search"),
        results_per_query=_opt(search, "results_per_query", 12, "search", lo=1),
        max_workers=_opt(search, "max_workers", 4, "search", lo=1),
        stagger=_opt(search, "stagger_seconds", 0.5, "search", lo=0),
        origin_pause=_opt(search, "origin_pause_seconds", 15.0, "search", lo=0),
        retry_pause=_opt(search, "retry_pause_seconds", 25.0, "search", lo=0),
        min_success_frac=_opt(search, "min_success_fraction", 0.75, "search", lo=0, hi=1),
        trend_runs=_opt(search, "trend_runs", 10, "search", lo=1),
        notify_method=_opt(notify, "method", "print", "notify"),
        notify_to=_opt(notify, "to", "", "notify"),
        smtp=_table(notify, "smtp", "notify.smtp"),
        # a relative log path is relative to the config file, not the cwd, so a
        # scheduler running from / still finds the same history
        log_path=(path.parent / _opt(storage, "log", "flight_watch_log.csv", "storage")).resolve(),
    )

    if cfg.seat not in ("economy", "premium-economy", "business", "first"):
        raise ConfigError("search.seat must be economy, premium-economy, business or first")
    if cfg.notify_method not in ("print", "smtp", "mail_app"):
        raise ConfigError("notify.method must be print, smtp or mail_app")
    if cfg.notify_method in ("smtp", "mail_app") and not cfg.notify_to:
        raise ConfigError(f"notify.method = {cfg.notify_method} needs notify.to")
    if cfg.notify_method == "smtp":
        for k in ("host", "user"):
            if not cfg.smtp.get(k):
                raise ConfigError(f"[notify.smtp] is missing {k}")
            _opt(cfg.smtp, k, "", "notify.smtp")
        _opt(cfg.smtp, "from", "", "notify.smtp")
        _opt(cfg.smtp, "port", 587, "notify.smtp", lo=1, hi=65535)
    return cfg


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------

def price_combo(cfg: Config, origin: str, out_date: str, back_date: str | None):
    """(cheapest_overall, cheapest_on_watch_airline) for one origin + date combo.
    Either can be None if nothing came back."""
    from fast_flights import FlightQuery, Passengers, create_query, get_flights

    legs = [FlightQuery(date=out_date, from_airport=origin, to_airport=cfg.dest_code)]
    if back_date:
        legs.append(FlightQuery(date=back_date, from_airport=cfg.dest_code,
                                to_airport=origin))
    q = create_query(flights=legs, trip="one-way" if cfg.one_way else "round-trip",
                     seat=cfg.seat, passengers=Passengers(adults=cfg.adults),
                     currency=cfg.currency, max_stops=cfg.max_stops)
    overall = airline = None
    needle = cfg.watch_airline.lower()
    try:
        for f in get_flights(q)[:cfg.results_per_query]:
            try:
                pr = int(f.price)
            except (TypeError, ValueError):
                continue
            if pr <= 0:
                continue
            if overall is None or pr < overall:
                overall = pr
            if needle and any(needle in a.lower() for a in f.airlines):
                if airline is None or pr < airline:
                    airline = pr
    except Exception as exc:  # noqa: BLE001
        # never crash the run, but never fail silently either
        sys.stderr.write(f"\n[fetch-error] {origin}->{cfg.dest_code} "
                         f"{out_date}/{back_date or '-'}: {type(exc).__name__}: {exc}\n")
    return overall, airline


def _price_many(cfg: Config, origin: str, combos, label: str) -> dict:
    results, done = {}, 0
    with cf.ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
        futures = {}
        for combo in combos:
            futures[ex.submit(price_combo, cfg, origin, *combo)] = combo
            time.sleep(cfg.stagger)  # Google throttles bursts more than totals
        for fut in cf.as_completed(futures):
            combo = futures[fut]
            try:
                results[combo] = fut.result()
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"\n[worker-error] {origin} {combo}: {exc}\n")
                results[combo] = (None, None)
            done += 1
            sys.stderr.write(f"\r  {origin}: {label} {done}/{len(combos)}      ")
            sys.stderr.flush()
    sys.stderr.write("\r" + " " * 60 + "\r")
    return results


def scan(cfg: Config, origin: str) -> dict:
    """Price the whole date grid for one origin -> {combo: (overall, airline)}.

    Combos that come back empty get one retry after a cool-off: at this volume
    throttling is the normal failure, and it is transient. A completely empty
    grid is not retried, since that points at something structural (a bad
    airport code, no service on the route, a broken scraper)."""
    combos = cfg.combos
    results = _price_many(cfg, origin, combos, "pricing")
    missed = [c for c in combos if results[c][0] is None]
    if missed and len(missed) < len(combos):
        sys.stderr.write(f"[retry] {origin}: {len(missed)}/{len(combos)} empty, "
                         f"retrying in {cfg.retry_pause:g}s\n")
        time.sleep(cfg.retry_pause)
        for combo, val in _price_many(cfg, origin, missed, "retry").items():
            if val[0] is not None:
                results[combo] = val
    elif missed:
        sys.stderr.write(f"[retry] {origin}: whole grid empty, not retrying "
                         "(check the airport codes, or the route may not exist)\n")
    return results


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------

def read_rows(cfg: Config) -> list[dict]:
    """Logged rows for this destination and the origins currently configured."""
    if not cfg.log_path.exists():
        return []
    watched = {o.code for o in cfg.origins}
    with cfg.log_path.open(newline="") as fh:
        return [r for r in csv.DictReader(fh)
                if r.get("destination") == cfg.dest_code and r.get("origin") in watched]


def append_rows(cfg: Config, ts: str, results: dict) -> None:
    # an empty file needs the header too, or every row after it reads as garbage
    new = not cfg.log_path.exists() or cfg.log_path.stat().st_size == 0
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg.log_path.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(HEADER)
        for origin, res in results.items():
            for (out_d, back_d), (overall, airline) in res.items():
                w.writerow([ts, origin, cfg.dest_code, out_d, back_d or "",
                            "" if overall is None else overall,
                            "" if airline is None else airline])


def _price(row: dict, key: str) -> int | None:
    try:
        return int(row.get(key) or "")
    except ValueError:
        return None


def run_bests(rows: list[dict], key: str = "overall_price") -> dict:
    """{ts: {origin: (price, out_date, back_date)}}: each run's best fare per origin."""
    runs: dict = {}
    for r in rows:
        p = _price(r, key)
        if p is None:
            continue
        per = runs.setdefault(r["ts"], {})
        if r["origin"] not in per or p < per[r["origin"]][0]:
            per[r["origin"]] = (p, r["out_date"], r["back_date"])
    return runs


def all_time_lows(rows: list[dict], key: str = "overall_price") -> dict:
    """{origin: (price, out_date, back_date, ts)}"""
    lows: dict = {}
    for r in rows:
        p = _price(r, key)
        if p is not None and (r["origin"] not in lows or p < lows[r["origin"]][0]):
            lows[r["origin"]] = (p, r["out_date"], r["back_date"], r["ts"])
    return lows


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _d(iso: str | None) -> str:
    """2027-03-14 -> Mar 14"""
    if not iso:
        return ""
    try:
        d = date.fromisoformat(iso)
        return f"{d.strftime('%b')} {d.day}"
    except ValueError:
        return iso


def _dates(out_d: str | None, back_d: str | None) -> str:
    return f"{_d(out_d)} → {_d(back_d)}" if back_d else _d(out_d)


def _money(cfg: Config, amount: int) -> str:
    return f"${amount}" if cfg.currency == "USD" else f"{amount} {cfg.currency}"


def summarize(cfg: Config, results: dict) -> tuple[dict, list[str]]:
    """Collapse each origin's grid to its best fare -> (today, unhealthy)."""
    today, unhealthy = {}, []
    total = len(cfg.combos)
    for o in cfg.origins:
        res = results.get(o.code, {})
        hits = [(p, od, bd) for (od, bd), (p, _a) in res.items() if p is not None]
        air = [(p, od, bd) for (od, bd), (_o, p) in res.items() if p is not None]
        best = min(hits) if hits else (None, None, None)
        today[o.code] = {"price": best[0], "out": best[1], "back": best[2],
                         "airline": min(air)[0] if air else None, "priced": len(hits)}
        if len(hits) < cfg.min_success_frac * total:
            unhealthy.append(f"{o.code}: only {len(hits)}/{total} date combos priced")
    return today, unhealthy


def build_report(cfg: Config, today: dict, prev_rows: list[dict],
                 unhealthy: list[str]) -> tuple[str, str]:
    """The one message per run -> (subject, body).

    `today` is {origin_code: {price, out, back, airline, priced}} for this run and
    `prev_rows` is the log as it stood before this run."""
    lows = all_time_lows(prev_rows)
    prev_runs = run_bests(prev_rows)
    prev_run = prev_runs[max(prev_runs)] if prev_runs else {}
    live = {c: v for c, v in today.items() if v["price"] is not None}
    single = len(cfg.origins) == 1
    route = (f"{cfg.origins[0].city} → {cfg.dest_city}" if single else cfg.dest_city)
    prior_low = min((v[0] for v in lows.values()), default=None)
    m = lambda a: _money(cfg, a)  # noqa: E731

    # ---- subject: the one number worth knowing, and which way it moved ----
    if not live:
        subject = f"⚠️ {route} watch: no prices this run"
    else:
        code = min(live, key=lambda c: live[c]["price"])
        b = live[code]
        prev_best = min((v[0] for v in prev_run.values()), default=None)
        if prior_low is not None and b["price"] < prior_low:
            move = " ▼ NEW LOW"
        elif prev_best is None:
            move = ""
        else:
            delta = b["price"] - prev_best
            move = (f" ▲ +{m(delta)}" if delta > 0
                    else f" ▼ -{m(-delta)}" if delta < 0 else " = flat")
        where = "" if single else f" from {code}"
        subject = f"✈️ {route} {m(b['price'])}{where} ({_dates(b['out'], b['back'])}){move}"

    # ---- headline ----
    lines = []
    if live:
        code = min(live, key=lambda c: live[c]["price"])
        b = live[code]
        where = "" if single else f" from {code}"
        headline = route if single else f"to {cfg.dest_city}"
        lines.append(f"Cheapest {headline} right now: {m(b['price'])}{where}")
        lines.append(f"  out {b['out']}" + (f"  ·  back {b['back']}" if b["back"] else ""))
        if prior_low is None:
            lines.append("  First run, so there is no history to compare against yet.")
        elif b["price"] < prior_low:
            lines.append(f"  ▼ The cheapest it has ever been (previous best {m(prior_low)}).")
        else:
            lines.append(f"  All-time cheapest seen: {m(prior_low)}. "
                         f"You are {m(b['price'] - prior_low)} above that.")
    else:
        lines.append("No price came back this run. Google Flights is probably throttling, "
                     "or the scraper broke. Today's numbers are missing, not zero.")

    # ---- per-origin table ----
    lines += ["", f"{'FROM':<5} {'BEST':>9}  {'DATES':<16} {'vs LAST':>8}  "
                  f"{'ALLTIME':>9}  {'TARGET':>9}", "-" * 64]
    for o in sorted(cfg.origins, key=lambda x: today[x.code]["price"] or 10**9):
        t = today[o.code]
        if t["price"] is None:
            lines.append(f"{o.code:<5} {'—':>9}  no price returned")
            continue
        prev = prev_run.get(o.code)
        vs = "new" if not prev else (
            "flat" if t["price"] == prev[0] else f"{t['price'] - prev[0]:+d}")
        low = lows.get(o.code)
        hit = " **" if o.alert is not None and t["price"] <= o.alert else ""
        lines.append(f"{o.code:<5} {m(t['price']):>9}  {_dates(t['out'], t['back']):<16} "
                     f"{vs:>8}  {m(low[0]) if low else '—':>9}  "
                     f"{m(o.alert) if o.alert is not None else '—':>9}{hit}")
    if any(o.alert is not None for o in cfg.origins):
        lines += ["", "** = at or below your target for that airport."]

    # ---- targets hit ----
    hits = [o for o in cfg.origins if o.alert is not None
            and today[o.code]["price"] is not None and today[o.code]["price"] <= o.alert]
    if hits:
        lines += ["", "TARGET HIT, worth acting on:"]
        for o in hits:
            t = today[o.code]
            extra = ""
            if cfg.watch_airline:
                a = t["airline"]
                extra = f"  ·  cheapest {cfg.watch_airline}: {m(a) if a else 'none seen'}"
            lines.append(f"  {o.code} {m(t['price'])} <= {m(o.alert)}  ·  "
                         f"{_dates(t['out'], t['back'])}{extra}")

    # ---- watched airline ----
    if cfg.watch_airline:
        lines += ["", f"Cheapest on {cfg.watch_airline} (as Google Flights shows it):"]
        for o in cfg.origins:
            a = today[o.code]["airline"]
            lines.append(f"  {o.code}  {m(a) if a else 'none seen'}")
        lines.append("  Airline sites often price differently. Check theirs before booking.")

    # ---- trend ----
    if prev_runs:
        lines += ["", f"Trend, cheapest fare over the last {cfg.trend_runs} runs:"]
        for ts in sorted(prev_runs)[-cfg.trend_runs:]:
            per = prev_runs[ts]
            code = min(per, key=lambda c: per[c][0])
            p, od, bd = per[code]
            where = "" if single else f" {code}"
            lines.append(f"  {ts[:10]}  {m(p):>9}{where}  ({_dates(od, bd or None)})")

    # ---- scrape health, folded in rather than sent separately ----
    if unhealthy:
        lines += ["", "SCRAPE HEALTH: these came back thin, so the numbers above may "
                      "miss cheaper fares:"] + [f"  {u}" for u in unhealthy]

    grid = (f"{_d(cfg.out_dates[0])}–{_d(cfg.out_dates[-1])} outbound"
            + ("" if cfg.one_way else f" × {_d(cfg.back_dates[0])}–{_d(cfg.back_dates[-1])} return"))
    lines += ["", f"Grid: {grid}, {len(cfg.combos)} date combos per airport, "
                  f"{cfg.seat}, {cfg.adults} adult(s)."]
    return subject, "\n".join(lines)


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------

MAIL_APP_SCRIPT = '''
on run argv
  tell application "Mail"
    set m to make new outgoing message with properties {subject:item 1 of argv, content:item 2 of argv, visible:false}
    tell m
      make new to recipient at end of to recipients with properties {address:item 3 of argv}
      send
    end tell
  end tell
end run
'''


def deliver(cfg: Config, subject: str, body: str) -> bool:
    if cfg.notify_method == "print":
        print(f"Subject: {subject}\n\n{body}")
        return True

    if cfg.notify_method == "mail_app":
        # macOS only. Text goes in as argv, never spliced into the script, so
        # quotes and backslashes in a report can't break or alter the AppleScript.
        # Success means Mail accepted the message; it may sit in the outbox a
        # few minutes before it actually leaves.
        try:
            r = subprocess.run(["osascript", "-", subject, body, cfg.notify_to],
                               input=MAIL_APP_SCRIPT, text=True, capture_output=True,
                               timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            sys.stderr.write(f"[notify-error] {type(exc).__name__}: {exc}\n")
            return False
        if r.returncode != 0:
            sys.stderr.write(f"[notify-error] osascript: {r.stderr.strip()[:400]}\n")
            return False
        return True

    # smtp
    password = os.environ.get(SMTP_PASSWORD_ENV) or cfg.smtp.get("password")
    if not password:
        sys.stderr.write(f"[notify-error] set {SMTP_PASSWORD_ENV} to your SMTP password\n")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.smtp.get("from", cfg.smtp["user"])
    msg["To"] = cfg.notify_to
    msg.set_content(body)
    port = int(cfg.smtp.get("port", 587))
    # smtplib's own default context skips certificate checks, which would hand
    # the password to anyone in the middle. Always verify.
    tls = ssl.create_default_context()
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(cfg.smtp["host"], port, timeout=60, context=tls)
        else:
            server = smtplib.SMTP(cfg.smtp["host"], port, timeout=60)
        with server:
            if port != 465:
                server.starttls(context=tls)
            server.login(cfg.smtp["user"], password)
            server.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        sys.stderr.write(f"[notify-error] smtp: {type(exc).__name__}: {exc}\n")
        return False


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_history(cfg: Config) -> int:
    rows = read_rows(cfg)
    if not rows:
        print(f"nothing logged yet at {cfg.log_path}")
        return 0
    runs, lows = run_bests(rows), all_time_lows(rows)
    for o in cfg.origins:
        seen = sorted((ts, per[o.code]) for ts, per in runs.items() if o.code in per)
        print(f"\n{o.code} ({o.city}) → {cfg.dest_code}: {len(seen)} run(s)")
        for ts, (p, od, bd) in seen:
            print(f"  {ts[:16]}  {_money(cfg, p):>9}  ({_dates(od, bd or None)})")
        if o.code in lows:
            p, od, bd, ts = lows[o.code]
            target = f", target {_money(cfg, o.alert)}" if o.alert is not None else ""
            print(f"  all-time low {_money(cfg, p)} ({_dates(od, bd or None)}), "
                  f"seen {ts[:10]}{target}")
    return 0


def cmd_test(cfg: Config) -> int:
    ok = deliver(cfg, f"✈️ {cfg.dest_city} flight watch: test",
                 "This is a test. If you can read it, delivery works. The real report "
                 "arrives once per run with the cheapest fare, the move since last "
                 "time, and the trend.")
    print("test sent" if ok else "test failed, see the error above")
    return 0 if ok else 1


def cmd_run(cfg: Config, dry_run: bool) -> int:
    if not cfg.combos:
        sys.stderr.write("config error: every outbound date is already in the past. Move "
                         "[dates] forward, or stop the schedule if the trip is booked.\n")
        return 2
    try:
        import fast_flights  # noqa: F401
    except ImportError as exc:
        # checked up front: otherwise every fetch fails and the report blames Google
        sys.stderr.write(f"setup error: {exc} (python: {sys.executable}). "
                         "Install the requirements: pip install -r requirements.txt\n")
        return 2
    prev_rows = read_rows(cfg)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = {}
    for i, o in enumerate(cfg.origins):
        if i:
            time.sleep(cfg.origin_pause)  # breathe between grids
        results[o.code] = scan(cfg, o.code)

    today, unhealthy = summarize(cfg, results)
    for o in cfg.origins:
        t = today[o.code]
        sys.stderr.write(f"{o.code}->{cfg.dest_code}: best {t['price']} "
                         f"({t['out']}/{t['back']}), {t['priced']}/{len(cfg.combos)} priced\n")

    subject, body = build_report(cfg, today, prev_rows, unhealthy)
    if dry_run:
        print(f"[dry-run] nothing logged, nothing sent\n\nSubject: {subject}\n\n{body}")
        return 0
    append_rows(cfg, ts, results)
    return 0 if deliver(cfg, subject, body) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily flight price watch over a date grid.")
    ap.add_argument("--config", default="config.toml", type=Path,
                    help="path to config file (default: ./config.toml)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true",
                   help="price the grid and print the report, but log and send nothing")
    g.add_argument("--history", action="store_true", help="print the logged history")
    g.add_argument("--test-notify", action="store_true", help="send a test report")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config.expanduser().resolve())
    except ConfigError as exc:
        sys.stderr.write(f"config error: {exc}\n")
        return 2
    if args.history:
        return cmd_history(cfg)
    if args.test_notify:
        return cmd_test(cfg)
    return cmd_run(cfg, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
