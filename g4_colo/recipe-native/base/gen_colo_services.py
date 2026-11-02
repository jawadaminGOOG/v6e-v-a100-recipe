#!/usr/bin/env python3
"""Generate recipe-native/base/colo-services.yaml: backfill + overflow Deployments, overflow HPAs, warm buffer.

Build-time only (like recipe/base/gen_colo_services.py); nothing here runs in the cluster.

How the split works without a controller:
  * <svc>-backfill holds the service's whole demand (`replicas` set by your HPA or `kubectl scale`; the demo
    sliders patch it). Its pods may only bind next to a vLLM pod on a G4 node (required podAffinity). The ones
    that do not fit stay Pending at priority -100, which the cluster autoscaler ignores.
  * <svc>-overflow on N4D is scaled by an HPA on `colo_overflow_needed{service}`: backfill pods that are not
    Ready next to a Ready vLLM pod (base/rules.yaml). Pending or stranded backfill becomes N4D capacity.
  * memory-heavy services never backfill (guardrails.yaml bans them from G4), so they get one N4D Deployment.
"""
from pathlib import Path

REG = "colo-registry.invalid"
TAG = "v0.2.4"
# Same finding as recipe/: gVisor's runsc-sandbox runs in the pod cgroup (cpuset 0-47), outside the container
# cpuset that kubelet's static CPU manager pins, so G4 backfill uses runc and only N4D overflow uses gVisor.
P6_REASON = (
    "default-runc-on-g4: gVisor runsc-sandbox runs in pod cgroup (0-47) bypassing kubelet static CPU manager "
    "container cpuset and contending with vLLM cores (P6 probe: 50ms vs 20ms baseline; runc control: 22ms)"
)
SERVICES = [
    # name, mode, class, cpu, memory, backfill_runtimeClass, overflow_runtimeClass
    ("sandbox", "sandbox", "integer", 2, "4Gi", None, "gvisor"),
    ("toolcall", "toolcall", "integer", 2, "2Gi", None, None),
    ("analytics", "analytics", "memory-heavy", 2, "6Gi", None, None),
]
G4_TOL = """      - {key: cloud.google.com/compute-class, operator: Equal, value: g4-gpu, effect: NoSchedule}
      - {key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}"""
N4D_TOL = """      - {key: cloud.google.com/compute-class, operator: Equal, value: n4d-cpu, effect: NoSchedule}
      - {key: sandbox.gke.io/runtime, operator: Equal, value: gvisor, effect: NoSchedule}"""
# One overflow Deployment per service caps how far N4D can grow; colo-svc-quota (guardrails.yaml) is the hard cap.
OVERFLOW_MAX = 60


def deploy(name, mode, cls, cpu, mem, bf_rtc, of_rtc, tier):
    rtc = bf_rtc if tier == "backfill" else of_rtc
    if tier == "backfill":
        sched = f"""      priorityClassName: colo-backfill
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - {{key: cloud.google.com/compute-class, operator: In, values: [g4-gpu]}}
        # The whole placement rule. A backfill pod binds only to a node that already runs a vLLM pod, so:
        #   - it never lands on a G4 node without vLLM (empty, or draining after vLLM scale-in);
        #   - the cluster autoscaler never adds a G4 node for it: CA simulates a new node from the pool template,
        #     which has no vLLM pod, so the pod does not fit (and priority -100 is below CA's cutoff anyway);
        #   - when vLLM lands on a new G4 node, Pending backfill pods bind there by themselves.
        podAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
          - labelSelector: {{matchLabels: {{app: vllm}}}}
            namespaces: [inference]
            topologyKey: kubernetes.io/hostname
      tolerations:
{G4_TOL}"""
    else:
        sched = f"""      priorityClassName: colo-overflow
      nodeSelector: {{cloud.google.com/compute-class: n4d-cpu}}
      tolerations:
{N4D_TOL}"""
    rt = f"      runtimeClassName: {rtc}\n" if rtc else ""
    extra_ann = ""
    if name == "sandbox" and tier == "backfill":
        extra_ann = f'\n        colo.gke.io/sandbox-runtime-reason: "{P6_REASON}"'
    extra_env = ""
    if mode == "analytics":
        extra_env += '        - {name: ARRAY_MB, value: "1500"}\n'
    if rtc == "gvisor":
        extra_env += f'        - {{name: NPROC, value: "{cpu}"}}\n'
    sec = ""
    if name == "sandbox":
        sec = """        securityContext:
          runAsNonRoot: true
          runAsUser: 65534
          allowPrivilegeEscalation: false
          capabilities: {drop: [ALL]}
          seccompProfile: {type: RuntimeDefault}
"""
    if tier == "backfill":
        owner = "# No `replicas:` field: this is the service's demand. Your HPA (or `kubectl scale`) owns it."
    elif cls == "memory-heavy":
        owner = "# No `replicas:` field: memory-heavy services never backfill, so this is the service's demand."
    else:
        owner = "# No `replicas:` field: HorizontalPodAutoscaler/<name> owns it (colo_overflow_needed)."
    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}-{tier}
  namespace: colo-svc
  labels: {{app: {name}, colo.tier: {tier}, colo.class: {cls}}}
spec:
  {owner}
  selector: {{matchLabels: {{app: {name}, colo.tier: {tier}}}}}
  template:
    metadata:
      labels: {{app: {name}, colo.tier: {tier}, colo.class: {cls}}}
      annotations:
        cluster-autoscaler.kubernetes.io/safe-to-evict: "true"{extra_ann}
    spec:
{rt}{sched}
      terminationGracePeriodSeconds: 10
      containers:
      - name: worker
        image: {REG}/colo-worker:{TAG}
        imagePullPolicy: IfNotPresent
        env:
        - {{name: MODE, value: {mode}}}
{extra_env}        ports: [{{name: metrics, containerPort: 9100}}]
{sec}        # Guaranteed QoS + integer CPU => exclusive cores (and admission-policy compliant on G4 nodes).
        resources:
          requests: {{cpu: "{cpu}", memory: {mem}}}
          limits: {{cpu: "{cpu}", memory: {mem}}}
"""


def overflow_hpa(name):
    return f"""apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: {{name: {name}-overflow, namespace: colo-svc, labels: {{app: {name}, colo.tier: overflow}}}}
spec:
  scaleTargetRef: {{apiVersion: apps/v1, kind: Deployment, name: {name}-overflow}}
  # HPA cannot scale to 0 on GKE (HPAScaleToZero is alpha), so one overflow pod always runs on N4D.
  minReplicas: 1
  maxReplicas: {OVERFLOW_MAX}
  metrics:
  - type: External
    external:
      # Recorded by ClusterRules/colo-split (base/rules.yaml): backfill pods of this service that are not Ready
      # next to a Ready vLLM pod. AverageValue 1 => one overflow pod per missing backfill pod.
      metric:
        name: prometheus.googleapis.com|colo_overflow_needed|gauge
        selector:
          matchLabels:
            metric.labels.service: {name}
            resource.labels.cluster: your-cluster-name   # PLACEHOLDER: overlays set clusterName
      target: {{type: AverageValue, averageValue: "1"}}
  behavior:
    # Up: at once, as fast as the scheduler can place pods.
    scaleUp:
      stabilizationWindowSeconds: 0
      selectPolicy: Max
      policies: [{{type: Percent, value: 100, periodSeconds: 15}}, {{type: Pods, value: 20, periodSeconds: 15}}]
    # Down: wait 60 s so a backfill pod that just became Ready on a new G4 node stays Ready before N4D shrinks.
    scaleDown:
      stabilizationWindowSeconds: 60
      policies: [{{type: Percent, value: 100, periodSeconds: 15}}]
"""


def fresh(sel):
    """`sel`, keeping only series scraped in the last FRESH_S seconds.

    Managed Prometheus has no staleness markers, so a deleted pod's kube_pod_info would otherwise still match an
    instant query for up to the 5 min lookback. KSM is scraped every 5 s, so 20 s means "exists now".
    """
    return f"({sel} and (time() - timestamp({sel}) < {FRESH_S}))"


FRESH_S = 20
RULE_INTERVAL = "10s"


def rules():
    # KSM is scraped from colo-system, so Managed Prometheus keeps its own target labels and renames the
    # conflicting KSM labels: the subject pod is exported_pod / exported_namespace. node is not a target label.
    info_bf = fresh('kube_pod_info{exported_namespace="colo-svc",exported_pod=~".+-backfill-.+"}')
    ready_bf = fresh('kube_pod_status_ready{exported_namespace="colo-svc",condition="true"}') + " == 1"
    deleting = fresh('kube_pod_deletion_timestamp{exported_namespace="colo-svc"}')
    info_vllm = fresh('kube_pod_info{exported_namespace="inference",exported_pod=~"vllm-.+"}')
    ready_vllm = fresh('kube_pod_status_ready{exported_namespace="inference",condition="true"}') + " == 1"
    svc = lambda e: f'label_replace({e}, "service", "$1", "exported_pod", "(.+)-backfill-.+")'  # noqa: E731
    on_pod = "on (exported_namespace, exported_pod)"
    # Nodes with a Ready vLLM pod.
    vllm_nodes = f"max by (node) ({info_vllm} * {on_pod} group_left () ({ready_vllm}))"
    # Backfill pods that exist and are not being deleted, per service (this is the service's demand).
    desired = f"count by (service) ({svc(f'{info_bf} unless {on_pod} {deleting}')})"
    # ... of which Ready and on a node that runs a Ready vLLM pod.
    effective = (f"count by (service) ({svc(f'({info_bf} * {on_pod} group_left () ({ready_bf})) '
                 f'* on (node) group_left () {vllm_nodes} unless {on_pod} {deleting}')})")
    zeros = " or ".join(f'label_replace(vector(0), "service", "{s[0]}", "", "")'
                        for s in SERVICES if s[2] != "memory-heavy")
    needed = f"(({desired}) - (({effective}) or ({desired}) * 0)) or {zeros}"
    effective_or0 = f"({effective}) or {zeros}"
    return f"""# GENERATED by gen_colo_services.py - the native recipe's whole "controller": three PromQL recording rules,
# evaluated every {RULE_INTERVAL} by Managed Prometheus' rule evaluator, read by the overflow HPAs.
#   colo_backfill_desired{{service}}   backfill pods that exist (= the service's demand)
#   colo_backfill_effective{{service}} backfill pods Ready on a node that runs a Ready vLLM pod
#   colo_overflow_needed{{service}}    desired - effective: Pending backfill, or backfill stranded on a G4 node
#                                    whose vLLM left (make-before-break: N4D scales up before that node drains)
apiVersion: monitoring.googleapis.com/v1
kind: ClusterRules
metadata: {{name: colo-split}}
spec:
  groups:
  - name: colo-split
    interval: {RULE_INTERVAL}
    rules:
    - record: colo_backfill_desired
      expr: |-
        ({desired}) or {zeros}
    - record: colo_backfill_effective
      expr: |-
        {effective_or0}
    - record: colo_overflow_needed
      expr: |-
        {needed}
"""


def main():
    Path(__file__).with_name("rules.yaml").write_text(rules())
    docs = ["# GENERATED by gen_colo_services.py - CPU services (backfill on G4, overflow on N4D), overflow HPAs, "
            "warm buffer."]
    for s in SERVICES:
        name, cls = s[0], s[2]
        if cls != "memory-heavy":
            docs.append(deploy(*s, "backfill"))
        docs.append(deploy(*s, "overflow"))
        if cls != "memory-heavy":
            docs.append(overflow_hpa(name))
    # Warm N4D buffer: GKE's documented overprovisioning pattern. Keeps one n4d-standard-16 node up 24x7 (a cost
    # floor); set replicas: 0 in your overlay for cost-first deployments.
    docs.append(f"""apiVersion: apps/v1
kind: Deployment
metadata: {{name: colo-buffer, namespace: colo-system, labels: {{app: colo-buffer}}}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: colo-buffer}}}}
  template:
    metadata: {{labels: {{app: colo-buffer}}}}
    spec:
      priorityClassName: colo-buffer
      nodeSelector: {{cloud.google.com/compute-class: n4d-cpu}}
      tolerations:
{N4D_TOL}
      terminationGracePeriodSeconds: 0
      containers:
      - name: pause
        image: registry.k8s.io/pause:3.10
        resources:
          requests: {{cpu: "2", memory: 4Gi}}
          limits: {{cpu: "2", memory: 4Gi}}
""")
    docs.append("""apiVersion: monitoring.googleapis.com/v1
kind: PodMonitoring
metadata: {name: colo-workers, namespace: colo-svc}
spec:
  selector: {matchExpressions: [{key: colo.tier, operator: Exists}]}
  endpoints: [{port: metrics, path: /metrics, interval: 15s}]
""")
    Path(__file__).with_name("colo-services.yaml").write_text("\n---\n".join(docs))


if __name__ == "__main__":
    main()
