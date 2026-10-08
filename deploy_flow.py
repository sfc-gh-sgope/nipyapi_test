#!/usr/bin/env python3
"""
deploy_flow.py — Download a NiFi flow JSON from GitHub and upload it to an OpenFlow runtime.

Usage:
    1. Fill in your .env file with your GitHub PAT, Snowflake account, and runtime details.
    2. Run:  python deploy_flow.py <bucket> <flow-name>

Example:
    python deploy_flow.py sgope Snowflake-to-Postgres
    python deploy_flow.py sgope SQLServer-Tuncate-Insert --start
"""

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import uuid

import nipyapi
import requests
import snowflake.connector
from dotenv import load_dotenv

# ──────────────────────────────────────────────
# 1. Load config from .env
# ──────────────────────────────────────────────

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

GITHUB_PAT = os.environ["GITHUB_PAT"].strip()
GITHUB_REPO = os.environ["GITHUB_REPO"].strip()
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main").strip()
GITHUB_FLOWS_PATH = os.environ.get("GITHUB_FLOWS_PATH", "flows").strip()

SNOWFLAKE_ACCOUNT = os.environ["SNOWFLAKE_ACCOUNT"].strip()
SNOWFLAKE_USER = os.environ["SNOWFLAKE_USER"].strip()
SNOWFLAKE_PAT = os.environ["SNOWFLAKE_PAT"].strip()
SNOWFLAKE_ROLE = os.environ.get("SNOWFLAKE_ROLE", "OPENFLOW_ADMIN").strip()
OPENFLOW_DATABASE = os.environ["OPENFLOW_DATABASE"].strip()
OPENFLOW_SCHEMA = os.environ["OPENFLOW_SCHEMA"].strip()
OPENFLOW_RUNTIME = os.environ["OPENFLOW_RUNTIME"].strip()
USE_PRIVATELINK = os.environ.get("USE_PRIVATELINK", "false").strip().lower() == "true"


# ──────────────────────────────────────────────
# 2. Resolve the OpenFlow runtime URL via SQL
# ──────────────────────────────────────────────

def resolve_runtime_url():
    """Connect to Snowflake, look up the runtime key, and build the NiFi API URL."""
    print("Connecting to Snowflake account: %s" % SNOWFLAKE_ACCOUNT)
    conn = snowflake.connector.connect(
        account=SNOWFLAKE_ACCOUNT,
        user=SNOWFLAKE_USER,
        token=SNOWFLAKE_PAT,
        authenticator="programmatic_access_token",
        role=SNOWFLAKE_ROLE,
    )
    try:
        cur = conn.cursor()
        cur.execute("SHOW OPENFLOW RUNTIMES IN SCHEMA %s.%s" % (OPENFLOW_DATABASE, OPENFLOW_SCHEMA))
        rows = cur.fetchall()
        columns = [desc[0].lower() for desc in cur.description]

        for row in rows:
            row_dict = dict(zip(columns, row))
            if row_dict["name"] == OPENFLOW_RUNTIME:
                runtime_key = row_dict["key"]
                status = row_dict["status"]
                print("Found runtime: %s (key: %s, status: %s)" % (OPENFLOW_RUNTIME, runtime_key, status))
                if status != "ACTIVE":
                    print("WARNING: Runtime is %s, not ACTIVE. Deploy may fail." % status, file=sys.stderr)

                # Build the URL: https://of1--{account}.snowflakecomputing.app/{key}
                # Account format: org-account (e.g. sfsenorthamerica-sgope_aws3)
                cur.execute("SELECT LOWER(CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME())")
                account_locator = cur.fetchone()[0].replace("_", "-")
                domain = "privatelink.snowflakecomputing.app" if USE_PRIVATELINK else "snowflakecomputing.app"
                url = "https://of1--%s.%s/%s" % (account_locator, domain, runtime_key)
                print("Runtime URL: %s" % url)
                return url

        raise ValueError("Runtime '%s' not found in %s.%s. Available: %s" % (
            OPENFLOW_RUNTIME, OPENFLOW_DATABASE, OPENFLOW_SCHEMA,
            ", ".join(dict(zip(columns, r))["name"] for r in rows)
        ))
    finally:
        conn.close()


# ──────────────────────────────────────────────
# 3. Download flow JSON from GitHub
# ──────────────────────────────────────────────

def download_flow_from_github(bucket, flow_name):
    """Fetch a flow definition JSON from the GitHub repo and return it as a dict."""
    filename = flow_name if flow_name.endswith(".json") else flow_name + ".json"
    path = "%s/%s/%s" % (GITHUB_FLOWS_PATH.strip("/"), bucket, filename)
    url = "https://api.github.com/repos/%s/contents/%s" % (GITHUB_REPO, path)

    print("Downloading from GitHub: %s (branch: %s)" % (path, GITHUB_BRANCH))
    resp = requests.get(
        url,
        headers={"Authorization": "Bearer " + GITHUB_PAT, "Accept": "application/vnd.github.v3+json"},
        params={"ref": GITHUB_BRANCH},
        timeout=30,
    )
    resp.raise_for_status()

    data = resp.json()
    content_b64 = data.get("content", "")
    if not content_b64:
        raise ValueError("GitHub returned empty content for " + path)

    raw = base64.b64decode(content_b64).decode("utf-8")
    flow_json = json.loads(raw)
    print("Downloaded flow: %s" % flow_json.get("flowContents", {}).get("name", flow_name))
    return flow_json


# ──────────────────────────────────────────────
# 4. Upload flow to OpenFlow runtime via nipyapi
# ──────────────────────────────────────────────

def deploy_flow_to_runtime(runtime_url, flow_json, flow_name=None, start=False):
    """Upload a flow JSON to the OpenFlow runtime and optionally start it."""
    base = re.sub(r"/nifi-api/?$", "", re.sub(r"/nifi/?$", "", runtime_url.rstrip("/")))
    nipyapi.config.nifi_config.host = base + "/nifi-api"
    nipyapi.security.set_service_auth_token(service="nifi", token=SNOWFLAKE_PAT)

    root_pg_id = nipyapi.canvas.get_root_pg_id()
    print("Connected to runtime. Root process group: %s" % root_pg_id)

    group_name = flow_name or flow_json.get("flowContents", {}).get("name", "Deployed Flow")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(flow_json, tmp)
        tmp_path = tmp.name

    with open(tmp_path, "rb") as fh:
        file_bytes = fh.read()
    os.unlink(tmp_path)

    result = nipyapi.nifi.ProcessGroupsApi().upload_process_group(
        id=root_pg_id,
        file=("flow.json", file_bytes, "application/json"),
        group_name=group_name,
        position_x="0.0",
        position_y="0.0",
        client_id=str(uuid.uuid4()),
    )

    pg_id = result.id
    print("Deployed process group: %s (id: %s)" % (group_name, pg_id))

    if start:
        print("Starting flow...")
        nipyapi.canvas.schedule_process_group(pg_id, True)
        print("Flow started.")

    return pg_id


# ──────────────────────────────────────────────
# 5. Main
# ──────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download a flow from GitHub and deploy to OpenFlow")
    parser.add_argument("bucket", help="Flow bucket directory (e.g. 'sgope', 'examples')")
    parser.add_argument("flow", help="Flow name without .json (e.g. 'Snowflake-to-Postgres')")
    parser.add_argument("--name", default=None, help="Override the process group name")
    parser.add_argument("--start", action="store_true", help="Start the flow after deploying")
    args = parser.parse_args()

    try:
        runtime_url = resolve_runtime_url()
        flow_json = download_flow_from_github(args.bucket, args.flow)
        pg_id = deploy_flow_to_runtime(runtime_url, flow_json, flow_name=args.name, start=args.start)
        print("\nDone! Process group ID: %s" % pg_id)
    except requests.HTTPError as e:
        print("GitHub API error: %s" % e, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print("Error: %s" % e, file=sys.stderr)
        sys.exit(1)
