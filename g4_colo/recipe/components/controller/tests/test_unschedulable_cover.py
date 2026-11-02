"""Unschedulable backfill gets N4D cover, with a hold so replaced Pending pods do not drop it."""
import sys, types, datetime
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import controller as C
def pod(svc, node=None, unsched_ago=None, now=1000.0):
    conds = []
    if unsched_ago is not None:
        conds = [types.SimpleNamespace(type="PodScheduled", status="False", reason="Unschedulable",
                 last_transition_time=datetime.datetime.fromtimestamp(now - unsched_ago, datetime.timezone.utc))]
    return types.SimpleNamespace(metadata=types.SimpleNamespace(namespace="colo-svc", labels={"app": svc, "colo.tier": "backfill"}, deletion_timestamp=None),
                                 spec=types.SimpleNamespace(node_name=node), status=types.SimpleNamespace(conditions=conds))


def test_unschedulable_backfill_cover_and_hold():
    c = C.Controller.__new__(C.Controller)
    c.unsched_hold = {}
    assert c._unschedulable_backfill([pod("analytics", unsched_ago=5)], 1000) == {}
    assert c._unschedulable_backfill([pod("analytics", unsched_ago=25), pod("analytics", unsched_ago=25)], 1000) == {"analytics": 2}
    # replaced by fresh pending pods: hold keeps cover
    assert c._unschedulable_backfill([pod("analytics", unsched_ago=1), pod("analytics", unsched_ago=1)], 1030) == {"analytics": 2}
    # bound later: cover drops to 0 within hold, then entry expires
    assert c._unschedulable_backfill([pod("analytics", node="n1")], 1050) == {"analytics": 0}
    assert c._unschedulable_backfill([], 1200) == {} and c.unsched_hold == {}
