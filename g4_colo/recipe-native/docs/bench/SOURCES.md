Merge sweep1 with the single-cluster reruns into one directory for plots.py.

sweep1 ran both clusters at the same time. Both draw N4D nodes from one regional quota (CPUS_PER_VM_FAMILY, 91 N4D
vCPUs in us-west8), so whichever cluster scaled N4D second was starved: native's demand step and the controller's
GPU scale-in in sweep1 measure quota contention, not the recipe. Those phases were rerun one cluster at a time:
  native:     step + gpu  -> sweep1_native_rerun
  controller: gpu         -> sweep1_controller_rerun
The backfill and load phases (no N4D scaling) come from sweep1, measured at the same moment on both clusters.

    python3 merge.py <bench dir>     # writes <bench dir>/sweep1_merged/
