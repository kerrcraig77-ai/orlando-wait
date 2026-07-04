# Disney Wait Watcher (Tier 2 — always-on)

A tiny scheduled job that watches your booked Lightning Lane / standby waits and
**pushes an alert to your phone even when the dashboard is closed**. Runs free on
GitHub Actions. No servers, your laptop can be off.

## What it does
Every 5 minutes it reads live data from themeparks.wiki and notifies you when:
- a ride's **Lightning Lane return time becomes earlier** than your booked time, or
- a ride's **standby** drops to/below a threshold you set.

It remembers what it already told you (`state.json`) so you get one ping per change.

---

## Setup (about 15 minutes, one time)

### 1. Create a personal GitHub account (NOT your work one)
- Go to **https://github.com/signup**
- Sign up with a **personal email** (Gmail/Outlook.com — do **not** use @microsoft.com;
  that address is tied to Microsoft's enterprise-managed GitHub which blocks public repos).
- Free plan is all you need.

### 2. Create the repo and upload these files
- Click **+ → New repository**, name it e.g. `disney-watcher`, set it **Public**
  (public repos get unlimited free Actions minutes; a 5-min schedule needs more than
  the private free allowance).
- Upload everything in this folder: `check_waits.py`, `config.json`,
  and the `.github/workflows/watch.yml` file (keep the folder structure).

### 3. Choose how you get pinged

**Option A — ntfy (easiest, free push, recommended)**
1. Install the **ntfy** app (iOS App Store / Google Play).
2. Pick a *secret* topic name, e.g. `craig-disney-9f3k2x` (make it long and unguessable —
   anyone who knows it can read your alerts).
3. In the app, **Subscribe** to that exact topic.
4. In `config.json`, set:
   ```json
   "notify": { "method": "ntfy", "ntfy_topic": "craig-disney-9f3k2x" }
   ```

**Option B — Email (no extra app)**
1. In `config.json` set `"method": "email"` and `"email_to": "you@example.com"`.
2. You need an SMTP sender. Easiest is a personal Gmail with an **App Password**
   (Google Account → Security → 2-Step Verification → App passwords).
3. In your GitHub repo: **Settings → Secrets and variables → Actions → New secret**,
   add:
   - `SMTP_HOST` = `smtp.gmail.com`
   - `SMTP_PORT` = `587`
   - `SMTP_USER` = your Gmail address
   - `SMTP_PASS` = the 16-char app password

### 4. Set your watches
Edit `config.json` → `watches`. One entry per booked ride:
```json
{ "ride": "Space Mountain", "ll_before": "16:30", "standby_at_or_below": null }
```
- `ll_before`: your booked LL return time in 24h park-local time (or null to skip).
- `standby_at_or_below`: alert when standby ≤ this many minutes (or null to skip).
- `ride`: must match the ride name exactly as it appears on the dashboard.

The watcher searches **all parks listed in `parks`** (all 7 Orlando theme parks by
default — Walt Disney World x4 + Universal Orlando x3), so you can mix Disney and
Universal rides in one watch list. Trim the `parks` list to speed things up.

### 5. Turn it on
- In the repo, open the **Actions** tab, enable workflows if prompted.
- Open **Disney wait watcher → Run workflow** to test it immediately.
- After that it runs every 5 minutes automatically. (GitHub schedules are
  best-effort and can lag a few minutes when busy — fine for this.)

---

## Notes & honest limits
- **When you're not in a park / off-season**, turn it off: Actions tab →
  workflow → ⋯ → Disable. Or just delete the repo.
- GitHub cron can occasionally skip or delay a run under load. This is a
  convenience nudge, not a guaranteed real-time system.
- The job only sees **public** next-available LL times — it can't read your actual
  My Disney Experience bookings. You tell it your booked time via `ll_before`.
- Keep your ntfy topic private; treat it like a password.
