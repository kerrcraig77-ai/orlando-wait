#!/usr/bin/env python3
"""
Disney Lightning Lane / standby watcher.

Runs on a schedule (GitHub Actions). For each watched ride it fetches the live
themeparks.wiki data and fires a notification when:
  - the ride's next-available Lightning Lane return time becomes EARLIER than
    your booked time (ll_before), or
  - the standby wait drops to or below your threshold (standby_at_or_below), or
  - a Lightning Lane becomes AVAILABLE (ll_available), optionally before a cutoff.

State is kept in state.json (committed back by the workflow) so you only get
alerted once per transition, not every run.

Self-healing state (v2)
-----------------------
Every state entry is stamped with the Orlando-local date and the value that
triggered it. This fixes stale-state problems:

  * Daily auto-reset: if the stored date is not today's Orlando date, the entry
    is treated as re-armed. This clears overnight carry-over (the watcher does
    not run while the park is closed) and any stray test/manual "true" values.

  * Improvement re-fire: if a condition is still active but the deal gets
    materially better (LL >= LL_IMPROVE_MIN minutes earlier, or standby
    >= SB_IMPROVE_MIN minutes lower), it alerts again once, then re-latches on
    the new, better value.

Legacy boolean state (e.g. {"Space Mountain": true}) is still read correctly and
transparently upgraded to the new format on the next run.

Notifications:
  - method "ntfy": free push to your phone via ntfy.sh (install the ntfy app,
    subscribe to your secret topic). No account needed.
  - method "email": SMTP using repo secrets SMTP_HOST/PORT/USER/PASS.

Zero third-party dependencies: standard library only.
"""
import json
import os
import sys
import ssl
import smtplib
import urllib.request
import urllib.error
from email.message import EmailMessage
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    ORLANDO_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo present on py3.9+ runners
    ORLANDO_TZ = None

API = "https://api.themeparks.wiki/v1"

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")

# How much "better" a still-active condition must get before we re-alert.
LL_IMPROVE_MIN = 15   # Lightning Lane must be >= 15 min earlier than last alert
SB_IMPROVE_MIN = 10   # Standby must be >= 10 min lower than last alert


def get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "disney-watcher/2.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def orlando_today():
    """Return today's date string (YYYY-MM-DD) in Orlando local time."""
    if ORLANDO_TZ is not None:
        return datetime.now(ORLANDO_TZ).strftime("%Y-%m-%d")
    # Fallback: approximate US Eastern as UTC-4 (DST). Good enough to bucket days.
    return datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - 4 * 3600, timezone.utc).strftime("%Y-%m-%d")


def park_local(iso):
    """The API returns times with the park's own UTC offset baked in
    (e.g. ...T12:15:00-04:00), so the parsed hour/minute are already
    park-local. Returns (minutes_of_day, '12:15 PM' style label)."""
    try:
        dt = datetime.fromisoformat(iso)
        mins = dt.hour * 60 + dt.minute
        h12 = dt.hour % 12 or 12
        label = f"{h12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"
        return mins, label
    except Exception:
        return None, ""


def hhmm_to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def read_entry(st):
    """Normalise a stored state entry (legacy bool or v2 dict) into
    (active, day, val)."""
    if isinstance(st, dict):
        return bool(st.get("active")), st.get("day"), st.get("val")
    return bool(st), None, None


def notify_ntfy(topic, title, body):
    data = body.encode("utf-8")
    req = urllib.request.Request(
        "https://ntfy.sh/" + topic,
        data=data,
        headers={"Title": title, "Priority": "high", "Tags": "ferris_wheel"},
    )
    try:
        urllib.request.urlopen(req, timeout=20)
        return True
    except urllib.error.URLError as e:
        print("ntfy error:", e, file=sys.stderr)
        return False


def notify_email(cfg, title, body):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = cfg["notify"].get("email_to")
    if not all([host, user, password, to_addr]):
        print("email not configured (need SMTP_HOST/USER/PASS secrets + email_to)", file=sys.stderr)
        return False
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=25) as s:
        s.starttls(context=ctx)
        s.login(user, password)
        s.send_message(msg)
    return True


def dispatch(cfg, title, body):
    method = cfg["notify"].get("method", "ntfy")
    if method == "email":
        return notify_email(cfg, title, body)
    return notify_ntfy(cfg["notify"]["ntfy_topic"], title, body)


def main():
    cfg = load(CONFIG_PATH, None)
    if not cfg:
        print("Missing or invalid config.json", file=sys.stderr)
        sys.exit(1)

    parks = cfg.get("parks")
    if not parks and cfg.get("park_id"):
        parks = [{"id": cfg["park_id"], "name": "Park"}]
    if not parks:
        print("config.json needs a 'parks' list", file=sys.stderr)
        sys.exit(1)

    today = orlando_today()

    # Search every configured park and index attractions by name.
    by_name = {}
    for p in parks:
        try:
            live = get_json(f"{API}/entity/{p['id']}/live").get("liveData", [])
        except Exception as e:
            print(f"warn: could not load park {p.get('name', p['id'])}: {e}", file=sys.stderr)
            continue
        for r in live:
            if r.get("entityType") == "ATTRACTION" and r["name"] not in by_name:
                by_name[r["name"]] = r

    state = load(STATE_PATH, {})
    fired = []

    for w in cfg.get("watches", []):
        name = w["ride"]
        key = name

        # "done" — you've ridden it (or muted it); never alert until un-done.
        if w.get("done"):
            state[key] = {"active": False, "day": today, "val": None}
            continue

        active, last_day, last_val = read_entry(state.get(key))

        # Daily auto-reset: yesterday's (or a stale test's) latch never blocks a
        # fresh day. If the stored stamp is not today, treat as re-armed.
        if last_day != today:
            active = False
            last_val = None

        r = by_name.get(name)
        if not r or r.get("status") != "OPERATING":
            state[key] = {"active": False, "day": today, "val": None}
            continue

        q = r.get("queue") or {}
        hit = None
        cur_val = None  # the numeric value behind this hit, for improvement tracking

        # standby rule
        thresh = w.get("standby_at_or_below")
        sb = (q.get("STANDBY") or {}).get("waitTime")
        if thresh is not None and sb is not None and sb <= thresh:
            hit = f"{name}: standby is now {sb} min (\u2264 {thresh})."
            cur_val = sb

        # lightning lane "earlier than" rule
        if not hit and w.get("ll_before"):
            rt = (q.get("RETURN_TIME") or {}).get("returnStart")
            if rt:
                cur, cur_label = park_local(rt)
                tgt = hhmm_to_minutes(w["ll_before"])
                if cur is not None and cur < tgt:
                    hit = f"{name}: Lightning Lane now {cur_label} \u2014 earlier than your {w['ll_before']}! Open the Disney app to modify."
                    cur_val = cur

        # lightning lane "becomes available" rule (for rides you don't have booked)
        if not hit and w.get("ll_available"):
            rtq = q.get("RETURN_TIME") or {}
            if rtq.get("state") == "AVAILABLE" and rtq.get("returnStart"):
                cur, cur_label = park_local(rtq["returnStart"])
                ok = True
                if w.get("avail_before"):
                    tgt = hhmm_to_minutes(w["avail_before"])
                    ok = cur is not None and cur < tgt
                if ok:
                    hit = f"{name}: Lightning Lane now AVAILABLE \u2014 return {cur_label}! Book it fast in the Disney app."
                    cur_val = cur

        # Decide whether to alert.
        #  * Newly true  -> alert (classic transition).
        #  * Still true but materially BETTER than the value we last alerted on
        #    -> alert again once, then re-latch on the improved value. For LL
        #    (earlier is better) and standby (lower is better), "better" means a
        #    smaller number, so the same margin test works for both.
        should_fire = False
        if hit:
            if not active:
                should_fire = True
            elif last_val is not None and cur_val is not None:
                margin = LL_IMPROVE_MIN if w.get("standby_at_or_below") is None else SB_IMPROVE_MIN
                if cur_val <= last_val - margin:
                    should_fire = True

        if should_fire:
            fired.append(hit)
            state[key] = {"active": True, "day": today, "val": cur_val}
        elif hit:
            # active and not enough improvement: keep latched, refresh stamp
            state[key] = {"active": True, "day": today, "val": last_val if last_val is not None else cur_val}
        else:
            # condition cleared: re-arm
            state[key] = {"active": False, "day": today, "val": None}

    if fired:
        title = "Disney alert"
        body = "\n".join(fired)
        ok = dispatch(cfg, title, body)
        print(("SENT" if ok else "FAILED") + ": " + body.replace("\n", " | "))
    else:
        print("No new alerts.")

    save_state(state)


if __name__ == "__main__":
    main()
