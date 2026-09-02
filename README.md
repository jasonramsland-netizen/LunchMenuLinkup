# School Lunch Menu Sync for Cozyla

This small service fetches the Westfield Washington Schools elementary lunch
menu from Health-e Pro, removes daily staples, and publishes an RFC 5545
iCalendar feed for Cozyla Smart Calendar.

The configured source is:

- District: `3660`
- Site: `18352` — Oak Trace Elementary School
- Lunch menu: `127895` — 26-27 Elementary Lunch

The script requests the supplied My School Menus endpoint first. Health-e Pro
currently redirects that endpoint and serves this menu through its modern API,
so the script automatically falls back to the modern menu metadata and monthly
`date_overwrites` endpoints when the legacy URL returns 404.

## What the sync does

For the current and upcoming published menu months, the sync:

1. Ignores past days, days off, category headings, and empty menu days.
2. Counts each recipe at most once per valid menu day.
3. Removes any item appearing on at least 70% of valid days.
4. Applies the exact-match `EXPLICIT_BLACKLIST` in `src/sync_menu.py`.
5. Creates one all-day event per remaining menu day.

The event summary is `Lunch: <first remaining item>`. The description lists
all remaining unique items for that day. The output file is `lunch_menu.ics`.

## Deploy to GitHub Pages

1. Create an empty GitHub repository and copy this project into it.
2. Commit and push the files to the repository’s default branch.
3. In GitHub, open **Settings > Actions > General** and allow the workflow to
   read and write repository contents if the repository uses restricted
   workflow permissions.
4. Open **Settings > Pages** and set the source to **GitHub Actions**.
5. Open the **Actions** tab, select **Sync school lunch calendar**, and choose
   **Run workflow** once. The workflow will then run automatically every Sunday
   at 06:00 UTC.

After the first successful run, the public feed URL is:

```text
https://<username>.github.io/<repo>/lunch_menu.ics
```

Replace `<username>` and `<repo>` with the GitHub account and repository name.
The workflow also commits the generated file to the repository, so it is easy
to inspect or download directly.

## Cozyla setup

Use the same public `.ics` URL in either method below. Calendar providers may
cache subscribed feeds, so a menu change may take several hours to appear.

### Method 1: Google Calendar — recommended

1. Open Google Calendar in a web browser.
2. Next to **Other calendars**, click **+ > From URL**.
3. Paste the GitHub Pages `lunch_menu.ics` URL and click **Add calendar**.
4. In the Cozyla App, open **Settings > Calendars > Add > Google Calendar**.
5. Sign in to the same Google account and select the subscribed lunch calendar.

### Method 2: iCloud / Apple Calendar

1. In Apple Calendar, choose **File > New Calendar Subscription** on a Mac,
   or use the subscribed-calendar option under the device’s Calendar account
   settings on iPhone/iPad.
2. Paste the GitHub Pages `lunch_menu.ics` URL and save the subscription.
3. In the Cozyla App/device, open **Settings > Calendars > Add > Apple**.
4. Choose **Add Account > Generate Password**, sign in to Apple, and create an
   Apple app-specific password.
5. Return to Cozyla, enter your Apple ID and that app-specific password, then
   connect the account.
6. Select the subscribed lunch calendar and the member profile, then tap
   **Done**.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\\Scripts\\activate
python -m pip install -r requirements.txt
python src/sync_menu.py
pytest --cov=src.sync_menu --cov-report=term-missing
```

A successful sync writes `lunch_menu.ics` in the repository root. A timeout,
HTTP error, invalid response, or unusable menu never replaces an existing
calendar file.

To change the district, site, menu, or staple rules, edit the constants at the
top of `src/sync_menu.py`. Keep `EXPLICIT_BLACKLIST` limited to exact item names
that should always be hidden.
