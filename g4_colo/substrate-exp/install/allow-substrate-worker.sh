#!/usr/bin/env bash
# Allow priorityClassName substrate-worker next to colo-backfill in the cluster's colo-g4-guard admission policy
# (the policy shipped by both recipes admits non-GPU pods on G4 only at colo-backfill). Patches the live object;
# no recipe file is edited. Idempotent. Re-run after any `kubectl apply` of a recipe that re-creates the policy.
#   CONTEXT=<kube context> g4_colo/substrate-exp/install/allow-substrate-worker.sh
set -euo pipefail
C=${CONTEXT:?set CONTEXT}
expr=$(kubectl --context "$C" get validatingadmissionpolicy colo-g4-guard -o jsonpath='{.spec.validations[0].expression}')
if grep -q "substrate-worker" <<<"$expr"; then echo "already allowed"; exit 0; fi
new=$(sed "s/object.spec.priorityClassName == 'colo-backfill'/object.spec.priorityClassName in ['colo-backfill', 'substrate-worker']/" <<<"$expr")
grep -q "substrate-worker" <<<"$new" || { echo "pattern not found; policy differs" >&2; exit 1; }
python3 - "$new" > /tmp/vap-patch.json <<'EOF'
import json, sys
print(json.dumps([{"op": "replace", "path": "/spec/validations/0/expression", "value": sys.argv[1]}]))
EOF
kubectl --context "$C" patch validatingadmissionpolicy colo-g4-guard --type=json --patch-file /tmp/vap-patch.json
