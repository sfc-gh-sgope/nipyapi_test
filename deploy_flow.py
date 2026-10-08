#!/usr/bin/env python3
"""
deploy_flow.py — Download a NiFi flow JSON from GitHub and upload it to an OpenFlow runtime.

Optionally reads a companion .config.yaml to set parameters and upload assets (e.g. JDBC drivers).

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
import yaml
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

GITHUB_HEADERS = {"Authorization": "Bearer " + GITHUB_PAT, "Accept": "application/vnd.github.v3+json"}


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
# 3. Download files from GitHub
# ──────────────────────────────────────────────

def _download_github_file(path):
    """Download a file from GitHub and return its decoded content as a string."""
    url = "https://api.github.com/repos/%s/contents/%s" % (GITHUB_REPO, path)
    resp = requests.get(url, headers=GITHUB_HEADERS, params={"ref": GITHUB_BRANCH}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    content_b64 = data.get("content", "")
    if not content_b64:
        raise ValueError("GitHub returned empty content for " + path)
    return base64.b64decode(content_b64).decode("utf-8")


def download_flow_from_github(bucket, flow_name):
    """Fetch a flow definition JSON from the GitHub repo and return it as a dict."""
    filename = flow_name if flow_name.endswith(".json") else flow_name + ".json"
    path = "%s/%s/%s" % (GITHUB_FLOWS_PATH.strip("/"), bucket, filename)
    print("Downloading flow from GitHub: %s (branch: %s)" % (path, GITHUB_BRANCH))
    flow_json = json.loads(_download_github_file(path))
    print("Downloaded flow: %s" % flow_json.get("flowContents", {}).get("name", flow_name))
    return flow_json


def download_config_from_github(bucket, flow_name):
    """Fetch the optional .config.yaml for a flow. Returns a dict or None if not found."""
    base_name = flow_name.removesuffix(".json")
    config_filename = base_name + ".config.yaml"
    path = "%s/%s/%s" % (GITHUB_FLOWS_PATH.strip("/"), bucket, config_filename)
    try:
        print("Looking for config: %s" % path)
        raw = _download_github_file(path)
        config = yaml.safe_load(raw)
        print("Found config with %d parameters, %d assets" % (
            len(config.get("parameters", {})),
            len(config.get("assets", [])),
        ))
        return config
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            print("No config.yaml found — skipping parameters/assets")
            return None
        raise


# ──────────────────────────────────────────────
# 4. Resolve Snowflake secrets in config parameters
# ──────────────────────────────────────────────

# Pattern: DB.SCHEMA.SECRET_NAME (3 dot-separated parts, all uppercase/underscores)
_SECRET_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")


def resolve_secrets(params_dict):
    """Replace parameter values that look like Snowflake secret references with the actual secret value.

    A secret reference is a fully qualified name like OPENFLOW_LOAD.PUBLIC.SQLSERVER_PWD.
    The script connects to Snowflake, reads the secret, and substitutes the real value.
    """
    if not params_dict:
        return params_dict

    secrets_to_resolve = {k: v for k, v in params_dict.items() if isinstance(v, str) and _SECRET_PATTERN.match(v)}
    if not secrets_to_resolve:
        return params_dict

    print("\nResolving %d secret(s) from Snowflake..." % len(secrets_to_resolve))
    conn = snowflake.connector.connect(
        account=SNOWFLAKE_ACCOUNT,
        user=SNOWFLAKE_USER,
        token=SNOWFLAKE_PAT,
        authenticator="programmatic_access_token",
        role=SNOWFLAKE_ROLE,
    )
    try:
        cur = conn.cursor()
        resolved = dict(params_dict)
        for param_name, secret_ref in secrets_to_resolve.items():
            print("  Resolving secret: %s -> %s" % (param_name, secret_ref))
            cur.execute("SELECT SYSTEM$GET_SECRET_AS_PLAIN_TEXT('%s')" % secret_ref)
            row = cur.fetchone()
            if row and row[0]:
                resolved[param_name] = row[0]
                print("  Resolved: %s (value hidden)" % param_name)
            else:
                print("  WARNING: Could not resolve secret %s — keeping original value" % secret_ref,
                      file=sys.stderr)
        return resolved
    finally:
        conn.close()


# ──────────────────────────────────────────────
# 5. Upload flow to OpenFlow runtime via nipyapi
# ──────────────────────────────────────────────

def connect_to_runtime(runtime_url):
    """Configure nipyapi and return the root process group ID."""
    base = re.sub(r"/nifi-api/?$", "", re.sub(r"/nifi/?$", "", runtime_url.rstrip("/")))
    nipyapi.config.nifi_config.host = base + "/nifi-api"
    nipyapi.security.set_service_auth_token(service="nifi", token=SNOWFLAKE_PAT)
    root_pg_id = nipyapi.canvas.get_root_pg_id()
    print("Connected to runtime. Root process group: %s" % root_pg_id)
    return root_pg_id


def deploy_flow_to_runtime(root_pg_id, flow_json, flow_name=None):
    """Upload a flow JSON to the OpenFlow runtime. Returns the process group ID."""
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
    return pg_id


# ──────────────────────────────────────────────
# 6. Apply parameters from config.yaml
# ──────────────────────────────────────────────

def _find_parameter_contexts(pg_id):
    """Find all parameter context IDs associated with a process group (recursively)."""
    context_ids = set()
    pg_entity = nipyapi.nifi.ProcessGroupsApi().get_process_group(pg_id)
    ctx_ref = pg_entity.component.parameter_context
    if ctx_ref and ctx_ref.id:
        context_ids.add(ctx_ref.id)

    flow_dto = nipyapi.nifi.FlowApi().get_flow(pg_id)
    for child_pg in (flow_dto.process_group_flow.flow.process_groups or []):
        context_ids.update(_find_parameter_contexts(child_pg.id))

    return context_ids


def apply_parameters(pg_id, params_dict):
    """Set parameter values on the flow's parameter contexts."""
    if not params_dict:
        return

    print("\nApplying %d parameters..." % len(params_dict))
    context_ids = _find_parameter_contexts(pg_id)
    if not context_ids:
        print("WARNING: No parameter contexts found on this process group", file=sys.stderr)
        return

    params_applied = set()
    pc_api = nipyapi.nifi.ParameterContextsApi()

    for ctx_id in context_ids:
        ctx_entity = pc_api.get_parameter_context(ctx_id, include_inherited_parameters=False)
        ctx_name = ctx_entity.component.name
        existing_params = {p.parameter.name: p for p in (ctx_entity.component.parameters or [])}

        updates = []
        for param_name, param_value in params_dict.items():
            if param_name in existing_params:
                updates.append({
                    "parameter": {
                        "name": param_name,
                        "value": str(param_value),
                        "sensitive": existing_params[param_name].parameter.sensitive,
                    }
                })
                params_applied.add(param_name)

        if not updates:
            continue

        print("  Setting %d parameters on context '%s'" % (len(updates), ctx_name))
        update_body = {
            "revision": {"version": ctx_entity.revision.version},
            "id": ctx_id,
            "component": {
                "id": ctx_id,
                "parameters": updates,
            },
        }
        update_request = pc_api.submit_parameter_context_update(ctx_id, update_body)

        # Wait for the update to complete
        request_id = update_request.request.request_id
        while True:
            status = pc_api.get_parameter_context_update(ctx_id, request_id)
            if status.request.complete:
                if status.request.failure_reason:
                    print("  ERROR: %s" % status.request.failure_reason, file=sys.stderr)
                else:
                    print("  Done.")
                pc_api.delete_update_request(ctx_id, request_id)
                break

    not_found = set(params_dict.keys()) - params_applied
    if not_found:
        print("WARNING: These parameters were not found in any context: %s" % ", ".join(sorted(not_found)),
              file=sys.stderr)


# ──────────────────────────────────────────────
# 7. Upload assets from config.yaml
# ──────────────────────────────────────────────

def upload_assets(pg_id, assets_list):
    """Download asset files and upload them to the flow's parameter contexts."""
    if not assets_list:
        return

    print("\nUploading %d asset(s)..." % len(assets_list))
    context_ids = _find_parameter_contexts(pg_id)
    pc_api = nipyapi.nifi.ParameterContextsApi()

    # Build a map of parameter_name -> context_id
    param_to_ctx = {}
    for ctx_id in context_ids:
        ctx_entity = pc_api.get_parameter_context(ctx_id, include_inherited_parameters=False)
        for p in (ctx_entity.component.parameters or []):
            param_to_ctx[p.parameter.name] = ctx_id

    for asset_def in assets_list:
        asset_name = asset_def["name"]
        asset_url = asset_def["url"]
        asset_param = asset_def["parameter"]

        if asset_param not in param_to_ctx:
            print("  WARNING: Parameter '%s' not found — skipping asset '%s'" % (asset_param, asset_name),
                  file=sys.stderr)
            continue

        ctx_id = param_to_ctx[asset_param]

        # Download the asset file
        print("  Downloading asset: %s" % asset_name)
        resp = requests.get(asset_url, timeout=120)
        resp.raise_for_status()

        # Upload to the parameter context as an asset
        print("  Uploading asset to parameter context...")
        file_tuple = (asset_name, resp.content, "application/octet-stream")
        pc_api.create_assets(id=ctx_id, file=file_tuple)
        print("  Uploaded: %s -> parameter '%s'" % (asset_name, asset_param))


# ──────────────────────────────────────────────
# 8. Main
# ──────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download a flow from GitHub and deploy to OpenFlow")
    parser.add_argument("bucket", help="Flow bucket directory (e.g. 'sgope', 'examples')")
    parser.add_argument("flow", help="Flow name without .json (e.g. 'Snowflake-to-Postgres')")
    parser.add_argument("--name", default=None, help="Override the process group name")
    parser.add_argument("--start", action="store_true", help="Start the flow after deploying")
    parser.add_argument("--skip-config", action="store_true", help="Skip loading config.yaml (no params/assets)")
    args = parser.parse_args()

    try:
        # Step 1: Resolve runtime URL
        runtime_url = resolve_runtime_url()

        # Step 2: Download flow JSON from GitHub
        flow_json = download_flow_from_github(args.bucket, args.flow)

        # Step 3: Download config YAML from GitHub (optional)
        flow_config = None
        if not args.skip_config:
            flow_config = download_config_from_github(args.bucket, args.flow)

        # Step 4: Resolve secrets in config parameters
        if flow_config and flow_config.get("parameters"):
            flow_config["parameters"] = resolve_secrets(flow_config["parameters"])

        # Step 5: Connect and deploy flow
        root_pg_id = connect_to_runtime(runtime_url)
        pg_id = deploy_flow_to_runtime(root_pg_id, flow_json, flow_name=args.name)

        # Step 6: Apply parameters
        if flow_config and flow_config.get("parameters"):
            apply_parameters(pg_id, flow_config["parameters"])

        # Step 7: Upload assets
        if flow_config and flow_config.get("assets"):
            upload_assets(pg_id, flow_config["assets"])

        # Step 8: Start the flow
        if args.start:
            print("\nStarting flow...")
            nipyapi.canvas.schedule_process_group(pg_id, True)
            print("Flow started.")

        print("\nDone! Process group ID: %s" % pg_id)

    except requests.HTTPError as e:
        print("GitHub API error: %s" % e, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print("Error: %s" % e, file=sys.stderr)
        sys.exit(1)
