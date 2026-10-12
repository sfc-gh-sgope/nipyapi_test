#!/usr/bin/env python3
"""
deploy_flow_secure.py — Secure version: passwords stay in Snowflake, never in .env or script.

Uses the runtime's Snowflake Parameter Provider to resolve secrets from Snowflake SECRET objects.
Config YAML uses $SF_SECRET{DB.SCHEMA.SECRET_NAME} references instead of plain text passwords.

Usage:
    python deploy_flow_secure.py deploy <bucket> <flow-name>
    python deploy_flow_secure.py list-runtimes
    python deploy_flow_secure.py list-flows

Example:
    python deploy_flow_secure.py deploy sgope SQLServer-Tuncate-Insert
"""

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
import uuid

import nipyapi
import requests
import snowflake.connector
import yaml
from dotenv import load_dotenv

# Patch nipyapi model to avoid serialization error with parameter_sensitivities
_PGConfigEntity = nipyapi.nifi.models.ParameterGroupConfigurationEntity
_orig_setter = _PGConfigEntity.parameter_sensitivities.fset
def _patched_setter(self, value):
    self._parameter_sensitivities = value
_PGConfigEntity.parameter_sensitivities = _PGConfigEntity.parameter_sensitivities.setter(_patched_setter)


# ──────────────────────────────────────────────
# 1. Load config from .env_secure
# ──────────────────────────────────────────────

load_dotenv(os.path.join(os.path.dirname(__file__), ".env_secure"))

GITHUB_PAT = os.environ["GITHUB_PAT"].strip()
GITHUB_REPO = os.environ["GITHUB_REPO"].strip()
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main").strip()
GITHUB_FLOWS_PATH = os.environ.get("GITHUB_FLOWS_PATH", "flows").strip()

SNOWFLAKE_ACCOUNT = os.environ["SNOWFLAKE_ACCOUNT"].strip()
SNOWFLAKE_USER = os.environ["SNOWFLAKE_USER"].strip()
SNOWFLAKE_PAT = os.environ["SNOWFLAKE_PAT"].strip()
SNOWFLAKE_ROLE = os.environ.get("SNOWFLAKE_ROLE", "OPENFLOW_ADMIN").strip()
OPENFLOW_RUNTIME = os.environ.get("OPENFLOW_RUNTIME", "").strip()
USE_PRIVATELINK = os.environ.get("USE_PRIVATELINK", "false").strip().lower() == "true"

GITHUB_HEADERS = {"Authorization": "Bearer " + GITHUB_PAT, "Accept": "application/vnd.github.v3+json"}


# ──────────────────────────────────────────────
# 2. Snowflake connection + runtime discovery
# ──────────────────────────────────────────────

def _get_snowflake_connection():
    return snowflake.connector.connect(
        account=SNOWFLAKE_ACCOUNT,
        user=SNOWFLAKE_USER,
        token=SNOWFLAKE_PAT,
        authenticator="programmatic_access_token",
        role=SNOWFLAKE_ROLE,
    )


def _get_account_locator(cur):
    cur.execute("SELECT LOWER(CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME())")
    return cur.fetchone()[0].replace("_", "-")


def _build_runtime_url(account_locator, runtime_key):
    domain = "privatelink.snowflakecomputing.app" if USE_PRIVATELINK else "snowflakecomputing.app"
    return "https://of1--%s.%s/%s" % (account_locator, domain, runtime_key)


def get_all_runtimes():
    print("Connecting to Snowflake account: %s" % SNOWFLAKE_ACCOUNT)
    conn = _get_snowflake_connection()
    try:
        cur = conn.cursor()
        cur.execute("SHOW OPENFLOW RUNTIMES IN ACCOUNT")
        rows = cur.fetchall()
        columns = [desc[0].lower() for desc in cur.description]
        account_locator = _get_account_locator(cur)
        runtimes = []
        for row in rows:
            r = dict(zip(columns, row))
            r["url"] = _build_runtime_url(account_locator, r["key"])
            runtimes.append(r)
        return runtimes
    finally:
        conn.close()


def resolve_runtime_url(runtime_name):
    runtimes = get_all_runtimes()
    for r in runtimes:
        if r["name"] == runtime_name:
            print("Found runtime: %s (%s.%s, key: %s, status: %s)" % (
                r["name"], r["database_name"], r["schema_name"], r["key"], r["status"]
            ))
            if r["status"] != "ACTIVE":
                print("WARNING: Runtime is %s, not ACTIVE." % r["status"], file=sys.stderr)
            print("Runtime URL: %s" % r["url"])
            return r["url"]
    available = ", ".join("%s (%s)" % (r["name"], r["status"]) for r in runtimes)
    raise ValueError("Runtime '%s' not found. Available: %s" % (runtime_name, available))


# ──────────────────────────────────────────────
# 3. Download files from GitHub
# ──────────────────────────────────────────────

def _download_github_file(path):
    url = "https://api.github.com/repos/%s/contents/%s" % (GITHUB_REPO, path)
    resp = requests.get(url, headers=GITHUB_HEADERS, params={"ref": GITHUB_BRANCH}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    content_b64 = data.get("content", "")
    if not content_b64:
        raise ValueError("GitHub returned empty content for " + path)
    return base64.b64decode(content_b64).decode("utf-8")


def download_flow_from_github(bucket, flow_name):
    filename = flow_name if flow_name.endswith(".json") else flow_name + ".json"
    path = "%s/%s/%s" % (GITHUB_FLOWS_PATH.strip("/"), bucket, filename)
    print("Downloading flow from GitHub: %s (branch: %s)" % (path, GITHUB_BRANCH))
    flow_json = json.loads(_download_github_file(path))
    print("Downloaded flow: %s" % flow_json.get("flowContents", {}).get("name", flow_name))
    return flow_json


def download_config_from_github(bucket, flow_name):
    base_name = flow_name.removesuffix(".json")
    config_filename = base_name + ".config.secure.yaml"
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
            print("No config.secure.yaml found — skipping parameters/assets")
            return None
        raise


# ──────────────────────────────────────────────
# 4. Resolve Snowflake secrets via Parameter Provider
# ──────────────────────────────────────────────

_SF_SECRET_PATTERN = re.compile(r"^\$SF_SECRET\{([^}]+)\}$")
_AUTO_PROVIDER_NAME = "Openflow - Snowflake Parameter Provider"


def _find_parameter_provider():
    result = nipyapi.nifi.FlowApi().get_parameter_providers()
    for pp in (result.parameter_providers or []):
        if pp.component.name == _AUTO_PROVIDER_NAME:
            return pp
    return None


def _fetch_parameter_provider(pp):
    body = nipyapi.nifi.ParameterProviderParameterFetchEntity(
        id=pp.id,
        revision=pp.revision,
    )
    result = nipyapi.nifi.ParameterProvidersApi().fetch_parameters(body, pp.id)
    groups = result.component.parameter_group_configurations or []
    print("  Fetched %d parameter group(s) from Snowflake" % len(groups))
    return result


def _apply_parameter_provider(provider_id, target_groups):
    """Enable target parameter groups and apply to create NiFi parameter contexts."""
    api = nipyapi.nifi.ParameterProvidersApi()
    pp = api.get_parameter_provider(provider_id)
    groups = pp.component.parameter_group_configurations or []

    if not groups:
        print("  WARNING: No parameter groups found after fetch", file=sys.stderr)
        return []

    for group in groups:
        group_key = group.group_name
        if group_key in target_groups:
            sensitivities = dict(group.parameter_sensitivities or {})
            for pname in sensitivities:
                sensitivities[pname] = "SENSITIVE"
            group.parameter_sensitivities = sensitivities
            group.synchronized = True
            if not group.parameter_context_name:
                group.parameter_context_name = group.group_name
            print("  Enabling parameter group: %s" % group_key)

    body = nipyapi.nifi.ParameterProviderParameterApplicationEntity(
        id=provider_id,
        revision=pp.revision,
        parameter_group_configurations=groups,
    )

    req = api.submit_apply_parameters(body, provider_id)
    request_id = req.request.request_id
    print("  Applying parameter groups...")

    while True:
        time.sleep(1)
        status = api.get_parameter_provider_apply_parameters_request(provider_id, request_id)
        if status.request.complete:
            api.delete_apply_parameters_request(provider_id, request_id)
            if status.request.failure_reason:
                raise RuntimeError("Parameter Provider apply failed: %s" % status.request.failure_reason)
            print("  Parameter groups applied successfully")
            return [g.parameter_context_name for g in groups if g.synchronized]


def _find_parameter_context_by_name(name):
    all_contexts = nipyapi.nifi.FlowApi().get_parameter_contexts()
    for ctx_ref in (all_contexts.parameter_contexts or []):
        if ctx_ref.component.name == name:
            return ctx_ref
    return None


def resolve_sf_secrets(params_dict):
    """Detect $SF_SECRET{DB.SCHEMA.SECRET} values, trigger the Parameter Provider,
    and return (resolved_params, context_names_to_inherit).

    resolved_params: params with $SF_SECRET replaced by #{SECRET_NAME}
    context_names_to_inherit: NiFi parameter context names to add as inherited
    """
    if not params_dict:
        return params_dict, []

    sf_secrets = {}
    for param_name, param_value in params_dict.items():
        if isinstance(param_value, str):
            match = _SF_SECRET_PATTERN.match(param_value)
            if match:
                fqn = match.group(1)
                parts = fqn.split(".")
                if len(parts) != 3:
                    print("  WARNING: Invalid secret reference '%s' — expected DB.SCHEMA.SECRET" % fqn, file=sys.stderr)
                    continue
                db, schema, secret_name = parts
                group_key = "%s.%s" % (db, schema)
                sf_secrets[param_name] = (fqn, group_key, secret_name)

    if not sf_secrets:
        return params_dict, []

    print("\nResolving %d Snowflake secret(s) via Parameter Provider..." % len(sf_secrets))

    pp = _find_parameter_provider()
    if not pp:
        raise RuntimeError("Parameter Provider '%s' not found on this runtime" % _AUTO_PROVIDER_NAME)

    _fetch_parameter_provider(pp)

    target_groups = set(info[1] for info in sf_secrets.values())
    context_names = _apply_parameter_provider(pp.id, target_groups)

    resolved = dict(params_dict)
    contexts_to_inherit = set()
    for param_name, (fqn, group_key, secret_name) in sf_secrets.items():
        resolved[param_name] = "#{%s}" % secret_name
        contexts_to_inherit.add(group_key)
        print("  %s -> #{%s} (from Snowflake secret %s)" % (param_name, secret_name, fqn))

    return resolved, list(contexts_to_inherit)


# ──────────────────────────────────────────────
# 5. Add inherited parameter contexts to a flow
# ──────────────────────────────────────────────

def add_inherited_contexts(pg_id, context_names):
    """Add parameter contexts as inherited on the flow's parameter context."""
    if not context_names:
        return

    pg = nipyapi.nifi.ProcessGroupsApi().get_process_group(pg_id)
    pc_ref = pg.component.parameter_context
    if not pc_ref:
        print("  WARNING: No parameter context on this flow — cannot add inheritance", file=sys.stderr)
        return

    pc_api = nipyapi.nifi.ParameterContextsApi()
    pc = pc_api.get_parameter_context(id=pc_ref.id)
    existing_inherited = list(pc.component.inherited_parameter_contexts or [])
    existing_ids = {ipc.id for ipc in existing_inherited}

    added = []
    for ctx_name in context_names:
        ctx = _find_parameter_context_by_name(ctx_name)
        if not ctx:
            print("  WARNING: Context '%s' not found — skipping" % ctx_name, file=sys.stderr)
            continue
        if ctx.id in existing_ids:
            print("  Context '%s' already inherited" % ctx_name)
            continue
        existing_inherited.append(nipyapi.nifi.ParameterContextReferenceEntity(
            id=ctx.id,
            component=nipyapi.nifi.ParameterContextReferenceDTO(id=ctx.id, name=ctx_name),
        ))
        added.append(ctx_name)

    if not added:
        return

    params = []
    for p in (pc.component.parameters or []):
        param = p.parameter
        if getattr(param, "referenced_assets", None):
            param.value = None
        params.append(nipyapi.nifi.ParameterEntity(parameter=param))

    body = nipyapi.nifi.ParameterContextEntity(
        id=pc_ref.id,
        revision=pc.revision,
        component=nipyapi.nifi.ParameterContextDTO(
            id=pc_ref.id,
            name=pc.component.name,
            parameters=params,
            inherited_parameter_contexts=existing_inherited,
        ),
    )
    req = pc_api.submit_parameter_context_update(context_id=pc_ref.id, body=body)
    request_id = req.request.request_id
    while True:
        time.sleep(1)
        status = pc_api.get_parameter_context_update(context_id=pc_ref.id, request_id=request_id)
        if status.request.complete:
            if status.request.failure_reason:
                print("  ERROR: %s" % status.request.failure_reason, file=sys.stderr)
            else:
                print("  Added inherited contexts: %s" % added)
            pc_api.delete_update_request(context_id=pc_ref.id, request_id=request_id)
            break


# ──────────────────────────────────────────────
# 6. Upload flow to OpenFlow runtime via nipyapi
# ──────────────────────────────────────────────

def connect_to_runtime(runtime_url):
    base = re.sub(r"/nifi-api/?$", "", re.sub(r"/nifi/?$", "", runtime_url.rstrip("/")))
    nipyapi.config.nifi_config.host = base + "/nifi-api"
    nipyapi.security.set_service_auth_token(service="nifi", token=SNOWFLAKE_PAT)
    root_pg_id = nipyapi.canvas.get_root_pg_id()
    print("Connected to runtime. Root process group: %s" % root_pg_id)
    return root_pg_id


def deploy_flow_to_runtime(root_pg_id, flow_json, flow_name=None):
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
# 7. Apply parameters from config.yaml
# ──────────────────────────────────────────────

def _find_parameter_contexts(pg_id):
    context_ids = set()
    pc_api = nipyapi.nifi.ParameterContextsApi()
    pg_entity = nipyapi.nifi.ProcessGroupsApi().get_process_group(pg_id)
    ctx_ref = pg_entity.component.parameter_context
    if ctx_ref and ctx_ref.id:
        context_ids.add(ctx_ref.id)
        ctx_full = pc_api.get_parameter_context(ctx_ref.id, include_inherited_parameters=False)
        for inherited in (ctx_full.component.inherited_parameter_contexts or []):
            context_ids.add(inherited.id)
    flow_dto = nipyapi.nifi.FlowApi().get_flow(pg_id)
    for child_pg in (flow_dto.process_group_flow.flow.process_groups or []):
        context_ids.update(_find_parameter_contexts(child_pg.id))
    if len(context_ids) <= 1:
        flow_name = pg_entity.component.name
        headers = {"Authorization": "Bearer " + SNOWFLAKE_PAT}
        resp = requests.get(nipyapi.config.nifi_config.host + "/flow/parameter-contexts", headers=headers, timeout=30)
        if resp.ok:
            for ctx in resp.json().get("parameterContexts", []):
                ctx_name = ctx["component"]["name"]
                flow_prefix = flow_name.split(" - ")[0].split(" (")[0]
                if ctx_name.startswith(flow_prefix) or flow_prefix.startswith(ctx_name.split(" ")[0]):
                    context_ids.add(ctx["id"])
    return context_ids


def apply_parameters(pg_id, params_dict):
    if not params_dict:
        return
    print("\nApplying %d parameters..." % len(params_dict))
    context_ids = _find_parameter_contexts(pg_id)
    if not context_ids:
        print("WARNING: No parameter contexts found", file=sys.stderr)
        return
    params_applied = set()
    for ctx_id in context_ids:
        pc_api = nipyapi.nifi.ParameterContextsApi()
        ctx_entity = pc_api.get_parameter_context(ctx_id, include_inherited_parameters=False)
        ctx_name = ctx_entity.component.name
        existing_params = {p.parameter.name: p for p in (ctx_entity.component.parameters or [])}
        updates = []
        for param_name, param_value in params_dict.items():
            if param_name in existing_params:
                updates.append({"parameter": {"name": param_name, "value": str(param_value), "sensitive": existing_params[param_name].parameter.sensitive}})
                params_applied.add(param_name)
        if not updates:
            continue
        print("  Setting %d parameters on context '%s'" % (len(updates), ctx_name))
        api_url = "%s/parameter-contexts/%s/update-requests" % (nipyapi.config.nifi_config.host, ctx_id)
        body = json.dumps({"revision": {"version": ctx_entity.revision.version}, "id": ctx_id, "component": {"id": ctx_id, "parameters": updates}})
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + SNOWFLAKE_PAT}
        resp = requests.post(api_url, data=body, headers=headers, timeout=30)
        resp.raise_for_status()
        request_id = resp.json()["request"]["requestId"]
        while True:
            sr = requests.get("%s/parameter-contexts/%s/update-requests/%s" % (nipyapi.config.nifi_config.host, ctx_id, request_id), headers=headers, timeout=30)
            sd = sr.json()
            if sd["request"]["complete"]:
                if sd["request"].get("failureReason"):
                    print("  ERROR: %s" % sd["request"]["failureReason"], file=sys.stderr)
                else:
                    print("  Done.")
                requests.delete("%s/parameter-contexts/%s/update-requests/%s" % (nipyapi.config.nifi_config.host, ctx_id, request_id), headers=headers, timeout=30)
                break
    not_found = set(params_dict.keys()) - params_applied
    if not_found:
        print("WARNING: Not found in any context: %s" % ", ".join(sorted(not_found)), file=sys.stderr)


# ──────────────────────────────────────────────
# 8. Upload assets from config.yaml
# ──────────────────────────────────────────────

def upload_assets(pg_id, assets_list):
    if not assets_list:
        return
    print("\nUploading %d asset(s)..." % len(assets_list))
    pc_api = nipyapi.nifi.ParameterContextsApi()
    pg_entity = nipyapi.nifi.ProcessGroupsApi().get_process_group(pg_id)
    pc_ref = pg_entity.component.parameter_context
    if not pc_ref:
        print("  WARNING: No parameter context — skipping assets", file=sys.stderr)
        return
    param_map = {}
    seen = set()
    def walk(cid):
        if cid in seen:
            return
        seen.add(cid)
        pc = pc_api.get_parameter_context(id=cid)
        for p in (pc.component.parameters or []):
            if p.parameter.name not in param_map:
                param_map[p.parameter.name] = (cid, p.parameter)
        for inh in (pc.component.inherited_parameter_contexts or []):
            walk(inh.id)
    walk(pc_ref.id)
    for asset_def in assets_list:
        asset_name, asset_url, asset_param = asset_def["name"], asset_def["url"], asset_def["parameter"]
        if asset_param not in param_map:
            print("  WARNING: Parameter '%s' not found — skipping '%s'" % (asset_param, asset_name), file=sys.stderr)
            continue
        ctx_id, _ = param_map[asset_param]
        existing = {ae.asset.name: ae.asset for ae in (pc_api.get_assets(context_id=ctx_id).assets or [])}
        if asset_name in existing and not existing[asset_name].missing_content:
            asset_id = existing[asset_name].id
            print("  Asset '%s' exists (id=%s) — skipping upload" % (asset_name, asset_id))
        else:
            print("  Downloading: %s" % asset_name)
            resp = requests.get(asset_url, timeout=120, headers={"User-Agent": "nifihub-deploy/1.0"})
            resp.raise_for_status()
            print("  Uploading to parameter context...")
            upload_result = pc_api.create_asset(body=resp.content, context_id=ctx_id, filename=asset_name)
            asset_id = upload_result.asset.id
            print("  Uploaded '%s' (id=%s)" % (asset_name, asset_id))
        print("  Binding '%s' -> '%s'" % (asset_param, asset_name))
        pc = pc_api.get_parameter_context(id=ctx_id)
        updated = []
        for name, param in {p.parameter.name: p.parameter for p in (pc.component.parameters or [])}.items():
            if name == asset_param:
                updated.append(nipyapi.nifi.ParameterEntity(parameter=nipyapi.nifi.ParameterDTO(
                    name=name, sensitive=param.sensitive, description=param.description,
                    referenced_assets=[nipyapi.nifi.AssetReferenceDTO(id=asset_id, name=asset_name)])))
            else:
                updated.append(nipyapi.nifi.ParameterEntity(parameter=param))
        body = nipyapi.nifi.ParameterContextEntity(id=ctx_id, revision=pc.revision,
            component=nipyapi.nifi.ParameterContextDTO(id=ctx_id, name=pc.component.name, parameters=updated))
        req = pc_api.submit_parameter_context_update(context_id=ctx_id, body=body)
        rid = req.request.request_id
        while True:
            time.sleep(1)
            st = pc_api.get_parameter_context_update(context_id=ctx_id, request_id=rid)
            if st.request.complete:
                if st.request.failure_reason:
                    print("  ERROR: %s" % st.request.failure_reason, file=sys.stderr)
                else:
                    print("  Bound '%s' to '%s'" % (asset_param, asset_name))
                pc_api.delete_update_request(context_id=ctx_id, request_id=rid)
                break


# ──────────────────────────────────────────────
# 9. List, start, and stop flows
# ──────────────────────────────────────────────

def _get_runtime_name(args):
    runtime_name = args.runtime or OPENFLOW_RUNTIME
    if not runtime_name:
        print("ERROR: No runtime specified. Use --runtime NAME or set OPENFLOW_RUNTIME in .env_secure", file=sys.stderr)
        sys.exit(1)
    return runtime_name

def _connect_to_named_runtime(runtime_name):
    connect_to_runtime(resolve_runtime_url(runtime_name))

def _get_root_process_groups():
    root_pg_id = nipyapi.canvas.get_root_pg_id()
    flow = nipyapi.nifi.FlowApi().get_flow(root_pg_id)
    return flow.process_group_flow.flow.process_groups or []

def _find_pg_by_name(name):
    pgs = _get_root_process_groups()
    matches = [pg for pg in pgs if pg.component.name == name]
    if not matches:
        print("ERROR: Flow '%s' not found. Available: %s" % (name, ", ".join(pg.component.name for pg in pgs)), file=sys.stderr)
        sys.exit(1)
    return matches[0]


# ──────────────────────────────────────────────
# 10. Command handlers
# ──────────────────────────────────────────────

def cmd_list_runtimes(args):
    runtimes = get_all_runtimes()
    if not runtimes:
        print("No runtimes found.")
        return
    print("%-25s %-15s %-15s %-12s %s" % ("NAME", "DATABASE", "SCHEMA", "STATUS", "URL"))
    print("-" * 120)
    for r in runtimes:
        print("%-25s %-15s %-15s %-12s %s" % (r["name"], r["database_name"], r["schema_name"], r["status"], r["url"]))

def cmd_list_flows(args):
    _connect_to_named_runtime(_get_runtime_name(args))
    pgs = _get_root_process_groups()
    if not pgs:
        print("No flows deployed.")
        return
    print("%-40s %-40s %s" % ("NAME", "ID", "STATUS"))
    print("-" * 100)
    for pg in pgs:
        snap = pg.status.aggregate_snapshot
        state = "RUNNING" if snap.active_thread_count and snap.active_thread_count > 0 else "STOPPED"
        print("%-40s %-40s %s" % (pg.component.name, pg.id, state))

def cmd_start(args):
    _connect_to_named_runtime(_get_runtime_name(args))
    pg = _find_pg_by_name(args.flow_name)
    print("Starting: %s" % pg.component.name)
    nipyapi.canvas.schedule_process_group(pg.id, True)
    print("Flow started.")

def cmd_stop(args):
    _connect_to_named_runtime(_get_runtime_name(args))
    pg = _find_pg_by_name(args.flow_name)
    print("Stopping: %s" % pg.component.name)
    nipyapi.canvas.schedule_process_group(pg.id, False)
    print("Flow stopped.")

def cmd_deploy(args):
    runtime_name = _get_runtime_name(args)

    # Step 1: Resolve runtime URL
    runtime_url = resolve_runtime_url(runtime_name)

    # Step 2: Download flow JSON from GitHub
    flow_json = download_flow_from_github(args.bucket, args.flow)

    # Step 3: Download config YAML from GitHub (optional)
    flow_config = None
    if not args.skip_config:
        flow_config = download_config_from_github(args.bucket, args.flow)

    # Step 4: Connect to runtime
    root_pg_id = connect_to_runtime(runtime_url)

    # Step 5: Resolve Snowflake secrets via Parameter Provider
    contexts_to_inherit = []
    if flow_config and flow_config.get("parameters"):
        flow_config["parameters"], contexts_to_inherit = resolve_sf_secrets(flow_config["parameters"])

    # Step 6: Deploy flow
    pg_id = deploy_flow_to_runtime(root_pg_id, flow_json, flow_name=args.name)

    # Step 7: Add inherited parameter contexts (from Parameter Provider)
    if contexts_to_inherit:
        print("\nWiring inherited parameter contexts...")
        add_inherited_contexts(pg_id, contexts_to_inherit)

    # Step 8: Apply parameters (secrets are now #{SECRET_NAME} references)
    if flow_config and flow_config.get("parameters"):
        apply_parameters(pg_id, flow_config["parameters"])

    # Step 9: Upload assets
    if flow_config and flow_config.get("assets"):
        upload_assets(pg_id, flow_config["assets"])

    # Step 10: Start the flow
    if args.start:
        print("\nStarting flow...")
        nipyapi.canvas.schedule_process_group(pg_id, True)
        print("Flow started.")

    print("\nDone! Process group ID: %s" % pg_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Secure deploy: NiFi flows with Snowflake secret resolution")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("list-runtimes", help="List all available OpenFlow runtimes")

    lf = subparsers.add_parser("list-flows", help="List deployed flows on a runtime")
    lf.add_argument("--runtime", default=None)

    dp = subparsers.add_parser("deploy", help="Deploy a flow to an OpenFlow runtime")
    dp.add_argument("bucket", help="Flow bucket directory (e.g. 'sgope')")
    dp.add_argument("flow", help="Flow name without .json")
    dp.add_argument("--runtime", default=None, help="Runtime name (overrides .env_secure)")
    dp.add_argument("--name", default=None, help="Override process group name")
    dp.add_argument("--start", action="store_true", help="Start after deploying")
    dp.add_argument("--skip-config", action="store_true", help="Skip config.secure.yaml")

    sp = subparsers.add_parser("start", help="Start a deployed flow")
    sp.add_argument("flow_name")
    sp.add_argument("--runtime", default=None)

    stp = subparsers.add_parser("stop", help="Stop a deployed flow")
    stp.add_argument("flow_name")
    stp.add_argument("--runtime", default=None)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        {"list-runtimes": cmd_list_runtimes, "list-flows": cmd_list_flows,
         "deploy": cmd_deploy, "start": cmd_start, "stop": cmd_stop}[args.command](args)
    except requests.HTTPError as e:
        print("API error: %s" % e, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print("Error: %s" % e, file=sys.stderr)
        sys.exit(1)
