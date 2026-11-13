// colo-exp-driver drives Agent Substrate actors for the substrate-exp experiment (wave 4).
//
// It is built inside a pinned Substrate checkout (build.sh copies it to cmd/colo-exp-driver) because it reuses
// Substrate's internal client (port-forwarded gRPC to ate-api-server + atenet-router dialer), exactly what
// `kubectl ate` uses. It changes nothing in Substrate.
//
// Subcommands (all write one JSON object per operation to -out):
//
//	create -n 50 -prefix a [-pool g4|n4d]       create actors from template colo-exp/counter
//	cycle  -n 50 -prefix a -rate 5 -dur 300s    resume -> POST -> suspend, round-robin, at <= rate resumes/s
//	touch  -n 50 -prefix a                      resume -> POST, leave RUNNING (for scale-in tests)
//	verify -n 50 -prefix a                      resume -> POST, check counters continue; prints lost count
//
// State (last counters seen per actor) lives in -state, so verify can run after anything else.
// The counter demo increments three counters per POST: memory, durable file, rootfs. An actor is "lost" when
// a resume fails after retries, it ends CRASHED, or any counter does not equal last+1.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	ateapi "go.ate.dev/substrate/api/ateapi/v1"
	"go.ate.dev/substrate/internal/ateclient"
	"go.ate.dev/substrate/internal/resources"
)

var (
	kubeconfig = flag.String("kubeconfig", "", "kubeconfig path (default: standard rules)")
	kctx       = flag.String("context", "gke_northam-ce-mlai-tpu_us-west8_g4cluster-jawadamin-us-west8-0726", "kube context")
	atespace   = flag.String("atespace", "colo-exp", "atespace")
	template   = flag.String("template", "counter", "actor template name")
	n          = flag.Int("n", 10, "number of actors")
	prefix     = flag.String("prefix", "a", "actor name prefix")
	start      = flag.Int("start", 0, "first actor index (actors are <prefix>-<start..start+n-1>)")
	pool       = flag.String("pool", "", "pin actors to a pool label colo.gke.io/pool=<g4|n4d> (create only)")
	rate       = flag.Float64("rate", 1, "max resumes per second (cycle)")
	dur        = flag.Duration("dur", 60*time.Second, "cycle duration")
	conc       = flag.Int("conc", 8, "max concurrent actor cycles")
	hold       = flag.Duration("hold", 0, "time an actor stays RUNNING between POST and suspend (cycle)")
	out        = flag.String("out", "driver.jsonl", "JSONL output")
	statePath  = flag.String("state", "driver-state.json", "counter state file")
	syncSusp   = flag.Bool("sync-suspend", false, "suspend with DURABILITY_MODE_SYNCHRONOUS")
	evacuateF  = flag.Bool("evacuate", false, "serve: poll each RUNNING actor's /evicting endpoint and suspend it (SYNCHRONOUS) when it reports SIGTERM; needs template counter-evict")
)

type rec struct {
	T        string  `json:"t"`
	Op       string  `json:"op"`
	Actor    string  `json:"actor"`
	OK       bool    `json:"ok"`
	Ms       float64 `json:"ms"`
	Node     string  `json:"node,omitempty"`
	Pool     string  `json:"pool,omitempty"`
	Worker   string  `json:"worker,omitempty"`
	State    string  `json:"state,omitempty"`
	Counters []int   `json:"counters,omitempty"`
	Expected []int   `json:"expected,omitempty"`
	Fidelity string  `json:"fidelity,omitempty"`
	Err      string  `json:"err,omitempty"`
}

type state struct {
	mu   sync.Mutex
	Last map[string][]int `json:"last"`
}

func (s *state) load() {
	s.Last = map[string][]int{}
	b, err := os.ReadFile(*statePath)
	if err == nil {
		_ = json.Unmarshal(b, s)
	}
}

func (s *state) save() {
	s.mu.Lock()
	defer s.mu.Unlock()
	b, _ := json.MarshalIndent(s, "", " ")
	_ = os.WriteFile(*statePath, b, 0o644)
}

type driver struct {
	api    *ateclient.Client
	router *ateclient.RouterDialer
	st     *state
	outMu  sync.Mutex
	outF   *os.File
	onRec  func(rec)
}

func (d *driver) emit(r rec) {
	r.T = time.Now().UTC().Format(time.RFC3339Nano)
	b, _ := json.Marshal(r)
	d.outMu.Lock()
	d.outF.Write(append(b, '\n'))
	d.outMu.Unlock()
	if d.onRec != nil {
		d.onRec(r)
	}
}

func name(i int) string { return fmt.Sprintf("%s-%03d", *prefix, *start+i) }

func ref(nm string) *ateapi.ObjectRef { return &ateapi.ObjectRef{Atespace: *atespace, Name: nm} }

func ms(t0 time.Time) float64 { return float64(time.Since(t0).Microseconds()) / 1000 }

func assign(a *ateapi.Actor, r *rec) {
	if a == nil {
		return
	}
	st := a.GetStatus()
	r.State = st.GetState().String()
	wa := st.GetWorkerAssignment()
	r.Node = wa.GetNodeName()
	if r.Node == "" {
		r.Node = st.GetAssignedNode()
	}
	r.Pool = wa.GetWorkerPool()
	r.Worker = wa.GetWorkerPod()
}

var counterRE = regexp.MustCompile(`memory count: (\d+) \| preserved file counter: (\d+) \| preserved rootfs counter: (\d+)`)

func (d *driver) post(ctx context.Context, nm string) ([]int, error) {
	aref := resources.ActorRef{Atespace: *atespace, Name: nm}
	tr := &http.Transport{
		DialContext:       func(ctx context.Context, _, _ string) (net.Conn, error) { return d.router.DialActor(ctx, aref, 80) },
		DisableKeepAlives: true,
	}
	cl := &http.Client{Transport: tr, Timeout: 30 * time.Second}
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, "http://actor/", nil)
	resp, err := cl.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	m := counterRE.FindStringSubmatch(string(body))
	if m == nil {
		return nil, fmt.Errorf("unexpected body (%d): %q", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	c := make([]int, 3)
	for i := range c {
		c[i], _ = strconv.Atoi(m[i+1])
	}
	return c, nil
}

// resume resumes nm and waits for RUNNING. Returns the actor and the latency to RUNNING.
func (d *driver) resume(ctx context.Context, nm string) (*ateapi.Actor, float64, error) {
	t0 := time.Now()
	var last error
	for attempt := 0; attempt < 5; attempt++ {
		resp, err := d.api.ResumeActor(ctx, &ateapi.ResumeActorRequest{Actor: ref(nm)})
		if err != nil {
			last = err
			time.Sleep(time.Duration(200*(attempt+1)) * time.Millisecond)
			continue
		}
		a := resp.GetActor()
		for a.GetStatus().GetState() != ateapi.ActorState_ACTOR_STATE_RUNNING {
			if a.GetStatus().GetState() == ateapi.ActorState_ACTOR_STATE_CRASHED {
				return a, ms(t0), fmt.Errorf("actor crashed")
			}
			if time.Since(t0) > 120*time.Second {
				return a, ms(t0), fmt.Errorf("timeout waiting for RUNNING (state %s)", a.GetStatus().GetState())
			}
			time.Sleep(20 * time.Millisecond)
			a, err = d.api.GetActor(ctx, &ateapi.GetActorRequest{Actor: ref(nm)})
			if err != nil {
				return nil, ms(t0), err
			}
		}
		return a, ms(t0), nil
	}
	return nil, ms(t0), last
}

// evictClient returns an HTTP client to actor nm that keeps ONE connection open. Every DialActor opens a new
// port-forward to atenet-router (several Kubernetes API calls, client-side rate limited), so polling must reuse the
// connection: one dial per hold, not one per poll. Call CloseIdleConnections when the hold ends.
func (d *driver) evictClient(nm string) (*http.Client, *http.Transport) {
	aref := resources.ActorRef{Atespace: *atespace, Name: nm}
	tr := &http.Transport{
		DialContext:         func(ctx context.Context, _, _ string) (net.Conn, error) { return d.router.DialActor(ctx, aref, 80) },
		MaxIdleConnsPerHost: 1,
		IdleConnTimeout:     5 * time.Minute,
	}
	return &http.Client{Timeout: 5 * time.Second, Transport: tr}, tr
}

// evicting asks the actor (template counter-evict) whether its Worker sent it SIGTERM. On "1" it acks first, so
// the flag is cleared in the snapshot the harness is about to take.
func evicting(cl *http.Client) bool {
	resp, err := cl.Get("http://actor/evicting")
	if err != nil {
		return false
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if strings.TrimSpace(string(body)) != "1" {
		return false
	}
	if ack, err := cl.Post("http://actor/evicting/ack", "text/plain", nil); err == nil {
		io.Copy(io.Discard, ack.Body)
		ack.Body.Close()
	}
	return true
}

func (d *driver) suspend(ctx context.Context, nm string) (*ateapi.Actor, float64, error) {
	return d.suspendMode(ctx, nm, *syncSusp)
}

func (d *driver) suspendMode(ctx context.Context, nm string, sync bool) (*ateapi.Actor, float64, error) {
	t0 := time.Now()
	opts := &ateapi.SuspendOptions{Mode: ateapi.DurabilityMode_DURABILITY_MODE_ASYNCHRONOUS}
	if sync {
		opts.Mode = ateapi.DurabilityMode_DURABILITY_MODE_SYNCHRONOUS
	}
	resp, err := d.api.SuspendActor(ctx, &ateapi.SuspendActorRequest{Actor: ref(nm), Options: opts})
	return resp.GetActor(), ms(t0), err
}

func latestFidelity(a *ateapi.Actor) string {
	ss := a.GetStatus().GetSnapshots()
	if len(ss) == 0 {
		return ""
	}
	return ss[len(ss)-1].GetFidelity().String()
}

// touch resumes and POSTs once, checking counter continuity. Returns false if the actor counts as lost.
func (d *driver) touch(ctx context.Context, nm string) bool {
	a, lat, err := d.resume(ctx, nm)
	r := rec{Op: "resume", Actor: nm, Ms: lat, OK: err == nil, Fidelity: latestFidelity(a)}
	assign(a, &r)
	if err != nil {
		r.Err = err.Error()
		d.emit(r)
		return false
	}
	d.emit(r)
	t0 := time.Now()
	var c []int
	for attempt := 0; attempt < 3; attempt++ {
		c, err = d.post(ctx, nm)
		if err == nil {
			break
		}
		time.Sleep(300 * time.Millisecond)
	}
	pr := rec{Op: "post", Actor: nm, Ms: ms(t0), OK: err == nil, Node: r.Node, Pool: r.Pool, Worker: r.Worker, Counters: c}
	d.st.mu.Lock()
	prev, seen := d.st.Last[nm]
	d.st.mu.Unlock()
	if err != nil {
		pr.Err = err.Error()
		d.emit(pr)
		return false
	}
	ok := true
	if seen {
		exp := []int{prev[0] + 1, prev[1] + 1, prev[2] + 1}
		pr.Expected = exp
		for i := range exp {
			if c[i] != exp[i] {
				ok = false
			}
		}
		if !ok {
			pr.OK = false
			pr.Err = "counter discontinuity"
		}
	}
	d.st.mu.Lock()
	d.st.Last[nm] = c
	d.st.mu.Unlock()
	d.emit(pr)
	return ok
}

func (d *driver) cycleOne(ctx context.Context, nm string) bool {
	ok := d.touch(ctx, nm)
	if *hold > 0 {
		time.Sleep(*hold)
	}
	a, lat, err := d.suspend(ctx, nm)
	r := rec{Op: "suspend", Actor: nm, Ms: lat, OK: err == nil}
	assign(a, &r)
	if err != nil {
		r.Err = err.Error()
	}
	d.emit(r)
	return ok && err == nil
}

func pctl(xs []float64, q float64) float64 {
	if len(xs) == 0 {
		return math.NaN()
	}
	s := append([]float64(nil), xs...)
	sort.Float64s(s)
	i := int(math.Ceil(q*float64(len(s)))) - 1
	if i < 0 {
		i = 0
	}
	return s[i]
}

func main() {
	flag.Parse()
	if flag.NArg() < 1 {
		fmt.Fprintln(os.Stderr, "usage: colo-exp-driver [flags] create|cycle|touch|verify|serve|guard")
		os.Exit(2)
	}
	cmd := flag.Arg(0)
	ctx := context.Background()
	if cmd == "serve" {
		serve(ctx)
		return
	}
	if cmd == "guard" {
		runGuard(ctx)
		return
	}
	api, err := ateclient.NewClient(ctx, *kubeconfig, *kctx, "", "", false)
	if err != nil {
		fmt.Fprintln(os.Stderr, "api:", err)
		os.Exit(1)
	}
	defer api.Close()
	d := &driver{api: api, st: &state{}}
	d.st.load()
	defer d.st.save()
	if cmd != "create" {
		d.router, err = ateclient.NewRouterDialer(ctx, *kubeconfig, *kctx)
		if err != nil {
			fmt.Fprintln(os.Stderr, "router:", err)
			os.Exit(1)
		}
	}
	d.outF, err = os.OpenFile(*out, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer d.outF.Close()

	switch cmd {
	case "create":
		for i := 0; i < *n; i++ {
			a := &ateapi.Actor{Metadata: &ateapi.ResourceMetadata{Atespace: *atespace, Name: name(i)}, ActorTemplate: ref(*template)}
			a.ActorTemplate.Atespace = *atespace
			if *pool != "" {
				a.WorkerSelector = &ateapi.Selector{MatchLabels: map[string]string{"colo.gke.io/pool": *pool}}
			}
			t0 := time.Now()
			_, err := api.CreateActor(ctx, &ateapi.CreateActorRequest{Actor: a})
			r := rec{Op: "create", Actor: name(i), Ms: ms(t0), OK: err == nil}
			if err != nil {
				r.Err = err.Error()
			}
			d.emit(r)
		}
	case "touch", "verify":
		var wg sync.WaitGroup
		sem := make(chan struct{}, *conc)
		var mu sync.Mutex
		lost := []string{}
		for i := 0; i < *n; i++ {
			wg.Add(1)
			sem <- struct{}{}
			go func(nm string) {
				defer wg.Done()
				defer func() { <-sem }()
				ok := d.touch(ctx, nm)
				if cmd == "verify" {
					a, _, err := d.suspend(ctx, nm)
					r := rec{Op: "suspend", Actor: nm, OK: err == nil}
					assign(a, &r)
					d.emit(r)
				}
				if !ok {
					mu.Lock()
					lost = append(lost, nm)
					mu.Unlock()
				}
			}(name(i))
		}
		wg.Wait()
		sort.Strings(lost)
		b, _ := json.Marshal(map[string]any{"op": cmd, "actors": *n, "lost": len(lost), "lost_actors": lost})
		fmt.Println(string(b))
	case "cycle":
		deadline := time.Now().Add(*dur)
		interval := time.Duration(float64(time.Second) / *rate)
		tick := time.NewTicker(interval)
		defer tick.Stop()
		sem := make(chan struct{}, *conc)
		busy := make([]bool, *n)
		var bmu sync.Mutex
		var wg sync.WaitGroup
		var mu sync.Mutex
		var lat []float64
		fails, started, skipped := 0, 0, 0
		next := 0
		for time.Now().Before(deadline) {
			<-tick.C
			select {
			case sem <- struct{}{}:
			default:
				skipped++
				continue
			}
			// next idle actor, round-robin
			bmu.Lock()
			idx := -1
			for k := 0; k < *n; k++ {
				j := (next + k) % *n
				if !busy[j] {
					idx = j
					break
				}
			}
			if idx < 0 {
				bmu.Unlock()
				<-sem
				skipped++
				continue
			}
			busy[idx] = true
			next = idx + 1
			bmu.Unlock()
			started++
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				defer func() { <-sem; bmu.Lock(); busy[i] = false; bmu.Unlock() }()
				t0 := time.Now()
				ok := d.cycleOne(ctx, name(i))
				mu.Lock()
				lat = append(lat, ms(t0))
				if !ok {
					fails++
				}
				mu.Unlock()
			}(idx)
		}
		wg.Wait()
		b, _ := json.Marshal(map[string]any{"op": "cycle", "rate_target": *rate, "started": started, "skipped_full": skipped,
			"achieved_rate": float64(started) / dur.Seconds(), "fails": fails, "cycle_p50_ms": pctl(lat, .5), "cycle_p99_ms": pctl(lat, .99)})
		fmt.Println(string(b))
	default:
		fmt.Fprintln(os.Stderr, "unknown subcommand", cmd)
		os.Exit(2)
	}
}
