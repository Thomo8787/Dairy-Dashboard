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

## Part 3 — Microsoft Graph / Azure AD (you do this)

The dashboard reads Excel attachments from Outlook using **application permissions** (no user sign-in). Set this up in the Azure portal.

### 3.1 Register an app

1. Go to [Azure Portal](https://portal.azure.com) → **Microsoft Entra ID** → **App registrations** → **New registration**.
2. Name: e.g. `Dairy Dashboard`.
3. Supported account types: **Accounts in this organizational directory only**.
4. Redirect URI: leave blank (not used for this flow).
5. Click **Register** and note:
   - **Application (client) ID** → `AZURE_CLIENT_ID`
   - **Directory (tenant) ID** → `AZURE_TENANT_ID`

### 3.2 Create a client secret

1. Open the app → **Certificates & secrets** → **New client secret**.
2. Copy the **Value** immediately → `AZURE_CLIENT_SECRET`.

Secrets expire; set a calendar reminder to rotate before expiry.

### 3.3 Add API permissions

1. **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions**.
2. Add:
   - `Mail.Read` (read mail in all mailboxes), **or**
   - `Mail.ReadBasic.All` plus `Mail.Read` if you need full attachment access — **`Mail.Read` is required** to download attachments.
3. Click **Grant admin consent for [your tenant]** (requires Global Admin or Privileged Role Administrator).

Without admin consent, sync will fail with an authentication or permission error.

### 3.4 Set the mailbox

Set `OUTLOOK_MAILBOX` to the user or shared mailbox email address, for example:

```
OUTLOOK_MAILBOX=reports@yourcompany.com
```

For application access to a **shared mailbox**, ensure the app has permission to read that mailbox (often via `Mail.Read` application permission on the tenant).

### 3.5 Add values to Render

1. Render dashboard → **dairy-dashboard** web service → **Environment**.
2. Set:
   - `AZURE_CLIENT_ID`
   - `AZURE_CLIENT_SECRET`
   - `AZURE_TENANT_ID`
   - `OUTLOOK_MAILBOX`
3. Optionally set `OUTLOOK_SENDER_FILTER` and `OUTLOOK_SUBJECT_FILTER`.
4. Save — Render redeploys automatically.

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
