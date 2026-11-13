// serve: in-cluster actor load generator for the demo UI (wave 4).
//
// Reads ConfigMap substrate-exp/actor-load every 3 s:
//
//	total   number of actors ui-000..ui-<total-1> that exist (scale out creates, scale in deletes idle ones)
//	awake   number of actors kept RUNNING at once (each awake slot loops resume -> POST -> hold -> suspend)
//	hold_s  seconds an actor stays RUNNING per cycle (default 5)
//
// and publishes rolling statistics to ConfigMap substrate-exp/actor-stats (the demo's contract) every 5 s.
// Auth is the same as kubectl ate: a bearer token minted for ate-system/ate-client and a router client certificate
// for ate-system/atenet-client, both through the pod's ServiceAccount RBAC (manifests/actor-load.yaml).
// The token lasts 1 h, so clients are rebuilt every 40 min, and at once after a transport error (the port-forward
// to ate-api-server died). With -evacuate, each awake slot polls its actor's
// /evicting endpoint (actor/counter-evict) and suspends it SYNCHRONOUSLY when the actor reports SIGTERM.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	ateapi "go.ate.dev/substrate/api/ateapi/v1"
	"go.ate.dev/substrate/internal/ateclient"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

const (
	loadNS    = "substrate-exp"
	loadCM    = "actor-load"
	statsCM   = "actor-stats"
	uiPrefix  = "ui"
	statsSpan = 60 * time.Second
)

type loadSpec struct {
	total, awake int
	hold         time.Duration
}

type server struct {
	mu      sync.Mutex
	d       *driver
	cs      kubernetes.Interface
	spec    loadSpec
	created map[string]bool
	busy    map[string]bool
	next    int
	slots   int // running awake goroutines
	recs    []rec
	running map[string]rec  // actor -> last resume while RUNNING
	lost    map[string]bool // actors Substrate reports CRASHED (state is gone)
	noRoom  int             // resumes refused with "no worker has room" (pool saturated; the actor waits)
	memRst  int             // resumes that came back without memory state (counter reset, files kept)
	fails   int             // other failed resume/POST attempts
	evacOK  int             // RUNNING actors suspended because they reported SIGTERM (/evicting = 1)
	evacErr int             // those suspends that failed
	reconn  atomic.Bool     // a call failed with a transport error: rebuild the clients now
}

func (s *server) connect(ctx context.Context) error {
	api, err := ateclient.NewClient(ctx, "", "", "", "", false)
	if err != nil {
		return fmt.Errorf("api: %w", err)
	}
	router, err := ateclient.NewRouterDialer(ctx, "", "")
	if err != nil {
		api.Close()
		return fmt.Errorf("router: %w", err)
	}
	s.mu.Lock()
	old := s.d.api
	s.d.api, s.d.router = api, router
	s.mu.Unlock()
	if old != nil {
		go func() { time.Sleep(2 * time.Minute); old.Close() }() // let in-flight calls finish
	}
	return nil
}

func atoi(m map[string]string, k string, def int) int {
	if v, err := strconv.Atoi(strings.TrimSpace(m[k])); err == nil && v >= 0 {
		return v
	}
	return def
}

func (s *server) readSpec(ctx context.Context) {
	cm, err := s.cs.CoreV1().ConfigMaps(loadNS).Get(ctx, loadCM, metav1.GetOptions{})
	if err != nil {
		return
	}
	sp := loadSpec{total: atoi(cm.Data, "total", 0), awake: atoi(cm.Data, "awake", 0),
		hold: time.Duration(atoi(cm.Data, "hold_s", 5)) * time.Second}
	if sp.total > 200 {
		sp.total = 200
	}
	if sp.awake > sp.total {
		sp.awake = sp.total
	}
	s.mu.Lock()
	s.spec = sp
	s.mu.Unlock()
}

// reconcileActors creates missing ui actors and deletes idle ones beyond total.
func (s *server) reconcileActors(ctx context.Context) {
	s.mu.Lock()
	total, api := s.spec.total, s.d.api
	s.mu.Unlock()
	for i := 0; i < total; i++ {
		nm := fmt.Sprintf("%s-%03d", uiPrefix, i)
		s.mu.Lock()
		have := s.created[nm]
		s.mu.Unlock()
		if have {
			continue
		}
		a := &ateapi.Actor{Metadata: &ateapi.ResourceMetadata{Atespace: *atespace, Name: nm},
			ActorTemplate: &ateapi.ObjectRef{Atespace: *atespace, Name: *template}}
		_, err := api.CreateActor(ctx, &ateapi.CreateActorRequest{Actor: a})
		if err == nil || strings.Contains(err.Error(), "AlreadyExists") || strings.Contains(err.Error(), "already exists") {
			s.mu.Lock()
			s.created[nm] = true
			s.mu.Unlock()
			s.d.emit(rec{Op: "create", Actor: nm, OK: true})
		} else {
			s.d.emit(rec{Op: "create", Actor: nm, OK: false, Err: err.Error()})
			return
		}
	}
	s.mu.Lock()
	var extra []string
	for nm := range s.created {
		if i, _ := strconv.Atoi(strings.TrimPrefix(nm, uiPrefix+"-")); i >= total && !s.busy[nm] {
			extra = append(extra, nm)
		}
	}
	s.mu.Unlock()
	for _, nm := range extra {
		_, err := api.DeleteActor(ctx, &ateapi.DeleteActorRequest{Actor: ref(nm)})
		if err == nil || strings.Contains(err.Error(), "NotFound") || strings.Contains(err.Error(), "not found") {
			s.mu.Lock()
			delete(s.created, nm)
			delete(s.d.st.Last, nm)
			delete(s.running, nm)
			s.mu.Unlock()
			s.d.emit(rec{Op: "delete", Actor: nm, OK: true})
		}
	}
}

// pick returns the next idle created actor below total, round robin.
func (s *server) pick() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	for k := 0; k < s.spec.total; k++ {
		j := (s.next + k) % max(1, s.spec.total)
		nm := fmt.Sprintf("%s-%03d", uiPrefix, j)
		if s.created[nm] && !s.busy[nm] {
			s.busy[nm] = true
			s.next = j + 1
			return nm
		}
	}
	return ""
}

func (s *server) slot(ctx context.Context, id int) {
	for {
		s.mu.Lock()
		if id >= s.spec.awake {
			s.slots--
			s.mu.Unlock()
			return
		}
		hold := s.spec.hold
		s.mu.Unlock()
		nm := s.pick()
		if nm == "" {
			time.Sleep(time.Second)
			continue
		}
		s.d.touch(ctx, nm)
		// Hold the actor RUNNING. With -evacuate, poll the actor's /evicting endpoint every 2 s (Substrate's
		// eviction contract: the Worker SIGTERMs the actor, the actor tells its harness, the harness suspends it
		// SYNCHRONOUSLY). On "1": ack (so the snapshot carries evicting=0), then suspend at once.
		op, deadline := "suspend", time.Now().Add(hold)
		var ecl *http.Client
		var etr *http.Transport
		if *evacuateF {
			ecl, etr = s.d.evictClient(nm)
		}
		for time.Now().Before(deadline) {
			step := min(2*time.Second, time.Until(deadline))
			time.Sleep(step)
			if ecl != nil && evicting(ecl) {
				op = "evacuate"
				break
			}
		}
		if etr != nil {
			etr.CloseIdleConnections()
		}
		var a *ateapi.Actor
		var lat float64
		var err error
		if op == "evacuate" {
			a, lat, err = s.d.suspendMode(ctx, nm, true)
		} else {
			a, lat, err = s.d.suspend(ctx, nm)
		}
		r := rec{Op: op, Actor: nm, Ms: lat, OK: err == nil}
		assign(a, &r)
		if err != nil {
			r.Err = err.Error()
		}
		s.d.emit(r)
		s.mu.Lock()
		delete(s.busy, nm)
		s.mu.Unlock()
	}
}

// transportErr reports errors that mean the port-forwarded gRPC connection itself is gone (not an API answer).
func transportErr(e string) bool {
	for _, k := range []string{"code = Unavailable", "connection refused", "127.0.0.1", "transport is closing", "error reading from server"} {
		if strings.Contains(e, k) {
			return true
		}
	}
	return false
}

func (s *server) onRec(r rec) {
	if !r.OK && transportErr(r.Err) {
		s.reconn.Store(true)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now()
	r.T = now.UTC().Format(time.RFC3339Nano)
	s.recs = append(s.recs, r)
	cut := 0
	for cut < len(s.recs) {
		t, _ := time.Parse(time.RFC3339Nano, s.recs[cut].T)
		if now.Sub(t) <= statsSpan {
			break
		}
		cut++
	}
	s.recs = s.recs[cut:]
	switch r.Op {
	case "resume":
		switch {
		case r.OK:
			s.running[r.Actor] = r
		case strings.Contains(r.Err, "ACTOR_STATE_CRASHED") || strings.Contains(r.Err, "actor crashed"):
			s.lost[r.Actor] = true
		case strings.Contains(r.Err, "no worker has room"):
			s.noRoom++
		default:
			s.fails++
		}
	case "post":
		switch {
		case r.Err == "counter discontinuity":
			s.memRst++
		case !r.OK:
			s.fails++
		}
	case "suspend":
		delete(s.running, r.Actor)
	case "evacuate":
		delete(s.running, r.Actor)
		if r.OK {
			s.evacOK++
		} else {
			s.evacErr++
		}
	}
}

func (s *server) publish(ctx context.Context) {
	s.mu.Lock()
	var res []float64
	byPool := map[string][]float64{}
	resumes := 0
	for _, r := range s.recs {
		if r.Op == "resume" && r.OK {
			res = append(res, r.Ms)
			byPool[r.Pool] = append(byPool[r.Pool], r.Ms)
			resumes++
		}
	}
	byNode := map[string]int{}
	byPoolRun := map[string]int{}
	workers := []string{}
	for _, r := range s.running {
		byNode[r.Node]++
		byPoolRun[r.Pool]++
		if r.Worker != "" {
			workers = append(workers, r.Worker)
		}
	}
	sort.Strings(workers)
	p99 := map[string]float64{}
	for k, v := range byPool {
		p99[k] = round1(pctl(v, .99))
	}
	out := map[string]any{"updated": time.Now().UTC().Format(time.RFC3339), "actors_total": len(s.created),
		"actors_running": len(s.running), "actors_by_node": byNode, "actors_running_by_pool": byPoolRun,
		"running_workers": workers,
		"resumes_per_s":   round1(float64(resumes) / statsSpan.Seconds()), "lost_actors": len(s.lost),
		"resumes_waited_no_room": s.noRoom, "memory_resets": s.memRst, "failed_requests": s.fails,
		"evacuated": s.evacOK, "evacuate_failed": s.evacErr,
		"target_total": s.spec.total, "target_awake": s.spec.awake, "hold_s": s.spec.hold.Seconds(), "source": "actor-load"}
	if len(res) > 0 {
		out["resume_p50_ms"], out["resume_p99_ms"], out["resume_p99_ms_by_pool"] = round1(pctl(res, .5)), round1(pctl(res, .99)), p99
	}
	s.mu.Unlock()
	b, _ := json.Marshal(out)
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: statsCM, Namespace: loadNS}, Data: map[string]string{"stats.json": string(b)}}
	if _, err := s.cs.CoreV1().ConfigMaps(loadNS).Update(ctx, cm, metav1.UpdateOptions{}); apierrors.IsNotFound(err) {
		_, _ = s.cs.CoreV1().ConfigMaps(loadNS).Create(ctx, cm, metav1.CreateOptions{})
	}
}

func round1(x float64) float64 { return float64(int(x*10+0.5)) / 10 }

func serve(ctx context.Context) {
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
	s := &server{cs: cs, created: map[string]bool{}, busy: map[string]bool{}, running: map[string]rec{}, lost: map[string]bool{}}
	s.d = &driver{st: &state{Last: map[string][]int{}}, outF: os.Stdout}
	s.d.onRec = s.onRec
	for {
		if err = s.connect(ctx); err == nil {
			break
		}
		fmt.Fprintln(os.Stderr, "connect:", err)
		time.Sleep(10 * time.Second)
	}
	lastConnect := time.Now()
	lastPub := time.Time{}
	for {
		s.readSpec(ctx)
		s.reconcileActors(ctx)
		s.mu.Lock()
		for s.slots < s.spec.awake {
			id := s.slots
			s.slots++
			go s.slot(ctx, id)
		}
		s.mu.Unlock()
		if time.Since(lastPub) >= 5*time.Second {
			s.publish(ctx)
			lastPub = time.Now()
		}
		if time.Since(lastConnect) > 40*time.Minute || (s.reconn.Load() && time.Since(lastConnect) > 5*time.Second) {
			s.reconn.Store(false)
			if err := s.connect(ctx); err != nil {
				fmt.Fprintln(os.Stderr, "reconnect:", err)
			} else {
				lastConnect = time.Now()
			}
		}
		time.Sleep(3 * time.Second)
	}
}
