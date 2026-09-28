#!/usr/bin/env python3
"""
Risky account tracker - scraper.

Reads the "Live Trading Stats" table on algotradingspace.com, keeps rows whose
Strategy starts with "Risky", applies the business rules, and updates:
  data/accounts.json   current state of every Risky account (active and blown)
  data/daily_log.csv   one row per account per run (history is never deleted)
  data/status.json     result of the last run (the dashboard shows failures)

Usage:
  python scraper/scrape.py              # normal run (requests + BeautifulSoup)
  python scraper/scrape.py --browser    # fallback: render the page with Playwright
  python scraper/scrape.py --html page.html --now 2026-09-28T23:00:00Z   # offline test
"""
import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
URL = "https://algotradingspace.com/trading-stats/live"
NY = ZoneInfo("America/New_York")

DEPOSIT = 1000.0              # Rule 1: every account is opened with $1,000
BLOWN_BALANCE_BELOW = 50.0    # Blown if the closed balance drops under this
MISSING_RUNS_TO_BLOW = 2      # Blown after missing this many runs in a row (1 = immediately)
SESSION_SPLIT_HOUR = 13       # NY hour: runs before this are "AM", after are "PM"

REQUIRED_COLUMNS = [
    "Strategy", "Type", "Broker", "Deposit", "Balance", "Floating", "Withdrawals",
    "Gain %", "Monthly %", "Trades", "Days", "Profit Factor", "Max DD %",
]

CSV_FIELDS = [
    "snapshot", "date_ny", "session", "run_utc", "run_ny",
    "account", "account_key", "site_id", "status", "type", "broker", "open_date",
    "deposit", "balance", "floating", "withdrawals", "net_pl",
    "gain_pct", "monthly_pct", "trades", "days", "profit_factor", "max_dd_pct",
    "site_deposit", "flag",
]

# Figures compared to decide "No change / possibly cached"
COMPARE_FIELDS = ["account_key", "status", "balance", "floating", "withdrawals", "trades",
                  "days", "gain_pct", "monthly_pct", "profit_factor", "max_dd_pct"]

log = logging.getLogger("tracker")


class ScrapeError(Exception):
    """Anything that should fail the run loudly."""


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def num(text):
    """'$1,289.74' -> 1289.74, '-$225.13' -> -225.13, '+ 143.95 %' -> 143.95, '40 days' -> 40."""
    if text is None:
        return None
    t = text.replace(",", "").replace("$", "").replace("%", "").replace("days", "")
    t = t.replace("day", "").replace(" ", "").replace("/", "").replace("\u2212", "-")
    if t in ("", "-", "\u2014", "\u2013", "N/A"):
        return None
    m = re.search(r"[-+]?\d+(\.\d+)?", t)
    return float(m.group(0)) if m else None


def ordinal(n):
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def atomic_write(path, text):
    """Write to a temp file then swap it in, so a crash never leaves a half-written file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def net_pl(a):
    return round((a.get("balance") or 0) + (a.get("withdrawals") or 0) - a.get("deposit", DEPOSIT), 2)


# ----------------------------------------------------------------------------
# Fetching
# ----------------------------------------------------------------------------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def fetch_with_requests():
    session = requests.Session()
    retry = Retry(total=5, connect=5, read=5, backoff_factor=3,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], raise_on_status=False)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    last_err = None
    for attempt in range(1, 4):
        try:
            # The _ts parameter stops any cache between us and the site from serving an old copy
            r = session.get(URL, params={"_ts": int(time.time())}, headers=HEADERS, timeout=45)
            log.info("HTTP %s, %s bytes (attempt %d)", r.status_code, len(r.text), attempt)
            if r.status_code != 200:
                raise ScrapeError(f"Site returned HTTP {r.status_code}")
            if "<table" not in r.text:
                raise ScrapeError("Page loaded but has no <table> (it may now be built by JavaScript)")
            return r.text
        except (requests.RequestException, ScrapeError) as e:
            last_err = e
            log.warning("Attempt %d failed: %s", attempt, e)
            time.sleep(10 * attempt)
    raise ScrapeError(f"Could not download the page after retries: {last_err}")


def fetch_with_browser():
    from playwright.sync_api import sync_playwright  # installed only when needed
    last_err = None
    for attempt in range(1, 4):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(user_agent=HEADERS["User-Agent"])
                page.goto(f"{URL}?_ts={int(time.time())}", wait_until="domcontentloaded", timeout=90000)
                page.wait_for_selector("table tr td", timeout=60000)
                html = page.content()
                browser.close()
                log.info("Browser render OK (%s bytes, attempt %d)", len(html), attempt)
                return html
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("Browser attempt %d failed: %s", attempt, e)
            time.sleep(15 * attempt)
    raise ScrapeError(f"Browser could not load the table: {last_err}")


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------
def clean_header(text):
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z% ]", "", text)).strip()


def parse_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for t in soup.find_all("table"):
        heads = [clean_header(th.get_text(" ", strip=True)) for th in t.find_all("th")]
        if "Strategy" in heads:
            table, headers = t, heads
            break
    if table is None:
        raise ScrapeError("Could not find the Live Trading Stats table (no 'Strategy' column)")

    col = {h: i for i, h in enumerate(headers)}
    missing = [c for c in REQUIRED_COLUMNS if c not in col]
    if missing:
        raise ScrapeError(f"Site layout changed - columns not found: {', '.join(missing)}. "
                          f"Columns seen: {headers}")

    data_rows = [tr for tr in table.find_all("tr") if tr.find_all("td")]
    if len(data_rows) < 3:
        raise ScrapeError(f"Table has only {len(data_rows)} data rows - page probably did not load fully")

    rows = []
    for tr in data_rows:
        tds = tr.find_all("td")
        if len(tds) < len(REQUIRED_COLUMNS):
            continue

        def cell(name, sep=" "):
            return tds[col[name]].get_text(sep, strip=True)

        strategy = cell("Strategy")
        if not re.match(r"^\s*Risky\b", strategy, re.I):
            continue
        m = re.match(r"^\s*Risky\s*(\d+)", strategy, re.I)
        base = f"Risky {m.group(1)}" if m else "Risky"
        label = strategy.split(" - ", 1)[1] if " - " in strategy else ""
        label = re.sub(r"\s*\(\d+(st|nd|rd|th)\)\s*$", "", label).strip()

        # The set-files link carries the account's permanent ID
        raw = str(tr)
        idm = re.search(r"set-files(?:%2F|/)(\d{4,})", raw)
        site_id = idm.group(1) if idm else None

        rows.append({
            "site_name": strategy,
            "base": base,
            "label": label,
            "site_id": site_id,
            "type": cell("Type", " / "),
            "broker": cell("Broker"),
            "site_deposit": num(cell("Deposit")),
            "balance": num(cell("Balance")),
            "floating": num(cell("Floating")),
            "withdrawals": num(cell("Withdrawals")) or 0.0,
            "gain_pct": num(cell("Gain %", "")),
            "monthly_pct": num(cell("Monthly %", "")),
            "trades": int(num(cell("Trades")) or 0),
            "days": int(num(cell("Days")) or 0),
            "profit_factor": num(cell("Profit Factor")),
            "max_dd_pct": num(cell("Max DD %")),
        })
        if rows[-1]["balance"] is None:
            raise ScrapeError(f"Could not read the Balance of '{strategy}'")
    return rows


# ----------------------------------------------------------------------------
# State handling
# ----------------------------------------------------------------------------
def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_log(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def blow(a, when, reason):
    a["status"] = "Blown"
    a["blown_date"] = when.isoformat() if isinstance(when, date) else when
    a["blown_reason"] = reason
    a["balance"] = 0.0          # Rule 3: closed balance = $0, loss = the deposit
    a["floating"] = 0.0
    a["missing_runs"] = 0
    log.warning("BLOWN: %s (%s)", a["name"], reason)


def new_account(row, accounts, today):
    same = [a for a in accounts if a["base"] == row["base"]]
    if same:
        # Rule 4: a reused number becomes "Risky N (2nd)", "(3rd)", ...
        for a in same:
            if a["name"] == row["base"]:
                a["name"] = f"{row['base']} (1st)"
        name = f"{row['base']} ({ordinal(len(same) + 1)})"
    else:
        name = row["base"]
    open_date = today - timedelta(days=row["days"] or 0)  # Rule 5, set once
    key = row["site_id"] or re.sub(r"[^a-z0-9]+", "-", f"{name}-{open_date}".lower())
    log.info("NEW account: %s (site id %s, opened %s)", name, row["site_id"], open_date)
    return {
        "key": key, "name": name, "base": row["base"], "site_id": row["site_id"],
        "label": row["label"], "status": "Active", "open_date": open_date.isoformat(),
        "deposit": DEPOSIT, "first_seen": today.isoformat(),
        "missing_runs": 0, "missing_since": None, "blown_date": None, "blown_reason": None,
    }


def find_account(row, accounts, today):
    if row["site_id"]:
        for a in accounts:
            if a.get("site_id") == row["site_id"]:
                return a
    # An account we know about but whose site ID was never recorded (or the ID vanished
    # from the page): match it by number, preferring the one whose open date fits.
    candidates = [a for a in accounts if a["base"] == row["base"] and a["status"] != "Blown"
                  and (not a.get("site_id") or not row["site_id"])]
    if not candidates:
        return None
    est_open = today - timedelta(days=row["days"] or 0)
    candidates.sort(key=lambda a: abs((date.fromisoformat(a["open_date"]) - est_open).days))
    best = candidates[0]
    if abs((date.fromisoformat(best["open_date"]) - est_open).days) <= 3 or len(candidates) == 1:
        if row["site_id"] and not best.get("site_id"):
            best["site_id"] = row["site_id"]
            log.info("Linked %s to site id %s", best["name"], row["site_id"])
        return best
    return None


def page_is_older(rows, accounts):
    """Trades and Days only go up. If the page shows lower numbers than we already
    stored, it is an old cached copy and must not overwrite newer data."""
    for r in rows:
        for a in accounts:
            if r["site_id"] and a.get("site_id") == r["site_id"] and a["status"] != "Blown":
                if a.get("trades") is not None and r["trades"] < a["trades"]:
                    return f"{a['name']}: trades {r['trades']} < stored {a['trades']}"
                if a.get("days") is not None and r["days"] < a["days"]:
                    return f"{a['name']}: days {r['days']} < stored {a['days']}"
    return None


def update_accounts(rows, accounts, today, now_iso):
    notes = []
    seen = set()
    for r in rows:
        a = find_account(r, accounts, today)
        if a is None:
            a = new_account(r, accounts, today)
            accounts.append(a)
        seen.add(a["key"])
        if a["status"] == "Blown":
            notes.append(f"{a['name']} is still listed but was already Blown - ignored")
            log.info("Ignoring listing of already-blown %s", a["name"])
            continue
        for k in ("type", "broker", "site_deposit", "balance", "floating", "withdrawals",
                  "gain_pct", "monthly_pct", "trades", "days", "profit_factor", "max_dd_pct",
                  "label", "site_name"):
            a[k] = r[k]
        a["deposit"] = DEPOSIT
        a["status"] = "Active"
        a["missing_runs"] = 0
        a["missing_since"] = None
        a["last_seen"] = now_iso
        if r["balance"] < BLOWN_BALANCE_BELOW:
            blow(a, today, f"Closed balance ${r['balance']:,.2f} is under ${BLOWN_BALANCE_BELOW:,.0f}")
            notes.append(f"{a['name']} blown (balance under ${BLOWN_BALANCE_BELOW:,.0f})")

    for a in accounts:
        if a["status"] == "Blown" or a["key"] in seen:
            continue
        a["missing_runs"] = a.get("missing_runs", 0) + 1
        a["missing_since"] = a.get("missing_since") or today.isoformat()
        if a["missing_runs"] >= MISSING_RUNS_TO_BLOW:
            blow(a, date.fromisoformat(a["missing_since"]), "No longer listed on the site")
            notes.append(f"{a['name']} blown (no longer listed)")
        else:
            a["status"] = "Missing"
            notes.append(f"{a['name']} not listed this run - will be marked Blown if still "
                         f"missing next run")
            log.warning("%s not on the page (miss %d of %d)", a["name"], a["missing_runs"],
                        MISSING_RUNS_TO_BLOW)
    return notes


def build_log_rows(accounts, snapshot, date_ny, session, run_utc, run_ny, flag):
    out = []
    for a in sorted(accounts, key=lambda x: (x["open_date"], x["name"])):
        out.append({
            "snapshot": snapshot, "date_ny": date_ny, "session": session,
            "run_utc": run_utc, "run_ny": run_ny,
            "account": a["name"], "account_key": a["key"], "site_id": a.get("site_id") or "",
            "status": a["status"], "type": a.get("type") or "", "broker": a.get("broker") or "",
            "open_date": a["open_date"], "deposit": fmt(a["deposit"]),
            "balance": fmt(a.get("balance")), "floating": fmt(a.get("floating")),
            "withdrawals": fmt(a.get("withdrawals") or 0.0), "net_pl": fmt(net_pl(a)),
            "gain_pct": fmt(a.get("gain_pct")), "monthly_pct": fmt(a.get("monthly_pct")),
            "trades": fmt(a.get("trades")), "days": fmt(a.get("days")),
            "profit_factor": fmt(a.get("profit_factor")), "max_dd_pct": fmt(a.get("max_dd_pct")),
            "site_deposit": fmt(a.get("site_deposit")), "flag": flag,
        })
    return out


def same_figures(new_rows, old_rows):
    key = lambda rows: sorted(tuple(r.get(f, "") for f in COMPARE_FIELDS) for r in rows)
    return bool(old_rows) and key(new_rows) == key(old_rows)


# ----------------------------------------------------------------------------
# Main run
# ----------------------------------------------------------------------------
def run(rows, data_dir, now_utc, method):
    acc_path = os.path.join(data_dir, "accounts.json")
    log_path = os.path.join(data_dir, "daily_log.csv")

    state = load_json(acc_path, {"accounts": []})
    accounts = state["accounts"]
    history = load_log(log_path)

    now_ny = now_utc.astimezone(NY)
    today = now_ny.date()
    session = "AM" if now_ny.hour < SESSION_SPLIT_HOUR else "PM"
    snapshot = f"{today.isoformat()} {session}"
    run_utc = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    run_ny = now_ny.strftime("%Y-%m-%d %H:%M %Z")

    older = page_is_older(rows, accounts)
    if older:
        flag = "Stale page (older than last run) - previous figures kept"
        notes = [f"Site served an older copy ({older})"]
        log.warning("Page is older than stored data (%s). Keeping previous figures.", older)
    else:
        notes = update_accounts(rows, accounts, today, now_utc.isoformat(timespec="seconds"))
        flag = ""

    for a in accounts:
        a["net_pl"] = net_pl(a)

    kept = [r for r in history if r["snapshot"] != snapshot]   # replace a re-run of the same slot
    new_rows = build_log_rows(accounts, snapshot, today.isoformat(), session, run_utc, run_ny, flag)

    if not flag:
        prev_snaps = [r["snapshot"] for r in kept]
        if prev_snaps:
            last = prev_snaps[-1]
            if same_figures(new_rows, [r for r in kept if r["snapshot"] == last]):
                flag = ("No change (market closed)" if now_ny.weekday() >= 5
                        else "No change / possibly cached")
                for r in new_rows:
                    r["flag"] = flag
                log.warning("All figures identical to %s -> flagged '%s'", last, flag)

    # Write CSV (history kept, this snapshot replaced/added)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, lineterminator="\n")
    w.writeheader()
    for r in kept + new_rows:
        w.writerow({f: r.get(f, "") for f in CSV_FIELDS})
    atomic_write(log_path, buf.getvalue())

    state.update({
        "updated_utc": now_utc.isoformat(timespec="seconds"),
        "updated_ny": now_ny.isoformat(timespec="seconds"),
        "snapshot": snapshot, "flag": flag, "notes": notes, "source": URL,
        "rules": {"deposit": DEPOSIT, "blown_balance_below": BLOWN_BALANCE_BELOW,
                  "missing_runs_to_blow": MISSING_RUNS_TO_BLOW},
        "accounts": accounts,
    })
    atomic_write(acc_path, json.dumps(state, indent=2) + "\n")
    write_status(data_dir, ok=True, now_utc=now_utc, snapshot=snapshot, method=method,
                 message=flag or "OK", notes=notes)

    # Readable summary in the log and on the Actions run page
    dep = sum(a["deposit"] for a in accounts)
    bal = sum(a.get("balance") or 0 for a in accounts)
    wd = sum(a.get("withdrawals") or 0 for a in accounts)
    lines = [f"### Risky tracker - {snapshot} ({method})", "",
             "| Account | Status | Closed balance | Net P/L |", "|---|---|---:|---:|"]
    for a in sorted(accounts, key=lambda x: x["open_date"]):
        lines.append(f"| {a['name']} | {a['status']} | ${a.get('balance') or 0:,.2f} | "
                     f"{a['net_pl']:+,.2f} |")
    lines += ["", f"**Deposited ${dep:,.2f} - closed ${bal:,.2f} - net {bal + wd - dep:+,.2f}**"]
    if flag:
        lines.append(f"\n> {flag}")
    for n in notes:
        lines.append(f"- {n}")
    print("\n".join(lines))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def write_status(data_dir, ok, now_utc, snapshot=None, method=None, message="", notes=None,
                 error=None):
    path = os.path.join(data_dir, "status.json")
    status = load_json(path, {})
    status.update({"ok": ok, "last_attempt_utc": now_utc.isoformat(timespec="seconds"),
                   "message": message, "error": error})
    if ok:
        status.update({"last_success_utc": now_utc.isoformat(timespec="seconds"),
                       "snapshot": snapshot, "method": method, "notes": notes or []})
    atomic_write(path, json.dumps(status, indent=2) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", action="store_true", help="render the page with Playwright")
    ap.add_argument("--html", help="parse a saved HTML file instead of downloading (testing)")
    ap.add_argument("--now", help="pretend the run happens at this UTC time (testing)")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    now_utc = (datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now
               else datetime.now(timezone.utc))
    method = "saved file" if args.html else ("browser" if args.browser else "requests")
    try:
        if args.html:
            with open(args.html, encoding="utf-8") as f:
                html = f.read()
        elif args.browser:
            html = fetch_with_browser()
        else:
            html = fetch_with_requests()
        rows = parse_rows(html)
        log.info("Found %d Risky rows: %s", len(rows),
                 ", ".join(f"{r['site_name']} [{r['site_id']}] ${r['balance']:,.2f}" for r in rows))
        run(rows, args.data_dir, now_utc, method)
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("Run failed")
        print(f"::error title=Risky tracker failed ({method})::{e}")
        try:
            write_status(args.data_dir, ok=False, now_utc=now_utc, method=method,
                         message="Last run failed - showing previous data", error=str(e))
        except Exception:  # noqa: BLE001
            log.exception("Could not write status.json")
        return 1


if __name__ == "__main__":
    sys.exit(main())
