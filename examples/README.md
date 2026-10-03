# kvm connector — examples

KVM keyboard/mouse control (gated by a device).

## Install
```bash
urirun install urirun-connector-kvm
```
`urirun install` resolves catalog ids via connect.ifuri.com; `--catalog <url>` points at a
local/on-prem registry; a full package name / git URL / path falls back to `pip install`.

## Run
```bash
# KVM keyboard/mouse control (gated by a device) (read)
urirun run 'kvm://host/input/command/key' --payload '{"key": "a"}' --allow 'kvm://*'

# preview without running (dry-run): drop --execute
urirun run 'kvm://host/input/command/key' --payload '{"key": "a"}' --allow 'kvm://*'
```
> Config-gated: without runtime config this prints the plan (dry-run).

## Verified ONLYOFFICE flow (KVM + URI process)

[`onlyoffice-uri-flow.yaml`](onlyoffice-uri-flow.yaml) is an executable host flow that:

1. checks the KVM backends and installed office apps,
2. launches ONLYOFFICE through its XDG/Flatpak desktop id,
3. requires the word `ONLYOFFICE` to be visible before sending any keyboard input,
4. batches focus, `ctrl+n`, and typing in one `task/command/run`,
5. verifies the typed text and stores a final screenshot.

The target desktop session must already be unlocked. The required visibility check is a safety
gate: if the laptop is on the GNOME lock screen, the flow stops before `ctrl+n` or typing. Preview
the resolved routes first, then opt into execution:

```bash
urirun host flow run examples/onlyoffice-uri-flow.yaml \
  --node-url laptop=http://192.168.188.201:8765

urirun host flow run examples/onlyoffice-uri-flow.yaml \
  --node-url laptop=http://192.168.188.201:8765 \
  --execute --rollback-on-failure \
  --artifact-dir .urirun/artifacts/onlyoffice-smoke
```

This is a pixel/UI smoke test, not a semantic document API. For reliable production editing,
prefer a LibreOffice UNO or ONLYOFFICE document adapter for document operations and use KVM only
for launch, exceptional dialogs, and visual verification.

## Inspect the runtime (no path — like error:// / log://)
```bash
urirun list | grep 'kvm://'                                   # this connector's routes
urirun run 'registry://local/routes/query/list' --payload '{"scheme":"kvm"}' --allow 'registry://*'
urirun run 'registry://local/bindings/query/show' --payload '{"uri":"kvm://host/input/command/key"}' --allow 'registry://*'   # full typed contract
urirun errors                                                      # recent runtime errors (error://)
```

## Contract scenarios — many URIs, one gate (`urirun-contract-*`)
Drive every route + every wire across the sibling scenario packages (capture-click, windowpair,
filepair, kvstore) through the standalone `urirun_contract` gate, then optionally the live
cross-process/cross-language handoff:
```bash
python examples/contract_scenarios.py                 # in-process: conform each URI + each wire + teeth
bash   examples/contract_scenarios.sh integration     # + real HTTP producer→consumer (py & go)
```
Covers 4 scenarios × 2 URIs × 1 wire each (8 routes, 4 edges): each golden payload/envelope is
checked against the shared `contracts.json`, each wire's handoff is typed (FULL/PARTIAL), and a
corrupted envelope is rejected. Enforced in CI by `tests/test_contract_scenarios.py`.

## Generate a client / API surface from the binding
```bash
urirun discover | urirun gen openapi - --out openapi.json   # OpenAPI 3 (one path per route)
urirun discover | urirun gen proto   - --out service.proto  # protobuf + gRPC (typed rpc per route)
urirun discover | urirun gen client  - --out client.py      # typed Python client
```
