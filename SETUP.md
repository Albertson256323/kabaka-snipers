# Kabaka Snipers — Forward Test Setup

Runs the Malaysian SNR strategy against live Bybit prices on a schedule,
tracks pending/open/closed trades, and publishes a dashboard — with no
server, and no dependency on your phone being on.

## 1. Create the repo
- Create a new **public** GitHub repo (private repos get far fewer free
  Actions minutes) — e.g. `kabaka-snipers-forward`.
- Upload these files keeping the folder structure exactly as given:
  - `forward_scan.py`
  - `templates/index_template.html`
  - `templates/history_template.html`
  - `static/style.css`
  - `.github/workflows/forward-scan.yml`

## 2. Turn on GitHub Pages
- Repo → **Settings → Pages**
- Source: **Deploy from a branch**
- Branch: `main`, folder: `/docs`
- Save. Your dashboard will be live at
  `https://<your-username>.github.io/<repo-name>/` once the first scan runs.

## 3. Run it once by hand
- Repo → **Actions** tab → select **Forward Test Scan** → **Run workflow**
- This creates `state.json` and the `docs/` folder for the first time.
  Check the run went green, then check the Pages URL loads.

## 4. Keep it running every 30 minutes (the reliable way)
GitHub's own `schedule:` cron is a backup only — it drifts badly under load,
same issue you already ran into with the old bot. Use the same fix:

- Go to **cron-job.org** (free, no card) and log into your existing account.
- Create a fine-grained GitHub token scoped to **only this new repo**,
  with **Actions: Read and write** permission
  (Settings → Developer settings → Personal access tokens → Fine-grained).
- New cron job in cron-job.org:
  - URL: `https://api.github.com/repos/<your-username>/<repo-name>/actions/workflows/forward-scan.yml/dispatches`
  - Method: `POST`
  - Headers:
    - `Authorization: Bearer <your-token>`
    - `Accept: application/vnd.github+json`
  - Body: `{"ref":"main"}`
  - Schedule: every 30 minutes

That's it — from here it runs whether your phone is on, off, or in a
drawer.

## Notes
- Starting balance, trade mode (single/multi), and the breakeven toggle
  are set as constants near the top of `forward_scan.py` — edit and
  commit to change them (currently: $62, multi-way, breakeven off, to
  match what you last ran in the Backtest Terminal).
- The dashboard only counts signals that form *after* the first scan —
  it will not retroactively backfill past setups as if they were traded.
- This is a separate, read-only dashboard from your Flask Backtest
  Terminal — the two aren't connected. The Backtest Terminal is still
  the tool for exploring historical "what if" runs on demand.
