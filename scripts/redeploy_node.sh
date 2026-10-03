#!/usr/bin/env bash
# Author: Tom Sapletta · Part of the ifURI solution.
#
# Redeploy the kvm connector to a mesh node that lost its merge-deployed routes
# (symptom: "Route not found: kvm.screen.query", routeCount collapses to built-ins).
# Merge-deploy does not reliably survive a node restart — this makes recovery one
# command, and vguard.Screen calls it automatically on NOT_FOUND (self-heal).
#
# Usage: scripts/redeploy_node.sh [NODE_URL]
#   NODE_URL   default http://192.168.188.201:8765 (lenovo)
#   URIRUN_PY  python of a venv that has BOTH urirun and urirun-connector-kvm
#              (default: ~/github/if-uri/urirun/venv/bin/python — the connector's
#               own venv has no urirun, do not use it)
#   URIRUN_NODE_NAME  optional URI authority alias; defaults to name from /health
#   URIRUN_KVM_SCREEN optional capture geometry, e.g. 1920x1080
set -euo pipefail

NODE_URL="${1:-http://192.168.188.201:8765}"
PY="${URIRUN_PY:-$HOME/github/if-uri/urirun/venv/bin/python}"
URIRUN="$(dirname "$PY")/urirun"
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../urirun_connector_kvm" && pwd)"
NODE_NAME="${URIRUN_NODE_NAME:-}"
if [[ -z "$NODE_NAME" ]]; then
  NODE_NAME="$("$PY" -c 'import json, sys, urllib.request; print(json.load(urllib.request.urlopen(sys.argv[1].rstrip("/") + "/health", timeout=3)).get("name", ""))' "$NODE_URL" 2>/dev/null || true)"
fi

BINDINGS="$(mktemp --suffix=.kvm-bindings.json)"
trap 'rm -f "$BINDINGS"' EXIT

# Neutral cwd: launched from ~/github/if-uri the repo FOLDER `urirun/` shadows the
# installed package (`urirun has no attribute connector`) — see urirun-package-shadow.
cd "$PKG_DIR"

# Flat-module refs: /deploy --code pushes files FLAT, so python.module must be
# "core", not "urirun_connector_kvm.core" (each module has a flat-import fallback).
URIRUN_DEPLOY_NODE_NAME="$NODE_NAME" "$PY" -c '
import copy, json, os
from urirun_connector_kvm.core import urirun_bindings

doc = urirun_bindings()
alias = os.environ.get("URIRUN_DEPLOY_NODE_NAME", "").strip()
if alias and alias != "host":
    for uri, binding in list(doc.get("bindings", {}).items()):
        marker = "://host/"
        if marker not in uri:
            continue
        alias_uri = uri.replace(marker, f"://{alias}/", 1)
        cloned = copy.deepcopy(binding)
        cloned["uri"] = alias_uri
        doc["bindings"][alias_uri] = cloned
print(json.dumps(doc).replace("urirun_connector_kvm.", ""))
' > "$BINDINGS"

CODE=()
for f in core.py _urirun_compat.py backends.py cdp.py _cdp_impl.py control.py environment.py \
         strategies.py surface.py _backends_surface.py _backends_uinput.py \
         launch_backends.py vnc.py contracts.py capture_worker.py readiness.py; do
  CODE+=(--code "$PKG_DIR/$f")
done

# URIRUN_NODE_SELF_URL lets kvm handlers compose SIBLING connector routes (vdisplay://,
# vql://) over the node's own loopback — in-node route composition, not package import.
ENV_ARGS=(--env URIRUN_NODE_SELF_URL=http://127.0.0.1:8765)
if [[ -n "${URIRUN_KVM_SCREEN:-}" ]]; then
  ENV_ARGS+=(--env "URIRUN_KVM_SCREEN=$URIRUN_KVM_SCREEN")
fi
exec "$URIRUN" host deploy "$NODE_URL" --bindings "$BINDINGS" \
  --allow 'kvm://**' --allow 'app://**' "${CODE[@]}" \
  "${ENV_ARGS[@]}" \
  --merge --persist --identity ~/.ssh/id_ed25519
