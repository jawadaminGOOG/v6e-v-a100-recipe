#!/usr/bin/env bash
# Node pools for the G4 colo recipe (GKE Standard). Set the variables, then run once:
#   PROJECT=my-project CLUSTER=my-cluster REGION=us-central1 SUFFIX=prod \
#   G4_ZONES=us-central1-a N4D_ZONES=us-central1-a,us-central1-b \
#     bash g4_colo/recipe/infra/create-node-pools.sh
#
#   - G4 pool: GPU nodes; static CPU manager (exclusive cores), image streaming for fast scale-out; labelled +
#     tainted for the g4-gpu ComputeClass. No GKE Sandbox: G4 backfill runs under runc (gVisor bypasses the
#     static CPU manager's container cpuset, see README section 2), so the sandbox taint would only add a
#     toleration every G4 pod must carry.
#   - N4D pool: CPU overflow nodes across zones; same kubelet config plus GKE Sandbox (gVisor) for
#     sandbox-overflow; n4d-cpu ComputeClass.
#   - loadgen pool (CREATE_LOADGEN_POOL=1 only): demo load generator and colo controller. Not needed in production.
#
# Pool names are printed at the end; copy them into overlays/<your-overlay>/settings.yaml.
set -euo pipefail
: "${PROJECT:?set PROJECT (GCP project id)}"
: "${CLUSTER:?set CLUSTER (existing GKE Standard cluster name)}"
: "${REGION:?set REGION (cluster region, e.g. us-central1)}"
: "${SUFFIX:?set SUFFIX (appended to pool, router and NAT names, e.g. prod)}"
: "${G4_ZONES:?set G4_ZONES (comma-separated zones with RTX PRO 6000 capacity)}"
: "${N4D_ZONES:?set N4D_ZONES (comma-separated zones for N4D overflow)}"
G4_MAX=${G4_MAX:-2}
N4D_MAX=${N4D_MAX:-4}
NETWORK=${NETWORK:-default}
CREATE_NAT=${CREATE_NAT:-1}
CREATE_LOADGEN_POOL=${CREATE_LOADGEN_POOL:-0}
G4_POOL=${G4_POOL:-g4gpu-${SUFFIX}}
N4D_POOL=${N4D_POOL:-n4dcpu-${SUFFIX}}
LOADGEN_POOL=${LOADGEN_POOL:-loadgen-${SUFFIX}}
HERE=$(cd "$(dirname "$0")" && pwd)

if [[ "$CREATE_NAT" == 1 ]]; then
  # Cloud NAT so nodes need no external IPs (private nodes); avoids the per-region external-IP quota.
  gcloud compute routers create "router-${SUFFIX}" --network "$NETWORK" --region "$REGION" --project "$PROJECT" || true
  gcloud compute routers nats create "nat-${SUFFIX}" --router "router-${SUFFIX}" --region "$REGION" \
    --project "$PROJECT" --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges || true
fi

# Network policy (Calico) for sandbox isolation. Existing nodes pick it up when recreated (e.g. a node upgrade).
gcloud container clusters update "$CLUSTER" --region "$REGION" --project "$PROJECT" --update-addons=NetworkPolicy=ENABLED
gcloud container clusters update "$CLUSTER" --region "$REGION" --project "$PROJECT" --enable-network-policy

gcloud container node-pools create "$G4_POOL" --cluster "$CLUSTER" --region "$REGION" --project "$PROJECT" \
  --node-locations "$G4_ZONES" --machine-type g4-standard-48 \
  --accelerator type=nvidia-rtx-pro-6000,count=1,gpu-driver-version=latest \
  --system-config-from-file "$HERE/kubelet-config.yaml" --enable-image-streaming --enable-private-nodes \
  --disk-type hyperdisk-balanced --disk-size 150 \
  --enable-autoscaling --min-nodes 1 --max-nodes "$G4_MAX" --num-nodes 1 \
  --node-labels=cloud.google.com/compute-class=g4-gpu,colo.role=g4 \
  --node-taints=cloud.google.com/compute-class=g4-gpu:NoSchedule

gcloud container node-pools create "$N4D_POOL" --cluster "$CLUSTER" --region "$REGION" --project "$PROJECT" \
  --node-locations "$N4D_ZONES" --machine-type n4d-standard-16 \
  --system-config-from-file "$HERE/kubelet-config.yaml" --sandbox type=gvisor --enable-image-streaming --enable-private-nodes \
  --disk-type hyperdisk-balanced --disk-size 50 \
  --enable-autoscaling --total-min-nodes 0 --total-max-nodes "$N4D_MAX" --num-nodes 0 \
  --node-labels=cloud.google.com/compute-class=n4d-cpu,colo.role=n4d \
  --node-taints=cloud.google.com/compute-class=n4d-cpu:NoSchedule

if [[ "$CREATE_LOADGEN_POOL" == 1 ]]; then
  gcloud container node-pools create "$LOADGEN_POOL" --cluster "$CLUSTER" --region "$REGION" --project "$PROJECT" \
    --node-locations "${G4_ZONES%%,*}" --machine-type e2-standard-8 --disk-type pd-balanced --disk-size 50 --num-nodes 1 \
    --enable-image-streaming --enable-private-nodes --node-labels=colo.role=loadgen
fi

# Workload Identity (read-only Managed Prometheus access, no service-account keys):
#   - custom-metrics-stackdriver-adapter: feeds the vLLM HPA.
#   - colo-controller: reads vllm:time_to_first_token_seconds for the latency guard (ttft_source prometheus).
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
for ksa in custom-metrics/custom-metrics-stackdriver-adapter colo-system/colo-controller; do
  gcloud projects add-iam-policy-binding "$PROJECT" --role=roles/monitoring.viewer --condition=None \
    --member="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${PROJECT}.svc.id.goog/subject/ns/${ksa%%/*}/sa/${ksa##*/}"
done

echo "Put these in overlays/<your-overlay>/settings.yaml:"
echo "  g4NodePool: ${G4_POOL}"
echo "  n4dNodePool: ${N4D_POOL}"
