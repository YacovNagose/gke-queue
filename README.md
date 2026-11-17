# GKE + Kueue + Custom ComputeClass (L4/T4 Time-Sharing) Demo

An interactive demonstration showing how **GKE Standard (Dataplane V2)** in **Singapore (`asia-southeast1`)**, **Kueue**, **Custom Compute Classes (`ComputeClass`)**, and **GPU Time-Sharing (`maxSharedClientsPerGPU: 4`)** enable cost-efficient small-model training on cheap GPUs (`NVIDIA L4` & `Tesla T4`) while prioritizing critical jobs over batch jobs without wasting in-progress training runs.

![GPU Queueing and Time-Sharing on Kubernetes](docs/architecture.jpg)

---

## Why Job Queueing Is Needed on Kubernetes

Even in cloud clusters with autoscaling, GPUs are finite, quota-bound, and expensive. As explained in the official Kubernetes blog post **[Introducing Kueue](https://kubernetes.io/blog/2022/10/04/introducing-kueue/)**, plain Kubernetes falls short when multiple workloads or teams compete for batch compute:

- **Control-plane & autoscaler churn:** When a standard `batch/v1` `Job` is created, `job-controller` immediately creates Pods and `kube-scheduler` continuously retries unschedulable `Pending` Pods—which can overload the control plane at scale or trigger runaway GPU node autoscaling.
- **`ResourceQuota` rejects instead of queueing:** Native `ResourceQuota` blocks Pod creation once a limit is reached rather than holding jobs in a queue until capacity frees up.
- **No job-level ordering or flexible placement:** Plain Kubernetes schedules individual Pods rather than Jobs as a unit, with no built-in job queue ordering, fair-sharing across namespaces, or dynamic placement across heterogeneous hardware (On-Demand vs. Spot, `L4` vs. `T4`).
- **Kubernetes-native without replacing `kube-scheduler`:** Unlike custom batch schedulers that replace `kube-scheduler` or fragment Job APIs, **Kueue** works alongside standard Kubernetes components using native `.spec.suspend` (holding Jobs paused with zero Pods until quota is free) and mutable scheduling directives (injecting `nodeSelector` / tolerations via `ResourceFlavor` on admission).

---

## Key Highlights Demonstrated

1. **Custom Compute Class (`k8s/01-compute-class.yaml`)**:
   - **On-Demand First (`priorityScore: 20`)**: Groups `nvidia-l4` and `nvidia-tesla-t4` (`spot: false`) at `priorityScore: 20` so GKE evaluates both On-Demand GPU types together.
   - **Spot Fallback (`priorityScore: 10`)**: Groups `nvidia-l4` and `nvidia-tesla-t4` (`spot: true`) at `priorityScore: 10` to speed up fallback VM creation when On-Demand capacity is constrained in `asia-southeast1`.
   - **GPU Time-Sharing (`TIME_SHARING`, 4 clients/GPU)**: Each physical L4 or T4 GPU exposes `4` allocatable `nvidia.com/gpu` slices so 4 small-model training jobs run concurrently on a single physical GPU.
2. **Non-Preemptive Priority Scheduling (`k8s/02-kueue-config.yaml`)**:
   - **`critical-priority` (`1000`)** vs. **`low-priority` (`100`)**.
   - Configured with `preemption.withinClusterQueue: Never` so running low-priority training jobs are **never deleted mid-epoch** (preventing wasted compute/progress).
   - Newly submitted Critical jobs immediately **jump to `#1` in the waiting queue** ahead of all pending Low-Priority jobs and claim the very next freed GPU slice.
3. **Real GPU workload & live usage**:
   - Each job runs a small PyTorch training loop (`pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime`) sized to use ~25% of the GPU, like a small model.
   - The UI reads real GPU usage, memory, temperature and power from `nvidia-smi` inside a running job every 2 seconds.

---

## Quick Start

### 1. Provision the GKE cluster in Singapore (`asia-southeast1`)

```bash
export PROJECT_ID="your-gcp-project-id"
make setup-gke
```

This runs [`scripts/setup-gke-cluster.sh`](scripts/setup-gke-cluster.sh), which:
1. Creates (if missing) the VPC `gke-kueue-vpc` and subnet `gke-singapore` (`10.20.0.0/22`). Override with `NETWORK_NAME`, `SUBNET_NAME`, `SUBNET_RANGE`.
2. Creates a GKE Standard cluster on the `rapid` channel (GKE 1.35.2+) with `--enable-dataplane-v2`.
3. Installs **Kueue** and waits for `kueue-controller-manager`.
4. Applies [`k8s/01-compute-class.yaml`](k8s/01-compute-class.yaml) and [`k8s/02-kueue-config.yaml`](k8s/02-kueue-config.yaml).

Your project needs L4/T4 GPU quota in `asia-southeast1`.

### 2. Run the demo UI

```bash
make run-ui
```

Open **http://127.0.0.1:8000**. The UI connects to your current `kubectl` context automatically (live mode). Without a cluster it runs a built-in simulation; use the header button to switch.

Running on a remote workstation? Forward the port from your laptop:

```bash
ssh -N -L 8000:localhost:8000 <your-workstation>
```

### 3. Demo flow

1. Click **Send 6 jobs** (Research). 4 jobs share the GPU and 2 wait in line.
2. Click **Send urgent job** (Image model retraining). It moves to #1 in the line without stopping any running job.
3. When a seat frees up, the urgent job runs next. Watch the **GPU usage** card rise as seats fill.

Kubernetes details (ComputeClass rules, Kueue activity, job output) are under the **Technical details** toggles.

> The first job on a new GPU node waits ~1-3 minutes while the node starts and downloads the PyTorch image. GKE removes idle GPU nodes after a few minutes.

### Clean up

```bash
make clean-jobs    # delete demo jobs
make destroy-gke   # delete the cluster
```
