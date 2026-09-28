# Risky accounts tracker

Reads the "Risky" rows of https://algotradingspace.com/trading-stats/live twice a day
(7:00 AM and 7:00 PM New York time), keeps a permanent history, and publishes a
dashboard on GitHub Pages. Free to run, no server, nothing to maintain.

```
.github/workflows/tracker.yml   schedule + steps GitHub runs for you
scraper/scrape.py               reads the site and applies the rules
scraper/should_run.py           daylight-saving gate for the schedule
requirements.txt                Python packages
index.html                      the dashboard
data/accounts.json              current state of every account
data/daily_log.csv              full history, one row per account per run
data/status.json                result of the last run
```

## One-time setup (about 10 minutes, no programming)

1. **Create a GitHub account** at https://github.com/signup if you don't have one.
2. **Create the repository.** Click **+** (top right) → **New repository**. Name it
   `risky-tracker`, choose **Public**, leave everything else as is, click **Create repository**.
3. **Upload the files.** On the new repository page click **uploading an existing file**.
   Unzip `risky-tracker.zip` on your computer, open the folder, select **everything inside it**
   (`.github`, `data`, `scraper`, `index.html`, `README.md`, `requirements.txt`) and drag it
   onto the page. Click **Commit changes**.
   - The `.github` folder is hidden on Mac. In Finder press **Cmd + Shift + .** to show it.
   - Check it arrived: the repository should show a `.github` folder. If it doesn't, click
     **Add file → Create new file**, type `.github/workflows/tracker.yml` as the name (the
     slashes create the folders), paste the contents of that file, and click **Commit changes**.
4. **Let the workflow save data.** **Settings → Actions → General**, scroll to
   **Workflow permissions**, select **Read and write permissions**, click **Save**.
5. **Turn on the website.** **Settings → Pages**. Under **Build and deployment**, set Source to
   **Deploy from a branch**, Branch to **main** and **/ (root)**, click **Save**.
6. **Run it once by hand.** Open the **Actions** tab (if asked, click the green button to enable
   workflows). Click **Risky tracker** on the left → **Run workflow** → **Run workflow**.
   After about a minute you'll see a green tick. Click the run to see the summary table.
7. **Open your dashboard.** Go back to **Settings → Pages**; the address is shown at the top,
   e.g. `https://YOUR-USERNAME.github.io/risky-tracker/`. The first publish can take 2–5 minutes.
   Bookmark it. From now on it updates itself.

## How the numbers work

- Every account counts as a **$1,000 deposit**, whatever the site shows.
- **Net P/L = closed balance + withdrawals − $1,000.** Floating P/L is shown but never counted.
- Each account is tracked by the **permanent ID** in its set-files link, so a new
  "Risky 2" is never mixed up with an old one. A reused number becomes "Risky 2 (2nd)", "(3rd)"...
- **Open date** = run date − the site's "Days", set once when the account is first seen.
- An account is marked **Blown** (balance $0, loss $1,000) when:
  - its closed balance falls **under $50**, or
  - it is **no longer listed** for **2 runs in a row** (12 hours). After the first miss it shows
    as "Not listed" at its last balance. This stops a single bad or cached page from wrongly
    blowing an account. To blow it on the first miss instead, set `MISSING_RUNS_TO_BLOW = 1`
    near the top of `scraper/scrape.py`.
  - Once an ID is Blown it stays Blown, even if the site keeps listing it.
- **Runs are labelled AM / PM** (e.g. `2026-09-29 PM`). Running again in the same half-day
  replaces that snapshot instead of adding a duplicate. The dashboard history can show every run
  or one row per day.
- **Stale data checks:**
  - If every figure matches the previous run, the run is logged and flagged
    "No change / possibly cached" (or "No change (market closed)" on weekends, which is normal).
  - If the page is *older* than data already stored (trades or days went down), the new numbers
    are not saved; the previous figures are kept and the run is flagged.

## If something goes wrong

- The scraper retries network errors, and if the normal read fails it automatically retries
  with a real browser (Playwright).
- A failed run never overwrites good data. The dashboard keeps showing the last good data with a
  red note saying when the failure happened and why.
- Errors appear in red on the run page in the **Actions** tab. If the site changes its layout,
  copy that error into Claude and ask for a fix.

### Get an email when a run fails

1. Click your profile picture → **Settings** → **Notifications**.
2. Under **System → Actions**, tick **Email** and **Only notify for failed workflows**.

GitHub emails the person who last changed the workflow file, so make sure that's your account
(it is if you uploaded the files).

## Change the run times

Open `.github/workflows/tracker.yml`, click the pencil icon, and change two things.

1. **`RUN_TIMES_NY`**: your New York times in 24-hour format, e.g. `"06:30,18:30"`.
2. **The cron lines**: two per time, written as `"minute hour * * *"` in UTC.
   - The summer line is the New York time **+ 4 hours**.
   - The winter line is the New York time **+ 5 hours**.
   - Subtract 24 if the result passes midnight.

Example for 6:30 AM and 6:30 PM:

```yaml
    - cron: "30 10 * * *"   # 6:30 AM summer
    - cron: "30 11 * * *"   # 6:30 AM winter
    - cron: "30 22 * * *"   # 6:30 PM summer
    - cron: "30 23 * * *"   # 6:30 PM winter
```

Commit the change and you're done.

**Why two lines per time:** GitHub's clock is UTC and ignores daylight saving. With a single
line, the run would drift one hour when New York changes clocks (early November and mid-March).
With both lines, `should_run.py` lets through only the one that matches New York's current offset.
The other finishes in a few seconds and does nothing.

GitHub often starts scheduled runs 5–30 minutes late at busy times. That's normal; the run still
counts for its slot.

## Good to know

- GitHub pauses scheduled workflows in public repositories after 60 days without activity.
  The data commits this workflow makes should count as activity, but if GitHub ever emails you
  that the workflow was disabled, open **Actions → Risky tracker → Enable workflow**.
- The dashboard reads the newest data directly from the repository, so it's current within
  about 5 minutes of each run.
- To use the history in Excel, click **Download CSV** on the dashboard.
- Check the site's terms of use to confirm automated reading is allowed.
