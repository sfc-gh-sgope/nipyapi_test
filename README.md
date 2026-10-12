# Deploy NiFi Flows to OpenFlow Runtime

A Python tool that downloads NiFi flow definitions from a GitHub repo and deploys them to Snowflake OpenFlow runtimes using [nipyapi](https://nipyapi.readthedocs.io/en/latest/). Supports automatic parameter configuration, Snowflake secret resolution via Parameter Provider, JDBC driver uploads, and multi-runtime targeting.

## Repository Structure

```
nipyapi_test/
├── .env.example              # Template — copy to .env and fill in your credentials
├── .gitignore                # Excludes .env, *.jar, __pycache__ from git
├── requirements.txt          # Python dependencies
├── deploy_flow_secure.py     # Main deploy script (with Snowflake secret support)
├── deploy_flow.py            # Legacy deploy script (plain text secrets from .env)
├── README.md                 # This file
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
- **Legacy secret injection** — also supports `$SECRET{VAR_NAME}` for values stored in `.env`
- **Asset uploads** — downloads JDBC drivers (or other JARs) and binds them to parameters
- **Start/stop deployed flows** — enables/disables controller services automatically
- **List deployed flows** — see all flows on a runtime with their status
- **Multi-runtime support** — list all runtimes, deploy to any by name with `--runtime`
- **PrivateLink support** — toggle `USE_PRIVATELINK=true` for private connectivity
- **Local-first config** — loads config YAML from local disk first, falls back to GitHub

---

## Quick Reference

```bash
python deploy_flow_secure.py list-runtimes                                          # List all runtimes
python deploy_flow_secure.py list-flows [--runtime NAME]                            # List deployed flows
python deploy_flow_secure.py deploy <bucket> <flow> [--runtime NAME] [--start]      # Deploy a flow
python deploy_flow_secure.py start "<flow-name>" [--runtime NAME]                   # Start a deployed flow
python deploy_flow_secure.py stop "<flow-name>" [--runtime NAME]                    # Stop a deployed flow
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
Copy the token value (starts with `ver:1:...`) from the output.

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
SNOWFLAKE_PAT=ver:1:your_snowflake_pat_here
SNOWFLAKE_ROLE=ACCOUNTADMIN

# --- OpenFlow Runtime (default — can be overridden with --runtime flag) ---
OPENFLOW_RUNTIME=POSTGRES_DEMO

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
| `SNOWFLAKE_PAT` | Yes | Snowflake Programmatic Access Token (starts with `ver:1:`) |
| `SNOWFLAKE_ROLE` | No | Role for Snowflake operations (default: `OPENFLOW_ADMIN`) |
| `OPENFLOW_RUNTIME` | No | Default runtime name (can be overridden with `--runtime`) |
| `USE_PRIVATELINK` | No | Set to `true` for PrivateLink accounts |

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

Two patterns are supported for sensitive parameter values:

#### `$SF_SECRET{DB.SCHEMA.SECRET_NAME}` (recommended)

Resolves the password from a Snowflake SECRET object at runtime via the Parameter Provider. No passwords are stored in `.env` or config files.

```yaml
SQLServer Password: "$SF_SECRET{OPENFLOW.OPENFLOW.API_POSTGRESQL}"
```

How it works:
1. The script triggers the runtime's **Snowflake Parameter Provider** to fetch secrets
2. The Parameter Provider creates a NiFi parameter context (e.g. `OPENFLOW.OPENFLOW`) with the secret values
3. The script rewires the controller service property to reference `#{SECRET_NAME}` directly
4. The flow's parameter context inherits from the Parameter Provider context

#### `$SECRET{VAR_NAME}` (legacy)

Reads the value from `.env` and injects it directly into the NiFi parameter:

```yaml
SQLServer Password: "$SECRET{SQLSERVER_PWD}"
```

With corresponding `.env` entry:
```env
SQLSERVER_PWD=MySecretPassword123
```

### Assets

Assets (e.g. JDBC driver JARs) are:
1. Downloaded from the URL specified in the config
2. Uploaded to the NiFi parameter context
3. Bound to the specified parameter

If the asset already exists on the runtime, the upload is skipped (idempotent).

---

## How It Works

```
Step 1:  Connect to Snowflake, discover runtime by name (SHOW OPENFLOW RUNTIMES IN ACCOUNT)
Step 2:  Build runtime URL from account locator + runtime key
Step 3:  Download flow JSON from GitHub
Step 4:  Load config YAML (local file first, then GitHub; optional)
Step 5:  Resolve $SF_SECRET{...} via Parameter Provider (fetch, apply, wire inheritance)
Step 6:  Upload flow to runtime via nipyapi (NiFi REST API)
Step 7:  Add inherited parameter contexts from Parameter Provider
Step 8:  Apply parameters across all parameter contexts
Step 9:  Rewire controller service properties to reference Snowflake secrets directly
Step 10: Download and upload assets, bind to parameters
Step 11: Optionally start the flow (enable CS + start processors)
```

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `404 Not Found` from GitHub | Wrong bucket/flow name or branch | Check file path and case sensitivity (GitHub is case-sensitive) |
| `401 Network policy is required` | No network policy set for PAT access | Create a network policy with your IP (see Step 3) |
| `401 Bearer Token malformed` | Wrong PAT format in `.env` | Use a Snowflake PAT starting with `ver:1:...`, not a JWT |
| `401 Unauthorized` | PAT expired or invalid | Regenerate the Snowflake PAT |
| `403 Forbidden` on runtime | PAT role doesn't have access | Check role grants or use `--runtime` to target a different runtime |
| `Connection refused` | Runtime is suspended | Resume: `ALTER OPENFLOW RUNTIME <db>.<schema>.<name> RESUME` |
| `Runtime not found` | Wrong runtime name | Run `list-runtimes` to see available names |
| `Role not granted` | PAT's `ROLE_RESTRICTION` doesn't match `SNOWFLAKE_ROLE` | Change `SNOWFLAKE_ROLE` in `.env` or create a new PAT with the right role |
| `Login failed` on DB connection | Password incorrect or overwritten | Check Snowflake SECRET value; ensure asset binding doesn't overwrite sensitive params |
| `Parameter Provider not found` | Runtime doesn't have the Snowflake Parameter Provider | Verify the runtime has "Openflow - Snowflake Parameter Provider" enabled |
