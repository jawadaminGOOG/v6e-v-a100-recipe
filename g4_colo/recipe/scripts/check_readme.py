#!/usr/bin/env python3
"""Gate clause 4: fail if g4_colo/recipe/README.md states a default that differs from the manifests or code.

Usage:  python3 g4_colo/recipe/scripts/check_readme.py        (exit 0 = README matches, 1 = mismatches listed)

Sources of truth (stdlib only, no cluster access):
  - components/controller/controller.py   LOOP_S default (and a LOOP_S env override in base/controller.yaml, if any)
  - components/controller/colo_logic.py   DEFAULT_CONFIG (code defaults)
  - base/controller.yaml                  colo-config config.json (base)
  - overlays/demo, overlays/production-template settings.yaml   colo-config (coloConfig)
  - base/guardrails.yaml                  colo-svc-quota requests.cpu
  - base/cluster.yaml                     ComputeClass consolidationDelayMinutes
  - base/vllm.yaml (+ production patch)   HPA target and maxReplicas

Checks:
  1. The defaults table between `<!-- defaults-table:start -->` and `<!-- defaults-table:end -->` has exactly one
     row per known setting, and every cell equals the value computed from the sources ("—" = not applicable).
  2. Prose: "every N s" (controller loop), `consolidationDelayMinutes: N`, and a number written right after a
     known config key (e.g. "`cooldown_s` (600 s)", "`escalate_s: 0`") must be one of that setting's values.
  3. Required topics are documented (colo.active gating, dehydrate grace, memory-heavy-only guard trigger, ...).
"""
from __future__ import annotations

import ast
import copy
import json
import re
import sys
from pathlib import Path

RECIPE = Path(__file__).resolve().parents[1]
NA = "—"
COLUMNS = ("code", "base", "demo", "production-template")


def read(rel: str) -> str:
    return (RECIPE / rel).read_text()


def deep_merge(base, patch):
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def default_config() -> dict:
    tree = ast.parse(read("components/controller/colo_logic.py"))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == "DEFAULT_CONFIG" for t in targets):
            return ast.literal_eval(node.value)
    raise SystemExit("DEFAULT_CONFIG not found in colo_logic.py")


def loop_s() -> float:
    src = read("components/controller/controller.py")
    m = re.search(r'^LOOP_S\s*=\s*float\(os\.environ\.get\(\s*"LOOP_S"\s*,\s*"([0-9.]+)"\s*\)\)', src, re.M) \
        or re.search(r"^LOOP_S\s*=\s*([0-9.]+)", src, re.M)
    if not m:
        raise SystemExit("LOOP_S not found in controller.py")
    env = re.search(r'name:\s*LOOP_S,\s*value:\s*"?([0-9.]+)', read("base/controller.yaml"))
    return float(env.group(1) if env else m.group(1))


def config_json_block(text: str, where: str, key: str = "config.json") -> dict:
    """The `<key>: |` YAML literal block (first one) parsed as JSON."""
    m = re.search(r"^(\s*)" + re.escape(key) + r":\s*\|\s*\n", text, re.M)
    if not m:
        raise SystemExit(f"no {key} block in {where}")
    lines = text[m.end():].splitlines()
    indent = None
    body = []
    for ln in lines:
        if not ln.strip():
            body.append("")
            continue
        cur = len(ln) - len(ln.lstrip())
        if indent is None:
            indent = cur
        if cur < indent:
            break
        body.append(ln[indent:])
    return json.loads("\n".join(body))


def get(cfg: dict, dotted: str):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return NA
        cur = cur[part]
    return cur


def fmt(v) -> str:
    if v is NA:
        return NA
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def consolidation(cls: str) -> int:
    txt = read("base/cluster.yaml")
    m = re.search(r"metadata:\s*\{name:\s*" + re.escape(cls) + r"\}.*?consolidationDelayMinutes:\s*(\d+)", txt, re.S)
    if not m:
        raise SystemExit(f"consolidationDelayMinutes for {cls} not found in cluster.yaml")
    return int(m.group(1))


def compute_rows() -> dict[str, dict[str, str]]:
    code = default_config()
    eff = {
        "code": code,
        "base": deep_merge(code, config_json_block(read("base/controller.yaml"), "base/controller.yaml")),
        "demo": deep_merge(code, config_json_block(read("overlays/demo/settings.yaml"), "overlays/demo/settings.yaml",
                                                   "coloConfig")),
        "production-template": deep_merge(code, config_json_block(
            read("overlays/production-template/settings.yaml"), "overlays/production-template/settings.yaml",
            "coloConfig")),
    }
    rows: dict[str, dict[str, str]] = {}
    ls = fmt(loop_s())
    rows["loop period (s)"] = {c: ls for c in COLUMNS}
    for key in ("guard.enabled", "guard.baseline_ttft_p99_ms", "guard.factor", "guard.window_s", "guard.min_requests",
                "guard.cooldown_s", "guard.escalate_s", "buffer_replicas", "reserve_cpu_per_node",
                "dehydrate_grace_s", "max_total_cpu", "ttft_source.type"):
        rows[key] = {c: fmt(get(eff[c], key)) for c in COLUMNS}
    quota = re.search(r'requests\.cpu:\s*"?(\d+)"?', read("base/guardrails.yaml"))
    q = quota.group(1) if quota else "missing"
    rows["colo-svc-quota requests.cpu"] = {"code": NA, "base": q, "demo": q, "production-template": q}
    for cls in ("g4-gpu", "n4d-cpu"):
        v = str(consolidation(cls))
        rows[f"consolidationDelayMinutes {cls}"] = {"code": NA, "base": v, "demo": v, "production-template": v}
    vllm = read("base/vllm.yaml")
    tgt = re.search(r'averageValue:\s*"?(\d+)"?', vllm).group(1)
    mx = re.search(r"maxReplicas:\s*(\d+)", vllm).group(1)
    prod = read("overlays/production-template/kustomization.yaml")
    pm = re.search(r"path:\s*/spec/maxReplicas\s*\n\s*value:\s*(\d+)", prod)
    rows["vLLM HPA target (rps/replica)"] = {"code": NA, "base": tgt, "demo": tgt, "production-template": tgt}
    rows["vLLM HPA maxReplicas"] = {"code": NA, "base": mx, "demo": mx,
                                    "production-template": pm.group(1) if pm else mx}
    return rows


def parse_table(readme: str) -> dict[str, list[str]] | None:
    m = re.search(r"<!-- defaults-table:start -->(.*?)<!-- defaults-table:end -->", readme, re.S)
    if not m:
        return None
    out = {}
    for ln in m.group(1).splitlines():
        ln = ln.strip()
        if not ln.startswith("|") or set(ln) <= set("|-: "):
            continue
        cells = [c.strip().strip("`").strip() for c in ln.strip("|").split("|")]
        if cells[0].lower() == "setting":
            continue
        out[cells[0]] = cells[1:]
    return out


PROSE_KEYS = ("cooldown_s", "escalate_s", "window_s", "min_requests", "baseline_ttft_p99_ms", "buffer_replicas",
              "reserve_cpu_per_node", "dehydrate_grace_s", "max_total_cpu", "factor")
REQUIRED_TOPICS = {
    "colo.active gating": r"colo\.active",
    "dehydrate grace": r"dehydrate_grace_s",
    "make-before-break dehydrate": r"make-before-break",
    "guard reacts only with memory-heavy backfill": r"only (?:reacts|acts|fires)[^.]*memory-heavy",
    "no_signal fail-safe": r"no_signal",
    "quota sizing rule": r"max_total_cpu",
    "buffer cost floor (always-on N4D node)": r"(?:24\s*[x×]\s*7|always[- ]on)[^.]*n4d-standard-16|n4d-standard-16[^.]*(?:24\s*[x×]\s*7|always[- ]on)",
    "hostPath compile cache limit": r"(?:new|added|autoscaled)[^.]*node[^.]*(?:cold|empty)",
}


def main() -> int:
    readme = read("README.md")
    rows = compute_rows()
    errors: list[str] = []

    table = parse_table(readme)
    if table is None:
        errors.append("README has no <!-- defaults-table:start --> ... <!-- defaults-table:end --> block")
        table = {}
    for name, want in rows.items():
        got = table.get(name)
        if got is None:
            errors.append(f"table: missing row `{name}` (expected {' | '.join(want[c] for c in COLUMNS)})")
            continue
        for i, col in enumerate(COLUMNS):
            cell = got[i] if i < len(got) else "<missing>"
            if cell != want[col]:
                errors.append(f"table: `{name}` [{col}] README says {cell!r}, manifests/code say {want[col]!r}")
    for name in table:
        if name not in rows:
            errors.append(f"table: unknown row `{name}` (not checked; remove it or add it to check_readme.py)")

    prose = re.sub(r"<!-- defaults-table:start -->.*?<!-- defaults-table:end -->", "", readme, flags=re.S)
    ls = rows["loop period (s)"]["base"]
    for m in re.finditer(r"every\s+(\d+(?:\.\d+)?)\s*s\b", prose):
        if fmt(float(m.group(1))) != ls:
            errors.append(f"prose: 'every {m.group(1)} s' but the controller loop is {ls} s")
    cds = {rows[f"consolidationDelayMinutes {c}"]["base"] for c in ("g4-gpu", "n4d-cpu")}
    for m in re.finditer(r"consolidationDelayMinutes:?\s*`?\s*(\d+)", prose):
        if m.group(1) not in cds:
            errors.append(f"prose: consolidationDelayMinutes {m.group(1)} not in manifests {sorted(cds)}")
    for key in PROSE_KEYS:
        row = rows.get(f"guard.{key}") or rows.get(key)
        allowed = {v for v in row.values() if v != NA}
        for m in re.finditer(r"`" + re.escape(key) + r"(?::\s*([0-9.]+|null))?`(?:\s*(?:\(|=|of|is|:)\s*`?([0-9.]+|null))?",
                             prose):
            val = m.group(1) or m.group(2)
            if val is None:
                continue
            v = val if val == "null" else fmt(float(val))
            if v not in allowed:
                errors.append(f"prose: `{key}` stated as {val}, manifests/code values are {sorted(allowed)}")
    for topic, rx in REQUIRED_TOPICS.items():
        if not re.search(rx, readme, re.I | re.S):
            errors.append(f"topic missing: {topic} (regex {rx!r})")

    # Controller README (components/controller/README.md): LOOP_S and its DEFAULT_CONFIG JSON block.
    ctl_path = RECIPE / "components/controller/README.md"
    if ctl_path.exists():
        ctl = ctl_path.read_text()
        for m in re.finditer(r"(?<!x )(?<!× )`LOOP_S`\s*=\s*([0-9.]+)", ctl):
            if fmt(float(m.group(1))) != ls:
                errors.append(f"controller README: LOOP_S={m.group(1)} but controller.py default is {ls}")
        m = re.search(r"DEFAULT_CONFIG[^\n]*\n(?:[^\n]*\n)??```json\n(.*?)```", ctl, re.S)
        if m:
            try:
                doc = json.loads(m.group(1))
            except ValueError as e:
                errors.append(f"controller README: DEFAULT_CONFIG json block does not parse: {e}")
            else:
                if doc != default_config():
                    errors.append("controller README: DEFAULT_CONFIG json block differs from colo_logic.py")

    if errors:
        print(f"README mismatches: {len(errors)}")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"README matches manifests and code ({len(rows)} table rows, prose and {len(REQUIRED_TOPICS)} topics checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
