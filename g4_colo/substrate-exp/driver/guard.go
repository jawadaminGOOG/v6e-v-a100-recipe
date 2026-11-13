// guard: in-cluster "worker guard" for the substrate-exp experiment (wave 4, T6 fix).
//
// Kubernetes removes G4 nodes (vLLM scale-in → Cluster Autoscaler drain) and N4D worker pods (HPA scale-down)
// without telling Substrate, so actors bound to those workers crashed (T6: 30/60). The guard closes that gap with
// three rules, every 3 s:
//
//  1. Drain before the node goes. A G4 node that has had a Ready vLLM pod and no longer has one (works for both the
//     colo-controller and the native recipe), or that carries the Cluster Autoscaler's deletion taints, gets every
//     Substrate worker on it DrainWorker'ed (no new actors land there) and every actor bound to those workers
//     suspended with DURABILITY_MODE_SYNCHRONOUS (snapshot in Cloud Storage before the call returns).
//  2. Suspend actors on terminating worker pods (pod deletionTimestamp set) the same way, as a last resort.
//  3. Steer scale-down to idle workers: annotate worker pods with controller.kubernetes.io/pod-deletion-cost
//     (+100 while hosting an actor, -100 idle), so a WorkerPool scale-down deletes idle workers first.
//
// It never deletes pods or nodes and never changes replica counts.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	ateapi "go.ate.dev/substrate/api/ateapi/v1"
	"go.ate.dev/substrate/internal/ateclient"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
)

const deletionCost = "controller.kubernetes.io/pod-deletion-cost"

type guard struct {
	cs        kubernetes.Interface
	api       *ateclient.Client
	seenVLLM  map[string]bool // G4 nodes that have hosted a Ready vLLM pod
	drained   map[string]bool // worker names already drained
	suspended map[string]time.Time
	mu        sync.Mutex
}

func glog(kv map[string]any) {
	kv["t"] = time.Now().UTC().Format(time.RFC3339Nano)
	b, _ := json.Marshal(kv)
	fmt.Println(string(b))
}

func ready(p *corev1.Pod) bool {
	if p.DeletionTimestamp != nil {
		return false
	}
	for _, c := range p.Status.Conditions {
		if c.Type == corev1.PodReady && c.Status == corev1.ConditionTrue {
			return true
		}
	}
	return false
}

func caDeleting(n *corev1.Node) bool {
	for _, t := range n.Spec.Taints {
		if t.Key == "ToBeDeletedByClusterAutoscaler" || t.Key == "DeletionCandidateOfClusterAutoscaler" {
			return true
		}
	}
	return n.DeletionTimestamp != nil
}

func (g *guard) workers(ctx context.Context) ([]*ateapi.Worker, error) {
	var out []*ateapi.Worker
	tok := ""
	for {
		resp, err := g.api.ListWorkers(ctx, &ateapi.ListWorkersRequest{Options: &ateapi.ListOptions{PageSize: 1000, PageToken: tok}})
		if err != nil {
			return nil, err
		}
		out = append(out, resp.GetWorkers()...)
		if tok = resp.GetNextPageToken(); tok == "" {
			return out, nil
		}
	}
}

func (g *guard) actorsOn(ctx context.Context, w *ateapi.Worker) []*ateapi.ObjectRef {
	resp, err := g.api.ListWorkerActorAssignments(ctx, &ateapi.ListWorkerActorAssignmentsRequest{
		Worker: &ateapi.ObjectRef{Name: w.GetMetadata().GetName()}, Options: &ateapi.ListOptions{PageSize: 1000}})
	if err != nil {
		return nil
	}
	var refs []*ateapi.ObjectRef
	for _, a := range resp.GetActorAssignments() {
		refs = append(refs, a.GetActor())
	}
	return refs
}

func (g *guard) suspendSync(ctx context.Context, ref *ateapi.ObjectRef, why string) {
	key := ref.GetAtespace() + "/" + ref.GetName()
	g.mu.Lock()
	if t, ok := g.suspended[key]; ok && time.Since(t) < 20*time.Second {
		g.mu.Unlock()
		return
	}
	g.suspended[key] = time.Now()
	g.mu.Unlock()
	go func() {
		t0 := time.Now()
		_, err := g.api.SuspendActor(ctx, &ateapi.SuspendActorRequest{Actor: ref,
			Options: &ateapi.SuspendOptions{Mode: ateapi.DurabilityMode_DURABILITY_MODE_SYNCHRONOUS}})
		e := ""
		if err != nil {
			e = err.Error()
		}
		glog(map[string]any{"op": "suspend_sync", "actor": key, "why": why, "ok": err == nil, "ms": time.Since(t0).Milliseconds(), "err": e})
	}()
}

func (g *guard) tick(ctx context.Context) {
	nodes, err := g.cs.CoreV1().Nodes().List(ctx, metav1.ListOptions{LabelSelector: "cloud.google.com/compute-class=g4-gpu"})
	if err != nil {
		glog(map[string]any{"op": "error", "err": err.Error()})
		return
	}
	vllm, _ := g.cs.CoreV1().Pods("inference").List(ctx, metav1.ListOptions{LabelSelector: "app=vllm"})
	hasVLLM := map[string]bool{}
	for i := range vllm.Items {
		if ready(&vllm.Items[i]) {
			hasVLLM[vllm.Items[i].Spec.NodeName] = true
		}
	}
	drainNodes := map[string]string{}
	for i := range nodes.Items {
		n := &nodes.Items[i]
		if hasVLLM[n.Name] {
			g.seenVLLM[n.Name] = true
		}
		switch {
		case caDeleting(n):
			drainNodes[n.Name] = "ca-deleting"
		case g.seenVLLM[n.Name] && !hasVLLM[n.Name]:
			drainNodes[n.Name] = "vllm-left"
		}
	}
	ws, err := g.workers(ctx)
	if err != nil {
		glog(map[string]any{"op": "error", "err": "ListWorkers: " + err.Error()})
		return
	}
	pods, _ := g.cs.CoreV1().Pods(loadNS).List(ctx, metav1.ListOptions{LabelSelector: "ate.dev/worker-pool"})
	terminating := map[string]bool{}
	podCost := map[string]string{}
	for i := range pods.Items {
		p := &pods.Items[i]
		if p.DeletionTimestamp != nil {
			terminating[p.Name] = true
		}
		podCost[p.Name] = p.Annotations[deletionCost]
	}
	for _, w := range ws {
		name := w.GetMetadata().GetName()
		why := drainNodes[w.GetNodeName()]
		if why == "" && terminating[w.GetWorkerPod()] {
			why = "pod-terminating"
		}
		actors := g.actorsOn(ctx, w)
		if why != "" {
			if !g.drained[name] && w.GetStatus().GetState() != ateapi.WorkerState_WORKER_STATE_DRAINING {
				_, err := g.api.DrainWorker(ctx, &ateapi.DrainWorkerRequest{Worker: &ateapi.ObjectRef{Name: name}})
				e := ""
				if err != nil {
					e = err.Error()
				}
				glog(map[string]any{"op": "drain_worker", "worker": w.GetWorkerPod(), "node": w.GetNodeName(), "why": why, "ok": err == nil, "actors": len(actors), "err": e})
				if err == nil {
					g.drained[name] = true
				}
			}
			for _, a := range actors {
				g.suspendSync(ctx, a, why)
			}
		}
		// rule 3: deletion cost by occupancy
		want := "-100"
		if len(actors) > 0 {
			want = "100"
		}
		if pod := w.GetWorkerPod(); pod != "" && w.GetWorkerNamespace() == loadNS && podCost[pod] != want && !terminating[pod] {
			patch := fmt.Sprintf(`{"metadata":{"annotations":{%q:%q}}}`, deletionCost, want)
			if _, err := g.cs.CoreV1().Pods(loadNS).Patch(ctx, pod, types.MergePatchType, []byte(patch), metav1.PatchOptions{}); err == nil {
				podCost[pod] = want
			}
		}
	}
}

func runGuard(ctx context.Context) {
	cfg, err := ateclient.LoadKubeConfig("", "")
	if err != nil {
		fmt.Fprintln(os.Stderr, "kubeconfig:", err)
		os.Exit(1)
	}
	cs, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		fmt.Fprintln(os.Stderr, "clientset:", err)
		os.Exit(1)
	}
	g := &guard{cs: cs, seenVLLM: map[string]bool{}, drained: map[string]bool{}, suspended: map[string]time.Time{}}
	connect := func() error {
		api, err := ateclient.NewClient(ctx, "", "", "", "", false)
		if err != nil {
			return err
		}
		if g.api != nil {
			old := g.api
			go func() { time.Sleep(time.Minute); old.Close() }()
		}
		g.api = api
		return nil
	}
	for connect() != nil {
		time.Sleep(10 * time.Second)
	}
	last := time.Now()
	glog(map[string]any{"op": "start"})
	for {
		g.tick(ctx)
		if time.Since(last) > 40*time.Minute {
			if connect() == nil {
				last = time.Now()
			}
		}
		time.Sleep(3 * time.Second)
	}
}

var _ = strings.Contains
