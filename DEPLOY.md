# Deploy Dairy Dashboard to Render

This guide covers everything needed to run the dashboard on [Render](https://render.com). The repo already includes `render.yaml`, so Render can create the **Web Service** and **PostgreSQL database** for you.

---

## What gets deployed

| Render resource | Type | Purpose |
|---|---|---|
| `dairy-dashboard` | **Web Service** (Python) | Serves the dashboard UI and `/sync` endpoint |
| `dairy-dashboard-db` | **PostgreSQL** | Stores imported Excel data |

Select **Web Service** for the app. The database is a separate resource that the web service connects to via `DATABASE_URL`.

---

## Part 1 — Push code to GitHub (you do this)

Render deploys from a Git repository. If the project is not on GitHub yet:

1. Create a new repository on GitHub (e.g. `dairy-dashboard`).
2. In PowerShell, from the project folder:

```powershell
cd "C:\Dairy Dashboard"
git init
git add .
git commit -m "Initial dairy dashboard"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/dairy-dashboard.git
git push -u origin main
```

Do **not** commit `.env` — it is already in `.gitignore`.

---

## Part 2 — Create services on Render (you do this)

### Option A — Blueprint (recommended)

1. Sign in at [dashboard.render.com](https://dashboard.render.com).
2. Click **New +** → **Blueprint**.
3. Connect your GitHub account and select the `dairy-dashboard` repository.
4. Render reads `render.yaml` and shows two resources:
   - **Web Service:** `dairy-dashboard`
   - **PostgreSQL:** `dairy-dashboard-db`
5. Click **Apply**.

Render will prompt for environment variables marked `sync: false` (Azure and Outlook settings). You can add placeholders now and update them after Part 3.

### Option B — Manual Web Service

If you prefer not to use a Blueprint:

1. **New +** → **PostgreSQL**
   - Name: `dairy-dashboard-db`
   - Plan: Free (or paid)
   - Create the database and copy its **Internal Database URL**.

2. **New +** → **Web Service**
   - Connect the same GitHub repo
   - Name: `dairy-dashboard`
   - Runtime: **Python 3**
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app --bind 0.0.0.0:$PORT`
   - Health check path: `/health`

3. On the web service, open **Environment** and add:

| Key | Value |
|---|---|
| `SECRET_KEY` | Generate a random string (Render can generate one) |
| `FLASK_DEBUG` | `false` |
| `DATABASE_URL` | Paste the PostgreSQL **Internal** connection string |
| `AZURE_CLIENT_ID` | From Part 3 |
| `AZURE_CLIENT_SECRET` | From Part 3 |
| `AZURE_TENANT_ID` | From Part 3 |
| `OUTLOOK_MAILBOX` | Email address of the mailbox to read |
| `OUTLOOK_SENDER_FILTER` | *(optional)* Only sync emails from this sender |
| `OUTLOOK_SUBJECT_FILTER` | *(optional)* Subject must contain this text |

Use the **Internal** database URL (not External) so traffic stays on Render’s private network.

---

## Part 3 — Microsoft Graph / Azure AD (admin consent path)

With `GRAPH_AUTH_MODE=application`, an admin grants **Application** permissions once. The app then reads the Parlours mailbox/OneDrive with no interactive sign-in.

### 3.1 Permissions the admin must grant

In the Farm Dashboard app registration → **API permissions** → **Microsoft Graph** → **Application permissions**:

| Permission | Purpose |
|---|---|
| `Mail.Read` | Read Outlook inbox + download Excel attachments |
| `Files.Read.All` | Read OneDrive files for `parlours@alhfarm.com` |

Then click **Grant admin consent for [tenant]**. Status must show green checks.

Optional hardening: create an Exchange Application Access Policy so the app can only access `parlours@alhfarm.com`.

### 3.2 Render environment variables

| Key | Value |
|---|---|
| `GRAPH_AUTH_MODE` | `application` |
| `AZURE_CLIENT_ID` | Application (client) ID |
| `AZURE_CLIENT_SECRET` | Secret **Value** (not Secret ID) |
| `AZURE_TENANT_ID` | Directory (tenant) ID |
| `OUTLOOK_MAILBOX` | `parlours@alhfarm.com` (or whichever inbox receives the emails) |
| `ONEDRIVE_USER` | `parlours@alhfarm.com` |
| `ONEDRIVE_SHARE_URL` | Paste the OneDrive/SharePoint **folder** sharing link (preferred) |
| `ONEDRIVE_FOLDER_PATH` | Only if not using share URL — e.g. `Dairy Reports` |
| `OUTLOOK_SENDER_FILTER` | *(optional)* only emails from this sender |
| `OUTLOOK_SUBJECT_FILTER` | *(optional)* subject must contain this |

### 3.3 Sync

After admin consent and env vars are set, open the dashboard and use **Sync from Outlook** / **Sync from OneDrive** — no Connect button needed in application mode.

### 3.4 Fallback without admin (delegated)

Set `GRAPH_AUTH_MODE=delegated`, add redirect URI, use **Connect Microsoft 365**. That only accesses the signed-in user's own mailbox/OneDrive.

---

## Part 4 — Verify deployment (you do this)

1. Wait for the web service deploy to finish (green **Live** status).
2. Open your Render URL, e.g. `https://dairy-dashboard.onrender.com`.
3. Check health: `https://dairy-dashboard.onrender.com/health`  
   Expected: `{"status":"ok","timestamp":"..."}`
4. On the dashboard, click **Sync from Outlook**.
5. Confirm flash messages and that records appear in the table.

### If sync fails

| Symptom | Likely cause |
|---|---|
| Missing Microsoft Graph configuration | Azure env vars not set on Render |
| Authentication failed | Wrong client secret, expired secret, or wrong tenant ID |
| 403 / Access denied | Admin consent not granted, or missing `Mail.Read` |
| No matching Excel attachments | Adjust filters or confirm emails have `.xlsx` / `.xls` attachments |
| Database error on `/health` | `DATABASE_URL` wrong or DB not linked to web service |

Check **Logs** on the Render web service for the exact error message.

---

## Part 5 — Local development (optional)

```powershell
cd "C:\Dairy Dashboard"
.\setup.ps1
# Edit .env with local Postgres + Azure credentials
.\.venv\Scripts\Activate.ps1
python app.py
```

Open [http://localhost:5000](http://localhost:5000).

---

## Render free tier notes

- **Web services** spin down after ~15 minutes of no traffic; the first request after idle may take 30–60 seconds.
- **PostgreSQL free** databases expire after 30 days unless upgraded.
- Ephemeral disk on the web service is fine — Excel files are downloaded temporarily during sync, then parsed into Postgres.

---

## Quick reference — service to select on Render

When creating or reviewing resources:

- **Dashboard UI:** **Web Service** → `dairy-dashboard`
- **Data storage:** **PostgreSQL** → `dairy-dashboard-db`
- **Do not use:** Static Site, Background Worker, or Cron Job for this app
