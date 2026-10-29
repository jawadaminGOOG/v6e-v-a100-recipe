#!/usr/bin/env bash
# Node pools for the G4 colo recipe (GKE Standard). Edit the variables, then run once.
#   - G4 pool: GPU nodes; static CPU manager (exclusive cores), GKE Sandbox (gVisor) for agentic sandboxes,
#     image streaming for fast scale-out; labelled + tainted for the g4-gpu ComputeClass.
#   - N4D pool: CPU overflow nodes across zones; same kubelet config and gVisor; n4d-cpu ComputeClass.
#   - loadgen pool: demo load generator and colo controller (not needed in production).
set -euo pipefail
PROJECT=${PROJECT:-northam-ce-mlai-tpu}
CLUSTER=${CLUSTER:-g4cluster-jawadamin-us-west8-0726}
REGION=${REGION:-us-west8}
G4_ZONES=${G4_ZONES:-us-west8-a}            # zones with RTX PRO 6000 capacity
N4D_ZONES=${N4D_ZONES:-us-west8-a,us-west8-c}
G4_MAX=${G4_MAX:-2}
N4D_MAX=${N4D_MAX:-4}
HERE=$(cd "$(dirname "$0")" && pwd)

# Cloud NAT so nodes need no external IPs (private nodes); avoids the per-region external-IP quota.
gcloud compute routers create router-jawadamin-us-west8-1026 --network default --region "$REGION" --project "$PROJECT" || true
gcloud compute routers nats create nat-jawadamin-us-west8-1026 --router router-jawadamin-us-west8-1026 --region "$REGION" \
  --project "$PROJECT" --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges || true

# Network policy (Calico) for sandbox isolation. Existing nodes pick it up when recreated (e.g. a node upgrade).
gcloud container clusters update "$CLUSTER" --region "$REGION" --project "$PROJECT" --update-addons=NetworkPolicy=ENABLED
gcloud container clusters update "$CLUSTER" --region "$REGION" --project "$PROJECT" --enable-network-policy

gcloud container node-pools create g4gpu-jawadamin-us-west8-1026 --cluster "$CLUSTER" --region "$REGION" --project "$PROJECT" \
  --node-locations "$G4_ZONES" --machine-type g4-standard-48 \
  --accelerator type=nvidia-rtx-pro-6000,count=1,gpu-driver-version=latest \
  --system-config-from-file "$HERE/kubelet-config.yaml" --sandbox type=gvisor --enable-image-streaming --enable-private-nodes \
  --disk-type hyperdisk-balanced --disk-size 150 \
  --enable-autoscaling --min-nodes 1 --max-nodes "$G4_MAX" --num-nodes 1 \
  --node-labels=cloud.google.com/compute-class=g4-gpu,colo.role=g4 \
  --node-taints=cloud.google.com/compute-class=g4-gpu:NoSchedule

gcloud container node-pools create n4dcpu-jawadamin-us-west8-1026 --cluster "$CLUSTER" --region "$REGION" --project "$PROJECT" \
  --node-locations "$N4D_ZONES" --machine-type n4d-standard-16 \
  --system-config-from-file "$HERE/kubelet-config.yaml" --sandbox type=gvisor --enable-image-streaming --enable-private-nodes \
  --disk-type hyperdisk-balanced --disk-size 50 \
  --enable-autoscaling --total-min-nodes 0 --total-max-nodes "$N4D_MAX" --num-nodes 0 \
  --node-labels=cloud.google.com/compute-class=n4d-cpu,colo.role=n4d \
  --node-taints=cloud.google.com/compute-class=n4d-cpu:NoSchedule

gcloud container node-pools create loadgen-jawadamin-us-west8-1026 --cluster "$CLUSTER" --region "$REGION" --project "$PROJECT" \
  --node-locations "${G4_ZONES%%,*}" --machine-type e2-standard-8 --disk-type pd-balanced --disk-size 50 --num-nodes 1 \
  --enable-image-streaming --enable-private-nodes --node-labels=colo.role=loadgen

# Custom metrics adapter reads Managed Prometheus metrics for the HPA (Workload Identity, read-only).
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$PROJECT" --role=roles/monitoring.viewer --condition=None \
  --member="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${PROJECT}.svc.id.goog/subject/ns/custom-metrics/sa/custom-metrics-stackdriver-adapter"
