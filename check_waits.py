#!/usr/bin/env python3
"""
Disney Lightning Lane / standby watcher.

Runs on a schedule (GitHub Actions). For each watched ride it fetches the live
themeparks.wiki data and fires a notification when:
  - the ride's next-available Lightning Lane return time becomes EARLIER than
    your booked time (ll_before), or
  - the standby wait drops to or below your threshold (standby_at_or_below).

State is kept in state.json (committed back by the workflow) so you only get
alerted once per transition, not every run.

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
from datetime import datetime

API = "https://api.themeparks.wiki/v1"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")


def get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "disney-watcher/1.0"})
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
        # "done" — you've ridden it (or muted it); never alert until un-done.
        if w.get("done"):
            state[name] = False
            continue
        r = by_name.get(name)
        key = name
        # State is {active, rev}. "rev" tracks the watch's revision from the app.
        # If the watch was re-created/modified (rev changed), re-arm so it can alert
        # again even if the previous instance had already fired.
        st = state.get(key)
        if isinstance(st, dict):
            active = bool(st.get("active"))
            prev_rev = st.get("rev")
        else:
            active = bool(st)   # legacy boolean
            prev_rev = None
        cur_rev = w.get("rev")
        if cur_rev is not None and cur_rev != prev_rev:
            active = False      # watch changed since last alert — re-arm
        if not r or r.get("status") != "OPERATING":
            state[key] = {"active": False, "rev": cur_rev}
            continue

        q = r.get("queue") or {}
        hit = None

        # standby rule
        thresh = w.get("standby_at_or_below")
        sb = (q.get("STANDBY") or {}).get("waitTime")
        if thresh is not None and sb is not None and sb <= thresh:
            hit = f"{name}: standby is now {sb} min (\u2264 {thresh})."

        # lightning lane "earlier than" rule
        if not hit and w.get("ll_before"):
            rt = (q.get("RETURN_TIME") or {}).get("returnStart")
            if rt:
                cur, cur_label = park_local(rt)
                tgt = hhmm_to_minutes(w["ll_before"])
                if cur is not None and cur < tgt:
                    hit = f"{name}: Lightning Lane now {cur_label} \u2014 earlier than your {w['ll_before']}! Open the Disney app to modify."

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

        # Pure transition: alert ONCE when a condition newly becomes true, then stay
        # quiet while it persists. When it clears, re-arm so a genuine new occurrence
        # (e.g. a fresh cancellation) alerts again. To stop alerts for a ride you've
        # ridden or don't care about, mark it done in the app.
        if hit and not active:
            fired.append(hit)
            state[key] = {"active": True, "rev": cur_rev}
        elif not hit:
            state[key] = {"active": False, "rev": cur_rev}
        else:
            state[key] = {"active": True, "rev": cur_rev}

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
