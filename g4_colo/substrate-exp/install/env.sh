# Environment for setup-gcp (substrate-gke tools/setup-gcp) and ate-setup (substrate cmd/ate-setup) against the
# colo-controller G4 cluster. Source it: `source g4_colo/substrate-exp/install/env.sh`.
# Names follow <type>-jawadamin-<region>-MMYY; GSA ids are capped at 30 chars, so the region is shortened there.
export PROJECT_ID=northam-ce-mlai-tpu
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
export PROJECT_NUMBER
export GCE_REGION=us-west8
export CLUSTER_LOCATION=us-west8
export CLUSTER_NAME=g4cluster-jawadamin-us-west8-0726
export NETWORK=default
export SUBNETWORK=default
export KUBECTL_CONTEXT=gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726
export BUCKET_NAME=bucket-substrate-jawadamin-us-west8-1126
export SNAPSHOT_LOCATION="gs://${BUCKET_NAME}/"
export SNAPSHOT_TRANSFER_GSA_NAME=atexfer-jawadamin-usw8-1126
export NODE_GSA_NAME=atenode-jawadamin-usw8-1126
export SEPARATE_SYSTEM_NODES=true
# Substrate's control plane gets two dedicated one-node N4D pools (instead of the installer's e2-standard-4 default),
# labelled/tainted ate.dev/workloadType=ate-system-infra|ate-postgres. Bundled PostgreSQL requests 2 CPU, so > 2 vCPU.
export SYSTEM_INFRA_MACHINE_TYPE=n4d-standard-4
# N4D CPU quota in us-west8 (119 vCPU) was exhausted at 18:30Z; postgres pool runs on e2-standard-4 (user decision 18:44Z).
export POSTGRES_MACHINE_TYPE=e2-standard-4
export ENABLE_NESTED_VIRTUALIZATION=false
# Pre-built release images (no docker on the build host); ate-setup reads manifests from the v1.0.0 checkout.
export ATE_IMAGE_REPO=ghcr.io/agent-substrate/substrate
export ATE_IMAGE_TAG=v1.0.0
# Registry for the actor image (counter demo) and the node pull grant.
export KO_DOCKER_REPO=us-west8-docker.pkg.dev/northam-ce-mlai-tpu/repo-jawadamin-us-west8-1026/substrate
export KO_DEFAULTPLATFORMS=linux/amd64
# kubectl ate proxy / colo-exp-driver authenticate to the router as ate-system/atenet-client.
export ATE_INGRESS_ALLOWED_SPIFFE_IDS=spiffe://cluster.local/ns/ate-system/sa/atenet-client
