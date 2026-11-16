#!/usr/bin/env python3
"""Isolated Apigee X lab environment for the model router.

Creates, and later removes, a test surface that sits next to the production gateway on the
same Apigee runtime instance: its own environment, environment group and hostname, API
products, developers and apps. Proxy bundles are imported as new revisions and deployed to
the lab environment only.

It never writes to an environment, environment group, API product, developer or app it did
not create: lab resources must carry the `-lab` naming enforced below, and products,
developers, apps and the environment are tagged `managed-by=apigee_lab_env` so `down` can
check before it deletes. `./deploy.sh` (the production path) replaces API product
environment lists and prunes proxy revisions; do not point it at a lab environment.

    plan     show what exists and what `up` would create                      (read-only)
    up       create the environment, environment group and attachments
    access   create lab API products, developers and apps
    deploy   import compiled bundles as new revisions, deploy them to the lab environment
    status   environment, attachment and deployment state                     (read-only)
    key      write one lab app's consumer key to a file (mode 0600); it is never printed
    down     undeploy and delete everything the commands above created

Auth: Application Default Credentials (`gcloud auth application-default login`) or a token
in GCP_ACCESS_TOKEN. Standard library only.

Cost: an Apigee pay-as-you-go environment is billed per hour for as long as it exists, used
or not. Run `down` when the lab is no longer needed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

MANAGED_BY = "apigee_lab_env"
PROXIES = ("claude-code-gateway", "antigravity-gateway", "codex-gateway")
ALL_TIERS = "tier1-fast,tier2-daily,tier3-frontier,tier4-open"
STANDARD_TIERS = "tier1-fast,tier2-daily,tier4-open"

# (product name suffix, display name, allowed tiers, daily token quota)
PRODUCTS = (
    ("standard", "Coding Agent Lab - Standard Tier", STANDARD_TIERS, "5000000"),
    ("frontier", "Coding Agent Lab - Frontier Tier", ALL_TIERS, "25000000"),
)
# (developer local part, first name, last name, product suffix, app name)
DEVELOPERS = (
    ("lab-std", "Lab", "Standard", "standard", "router-lab-std-app"),
    ("lab-std-2", "Lab", "StandardTwo", "standard", "router-lab-std2-app"),
    ("lab-frontier", "Lab", "Frontier", "frontier", "router-lab-frontier-app"),
    ("lab-ci-svc", "Lab", "CiService", "standard", "router-lab-ci-svc-app"),
    ("priya.patel", "Priya", "Patel", "frontier", "router-lab-priya-claude-app"),
    ("marcus.chen", "Marcus", "Chen", "frontier", "router-lab-marcus-codex-app"),
    ("elena.rostova", "Elena", "Rostova", "frontier", "router-lab-elena-agy-app"),
    ("david.kim", "David", "Kim", "frontier", "router-lab-david-claude-app"),
    ("sarah.jenkins", "Sarah", "Jenkins", "standard", "router-lab-sarah-pi-app"),
)


class ApiError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: Any) -> None:
        detail = body
        if isinstance(body, dict):
            detail = (body.get("error") or {}).get("message") or body
        super().__init__("%s %s -> HTTP %s: %s" % (method, url.split("?")[0], status, str(detail)[:600]))
        self.status = status
        self.body = body


class Client:
    def __init__(self, project: str) -> None:
        self.project = project
        self.base = "https://apigee.googleapis.com/v1/organizations/%s" % urllib.parse.quote(project, safe="")
        self._token = ""
        self._token_at = 0.0
        # Google APIs are reached directly; ignore any proxy variables in the environment.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def token(self) -> str:
        env_token = os.environ.get("GCP_ACCESS_TOKEN", "").strip()
        if env_token:
            return env_token
        if self._token and time.time() - self._token_at < 20 * 60:
            return self._token
        try:
            out = subprocess.check_output(
                ["gcloud", "auth", "application-default", "print-access-token", "--quiet"],
                text=True, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        if not out:
            raise SystemExit("No Google credentials: run `gcloud auth application-default login` "
                             "or set GCP_ACCESS_TOKEN.")
        self._token, self._token_at = out, time.time()
        return out

    def call(self, method: str, url: str, body: Any = None, *, content_type: Optional[str] = "application/json",
             ok: Tuple[int, ...] = (200, 201), allow: Tuple[int, ...] = (), timeout: int = 60) -> Tuple[int, Any]:
        """One API call. Returns (status, parsed body). Statuses in `allow` are returned
        instead of raised, for the "does it exist yet" lookups."""
        if not url.startswith("https://"):
            url = self.base + url
        headers = {"Authorization": "Bearer " + self.token(), "X-Goog-User-Project": self.project}
        data = None
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
        elif isinstance(body, bytes):
            data = body
        if content_type and data is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                status = response.status
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            status = error.code
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"raw": raw[:600]}
        if status in ok or status in allow:
            return status, parsed
        raise ApiError(method, url, status, parsed)

    def wait_operation(self, operation: Dict[str, Any], what: str, timeout_s: int = 1800) -> None:
        """Poll a long-running operation until it finishes; raise if it reports an error."""
        name = operation.get("name", "")
        if not name or "/operations/" not in name:
            return
        deadline = time.time() + timeout_s
        last_state = ""
        while time.time() < deadline:
            _, current = self.call("GET", "https://apigee.googleapis.com/v1/" + name)
            if current.get("done"):
                if current.get("error"):
                    raise RuntimeError("%s failed: %s" % (what, current["error"].get("message", current["error"])))
                print("  %s: done" % what, flush=True)
                return
            state = (current.get("metadata") or {}).get("state", "IN_PROGRESS")
            if state != last_state:
                print("  %s: %s" % (what, state), flush=True)
                last_state = state
            time.sleep(10)
        raise RuntimeError("%s did not finish within %d s (operation %s)" % (what, timeout_s, name))


def require_lab_names(args: argparse.Namespace) -> None:
    """Refuse anything that is not unmistakably a lab resource."""
    if not args.env.endswith("-lab"):
        raise SystemExit("--env must end with '-lab' (got %r); this tool only manages lab environments." % args.env)
    if "-lab" not in args.envgroup:
        raise SystemExit("--envgroup must contain '-lab' (got %r)." % args.envgroup)
    if not args.product_prefix.endswith("-lab"):
        raise SystemExit("--product-prefix must end with '-lab' (got %r)." % args.product_prefix)


def attribute(entity: Dict[str, Any], name: str) -> str:
    for item in entity.get("attributes") or []:
        if item.get("name") == name:
            return str(item.get("value", ""))
    return ""


def env_is_managed(env: Dict[str, Any]) -> bool:
    for item in ((env.get("properties") or {}).get("property") or []):
        if item.get("name") == "managed-by" and item.get("value") == MANAGED_BY:
            return True
    return False


def product_name(args: argparse.Namespace, suffix: str) -> str:
    return "%s-%s" % (args.product_prefix, suffix)


def developer_email(args: argparse.Namespace, local_part: str) -> str:
    return "%s@%s" % (local_part, args.developer_domain)


# ----------------------------------------------------------------------------- plan/status


def describe(client: Client, args: argparse.Namespace) -> Dict[str, Any]:
    state: Dict[str, Any] = {}
    status, env = client.call("GET", "/environments/%s" % args.env, allow=(404,))
    state["environment"] = None if status == 404 else {
        "name": env.get("name"), "type": env.get("type"), "state": env.get("state"), "managed": env_is_managed(env)}
    status, group = client.call("GET", "/envgroups/%s" % args.envgroup, allow=(404,))
    state["envgroup"] = None if status == 404 else {
        "name": group.get("name"), "hostnames": group.get("hostnames") or [], "state": group.get("state")}
    if status != 404:
        _, attached = client.call("GET", "/envgroups/%s/attachments" % args.envgroup)
        state["envgroup"]["environments"] = [a.get("environment") for a in attached.get("environmentGroupAttachments") or []]
    _, instance_attachments = client.call("GET", "/instances/%s/attachments" % args.instance)
    state["instance_attached"] = any(
        a.get("environment") == args.env for a in instance_attachments.get("attachments") or [])
    deployments: List[Dict[str, Any]] = []
    if state["environment"]:
        _, deployed = client.call("GET", "/environments/%s/deployments" % args.env)
        for item in deployed.get("deployments") or []:
            deployments.append({"proxy": item.get("apiProxy"), "revision": item.get("revision")})
    state["deployments"] = deployments
    return state


def cmd_plan(client: Client, args: argparse.Namespace) -> int:
    require_lab_names(args)
    state = describe(client, args)
    print(json.dumps({"project": client.project, "current": state, "would_create": {
        "environment": None if state["environment"] else {"name": args.env, "type": args.env_type},
        "envgroup": None if state["envgroup"] else {"name": args.envgroup, "hostnames": args.hostname},
        "instance_attachment": None if state["instance_attached"] else args.instance,
    }}, indent=2))
    return 0


def cmd_status(client: Client, args: argparse.Namespace) -> int:
    require_lab_names(args)
    state = describe(client, args)
    for item in state["deployments"]:
        _, detail = client.call("GET", "/environments/%s/apis/%s/revisions/%s/deployments"
                                % (args.env, item["proxy"], item["revision"]))
        item["state"] = detail.get("state")
        item["service_account"] = detail.get("serviceAccount")
        item["errors"] = [e.get("message") for e in detail.get("errors") or []]
    print(json.dumps({"project": client.project, "current": state}, indent=2))
    return 0


# ----------------------------------------------------------------------------- up


def cmd_up(client: Client, args: argparse.Namespace) -> int:
    require_lab_names(args)
    if not args.hostname:
        raise SystemExit("up needs at least one --hostname for the lab environment group.")

    # A hostname may belong to one environment group only; never take one from another group.
    _, groups = client.call("GET", "/envgroups")
    for group in groups.get("environmentGroups") or []:
        if group.get("name") == args.envgroup:
            continue
        clash = sorted(set(group.get("hostnames") or []) & set(args.hostname))
        if clash:
            raise SystemExit("hostname %s already belongs to environment group %r" % (clash, group.get("name")))

    status, env = client.call("GET", "/environments/%s" % args.env, allow=(404,))
    if status == 404:
        print("Creating environment %s (type %s)" % (args.env, args.env_type), flush=True)
        _, operation = client.call("POST", "/environments", {
            "name": args.env, "type": args.env_type, "apiProxyType": "PROGRAMMABLE", "deploymentType": "PROXY",
            "description": "Model router lab: isolated test environment (created by tools/apigee_lab_env.py)",
            "properties": {"property": [{"name": "managed-by", "value": MANAGED_BY}]},
        })
        client.wait_operation(operation, "environment %s" % args.env)
    elif not env_is_managed(env):
        raise SystemExit("environment %r exists but was not created by this tool; refusing to reuse it." % args.env)
    else:
        print("Environment %s already exists (%s)" % (args.env, env.get("state")), flush=True)

    status, group = client.call("GET", "/envgroups/%s" % args.envgroup, allow=(404,))
    if status == 404:
        print("Creating environment group %s %s" % (args.envgroup, args.hostname), flush=True)
        _, operation = client.call("POST", "/envgroups?name=%s" % urllib.parse.quote(args.envgroup, safe=""),
                                   {"name": args.envgroup, "hostnames": args.hostname})
        client.wait_operation(operation, "environment group %s" % args.envgroup)
    else:
        missing = sorted(set(args.hostname) - set(group.get("hostnames") or []))
        if missing:
            raise SystemExit("environment group %r exists without hostname(s) %s; add them by hand or use "
                             "another group name." % (args.envgroup, missing))
        print("Environment group %s already exists" % args.envgroup, flush=True)

    _, attached = client.call("GET", "/envgroups/%s/attachments" % args.envgroup)
    members = [a.get("environment") for a in attached.get("environmentGroupAttachments") or []]
    others = [name for name in members if name != args.env]
    if others:
        raise SystemExit("environment group %r also routes to %s; refusing to share it." % (args.envgroup, others))
    if args.env not in members:
        print("Attaching %s to environment group %s" % (args.env, args.envgroup), flush=True)
        _, operation = client.call("POST", "/envgroups/%s/attachments" % args.envgroup, {"environment": args.env})
        client.wait_operation(operation, "environment group attachment")

    _, instance_attachments = client.call("GET", "/instances/%s/attachments" % args.instance)
    if not any(a.get("environment") == args.env for a in instance_attachments.get("attachments") or []):
        print("Attaching %s to runtime instance %s (takes several minutes)" % (args.env, args.instance), flush=True)
        _, operation = client.call("POST", "/instances/%s/attachments" % args.instance, {"environment": args.env})
        client.wait_operation(operation, "instance attachment", timeout_s=3600)
    print("Lab environment ready: %s via %s" % (args.env, ", ".join(args.hostname)))
    return 0


# ----------------------------------------------------------------------------- access


def cmd_access(client: Client, args: argparse.Namespace) -> int:
    require_lab_names(args)
    if not args.developer_domain:
        raise SystemExit("access needs --developer-domain (developer records are <name>@<domain>).")
    for suffix, display, tiers, quota in PRODUCTS:
        name = product_name(args, suffix)
        body = {
            "name": name, "displayName": display, "approvalType": "auto",
            "description": "Lab-only product for environment %s" % args.env,
            "environments": [args.env], "proxies": list(PROXIES),
            "attributes": [
                {"name": "access", "value": "private"},
                {"name": "managed_by", "value": MANAGED_BY},
                {"name": "tierName", "value": name},
                {"name": "allowed_model_tiers", "value": tiers},
                {"name": "daily_token_quota", "value": quota},
                {"name": "llmQuota", "value": quota},
                {"name": "llmQuotaInterval", "value": "1"},
                {"name": "llmQuotaTimeUnit", "value": "day"},
                {"name": "finops_ingestion_mode", "value": os.environ.get("FINOPS_INGESTION_MODE", "USAGE")},
            ],
        }
        status, existing = client.call("GET", "/apiproducts/%s" % name, allow=(404,))
        if status == 404:
            client.call("POST", "/apiproducts", body)
            print("Created API product %s" % name)
        elif attribute(existing, "managed_by") != MANAGED_BY:
            raise SystemExit("API product %r exists but was not created by this tool; refusing to change it." % name)
        else:
            client.call("PUT", "/apiproducts/%s" % name, body)
            print("Updated API product %s" % name)

    for local_part, first, last, suffix, app_name in DEVELOPERS:
        email = developer_email(args, local_part)
        quoted = urllib.parse.quote(email, safe="")
        status, existing = client.call("GET", "/developers/%s" % quoted, allow=(404,))
        if status == 404:
            client.call("POST", "/developers", {
                "email": email, "firstName": first, "lastName": last, "userName": local_part,
                "attributes": [{"name": "product_tier", "value": product_name(args, suffix)},
                               {"name": "managed_by", "value": MANAGED_BY}],
            })
            print("Created developer %s" % email)
        elif attribute(existing, "managed_by") != MANAGED_BY:
            raise SystemExit("developer %r exists but was not created by this tool; refusing to use it." % email)
        status, _ = client.call("GET", "/developers/%s/apps/%s" % (quoted, app_name), allow=(404,))
        if status == 404:
            app_attrs = [{"name": "managed_by", "value": MANAGED_BY}]
            if "ci-svc" in app_name or "service" in app_name:
                app_attrs.append({"name": "credential_type", "value": "service_key"})
            client.call("POST", "/developers/%s/apps" % quoted, {
                "name": app_name, "apiProducts": [product_name(args, suffix)],
                "attributes": app_attrs,
            })
            print("Created app %s (%s)" % (app_name, email))
        else:
            print("App %s already exists (%s)" % (app_name, email))
    return 0


def read_consumer_key(client: Client, args: argparse.Namespace, app_name: str) -> str:
    """Consumer key of a lab app. The caller is responsible for not printing it."""
    for local_part, _, _, _, name in DEVELOPERS:
        if name != app_name:
            continue
        quoted = urllib.parse.quote(developer_email(args, local_part), safe="")
        _, app = client.call("GET", "/developers/%s/apps/%s" % (quoted, app_name))
        if attribute(app, "managed_by") != MANAGED_BY:
            raise SystemExit("app %r was not created by this tool." % app_name)
        for credential in app.get("credentials") or []:
            if credential.get("status") == "approved" and credential.get("consumerKey"):
                return str(credential["consumerKey"])
        raise SystemExit("app %r has no approved credential." % app_name)
    raise SystemExit("unknown lab app %r (known: %s)" % (app_name, ", ".join(d[4] for d in DEVELOPERS)))


def cmd_key(client: Client, args: argparse.Namespace) -> int:
    require_lab_names(args)
    if not args.developer_domain:
        raise SystemExit("key needs --developer-domain.")
    key = read_consumer_key(client, args, args.app)
    path = os.path.abspath(os.path.expanduser(args.out))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(key + "\n")
    os.chmod(path, 0o600)
    print("Wrote the consumer key of %s to %s (mode 0600)" % (args.app, path))
    return 0


# ----------------------------------------------------------------------------- deploy


def cmd_deploy(client: Client, args: argparse.Namespace) -> int:
    require_lab_names(args)
    status, env = client.call("GET", "/environments/%s" % args.env, allow=(404,))
    if status == 404 or not env_is_managed(env):
        raise SystemExit("environment %r is not a lab environment created by this tool; run `up` first." % args.env)
    service_account = args.service_account or "apigee-vertex-router-sa@%s.iam.gserviceaccount.com" % client.project

    bundles = {}
    for proxy in PROXIES:
        path = os.path.join(args.build_dir, "%s.zip" % proxy)
        if not os.path.isfile(path):
            raise SystemExit("missing bundle %s (run ./deploy.sh --stage <stage> --build-only first)" % path)
        with open(path, "rb") as handle:
            bundles[proxy] = handle.read()

    # Ensure org-scoped DataCollectors referenced by DC-CaptureFinOpsMetrics exist before bundle validation
    for dc_name, dc_type in (
        ("dc_gateway_key_name", "STRING"),
        ("dc_credential_type", "STRING"),
        ("dc_net_cash_cost_usd", "FLOAT"),
        ("dc_total_latency_ms", "INTEGER"),
        ("dc_ttft_ms", "INTEGER"),
        ("dc_ingestion_mode", "STRING"),
    ):
        client.call("POST", "/datacollectors", {"name": dc_name, "type": dc_type}, allow=(409,))

    deployed: Dict[str, str] = {}
    for proxy, data in bundles.items():
        boundary = "----ApigeeLabBundle%d" % int(time.time() * 1000)
        multipart = (
            ("--%s\r\n" % boundary)
            + 'Content-Disposition: form-data; name="file"; filename="%s.zip"\r\n' % proxy
            + "Content-Type: application/zip\r\n\r\n"
        ).encode("utf-8") + data + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        _, imported = client.call(
            "POST", "/apis?action=import&name=%s&validate=true" % urllib.parse.quote(proxy, safe=""),
            multipart, content_type="multipart/form-data; boundary=%s" % boundary, timeout=120)
        revision = str(imported.get("revision", ""))
        if not revision:
            raise RuntimeError("import of %s returned no revision: %s" % (proxy, imported))
        # override=true swaps the revision currently deployed in THIS environment only.
        client.call(
            "POST", "/environments/%s/apis/%s/revisions/%s/deployments?override=true&serviceAccount=%s"
            % (args.env, proxy, revision, urllib.parse.quote(service_account, safe="")),
            None, content_type=None, timeout=120)
        deployed[proxy] = revision
        print("Imported %s as revision %s and requested deployment to %s" % (proxy, revision, args.env), flush=True)

    deadline = time.time() + args.wait_seconds
    pending = dict(deployed)
    while pending and time.time() < deadline:
        for proxy, revision in list(pending.items()):
            _, detail = client.call("GET", "/environments/%s/apis/%s/revisions/%s/deployments"
                                    % (args.env, proxy, revision))
            errors = [e.get("message") for e in detail.get("errors") or []]
            if errors:
                raise RuntimeError("deployment of %s revision %s reported: %s" % (proxy, revision, errors))
            if detail.get("state") == "READY":
                print("  %s revision %s: READY" % (proxy, revision), flush=True)
                del pending[proxy]
        if pending:
            time.sleep(10)
    if pending:
        raise RuntimeError("not READY after %d s: %s" % (args.wait_seconds, pending))
    print(json.dumps({"environment": args.env, "revisions": deployed}))
    return 0


# ----------------------------------------------------------------------------- down


def cmd_down(client: Client, args: argparse.Namespace) -> int:
    require_lab_names(args)
    if not args.yes:
        raise SystemExit("down deletes the lab environment and its access objects; re-run with --yes.")

    status, env = client.call("GET", "/environments/%s" % args.env, allow=(404,))
    if status != 404 and not env_is_managed(env):
        raise SystemExit("environment %r was not created by this tool; refusing to delete it." % args.env)

    if status != 404:
        _, deployed = client.call("GET", "/environments/%s/deployments" % args.env)
        for item in deployed.get("deployments") or []:
            proxy, revision = item.get("apiProxy"), item.get("revision")
            client.call("DELETE", "/environments/%s/apis/%s/revisions/%s/deployments" % (args.env, proxy, revision),
                        allow=(404,), timeout=120)
            print("Undeployed %s revision %s from %s" % (proxy, revision, args.env), flush=True)

    if args.developer_domain:
        for local_part, _, _, _, app_name in DEVELOPERS:
            quoted = urllib.parse.quote(developer_email(args, local_part), safe="")
            status_dev, developer = client.call("GET", "/developers/%s" % quoted, allow=(404,))
            if status_dev == 404:
                continue
            if attribute(developer, "managed_by") != MANAGED_BY:
                print("Skipping developer %s: not created by this tool" % developer.get("email"))
                continue
            client.call("DELETE", "/developers/%s/apps/%s" % (quoted, app_name), allow=(404,))
            client.call("DELETE", "/developers/%s" % quoted, allow=(404,))
            print("Deleted developer %s and app %s" % (developer.get("email"), app_name))
    else:
        print("No --developer-domain given: lab developers and apps were left in place.")

    for suffix, _, _, _ in PRODUCTS:
        name = product_name(args, suffix)
        status_product, product = client.call("GET", "/apiproducts/%s" % name, allow=(404,))
        if status_product == 404:
            continue
        if attribute(product, "managed_by") != MANAGED_BY:
            print("Skipping API product %s: not created by this tool" % name)
            continue
        client.call("DELETE", "/apiproducts/%s" % name, allow=(404,))
        print("Deleted API product %s" % name)

    if status != 404:
        _, instance_attachments = client.call("GET", "/instances/%s/attachments" % args.instance)
        for item in instance_attachments.get("attachments") or []:
            if item.get("environment") == args.env:
                _, operation = client.call("DELETE", "/instances/%s/attachments/%s" % (args.instance, item["name"]))
                client.wait_operation(operation, "instance detachment", timeout_s=3600)

    status_group, _ = client.call("GET", "/envgroups/%s" % args.envgroup, allow=(404,))
    if status_group != 404:
        _, attached = client.call("GET", "/envgroups/%s/attachments" % args.envgroup)
        members = attached.get("environmentGroupAttachments") or []
        if any(a.get("environment") != args.env for a in members):
            print("Environment group %s routes to other environments; leaving it in place." % args.envgroup)
        else:
            for item in members:
                _, operation = client.call("DELETE", "/envgroups/%s/attachments/%s" % (args.envgroup, item["name"]))
                client.wait_operation(operation, "environment group detachment")
            _, operation = client.call("DELETE", "/envgroups/%s" % args.envgroup)
            client.wait_operation(operation, "environment group deletion")
            print("Deleted environment group %s" % args.envgroup)

    if status != 404:
        _, operation = client.call("DELETE", "/environments/%s" % args.env)
        client.wait_operation(operation, "environment deletion")
        print("Deleted environment %s" % args.env)
    print("Proxy revisions imported for the lab are left on the proxies (undeployed); nothing else remains.")
    return 0


# ----------------------------------------------------------------------------- seed-demo


def cmd_seed_demo(client: Client, args: argparse.Namespace) -> int:
    """Provision lab persona apps and send live multi-developer, multi-harness traffic."""
    import concurrent.futures
    import secrets

    require_lab_names(args)
    if not args.developer_domain:
        raise SystemExit("seed-demo needs --developer-domain (e.g. example.com).")
    gw_url = (args.gateway_url or "").strip().rstrip("/")
    parsed_gw = urllib.parse.urlparse(gw_url)
    if parsed_gw.scheme not in ("http", "https") or not parsed_gw.netloc:
        raise SystemExit("--gateway-url must be a valid http:// or https:// gateway URL.")

    print("1/2 Ensuring multi-developer lab personas & CI service key exist in Apigee...", flush=True)
    cmd_access(client, args)

    app_keys: Dict[str, str] = {}
    for app_id in (
        "router-lab-priya-claude-app",
        "router-lab-marcus-codex-app",
        "router-lab-elena-agy-app",
        "router-lab-david-claude-app",
        "router-lab-sarah-pi-app",
        "router-lab-ci-svc-app",
    ):
        app_keys[app_id] = read_consumer_key(client, args, app_id)

    turns: List[Tuple[str, str, Dict[str, str], Dict[str, Any]]] = []

    def add_claude(label: str, app_id: str, team: str, repo: str, model: str, prompt: str,
                   extra_hdrs: Optional[Dict[str, str]] = None) -> None:
        hdrs = {
            "x-api-key": app_keys[app_id],
            "anthropic-version": "2023-06-01",
            "User-Agent": "claude-cli/1.0.28",
            "x-team-name": team,
            "x-project-name": repo,
            "x-bypass-cache": "true",
            "traceparent": "00-%s-%s-01" % (secrets.token_hex(16), secrets.token_hex(8)),
        }
        if model == "auto":
            hdrs["x-routing-mode"] = "auto"
        if extra_hdrs:
            hdrs.update(extra_hdrs)
        turns.append((label, "/v1/messages", hdrs, {
            "model": model,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": prompt}],
        }))

    # Persona 1: Priya Patel (Staff Engineer, Claude Code, smart `auto` + Sonnet + Frontier architecture)
    add_claude("priya:auto-simple-1", "router-lab-priya-claude-app", "core-graph-engine", "bolt-protocol-v6",
               "auto", "Reply OK: what HTTP status code indicates success?")
    add_claude("priya:auto-simple-2", "router-lab-priya-claude-app", "core-graph-engine", "bolt-protocol-v6",
               "auto", "Format this 1-line JSON key: {\"ok\":true}")
    add_claude("priya:auto-codegen", "router-lab-priya-claude-app", "core-graph-engine", "bolt-protocol-v6",
               "auto", "Write a 1-line Python unit test regex helper for trace IDs.")
    add_claude("priya:auto-frontier", "router-lab-priya-claude-app", "core-graph-engine", "bolt-protocol-v6",
               "auto", "Architect a distributed lock-free Raft consensus deadlock recovery protocol in 2 bullet points.")
    add_claude("priya:sonnet-direct", "router-lab-priya-claude-app", "core-graph-engine", "bolt-protocol-v6",
               "claude-sonnet-4-6", "Summarize connection pool backpressure in 1 sentence.")

    # Persona 2: David Kim (Backend Engineer, Claude Code, manual Frontier Opus pins on trivial tasks -> Overkill!)
    for idx, p_txt in enumerate((
        "Fix typo in README header",
        "Rename variable x to count",
        "Format 2-line config comment",
    ), 1):
        add_claude("david:opus-overkill-%d" % idx, "router-lab-david-claude-app", "query-optimizer", "query-planner",
                   "claude-opus-4-6", p_txt)
    add_claude("david:auto-search", "router-lab-david-claude-app", "query-optimizer", "query-planner",
               "auto", "Grep for index hints in planner",
               {"x-is-subagent": "true", "User-Agent": "claude-cli/1.0.28 subagent/explore"})

    # Persona 3: Marcus Chen (SRE, OpenAI Codex CLI `/v1/responses`, auto + Flash + DeepSeek)
    for idx, (mdl, p_txt) in enumerate((
        ("auto", "Check healthz status code in 3 words"),
        ("gemini-2.5-flash", "Give 1-line Terraform state lock tip"),
        ("deepseek-v3.2", "Write a 1-line bash awk filter for HTTP 500"),
        ("claude-sonnet-4-6", "Explain PSC regional failover in 1 sentence"),
    ), 1):
        turns.append((
            "marcus:codex-%d-%s" % (idx, mdl),
            "/v1/responses",
            {
                "Authorization": "Bearer " + app_keys["router-lab-marcus-codex-app"],
                "User-Agent": "codex-cli/0.118.0",
                "x-team-name": "cloud-platform-sre",
                "x-project-name": "cloud-control-plane",
                "x-bypass-cache": "true",
                "traceparent": "00-%s-%s-01" % (secrets.token_hex(16), secrets.token_hex(8)),
            },
            {"model": mdl, "max_output_tokens": 32, "input": p_txt},
        ))

    # Persona 4: Elena Rostova (Applied AI, Google Antigravity IDE `/v1beta`, $0 Gemini Credit Pool + Subagents)
    for idx, (mdl, is_sub, p_txt) in enumerate((
        ("gemini-2.5-flash", False, "Reply PONG from Antigravity"),
        ("gemini-2.5-pro", False, "Give 1 sentence on eval harness calibration"),
        ("gemini-2.5-flash", True, "Scan test directory filenames"),
    ), 1):
        agy_hdrs = {
            "x-goog-api-key": app_keys["router-lab-elena-agy-app"],
            "User-Agent": "antigravity/1.104.0 darwin/arm64",
            "x-team-name": "applied-ai-agents",
            "x-project-name": "agent-eval-suite",
            "traceparent": "00-%s-%s-01" % (secrets.token_hex(16), secrets.token_hex(8)),
        }
        if is_sub:
            agy_hdrs["x-is-subagent"] = "true"
        turns.append((
            "elena:agy-%d-%s" % (idx, mdl),
            "/v1beta/models/%s:generateContent" % mdl,
            agy_hdrs,
            {"contents": [{"role": "user", "parts": [{"text": p_txt}]}],
             "generationConfig": {"maxOutputTokens": 32}},
        ))

    # Persona 5: Sarah Jenkins (Core Engineer, Pi / Cursor `/v1/chat/completions`, Qwen3-Coder MaaS + Auto)
    for idx, (mdl, p_txt) in enumerate((
        ("qwen3-coder", "Write a 1-line SQL SELECT query"),
        ("qwen3-coder", "Write a 1-line Rust slice bounds check"),
        ("auto", "Reply OK from Pi agent"),
    ), 1):
        turns.append((
            "sarah:pi-%d-%s" % (idx, mdl),
            "/v1/chat/completions",
            {
                "Authorization": "Bearer " + app_keys["router-lab-sarah-pi-app"],
                "User-Agent": "pi-agent/2.1.0",
                "X-Client-Name": "pi-agent",
                "x-team-name": "core-graph-engine",
                "x-project-name": "bolt-protocol-v6",
                "x-bypass-cache": "true",
                "traceparent": "00-%s-%s-01" % (secrets.token_hex(16), secrets.token_hex(8)),
            },
            {"model": mdl, "max_tokens": 32, "messages": [{"role": "user", "content": p_txt}]},
        ))

    # Persona 6: GitHub Actions CI Service Key (`service_key` — attributed to key, excluded from human user tables!)
    for idx, pr_author in enumerate((
        "priya.patel@" + args.developer_domain,
        "marcus.chen@" + args.developer_domain,
        "sarah.jenkins@" + args.developer_domain,
    ), 1):
        add_claude(
            "ci-bot:pr-review-%d" % idx,
            "router-lab-ci-svc-app",
            "cloud-platform-sre",
            "codingagent-modelrouter",
            "gemini-2.5-flash",
            "Summarize PR #%d security diff in 5 words." % (400 + idx),
            {
                "User-Agent": "github-actions-claude-bot/1.0",
                "x-user-id": pr_author,
                "baggage": "langfuse.environment=ci,langfuse.tags=pr-review",
            },
        )

    print("2/2 Sending %d live multi-developer, multi-harness turns through %s..." % (len(turns), gw_url), flush=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def send_one(item: Tuple[str, str, Dict[str, str], Dict[str, Any]]) -> Tuple[str, int, str, str, str]:
        lbl, path, hdrs, payload = item
        req_hdrs = dict(hdrs)
        req_hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(
            gw_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=req_hdrs,
            method="POST",
        )
        try:
            with opener.open(req, timeout=45) as resp:
                resp.read()
                info = {k.lower(): v for k, v in resp.info().items()}
                return (
                    lbl,
                    resp.status,
                    info.get("x-vertex-target-model", "-"),
                    info.get("x-gateway-credential-type", "-"),
                    info.get("x-net-cash-cost-usd", "-"),
                )
        except urllib.error.HTTPError as err:
            err.read()
            return (lbl, err.code, "error", "-", "-")
        except Exception as exc:
            return (lbl, 0, str(exc)[:40], "-", "-")

    ok_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for lbl, code, routed, cred_t, net_c in pool.map(send_one, turns):
            if 200 <= code < 300:
                ok_count += 1
            print("  [HTTP %s] %-26s -> model=%-24s cred=%-12s net_cash=$%s" % (code, lbl, routed, cred_t, net_c),
                  flush=True)

    print("\nSeeded %d/%d live FinOps turns across 5 human engineers + 1 CI service_key, 4 teams, and 4 harnesses."
          % (ok_count, len(turns)), flush=True)
    print("  - Terminal FinOps (immediate): ./vertex-connect.sh finops --admin --today")
    print("  - Cloud Monitoring (~60s):     set top-right time range to 1h or 1d and refresh!")
    return 0 if ok_count > 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", ""),
                        help="Apigee organization / GCP project (default: $GCP_PROJECT_ID)")
    parser.add_argument("--env", default="ai-gateway-classifier-lab")
    parser.add_argument("--envgroup", default="ai-gateway-lab-envgroup")
    parser.add_argument("--instance", default=os.environ.get("APIGEE_INSTANCE", "apigee-us-central1"))
    parser.add_argument("--product-prefix", default="coding-agent-lab")
    parser.add_argument("--developer-domain", default=os.environ.get("LAB_DEVELOPER_DOMAIN", ""))
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="show current state and what `up` would create")
    plan.add_argument("--hostname", action="append", default=[])
    plan.add_argument("--env-type", default="INTERMEDIATE", choices=("INTERMEDIATE", "COMPREHENSIVE"))
    plan.set_defaults(handler=cmd_plan)

    up = commands.add_parser("up", help="create the environment, environment group and attachments")
    up.add_argument("--hostname", action="append", default=[],
                    help="hostname routed to the lab environment (repeatable)")
    up.add_argument("--env-type", default="INTERMEDIATE", choices=("INTERMEDIATE", "COMPREHENSIVE"),
                    help="extensible proxies need INTERMEDIATE or COMPREHENSIVE")
    up.set_defaults(handler=cmd_up)

    access = commands.add_parser("access", help="create lab API products, developers and apps")
    access.set_defaults(handler=cmd_access)

    deploy = commands.add_parser("deploy", help="import bundles as new revisions and deploy them to the lab")
    deploy.add_argument("--build-dir", required=True, help="directory with the three <proxy>.zip bundles")
    deploy.add_argument("--service-account", default="",
                        help="runtime identity (default: apigee-vertex-router-sa@<project>)")
    deploy.add_argument("--wait-seconds", type=int, default=600)
    deploy.set_defaults(handler=cmd_deploy)

    status = commands.add_parser("status", help="environment, attachment and deployment state")
    status.set_defaults(handler=cmd_status)

    key = commands.add_parser("key", help="write a lab app's consumer key to a file (never printed)")
    key.add_argument("--app", required=True, choices=[d[4] for d in DEVELOPERS])
    key.add_argument("--out", required=True)
    key.set_defaults(handler=cmd_key)

    down = commands.add_parser("down", help="undeploy and delete everything this tool created")
    down.add_argument("--yes", action="store_true")
    down.set_defaults(handler=cmd_down)

    seed = commands.add_parser(
        "seed-demo",
        help="provision 5 human engineer personas + 1 CI service_key and send live multi-harness FinOps turns",
    )
    seed.add_argument(
        "--gateway-url",
        default=os.environ.get("GATEWAY_URL", "https://lab.switchyard.internal"),
        help="live lab gateway URL (default: $GATEWAY_URL or https://lab.switchyard.internal)",
    )
    seed.set_defaults(handler=cmd_seed_demo)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.project:
        raise SystemExit("--project (or GCP_PROJECT_ID) is required.")
    client = Client(args.project)
    try:
        return int(args.handler(client, args))
    except (ApiError, RuntimeError) as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
