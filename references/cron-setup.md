# Keeping the data in sync — three options

The skill's `sync.py` is just a regular Python script. It needs to run
somewhere with a long-running scheduler — Claude itself can't keep cron
ticking between conversations. Pick whichever of these matches where you
actually want the database to live.

The recommendation in all three cases: **once a week, Sunday 3 AM local**.
Editorial guides update slowly, and the sources don't appreciate hourly
fetches.

---

## Option 1: Linux / WSL — crontab

The classic. Edit your user's crontab:

```bash
crontab -e
```

Add (adjust paths):

```cron
# m h dom mon dow  command
0 3 * * 0  cd /home/you/restaurant-finder && /usr/bin/python3 scripts/sync.py >> data/sync.log 2>&1
```

Notes:
- Use the full path to Python — cron has a stripped `PATH`.
- Logs to `data/sync.log`. Rotate it yourself if you care.
- If you want email on failure, set `MAILTO=you@example.com` at the top of
  the crontab; cron sends mail when the script exits non-zero.

To run it once manually first:

```bash
cd /home/you/restaurant-finder
python3 scripts/sync.py
```

---

## Option 2: macOS — launchd

cron exists on macOS but launchd is the supported way. Save this to
`~/Library/LaunchAgents/com.you.restaurant-finder.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.you.restaurant-finder</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/you/restaurant-finder/scripts/sync.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/you/restaurant-finder</string>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>0</integer>   <!-- Sunday -->
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>0</integer>
  </dict>

  <key>StandardOutPath</key>
  <string>/Users/you/restaurant-finder/data/sync.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/you/restaurant-finder/data/sync.err.log</string>
</dict>
</plist>
```

Then load it:

```bash
launchctl load ~/Library/LaunchAgents/com.you.restaurant-finder.plist
launchctl start com.you.restaurant-finder    # run once now to verify
```

To check next-run time and history:

```bash
launchctl list | grep restaurant-finder
```

To unload (if you're tweaking the plist):

```bash
launchctl unload ~/Library/LaunchAgents/com.you.restaurant-finder.plist
```

---

## Option 3: GitHub Actions — no machine of your own

If you'd rather not rely on a machine being awake on Sunday at 3 AM,
host the skill in a GitHub repo and have Actions run the sync, then
commit the updated `restaurants.db` back to the repo.

`.github/workflows/sync.yml`:

```yaml
name: Sync restaurant data

on:
  schedule:
    - cron: "0 3 * * 0"   # Sunday 03:00 UTC
  workflow_dispatch:        # manual trigger from the Actions tab

jobs:
  sync:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Run sync
        run: python scripts/sync.py
        env:
          NOMINATIM_CONTACT: ${{ secrets.NOMINATIM_CONTACT }}

      - name: Commit DB if changed
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data/restaurants.db data/geocode_cache.sqlite
          if git diff --cached --quiet; then
            echo "No changes."
          else
            git commit -m "chore: weekly restaurant sync"
            git push
          fi
```

Notes:
- The workflow uses `secrets.NOMINATIM_CONTACT` — set that in the repo
  Settings → Secrets to your email so Nominatim doesn't ratelimit you.
- SQLite files in git aren't ideal for huge data, but at the scale here
  (a few MB) it's fine. If the DB grows past ~100 MB, switch to GitHub
  Releases or an S3 bucket.

When you query locally, just `git pull` first to get the latest data.

---

## Verifying it works

After scheduling:

```bash
# Run sync manually
python scripts/sync.py

# Check what got ingested
python scripts/query.py --status

# Sample query
python scripts/query.py --near "Bologna, Italy" --limit 5
```

You should see all three sources with recent timestamps in `--status`.

---

## A note on incrementality

`sync_michelin.py` and `sync_splendido.py` do full pulls every run — they're
small enough that this is fine.

`sync_raisin.py` is incremental: it prefers URLs not yet in the DB, capped
at `--max-pages` (default 1000) per run. With weekly cadence this catches
new venues immediately and re-fetches stale ones over time. If you want a
guaranteed full refresh occasionally, run:

```bash
python scripts/sync.py --source raisin --max-pages 10000
```

…and let it run for ~50 minutes.
