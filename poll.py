#!/usr/bin/env python3
"""
Vote Ledger poller — America's Favorite Couple

Watches the contest dashboard (which only ever shows the 3 most recent free
votes) and keeps a cumulative ledger in state.json, regenerating ledger.html.

Runs entirely on your machine. No API keys, no LLM, no per-check cost.

Usage:
    python3 poll.py --login     # one time: opens a browser so you can sign in
    python3 poll.py             # one check, then exit  (use with a scheduler)
    python3 poll.py --watch     # runs continuously, one check a minute
    python3 poll.py --rebuild   # regenerate ledger.html from state.json only
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
TEMPLATE = HERE / "template.html"
OUTPUT = HERE / "ledger.html"
PROFILE = HERE / "browser-profile"
LOGFILE = HERE / "poll.log"
LOCK = HERE / "poll.lock"

DASHBOARD = "https://americasfavcouple.org/dashboard"
TZ = ZoneInfo("America/New_York")

# Checks are free now that this runs locally, so watch mode just polls on a
# fixed pulse (--every, default 60s) instead of the adaptive 2-10 min cadence
# the hosted version used to ration its costs. A floor keeps it polite to the
# contest site — more than every 20s buys nothing anyway, since the page's
# own timestamps only resolve to the minute.
MIN_EVERY = 20
FAILURE_PAUSE = 300       # after a failed check, wait at least this long

# How far apart two sightings of the same vote can look. The dashboard reports
# relative times only, and a label coarsens as it ages ("21 hours ago" becomes
# "1 day ago"), so the same vote can appear to jump by hours. Tolerance has to
# scale with how coarse the label was, or old donations get logged twice.
TOLERANCE = {
    "second": 90 * 60,
    "minute": 90 * 60,
    "hour":   3 * 3600,
    "day":    36 * 3600,
    "week":   10 * 86400,
}
DEFAULT_TOLERANCE = 90 * 60

# ---------------------------------------------------------------- extraction

# The dashboard is server-rendered with no API behind it, so we read the two
# vote sections out of the page's text.
EXTRACT_JS = """
() => {
  const t = document.body.innerText;
  const f = t.split('RECENT FREE VOTES')[1] || '';
  const d = f.split('RECENT DONATION VOTES');
  return {
    free: (d[0] || '').trim(),
    donation: (d[1] || '').split(/\\n[A-Z][A-Z ]{4,}/)[0].trim(),
    loggedIn: !/log ?in|sign ?in/i.test(document.title),
  };
}
"""

REL = re.compile(
    r"(\d+)\s+(second|minute|hour|day|week)s?\s+ago", re.I
)
FREE_LINE = re.compile(
    r"^(.*?\bago)\s*[-–]\s*(.+)$"
)
DONATION_LINE = re.compile(
    r"^(.+?)\s+contributed\s+(\d+)\s+votes?\s+(.*?ago)\s*$", re.I
)


def rel_to_dt(text, now):
    """'12 minutes ago' -> datetime. Returns `now` if it can't be parsed."""
    m = REL.search(text)
    if not m:
        return now
    n, unit = int(m.group(1)), m.group(2).lower()
    seconds = {
        "second": 1, "minute": 60, "hour": 3600,
        "day": 86400, "week": 604800,
    }[unit]
    return now - timedelta(seconds=n * seconds)


def parse_free(block, now):
    out = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = FREE_LINE.match(line)
        if not m:
            continue
        when, name = m.group(1).strip(), m.group(2).strip()
        if not name:
            continue
        out.append({
            "name": name,
            "approxTime": rel_to_dt(when, now).isoformat(),
            "relativeAtCapture": when,
            "firstLogged": now.isoformat(),
        })
    return out


def parse_donations(block, now):
    out = []
    lines = [l.strip() for l in block.splitlines()]
    for i, line in enumerate(lines):
        m = DONATION_LINE.match(line)
        if not m:
            continue
        name, votes, when = m.group(1).strip(), int(m.group(2)), m.group(3).strip()
        # a quoted comment, if present, sits a line or two below
        comment = None
        for follow in lines[i + 1:i + 4]:
            if follow.startswith('"') and follow.endswith('"') and len(follow) > 2:
                comment = follow[1:-1]
                break
            if DONATION_LINE.match(follow):
                break
        out.append({
            "name": name,
            "votes": votes,
            "approxTime": rel_to_dt(when, now).isoformat(),
            "relativeAtCapture": when,
            "comment": comment,
        })
    return out


# ------------------------------------------------------------------- merging

def _ts(iso):
    return datetime.fromisoformat(iso).timestamp()


def tolerance(entry):
    """Seconds of slop to allow, based on how coarse the label was."""
    m = REL.search(entry.get("relativeAtCapture") or "")
    return TOLERANCE.get(m.group(2).lower(), DEFAULT_TOLERANCE) if m \
        else DEFAULT_TOLERANCE


def is_known(entry, existing, match_votes=False):
    """Is this sighting a vote we already have?

    Slop comes from the INCOMING sighting's label only. A re-sighting of an
    old vote always carries an aged, coarse label ("1 day ago" -> wide
    tolerance, dedupes even though the timestamp drifted hours). A genuinely
    new vote is first seen with a fresh, fine label ("2 hours ago" -> narrow
    tolerance), so a repeat donation of the same amount a day later is NOT
    swallowed by the old entry. Using max(old, new) here was a bug: it
    silently dropped same-donor same-amount repeat donations ~1 day apart.
    """
    for old in existing:
        if old["name"] != entry["name"]:
            continue
        if match_votes and old.get("votes") != entry.get("votes"):
            continue
        if abs(_ts(old["approxTime"]) - _ts(entry["approxTime"])) <= tolerance(entry):
            return True
    return False


def merge(state, free, donations, now):
    new_free = [e for e in free if not is_known(e, state["freeVotes"])]
    new_don = [e for e in donations
               if not is_known(e, state["donationVotes"], match_votes=True)]

    # A full turnover means every visible slot changed since we last looked, so
    # we cannot tell whether exactly 3 people voted or more slipped past.
    if free and len(new_free) == len(free) == 3 and state.get("lastChecked"):
        state.setdefault("gapWarnings", []).append({
            "from": state["lastChecked"],
            "to": now.isoformat(),
        })

    state["freeVotes"] = new_free + state["freeVotes"]
    state["donationVotes"] = new_don + state["donationVotes"]
    state["freeVotes"].sort(key=lambda e: e["approxTime"], reverse=True)
    state["donationVotes"].sort(key=lambda e: e["approxTime"], reverse=True)
    state["lastChecked"] = now.isoformat()
    return new_free, new_don


# ------------------------------------------------------------------- outputs

def render(state, next_check=None):
    data = dict(state)
    if next_check:
        data["nextCheckAt"] = next_check.isoformat()
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    html = TEMPLATE.read_text().replace("__VOTE_DATA__", payload)
    OUTPUT.write_text(html)


def save(state):
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(STATE)  # atomic, so a crash mid-write can't corrupt the ledger


def log(msg):
    line = f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with LOGFILE.open("a") as fh:
        fh.write(line + "\n")
    # keep the log from growing forever: trim to the newest 2000 lines
    try:
        if LOGFILE.stat().st_size > 500_000:
            lines = LOGFILE.read_text().splitlines()[-2000:]
            LOGFILE.write_text("\n".join(lines) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------- lock

class AlreadyRunning(Exception):
    pass


def acquire_lock():
    """One run at a time. Two runs read-modify-writing state.json concurrently
    (a slow scheduled check overlapping the next one, or --watch plus a
    scheduler) would silently lose whichever finished first. A lock older
    than 10 minutes is treated as left behind by a crash and taken over."""
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return
    except FileExistsError:
        try:
            age = time.time() - LOCK.stat().st_mtime
        except OSError:      # vanished between the two calls: retry once
            return acquire_lock()
        if age > 600:
            log("! stale lock (previous run crashed?) — taking over")
            LOCK.unlink(missing_ok=True)
            return acquire_lock()
        raise AlreadyRunning


def release_lock():
    LOCK.unlink(missing_ok=True)


def run_hook(cmd):
    """User-supplied publish step (e.g. a git push for GitHub Pages)."""
    try:
        r = subprocess.run(cmd, shell=True, cwd=HERE, timeout=120,
                           capture_output=True, text=True)
        if r.returncode != 0:
            log(f"! hook exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
        else:
            log("hook ok")
    except subprocess.TimeoutExpired:
        log("! hook timed out after 120s")


# --------------------------------------------------------------------- check

def check_once(page):
    """Load the dashboard and merge whatever it shows. Returns newFreeCount."""
    page.goto(DASHBOARD, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)

    result = page.evaluate(EXTRACT_JS)
    now = datetime.now(TZ).replace(microsecond=0)

    if not result["free"].strip():
        # Either signed out, or the page changed shape. Never guess — and never
        # try to sign in; that is yours to do, in --login.
        log("! signed out — run: python3 poll.py --login"
            if not result.get("loggedIn")
            else "! no vote data on the page (layout changed?) — nothing written")
        return None

    state = json.loads(STATE.read_text())
    free = parse_free(result["free"], now)
    donations = parse_donations(result["donation"], now)
    new_free, new_don = merge(state, free, donations, now)

    save(state)

    bits = []
    if new_free:
        bits.append("new free: " + ", ".join(e["name"] for e in new_free))
    if new_don:
        bits.append("new donations: " + ", ".join(
            f'{e["name"]} ({e["votes"]})' for e in new_don))
    log(f"{len(new_free)} new · {len(state['freeVotes'])} total"
        + (" · " + " · ".join(bits) if bits else ""))

    if len(new_free) == 3:
        log("! all 3 slots turned over — gap logged, some votes may be uncaught")

    check_once.changed = bool(new_free or new_don)
    return len(new_free)


# ---------------------------------------------------------------------- main

def browser(playwright, headed=False):
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        headless=not headed,
        viewport={"width": 1280, "height": 900},
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true",
                    help="open a browser so you can sign in; session is remembered")
    ap.add_argument("--watch", action="store_true",
                    help="keep running, checking on a fixed pulse (see --every)")
    ap.add_argument("--every", type=int, default=60, metavar="SEC",
                    help="watch mode: seconds between checks (default 60, "
                         f"floor {MIN_EVERY})")
    ap.add_argument("--rebuild", action="store_true",
                    help="regenerate ledger.html from state.json, no network")
    ap.add_argument("--interval", type=int, default=5, metavar="MIN",
                    help="one-shot mode: minutes until your scheduler runs this "
                         "again, so the page can show an honest Next check "
                         "(default 5, matching the provided schedules)")
    ap.add_argument("--hook", metavar="CMD",
                    help="shell command run after any check that logged "
                         "something new — e.g. a git commit+push that "
                         "publishes ledger.html to GitHub Pages")
    args = ap.parse_args()

    if args.rebuild:
        render(json.loads(STATE.read_text()))
        log(f"rebuilt {OUTPUT.name}")
        return

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        if args.login:
            ctx = browser(pw, headed=True)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(DASHBOARD)
            print("\nSign in to the contest site in the window that just opened.")
            print("When your dashboard is showing, come back here and press Enter.")
            input()
            ctx.close()
            log("login saved")
            return

        try:
            acquire_lock()
        except AlreadyRunning:
            log("another check is already running — skipping this one")
            return

        ctx = page = None
        try:
            if not args.watch:
                ctx = browser(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    count = check_once(page)
                finally:
                    ctx.close()
                nxt = datetime.now(TZ) + timedelta(minutes=args.interval)
                render(json.loads(STATE.read_text()), next_check=nxt)
                if args.hook and getattr(check_once, "changed", False):
                    run_hook(args.hook)
                sys.exit(0 if count is not None else 1)

            # --watch: keep going even if the browser itself dies — a fresh
            # context is cheap, and hours-long sessions do occasionally wedge.
            every = max(args.every, MIN_EVERY)
            if every != args.every:
                log(f"--every raised to the {MIN_EVERY}s floor")
            log(f"watching · one check every {every}s")
            while True:
                try:
                    if ctx is None:
                        ctx = browser(pw)
                        page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    count = check_once(page)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    log(f"! check failed: {exc}")
                    count = None
                    try:
                        if ctx is not None:
                            ctx.close()
                    except Exception:
                        pass
                    ctx = page = None       # relaunch on the next pass
                pause = every if count is not None \
                    else max(every, FAILURE_PAUSE)
                nxt = datetime.now(TZ) + timedelta(seconds=pause)
                render(json.loads(STATE.read_text()), next_check=nxt)
                if args.hook and getattr(check_once, "changed", False):
                    run_hook(args.hook)
                    check_once.changed = False
                if count is None:
                    log(f"pausing {pause}s before retrying")
                time.sleep(pause)
        except KeyboardInterrupt:
            log("stopped")
            try:
                if ctx is not None:
                    ctx.close()
            except Exception:
                pass
        finally:
            release_lock()


if __name__ == "__main__":
    main()
