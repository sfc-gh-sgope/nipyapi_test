# Deploy NiFi Flow to OpenFlow Runtime

A simple Python script that downloads a NiFi flow definition JSON from a GitHub repo and uploads it to a Snowflake OpenFlow runtime using the [nipyapi](https://nipyapi.readthedocs.io/en/latest/) library.

## Files

```
deploy-flow/
├── .env.example         # Template — copy to .env and fill in your credentials
├── .gitignore           # Excludes .env (real credentials) from git
├── requirements.txt     # Python dependencies (nipyapi, requests, python-dotenv)
├── deploy_flow.py       # The script
└── README.md            # This file
```

---

## Step-by-Step Runbook

### Step 1: Prerequisites

- Python 3.9+
- A GitHub repo with NiFi flow JSON files (e.g. `flows/<bucket>/<flow-name>.json`)
- A running Snowflake OpenFlow runtime
- A GitHub Personal Access Token (PAT) with repo read access
- A Snowflake Programmatic Access Token (PAT)

### Step 2: Generate Tokens

**GitHub PAT:**
1. Go to GitHub > Settings > Developer settings > Personal access tokens > Fine-grained tokens
2. Create a token with read access to the repo containing your flows

**Snowflake PAT:**
```sql
-- Run in Snowflake as ACCOUNTADMIN
ALTER USER <your_user> ADD PROGRAMMATIC ACCESS TOKEN NIFI_DEPLOY_PAT
  ROLE_RESTRICTION = ACCOUNTADMIN
  DAYS_TO_EXPIRY = 365
  COMMENT = 'PAT for NiFi deploy script';
```
Copy the token value from the output.

### Step 3: Set Up Network Policy (Required for PAT Access)

Snowflake requires a network policy when using programmatic access tokens. Add your IP:

```sql
-- Find your IP: curl -s -4 ifconfig.me
CREATE NETWORK POLICY IF NOT EXISTS NIFIHUB_DEPLOY_POLICY
  ALLOWED_IP_LIST = ('<your-ip-address>')
  COMMENT = 'Network policy for PAT-based deploy script';

ALTER USER <your_user> SET NETWORK_POLICY = NIFIHUB_DEPLOY_POLICY;
```

If your IP changes later:
```sql
ALTER NETWORK POLICY NIFIHUB_DEPLOY_POLICY SET ALLOWED_IP_LIST = ('<new-ip>');
```

### Step 4: Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 5: Configure .env

```bash
cp .env.example .env
```

Edit `.env` with your real values:

```env
# --- GitHub ---
GITHUB_PAT=ghp_your_github_token_here
GITHUB_REPO=sfc-gh-sgope/nifihub
GITHUB_BRANCH=feature/add-snowflake-to-postgres-flow
GITHUB_FLOWS_PATH=flows

# --- OpenFlow Runtime ---
SNOWFLAKE_RUNTIME_URL=https://of1--your-account.snowflakecomputing.app/your-runtime-key
SNOWFLAKE_RUNTIME_PAT=your_snowflake_pat_here
```

| Variable | Description |
|---|---|
| `GITHUB_PAT` | GitHub Personal Access Token (starts with `ghp_`) |
| `GITHUB_REPO` | GitHub repo in `owner/repo` format |
| `GITHUB_BRANCH` | Branch to pull flow JSON from |
| `GITHUB_FLOWS_PATH` | Base directory in the repo where flows live |
| `SNOWFLAKE_RUNTIME_URL` | Your OpenFlow runtime URL (without `/nifi-api`) |
| `SNOWFLAKE_RUNTIME_PAT` | Snowflake Programmatic Access Token |

### Step 6: Run the Script

```bash
# Deploy a flow (downloads from GitHub and uploads to OpenFlow)
python deploy_flow.py <bucket> <flow-name>

# Example:
python deploy_flow.py sgope Snowflake-to-Postgres

# Deploy and auto-start the flow:
python deploy_flow.py sgope Snowflake-to-Postgres --start

# Override the process group name:
python deploy_flow.py sgope Snowflake-to-Postgres --name "My Custom Flow"
```

The `<bucket>` and `<flow-name>` map to the file path in GitHub:
```
flows / <bucket> / <flow-name>.json
```

### Step 7: Verify in OpenFlow UI

1. Open your OpenFlow runtime canvas in a browser
2. You should see the newly deployed process group on the canvas
3. Configure any parameters, then start the flow

---

## How It Works

1. **Reads `.env`** — loads GitHub PAT, Snowflake runtime URL, and Snowflake PAT
2. **Downloads flow JSON from GitHub** — uses the GitHub REST API (`GET /repos/{owner}/{repo}/contents/{path}`) to fetch the flow definition
3. **Uploads to OpenFlow runtime** — uses `nipyapi` to connect to the NiFi REST API on the runtime and calls `upload_process_group` to deploy the flow as a new process group

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `404 Not Found` from GitHub | Wrong bucket/flow name or branch | Check file path and case sensitivity — GitHub is case-sensitive |
| `401 Network policy is required` | No network policy set for PAT access | Create a network policy with your IP (see Step 3) |
| `401 Unauthorized` (no network policy message) | PAT is expired or invalid | Regenerate the Snowflake PAT |
| `Connection refused` | Runtime is suspended | Resume the runtime: `ALTER OPENFLOW RUNTIME ... RESUME` |
