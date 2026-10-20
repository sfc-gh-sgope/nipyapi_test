# Deploy NiFi Flows to OpenFlow Runtime

A Python tool that downloads NiFi flow definitions from a GitHub repo and deploys them to Snowflake OpenFlow runtimes using [nipyapi](https://nipyapi.readthedocs.io/en/latest/). Supports automatic parameter configuration, Snowflake secret resolution via Parameter Provider, JDBC driver uploads, and multi-runtime targeting.

## Repository Structure

```
nipyapi_test/
├── .env.example              # Template — copy to .env and fill in your credentials
├── .gitignore                # Excludes .env, *.jar, __pycache__ from git
├── requirements.txt          # Python dependencies (nipyapi pinned to 1.5.0)
├── deploy_flow_secure.py     # Main deploy script (with Snowflake secret support)
├── README.md                 # This file
├── archive/
│   └── deploy_flow.py        # Legacy deploy script (plain text secrets) — not maintained
└── flows/
    ├── sgope/
    │   ├── Snowflake-to-Postgres.json                    # Flow definition (no config)
    │   ├── SQLServer-Tuncate-Insert.json                 # Flow definition
    │   └── SQLServer-Tuncate-Insert.config.yaml          # Parameters + assets
    └── UHG/
        ├── avro-kafka-high-performance-connector.json
        └── avro-kafka-high-performance-connector.config.yaml
```

## Features

- **Deploy flows from GitHub** — downloads flow JSON and uploads to any OpenFlow runtime
- **Auto-configure parameters** — optional `.config.yaml` sets parameter values after deploy
- **Snowflake secret resolution** — passwords resolved from Snowflake SECRET objects via the runtime's Parameter Provider (`$SF_SECRET{DB.SCHEMA.SECRET}`)
- **Asset uploads** — downloads JDBC drivers (or other JARs) and binds them to parameters
- **Start/stop deployed flows** — enables/disables controller services automatically
- **Delete deployed flows** — removes the process group and the parameter contexts only it used
- **Fail-fast validation** — missing Snowflake secrets or stage files abort the deploy before anything is uploaded
- **Automatic rollback** — if a deploy fails after upload, the partial process group is deleted
- **List deployed flows** — see all flows on a runtime with their status
- **Multi-runtime support** — list all runtimes, deploy to any by name with `--runtime`
- **PrivateLink support** — toggle `USE_PRIVATELINK=true` for private connectivity
- **Configurable runtime host prefix** — `OPENFLOW_HOST_PREFIX` for runtimes on `of2--`, `of3--`, etc.
- **Local-first config** — loads config YAML from local disk first, falls back to GitHub

---

## Quick Reference

```bash
python deploy_flow_secure.py list-runtimes                                          # List all runtimes
python deploy_flow_secure.py list-flows [--runtime NAME]                            # List deployed flows
python deploy_flow_secure.py deploy <bucket> <flow> [--runtime NAME] [--start]      # Deploy a flow
python deploy_flow_secure.py start "<flow-name>" [--runtime NAME]                   # Start a deployed flow
python deploy_flow_secure.py stop "<flow-name>" [--runtime NAME]                    # Stop a deployed flow
python deploy_flow_secure.py delete "<flow-name>" [--runtime NAME] [--yes]          # Delete a flow + its parameter contexts
```

---

## Step-by-Step Setup

### Step 1: Prerequisites

- Python 3.9+
- A GitHub repo with NiFi flow JSON files under `flows/<bucket>/`
- One or more Snowflake OpenFlow runtimes (gen2)
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
Copy the `token_secret` value from the output — it is only shown once.

### Step 3: Set Up Network Policy (Required for PAT Access)

Snowflake requires a network policy when using programmatic access tokens:

```sql
-- Find your IP: curl -s -4 ifconfig.me
CREATE NETWORK POLICY IF NOT EXISTS NIFIHUB_DEPLOY_POLICY
  ALLOWED_IP_LIST = ('<your-ip-address>')
  COMMENT = 'Network policy for PAT-based deploy script';

ALTER USER <your_user> SET NETWORK_POLICY = NIFIHUB_DEPLOY_POLICY;
```

### Step 4: Create Snowflake Secrets (for `$SF_SECRET` references)

Store sensitive values (e.g. database passwords) as Snowflake SECRET objects:

```sql
CREATE SECRET IF NOT EXISTS OPENFLOW.OPENFLOW.API_POSTGRESQL
  TYPE = GENERIC_STRING
  SECRET_STRING = 'YourPasswordHere';
```

The runtime's built-in **Snowflake Parameter Provider** exposes these secrets as NiFi parameter contexts. No plain text passwords are stored in `.env` or config files.

### Step 5: Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 6: Configure .env

```bash
cp .env.example .env
```

Edit `.env` with your real values:

```env
# --- GitHub ---
GITHUB_PAT=ghp_your_github_token_here
GITHUB_REPO=sfc-gh-sgope/nipyapi_test
GITHUB_BRANCH=main
GITHUB_FLOWS_PATH=flows

# --- Snowflake ---
SNOWFLAKE_ACCOUNT=sfsenorthamerica-sgope_aws3
SNOWFLAKE_USER=sgope
SNOWFLAKE_PAT=your_snowflake_pat_token_secret_here
SNOWFLAKE_ROLE=ACCOUNTADMIN

# --- OpenFlow Runtime (default — can be overridden with --runtime flag) ---
OPENFLOW_RUNTIME=POSTGRES_DEMO

# Host prefix of the runtime URL (default: of1) — see "Finding your host prefix" below
# OPENFLOW_HOST_PREFIX=of2

# --- Private Link (optional) ---
# USE_PRIVATELINK=true
```

| Variable | Required | Description |
|---|---|---|
| `GITHUB_PAT` | Yes | GitHub Personal Access Token (starts with `ghp_`) |
| `GITHUB_REPO` | Yes | GitHub repo in `owner/repo` format |
| `GITHUB_BRANCH` | No | Branch to pull flows from (default: `main`) |
| `GITHUB_FLOWS_PATH` | No | Base directory in the repo where flows live (default: `flows`) |
| `SNOWFLAKE_ACCOUNT` | Yes | Snowflake account identifier (e.g. `sfsenorthamerica-sgope_aws3`) |
| `SNOWFLAKE_USER` | Yes | Snowflake username |
| `SNOWFLAKE_PAT` | Yes | Snowflake Programmatic Access Token (the `token_secret` value) |
| `SNOWFLAKE_ROLE` | No | Role for Snowflake operations (default: `OPENFLOW_ADMIN`) |
| `OPENFLOW_RUNTIME` | No | Default runtime name (can be overridden with `--runtime`) |
| `OPENFLOW_HOST_PREFIX` | No | Prefix of the runtime host, e.g. `of1`, `of2` (default: `of1`) |
| `USE_PRIVATELINK` | No | Set to `true` for PrivateLink accounts |

### Finding your host prefix

The script builds the runtime URL as:

```
https://<OPENFLOW_HOST_PREFIX>--<org>-<account>.snowflakecomputing.app/<runtime-key>/nifi-api
```

Open the runtime in Snowsight and look at its URL. If it starts with `of2--`, set `OPENFLOW_HOST_PREFIX=of2`. A wrong prefix shows up as `401 Unauthorized` with `Token introspection returned no principal`, even though the PAT is valid.

To check the endpoint and PAT outside the script:

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $SNOWFLAKE_PAT" \
  https://of1--<org>-<account>.snowflakecomputing.app/<runtime-key>/nifi-api/flow/process-groups/root
```

`200` = endpoint and auth OK; `401` = token, role, or wrong host prefix; `403` = role lacks access to the runtime.

---

## Usage

### List Available Runtimes

```bash
python deploy_flow_secure.py list-runtimes
```

Output:
```
NAME                      DATABASE        SCHEMA          STATUS       URL
------------------------------------------------------------------------------------------------------------------------
POSTGRES_DEMO             OPENFLOW        OPENFLOW        ACTIVE       https://of1--org-account.snowflakecomputing.app/postgresdemo-100
SQL_API_TEST              OPENFLOW        OPENFLOW        ACTIVE       https://of1--org-account.snowflakecomputing.app/sqlapitest-100
```

### Deploy a Flow

```bash
# Deploy to default runtime (from .env)
python deploy_flow_secure.py deploy sgope Snowflake-to-Postgres

# Deploy to a specific runtime
python deploy_flow_secure.py deploy sgope SQLServer-Tuncate-Insert --runtime SQL_API_TEST

# Deploy and auto-start the flow
python deploy_flow_secure.py deploy sgope SQLServer-Tuncate-Insert --start

# Override the process group name
python deploy_flow_secure.py deploy sgope Snowflake-to-Postgres --name "My Custom Flow"

# Skip config.yaml (deploy flow only, no parameters/assets)
python deploy_flow_secure.py deploy sgope SQLServer-Tuncate-Insert --skip-config
```

The `<bucket>` and `<flow-name>` map to the file path:
```
flows / <bucket> / <flow-name>.json
flows / sgope   / SQLServer-Tuncate-Insert.json
```

### List Deployed Flows on a Runtime

```bash
python deploy_flow_secure.py list-flows
python deploy_flow_secure.py list-flows --runtime SQL_API_TEST
```

### Start a Deployed Flow

Enables all controller services, then starts processors:

```bash
python deploy_flow_secure.py start "SQLServer-Tuncate-Insert"
python deploy_flow_secure.py start "SQLServer-Tuncate-Insert" --runtime SQL_API_TEST
```

### Stop a Deployed Flow

Stops processors, then disables controller services:

```bash
python deploy_flow_secure.py stop "SQLServer-Tuncate-Insert"
python deploy_flow_secure.py stop "SQLServer-Tuncate-Insert" --runtime SQL_API_TEST
```

### Delete a Deployed Flow

Stops the flow, disables its controller services, empties queued FlowFiles, deletes the process group, then deletes the parameter contexts that only this flow used (including uploaded assets in them). It asks for confirmation unless `--yes` is passed. If several process groups share the name, all of them are deleted.

```bash
python deploy_flow_secure.py delete "SQLServer-Tuncate-Insert" --runtime SQL_API_TEST
python deploy_flow_secure.py delete "SQLServer-Tuncate-Insert" --runtime SQL_API_TEST --yes
```

Parameter contexts are **kept** when they are managed by the Parameter Provider (e.g. `OPENFLOW.OPENFLOW`) or still bound to another process group. Each context is reported as deleted or kept.

---

## Config YAML (Parameters and Assets)

Each flow can have an optional `.config.yaml` file alongside it. The script checks for a **local file first**, then falls back to GitHub.

```
flows/sgope/
├── SQLServer-Tuncate-Insert.json               # Flow definition
└── SQLServer-Tuncate-Insert.config.yaml        # Parameters + assets (optional)
```

If no `.config.yaml` exists (locally or on GitHub), the flow is deployed as-is.

### Config YAML Format

```yaml
parameters:
  # --- Source ---
  SQLServer Connection URL: "jdbc:sqlserver://my-host:1433;encrypt=false"
  SQLServer Username: "admin"
  SQLServer Password: "$SF_SECRET{OPENFLOW.OPENFLOW.API_POSTGRESQL}"    # From Snowflake secret

  # --- Destination ---
  Destination Database: "OPENFLOW_LOAD"
  Snowflake Warehouse: "openflow_wh"

# Assets (JDBC drivers, keystores, etc.)
assets:
  - name: "mssql-jdbc-12.8.1.jre11.jar"
    url: "https://repo1.maven.org/maven2/com/microsoft/sqlserver/mssql-jdbc/12.8.1.jre11/mssql-jdbc-12.8.1.jre11.jar"
    parameter: "SQLServer JDBC Driver"
```

### Secret Handling

Sensitive parameter values are resolved from Snowflake SECRET objects at runtime via the **Snowflake Parameter Provider**. No passwords are stored in `.env` or config files.

```yaml
SQLServer Password: "$SF_SECRET{OPENFLOW.OPENFLOW.API_POSTGRESQL}"
```

The format is `$SF_SECRET{DATABASE.SCHEMA.SECRET_NAME}`, referencing a Snowflake SECRET object:

```sql
CREATE SECRET IF NOT EXISTS OPENFLOW.OPENFLOW.API_POSTGRESQL
  TYPE = GENERIC_STRING
  SECRET_STRING = 'YourPasswordHere';
```

How it works:
1. The script triggers the runtime's **Snowflake Parameter Provider** to fetch secrets
2. The Parameter Provider creates a NiFi parameter context (e.g. `OPENFLOW.OPENFLOW`) with the secret values
3. The script rewires the controller service property to reference `#{SECRET_NAME}` directly
4. The flow's parameter context inherits from the Parameter Provider context

If a referenced secret or schema isn't visible to the Parameter Provider, the deploy stops **before** the flow is uploaded and lists what is missing. Check that the SECRET exists and that the runtime's execute-as role has READ on it and USAGE on its database and schema.

### Assets

Each asset needs a `name`, a `parameter`, and either a `url` or a `stage`:

```yaml
assets:
  - name: "mssql-jdbc-12.8.1.jre11.jar"
    url: "https://repo1.maven.org/maven2/.../mssql-jdbc-12.8.1.jre11.jar"
    parameter: "SQLServer JDBC Driver"
  - name: "kafka-keystore.jks"
    stage: "MY_DB.MY_SCHEMA.MY_STAGE/certs"      # file is read from @MY_DB.MY_SCHEMA.MY_STAGE/certs/kafka-keystore.jks
    parameter: "Keystore Filename"
```

Assets are:
1. Validated before deploy — stage files are checked with `LIST`; a missing file or incomplete entry stops the deploy
2. Downloaded from the URL or Snowflake stage
3. Uploaded to the NiFi parameter context
4. Bound to the specified parameter (the deploy fails if the parameter doesn't exist in the flow)

If the asset already exists on the runtime, the upload is skipped (idempotent).

---

## How It Works

```
Step 1:  Connect to Snowflake, discover runtime by name (SHOW OPENFLOW RUNTIMES IN ACCOUNT)
Step 2:  Build runtime URL from account locator + runtime key
Step 3:  Load flow JSON (local file first, then GitHub)
Step 4:  Load config YAML (local file first, then GitHub; optional)
Step 5:  Validate assets (stage files exist, entries complete)            -- fails before upload
Step 6:  Resolve $SF_SECRET{...} via Parameter Provider (all must exist)  -- fails before upload
Step 7:  Upload flow to runtime via nipyapi (NiFi REST API)
Step 8:  Add inherited parameter contexts from Parameter Provider
Step 9:  Apply parameters across all parameter contexts
Step 10: Rewire controller service properties to reference Snowflake secrets directly
Step 11: Download and upload assets, bind to parameters
Step 12: Optionally start the flow (enable CS + start processors)
```

If any of steps 8-12 fails, the script **rolls back**: it deletes the new process group and the parameter contexts only it used (same logic as `delete`), then exits with the original error. If the rollback itself fails, the process group ID is printed so it can be removed manually.

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `404 Not Found` from GitHub | Wrong bucket/flow name or branch | Check file path and case sensitivity (GitHub is case-sensitive) |
| `401 Network policy is required` | No network policy set for PAT access | Create a network policy with your IP (see Step 3) |
| `401 Bearer Token malformed` | Wrong value in `SNOWFLAKE_PAT` | Use the PAT `token_secret`; check for stray quotes or line breaks in `.env` |
| `401 Unauthorized` — `Token introspection returned no principal` | Wrong runtime host prefix (e.g. runtime is on `of2--`) | Set `OPENFLOW_HOST_PREFIX` in `.env` (see "Finding your host prefix") |
| `401 Unauthorized` | PAT expired or invalid | Regenerate the Snowflake PAT |
| `403 Forbidden` on runtime | PAT role doesn't have access | Check role grants or use `--runtime` to target a different runtime |
| `Connection refused` | Runtime is suspended | Resume: `ALTER OPENFLOW RUNTIME <db>.<schema>.<name> RESUME` |
| `Runtime not found` | Wrong runtime name | Run `list-runtimes` to see available names |
| `Role not granted` | PAT's `ROLE_RESTRICTION` doesn't match `SNOWFLAKE_ROLE` | Change `SNOWFLAKE_ROLE` in `.env` or create a new PAT with the right role |
| `Login failed` on DB connection | Password incorrect or overwritten | Check Snowflake SECRET value; ensure asset binding doesn't overwrite sensitive params |
| `Parameter Provider not found` | Runtime doesn't have the Snowflake Parameter Provider | Verify the runtime has "Openflow - Snowflake Parameter Provider" enabled |
| `Snowflake secret(s) not found via Parameter Provider` | Secret doesn't exist or execute-as role can't read it | Create the SECRET, or grant READ on it and USAGE on its database/schema to the runtime's execute-as role |
| `Asset validation failed` | Stage file missing, stage not accessible, or asset entry incomplete | Check `LIST @<stage>` and the `name`/`parameter`/`url`/`stage` fields in the config |
| `'FlowApi' object has no attribute 'get_flow'` | Different nipyapi version installed | `pip install -r requirements.txt` (pins `nipyapi==1.5.0`) |
| `ROLLBACK FAILED` | Cleanup after a failed deploy hit an error | Delete the printed process group ID in the canvas or with the `delete` command |
