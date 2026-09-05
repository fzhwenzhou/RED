"""GCP helper for scripts/red_env.sh — every call uses Application Default
Credentials (google-auth), so none of this needs the gcloud CLI to be
authenticated (only `gcloud auth application-default login` once, which
red_env.sh setup handles).

Subcommands:
    default-sa      print the project's default compute service account email
    ensure-apis     enable compute + aiplatform APIs if not already enabled
    llm-sa          print the service account attached to chia-red-llm-0 (or NONE)
    ensure-iam      grant the default compute SA roles/aiplatform.user (the
                    llm node calls Gemini on Vertex as this SA via metadata
                    ADC; without the role every prompt 403s with
                    aiplatform.endpoints.predict denied)
    count           print the number of cluster instances (0 when none)
    list            show cluster instances, firewall rules, disks, addresses
    verify-clean    exit 0 when no cluster instances/disks/addresses remain
    force-clean     delete ALL cluster resources directly (instances, disks,
                    firewall rules, addresses) — fallback when `chia down`
                    fails or leaves leftovers

Project is RED_GCP_PROJECT (default: the RED project id below).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

import google.auth
import google.auth.transport.requests
from google.cloud import compute_v1

PROJECT = os.environ.get("RED_GCP_PROJECT", "project-160a0199-6b4a-464c-86a")
CLUSTER = os.environ.get("RED_CLUSTER_NAME", "red")

APIS = ("compute.googleapis.com", "aiplatform.googleapis.com")


# ---------------------------------------------------------------------------
# REST (serviceusage / project metadata) with ADC
# ---------------------------------------------------------------------------

def _token() -> str:
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def _rest(method: str, url: str, data: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {_token()}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            body = r.read() or b"{}"
            return r.status, json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {}


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_default_sa() -> int:
    s, b = _rest("GET", f"https://compute.googleapis.com/compute/v1/projects/{PROJECT}")
    sa = b.get("defaultServiceAccount")
    if not sa:
        print(f"error: no default compute SA (HTTP {s}): {b}", file=sys.stderr)
        return 1
    print(sa)
    return 0


def cmd_ensure_apis() -> int:
    rc = 0
    for svc in APIS:
        url = f"https://serviceusage.googleapis.com/v1/projects/{PROJECT}/services/{svc}"
        s, b = _rest("GET", url)
        if b.get("state") == "ENABLED":
            print(f"api {svc}: ENABLED")
            continue
        s, b = _rest("POST", url + ":enable", {})
        if s in (200, 201):
            print(f"api {svc}: enable issued")
        else:
            print(f"api {svc}: enable FAILED (HTTP {s}): {b}", file=sys.stderr)
            rc = 1
    return rc


def cmd_ensure_iam() -> int:
    """Grant the project's default compute SA roles/aiplatform.user.

    Idempotent. Uses getIamPolicy/setIamPolicy with the policy's etag so
    concurrent updates aren't clobbered.
    """
    s, sa_body = _rest("GET", f"https://compute.googleapis.com/compute/v1/projects/{PROJECT}")
    sa = sa_body.get("defaultServiceAccount")
    if not sa:
        print(f"error: no default compute SA (HTTP {s})", file=sys.stderr)
        return 1
    member = f"serviceAccount:{sa}"
    role = "roles/aiplatform.user"

    url = f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}"
    s, policy = _rest("POST", f"{url}:getIamPolicy", {})
    if s != 200:
        print(f"error: getIamPolicy HTTP {s}: {policy}", file=sys.stderr)
        return 1
    for b in policy.get("bindings", []):
        if b.get("role") == role:
            if member in b.get("members", []):
                print(f"{role}: already granted to {sa}")
                return 0
            b.setdefault("members", []).append(member)
            break
    else:
        policy.setdefault("bindings", []).append(
            {"role": role, "members": [member]})
    s, resp = _rest("POST", f"{url}:setIamPolicy", {"policy": policy})
    if s != 200:
        print(f"error: setIamPolicy HTTP {s}: {resp}", file=sys.stderr)
        return 1
    print(f"{role}: granted to {sa}")
    return 0


def _cluster_instances():
    c = compute_v1.InstancesClient()
    req = compute_v1.AggregatedListInstancesRequest(
        project=PROJECT, filter=f"labels.chia-cluster={CLUSTER}")
    for _zone, lst in c.aggregated_list(request=req):
        for i in (lst.instances or []):
            yield i


def cmd_llm_sa() -> int:
    for i in _cluster_instances():
        if i.labels.get("chia-node-type") == "llm":
            sas = [sa.email for sa in (i.service_accounts or [])]
            print(",".join(sas) if sas else "NONE")
            return 0
    print("ABSENT")  # no llm instance right now
    return 0


def cmd_count() -> int:
    """Print how many instances this cluster currently has (0 when none).

    red_env.sh uses it to pick between `chia up` (fresh provision) and
    `chia up --add` (discover + join what already exists)."""
    print(len(list(_cluster_instances())))
    return 0


def cmd_list() -> int:
    insts = list(_cluster_instances())
    print(f"instances ({len(insts)}):")
    for i in insts:
        ips = [ac.nat_i_p for ni in (i.network_interfaces or [])
               for ac in (ni.access_configs or []) if ac.nat_i_p]
        spot = "spot" if i.scheduling and i.scheduling.provisioning_model == "SPOT" else "std "
        sas = [sa.email.split("@")[0] for sa in (i.service_accounts or [])]
        print(f"  {i.name:<24} {i.status:<10} {spot} "
              f"{','.join(ips):<16} sa={','.join(sas) or '-'}")

    fw = compute_v1.FirewallsClient()
    rules = [f.name for f in fw.list(project=PROJECT)
             if f.name.startswith(f"chia-{CLUSTER}")]
    print(f"firewall rules ({len(rules)}): {', '.join(rules) or '-'}")

    d = compute_v1.DisksClient()
    ndisks = sum(len(lst.disks or []) for _z, lst in d.aggregated_list(project=PROJECT))
    a = compute_v1.AddressesClient()
    naddr = sum(len(lst.addresses or []) for _r, lst in a.aggregated_list(project=PROJECT))
    print(f"disks: {ndisks}   static addresses: {naddr}")
    return 0


def cmd_force_clean() -> int:
    """Delete every cluster resource directly via the compute API.

    `chia down` terminates instances by label discovery; if it crashes (or
    was never run) this is the safety net that guarantees nothing keeps
    billing. Only touches resources named/labeled for this cluster.
    """
    prefix = f"chia-{CLUSTER}"

    # 1. Instances (cluster-labeled, any zone) — boot disks auto-delete.
    insts = list(_cluster_instances())
    c = compute_v1.InstancesClient()
    for i in insts:
        zone = i.zone.rsplit("/", 1)[-1]
        print(f"delete instance {i.name} ({zone}, {i.status})")
        try:
            c.delete(project=PROJECT, zone=zone, instance=i.name)
        except Exception as e:  # already gone / in-flight delete
            print(f"  warning: {e}", file=sys.stderr)

    # Wait for instances to disappear so their disks release.
    if insts:
        deadline = time.time() + 240
        while time.time() < deadline:
            if not list(_cluster_instances()):
                break
            time.sleep(5)
        else:
            print("warning: instances still present after 240s", file=sys.stderr)

    # 2. Orphaned cluster-named disks (boot disks without auto_delete).
    d = compute_v1.DisksClient()
    for z, lst in d.aggregated_list(project=PROJECT):
        for dk in (lst.disks or []):
            if dk.name.startswith(prefix):
                zone = z.rsplit("/", 1)[-1]
                print(f"delete disk {dk.name} ({zone})")
                try:
                    d.delete(project=PROJECT, zone=zone, disk=dk.name)
                except Exception as e:
                    print(f"  warning: {e}", file=sys.stderr)

    # 3. Cluster firewall rules (free, but keep the project tidy).
    fw = compute_v1.FirewallsClient()
    for f in fw.list(project=PROJECT):
        if f.name.startswith(prefix):
            print(f"delete firewall {f.name}")
            try:
                fw.delete(project=PROJECT, firewall=f.name)
            except Exception as e:
                print(f"  warning: {e}", file=sys.stderr)

    # 4. Cluster-named static addresses.
    a = compute_v1.AddressesClient()
    for r, lst in a.aggregated_list(project=PROJECT):
        for ad in (lst.addresses or []):
            if ad.name.startswith(prefix):
                region = r.rsplit("/", 1)[-1]
                print(f"delete address {ad.name} ({region})")
                try:
                    a.delete(project=PROJECT, region=region, address=ad.name)
                except Exception as e:
                    print(f"  warning: {e}", file=sys.stderr)
    return 0


def cmd_verify_clean() -> int:
    insts = [i.name for i in _cluster_instances()]
    d = compute_v1.DisksClient()
    disks = [dk.name for _z, lst in d.aggregated_list(project=PROJECT)
             for dk in (lst.disks or [])]
    a = compute_v1.AddressesClient()
    addrs = [ad.name for _r, lst in a.aggregated_list(project=PROJECT)
             for ad in (lst.addresses or [])]
    leftovers = insts + disks + addrs
    if leftovers:
        print("LEFTOVER GCP RESOURCES (still billing):")
        for n in leftovers:
            print(f"  {n}")
        return 1
    print("clean: 0 instances, 0 disks, 0 static addresses — nothing billing")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    fn = {
        "default-sa": cmd_default_sa,
        "ensure-apis": cmd_ensure_apis,
        "llm-sa": cmd_llm_sa,
        "ensure-iam": cmd_ensure_iam,
        "count": cmd_count,
        "list": cmd_list,
        "verify-clean": cmd_verify_clean,
        "force-clean": cmd_force_clean,
    }.get(cmd)
    if fn is None:
        print(__doc__, file=sys.stderr)
        return 2
    return fn()


if __name__ == "__main__":
    sys.exit(main())
