# Installing Agent Substrate v1.0.0 on the colo-controller G4 cluster

Cluster `g4cluster-jawadamin-us-west8-0726` (us-west8, Regular channel). Done once per cluster. All commands use
`install/env.sh`. Checkouts: `ai-on-gke/substrate-gke` (for `tools/setup-gcp`) and `agent-substrate/substrate` at
tag `v1.0.0` (for `cmd/ate-setup`, and the manifests ate-setup applies).

1. **GKE 1.37 everywhere** (Substrate needs the PodCertificateRequest and ClusterTrustBundle v1 APIs). The control
   plane moves one minor at a time; node pools can jump:
   ```bash
   gcloud container clusters upgrade $CLUSTER_NAME --region us-west8 --master --cluster-version 1.36.4-gke.2377000
   gcloud container clusters upgrade $CLUSTER_NAME --region us-west8 --master --cluster-version 1.37.1-gke.1552000
   for p in <every pool>; do gcloud container clusters upgrade $CLUSTER_NAME --region us-west8 --node-pool $p --cluster-version 1.37.1-gke.1552000; done
   ```
   Then turn node auto-upgrade off on the pools that run workers (G4, N4D). It's a management setting with no
   node recreation. Substrate's guidance: a worker deleted mid-turn can crash a running actor.
2. **GCP resources** (bucket `bucket-substrate-jawadamin-us-west8-1126` + `-assets`, transfer/node GSAs, IAM,
   dashboards, node pull grants on our AR repo):
   `source install/env.sh && go -C <substrate-gke>/tools/setup-gcp run . bootstrap --atelet-snapshot-access=false`
3. **System node pools.** On an existing cluster, bootstrap only prints these commands. Create them with the names
   it prints, then deploy with `ATE_INSTALL_CORDON_SYSTEM_INFRA_COMPONENTS=true`.
4. **Control plane.** From the v1.0.0 checkout:
   `go run ./cmd/ate-setup deploy ate-system --credential-provider='{"name":"k8s.io"}' --sandbox-classes=gvisor`
   (images from `ATE_IMAGE_REPO`/`ATE_IMAGE_TAG`, router allowlist from `ATE_INGRESS_ALLOWED_SPIFFE_IDS`).
5. **G4/N4D compatibility.** Both pools on this cluster carry the GKE Sandbox taint
   `sandbox.gke.io/runtime=gvisor:NoSchedule`, which upstream atelet doesn't tolerate. Patch:
   `kubectl -n ate-system patch ds atelet --type=json --patch-file install/atelet-tolerations.json`.
   Label `ate-system` with `colo.gke.io/g4-daemonsets=allowed` so `colo-g4-guard` admits atelet on G4.
   Each ate-setup deploy re-applies upstream manifests, so re-run this step after any redeploy.
6. **Actor image + template.** `ko build` `demos/counter` from the v1.0.0 checkout to `$KO_DOCKER_REPO`, then
   `kubectl ate create atespace colo-exp` and `kubectl ate create actor-template -f actor/counter-template.yaml`.
7. **Experiment objects.** `kubectl apply -k manifests/`.

Teardown: `go run ./cmd/ate-setup delete ate-system` (v1.0.0 checkout), then
`<substrate-gke>/tools/setup-gcp/teardown.sh --cluster-scoped`, then delete the two system node pools.

## Cluster-specific fixes found during the 0726 install (2026-11-11)
- **Kueue v0.7.0 pod webhook strips `podCertificate` volumes.** The cluster's old Kueue `mpod.kb.io` mutating webhook
  round-trips every pod through pre-1.35 types, dropping projected `podCertificate` sources, so postgres (and every
  Substrate pod that uses pod certificates) starts without its TLS bundle and crash-loops. Fix: add `ate-system`,
  `substrate-exp`, `podcertificate-controller-system` to that webhook's `namespaceSelector` NotIn list
  (original saved in `.agents/wave-4/logs/kueue-mwc-before.yaml`).
- **SSD quota.** Bundled PostgreSQL requests 500Gi; the regional SSD quota had 367 GB left, so postgres uses
  `install/storageclass-pd-standard.yaml` (`ATE_API_POSTGRES_STORAGE_CLASS=substrate-pd-standard`).
- **N4D CPU quota (119/region).** The postgres pool is e2-standard-4; the infra pool is n4d-standard-4.
- **Rollout timeout.** ate-setup's default 1m is too short for a fresh 500Gi pd-standard volume:
  `ATE_INSTALL_ROLLOUT_TIMEOUT=10m`.
