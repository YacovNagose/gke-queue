import hmac
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Optional Kubernetes client import (used when connected to a real GKE cluster)
try:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes.client.rest import ApiException
    from kubernetes.stream import stream as k8s_stream

    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("gke-kueue-demo")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
K8S_DIR = BASE_DIR.parent / "k8s"

# ==============================================================================
# Security Configuration (CSRF, Secret Resolution, Rate Limiting)
# TODO(security): In a multi-user production deployment, integrate OAuth 2.0 /
# IAP (Identity-Aware Proxy) with MFA and external KMS secret management.
# ==============================================================================


def _resolve_csrf_secret() -> str:
    env_secret = os.getenv("DEMO_CSRF_SECRET")
    if env_secret:
        return env_secret
    secret_file = BASE_DIR / ".csrf_secret"
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()
    logger.warning("Generating ephemeral CSRF secret key. Instance-isolated for local demo!")
    return secrets.token_hex(32)


CSRF_SECRET = _resolve_csrf_secret()
SESSION_COOKIE_NAME = "gke_demo_session"
JOB_NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def _compute_csrf_token(session_id: str) -> str:
    return hmac.new(
        CSRF_SECRET.encode("utf-8"),
        session_id.encode("utf-8"),
        digestmod="sha256",
    ).hexdigest()


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: Dict[str, List[float]] = {}

    def allow(self, key: str, max_requests: int, window_seconds: float = 60.0) -> bool:
        now = time.time()
        with self._lock:
            timestamps = [t for t in self._buckets.get(key, []) if now - t < window_seconds]
            if len(timestamps) >= max_requests:
                self._buckets[key] = timestamps
                return False
            timestamps.append(now)
            self._buckets[key] = timestamps
            return True


rate_limiter = RateLimiter()

# ==============================================================================
# Strict Input Validation Models (Pydantic Allow-Lists)
# ==============================================================================


class SubmitJobRequest(BaseModel):
    job_type: Literal["critical-retrain", "batch-sweep"]
    duration_seconds: int = Field(default=25, ge=15, le=180)


class BurstJobsRequest(BaseModel):
    job_type: Literal["critical-retrain", "batch-sweep"] = "batch-sweep"
    count: int = Field(default=6, ge=2, le=10)
    duration_seconds: int = Field(default=25, ge=15, le=180)


class ModeSwitchRequest(BaseModel):
    mode: Literal["simulation", "live"]


class SimNodeTierRequest(BaseModel):
    tier: Literal["on-demand-l4", "on-demand-t4", "spot-l4", "spot-t4"]


# ==============================================================================
# ComputeClass Tier Metadata (Matches k8s/01-compute-class.yaml)
# ==============================================================================

COMPUTE_CLASS_TIERS: Dict[str, Dict[str, object]] = {
    "on-demand-l4": {
        "id": "on-demand-l4",
        "rule_index": 1,
        "priority_score": 20,
        "provisioning": "On-Demand",
        "spot": False,
        "gpu_type": "nvidia-l4",
        "gpu_Title": "NVIDIA L4 (24 GB VRAM)",
        "machine_type": "g2-standard-4",
        "vram_total_mb": 23034,
        "zone": "asia-southeast1-b",
        "node_name": "gke-kueue-demo-ccc-od-l4-9f8a-node1",
        "sharing_strategy": "TIME_SHARING",
        "max_shared_clients": 4,
    },
    "on-demand-t4": {
        "id": "on-demand-t4",
        "rule_index": 2,
        "priority_score": 20,
        "provisioning": "On-Demand",
        "spot": False,
        "gpu_type": "nvidia-tesla-t4",
        "gpu_Title": "NVIDIA Tesla T4 (16 GB VRAM)",
        "machine_type": "n1-standard-4",
        "vram_total_mb": 15360,
        "zone": "asia-southeast1-a",
        "node_name": "gke-kueue-demo-ccc-od-t4-4c2b-node1",
        "sharing_strategy": "TIME_SHARING",
        "max_shared_clients": 4,
    },
    "spot-l4": {
        "id": "spot-l4",
        "rule_index": 3,
        "priority_score": 10,
        "provisioning": "Spot VM",
        "spot": True,
        "gpu_type": "nvidia-l4",
        "gpu_Title": "NVIDIA L4 (24 GB VRAM)",
        "machine_type": "g2-standard-4",
        "vram_total_mb": 23034,
        "zone": "asia-southeast1-c",
        "node_name": "gke-kueue-demo-ccc-spot-l4-7d1e-node1",
        "sharing_strategy": "TIME_SHARING",
        "max_shared_clients": 4,
    },
    "spot-t4": {
        "id": "spot-t4",
        "rule_index": 4,
        "priority_score": 10,
        "provisioning": "Spot VM",
        "spot": True,
        "gpu_type": "nvidia-tesla-t4",
        "gpu_Title": "NVIDIA Tesla T4 (16 GB VRAM)",
        "machine_type": "n1-standard-4",
        "vram_total_mb": 15360,
        "zone": "asia-southeast1-b",
        "node_name": "gke-kueue-demo-ccc-spot-t4-2a9c-node1",
        "sharing_strategy": "TIME_SHARING",
        "max_shared_clients": 4,
    },
}

JOB_CATALOG = {
    "critical-retrain": {
        "title": "Urgent Image Model Retraining",
        "model_name": "image-resnet18-v4 (11M params)",
        "priority_class": "critical-priority",
        "priority_value": 1000,
        "tier_label": "CRITICAL",
        "vram_mb": 3420,
    },
    "batch-sweep": {
        "title": "Research Training Run (Small Model)",
        "model_name": "distilbert-tiny-sweep (66M params)",
        "priority_class": "low-priority",
        "priority_value": 100,
        "tier_label": "LOW PRIORITY",
        "vram_mb": 2680,
    },
}

# Real GPU workload: a small PyTorch training loop. Each step is followed by a
# pause ~3x the step time, so one job keeps the GPU ~25% busy (like a small model)
# and 4 jobs sharing the GPU push it toward ~100%.
TRAINER_IMAGE = "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime"
TRAIN_SCRIPT = """
import os, time, torch
dur = int(os.environ.get("DURATION_SECONDS", "25"))
print(f"[JOB] {os.environ.get('JOB_TITLE')} ({os.environ.get('JOB_NAME')})", flush=True)
print(f"[PRIORITY] {os.environ.get('PRIORITY_LABEL')}", flush=True)
dev = "cuda" if torch.cuda.is_available() else "cpu"
print(f"[GPU] {torch.cuda.get_device_name(0) if dev == 'cuda' else 'not found, using CPU'}", flush=True)
torch.manual_seed(0)
model = torch.nn.Sequential(
    torch.nn.Linear(2048, 4096), torch.nn.ReLU(),
    torch.nn.Linear(4096, 4096), torch.nn.ReLU(),
    torch.nn.Linear(4096, 10),
).to(dev)
opt = torch.optim.AdamW(model.parameters(), lr=1e-4)
loss_fn = torch.nn.CrossEntropyLoss()
teacher = torch.randn(2048, 10, device=dev)  # hidden rule the model has to learn
start = time.time()
next_log = start + 5
epoch = 0
while time.time() - start < dur:
    t0 = time.time()
    x = torch.randn(2048, 2048, device=dev)
    y = (x @ teacher).argmax(dim=1)
    opt.zero_grad(set_to_none=True)
    loss = loss_fn(model(x), y)
    loss.backward()
    opt.step()
    if dev == "cuda":
        torch.cuda.synchronize()
    time.sleep((time.time() - t0) * 3)
    if time.time() >= next_log:
        epoch += 1
        mem = torch.cuda.memory_allocated() / 1e9 if dev == "cuda" else 0.0
        print(f"[Epoch {epoch}] loss={loss.item():.4f} gpu_mem={mem:.2f}GB elapsed={int(time.time() - start)}s", flush=True)
        next_log += 5
print("[DONE] Training finished.", flush=True)
"""

# Fixed command (no user input) run inside a job container to read real GPU stats.
GPU_QUERY_COMMAND = [
    "/bin/sh",
    "-c",
    "PATH=$PATH:/usr/local/nvidia/bin nvidia-smi "
    "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw "
    "--format=csv,noheader,nounits",
]
GPU_HISTORY_LEN = 60

# ==============================================================================
# Simulation & Live GKE State Engine
# ==============================================================================


class DemoEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.mode: Literal["simulation", "live"] = "simulation"
        self.k8s_connected: bool = False
        self.k8s_context_name: str = "Not Connected (Simulation Mode)"
        self.sim_tier_id: str = "on-demand-l4"
        self.max_gpu_slots: int = 4
        self._seq: int = 100
        self._jobs: List[Dict[str, object]] = []
        self._events: List[Dict[str, object]] = []
        self._init_k8s_if_available()
        if self.k8s_connected and os.getenv("DEMO_DEFAULT_MODE") != "simulation":
            self.mode = "live"
            self._record_event(
                "info",
                f"Connected to live GKE cluster ({self.k8s_context_name}) in asia-southeast1 (Dataplane V2 + Kueue).",
            )
        else:
            self._record_event(
                "info",
                "ClusterQueue 'gpu-cluster-queue' ready in asia-southeast1 (4x GPU TIME_SHARING slots, Preemption=Never).",
            )

        # GPU utilization sampling (refreshed every 2s in a background thread)
        self._metrics_lock = threading.Lock()
        self._gpu_metrics: Dict[str, object] = {"source": "starting", "gpus": [], "history": [], "updated_at": None}
        self._gpu_history: List[int] = []
        self._util_ema: Dict[str, float] = {}
        threading.Thread(target=self._gpu_sampler_loop, name="gpu-sampler", daemon=True).start()

    # --------------------------------------------------------------------------
    # GPU utilization sampling
    # --------------------------------------------------------------------------

    def _gpu_sampler_loop(self) -> None:
        while True:
            try:
                if self.mode == "live" and self.k8s_connected:
                    source = "live"
                    gpus, had_running_pods = self._sample_gpus_live()
                    if had_running_pods and not gpus:
                        time.sleep(2)  # read failed mid-run; keep the last good value
                        continue
                else:
                    source, gpus = "simulated", self._sample_gpus_sim()
                for g in gpus:
                    raw = float(g["utilization_pct"])
                    prev = self._util_ema.get(str(g["node"]), raw)
                    smoothed = 0.35 * raw + 0.65 * prev
                    self._util_ema[str(g["node"])] = smoothed
                    g["utilization_raw_pct"] = int(raw)
                    g["utilization_pct"] = int(round(smoothed))
                if not gpus:
                    self._util_ema.clear()
                avg_util = int(sum(int(g["utilization_pct"]) for g in gpus) / len(gpus)) if gpus else 0
                with self._metrics_lock:
                    self._gpu_history.append(avg_util)
                    del self._gpu_history[:-GPU_HISTORY_LEN]
                    self._gpu_metrics = {
                        "source": source,
                        "gpus": gpus,
                        "history": list(self._gpu_history),
                        "updated_at": time.strftime("%H:%M:%S", time.gmtime()),
                    }
            except Exception as exc:  # keep sampling even if one read fails
                logger.warning("GPU sampling failed: %s", type(exc).__name__)
            time.sleep(2)

    def _sample_gpus_live(self) -> "tuple[List[Dict[str, object]], bool]":
        core_v1 = k8s_client.CoreV1Api()
        pods = core_v1.list_namespaced_pod(
            namespace="default",
            label_selector="demo.gke.io/job-type",
            field_selector="status.phase=Running",
            _request_timeout=5,
        )
        # Time-shared jobs all see the whole GPU, so one successful read per node is enough.
        pods_per_node: Dict[str, List[str]] = {}
        for pod in pods.items:
            node = pod.spec.node_name or ""
            statuses = pod.status.container_statuses or []
            if not node or not any(s.ready for s in statuses):
                continue
            pods_per_node.setdefault(node, []).append(pod.metadata.name)

        gpus: List[Dict[str, object]] = []
        for node, pod_names in sorted(pods_per_node.items()):
            parts: List[str] = []
            for pod_name in pod_names[:3]:
                try:
                    out = k8s_stream(
                        core_v1.connect_get_namespaced_pod_exec,
                        pod_name,
                        "default",
                        container="trainer",
                        command=GPU_QUERY_COMMAND,
                        stderr=False,
                        stdin=False,
                        stdout=True,
                        tty=False,
                        _request_timeout=5,
                    )
                except Exception:
                    continue  # pod may have just finished; try the next one
                text = str(out).strip()
                parts = [p.strip() for p in text.splitlines()[0].split(",")] if text else []
                if len(parts) >= 6:
                    break
            if len(parts) < 6:
                continue

            def num(value: str) -> float:
                try:
                    return float(value)
                except ValueError:
                    return 0.0

            gpus.append(
                {
                    "node": node,
                    "name": parts[0],
                    "utilization_pct": int(num(parts[1])),
                    "memory_used_mb": int(num(parts[2])),
                    "memory_total_mb": int(num(parts[3])),
                    "temperature_c": int(num(parts[4])),
                    "power_w": round(num(parts[5]), 1),
                    "running_jobs": len(pod_names),
                }
            )
        return gpus, bool(pods_per_node)

    def _sample_gpus_sim(self) -> List[Dict[str, object]]:
        with self._lock:
            running = [j for j in self._jobs if j["status"] == "running"]
            tier = COMPUTE_CLASS_TIERS[self.sim_tier_id]
        if not running:
            return []
        jitter = secrets.randbelow(7) - 3
        return [
            {
                "node": str(tier["node_name"]),
                "name": "NVIDIA L4" if "l4" in str(tier["gpu_type"]) else "NVIDIA T4",
                "utilization_pct": max(0, min(99, 24 * len(running) + jitter)),
                "memory_used_mb": sum(int(j["vram_mb"]) for j in running),
                "memory_total_mb": int(tier["vram_total_mb"]),
                "temperature_c": 44 + 6 * len(running),
                "power_w": round(18 + 12.5 * len(running), 1),
                "running_jobs": len(running),
            }
        ]

    def _metrics_snapshot(self) -> Dict[str, object]:
        with self._metrics_lock:
            return dict(self._gpu_metrics)

    def _init_k8s_if_available(self) -> None:
        if not K8S_AVAILABLE:
            return
        try:
            k8s_config.load_kube_config()
            contexts, active_context = k8s_config.list_kube_config_contexts()
            if active_context:
                self.k8s_context_name = str(active_context.get("name", "kubeconfig"))
                self.k8s_connected = True
        except Exception:
            try:
                k8s_config.load_incluster_config()
                self.k8s_context_name = "in-cluster-gke"
                self.k8s_connected = True
            except Exception:
                self.k8s_connected = False

    def _record_event(self, level: str, message: str) -> None:
        self._events.insert(
            0,
            {
                "timestamp": time.strftime("%H:%M:%S", time.gmtime()),
                "level": level,
                "message": message,
            },
        )
        del self._events[25:]

    def _reconcile_sim_locked(self) -> None:
        now = time.time()
        # 1. Check running jobs for completion
        for job in self._jobs:
            if job["status"] == "running":
                started_at = float(job["started_at"] or now)
                duration = float(job["duration_seconds"])
                elapsed = max(0.0, now - started_at)
                if elapsed >= duration:
                    job["status"] = "succeeded"
                    job["completed_at"] = now
                    job["progress_pct"] = 100
                    job["gpu_slot"] = None
                    self._record_event(
                        "success",
                        f"Job '{job['name']}' ({job['priority_class']}) completed training and released its shared GPU slot.",
                    )
                else:
                    job["progress_pct"] = min(99, int((elapsed / duration) * 100))

        # 2. Count occupied GPU time-sharing slots (1..4)
        used_slots = {
            int(job["gpu_slot"])
            for job in self._jobs
            if job["status"] == "running" and job.get("gpu_slot") is not None
        }
        available_slots = [s for s in range(1, self.max_gpu_slots + 1) if s not in used_slots]

        # 3. Sort queued jobs by Kueue priority-class value DESC, then created_at ASC
        #    Note: Non-preemptive! Running low-priority jobs are never evicted.
        queued_jobs = [j for j in self._jobs if j["status"] == "queued"]
        queued_jobs.sort(key=lambda j: (-int(j["priority_value"]), float(j["created_at"])))

        # 4. Admit from the head of the priority queue into any free GPU time-sharing slot
        while available_slots and queued_jobs:
            next_job = queued_jobs.pop(0)
            slot = available_slots.pop(0)
            next_job["status"] = "running"
            next_job["started_at"] = now
            next_job["gpu_slot"] = slot
            next_job["node_tier_id"] = self.sim_tier_id
            next_job["progress_pct"] = 2
            jumped = int(next_job.get("jumped_count", 0))
            if jumped > 0:
                self._record_event(
                    "critical",
                    f"Kueue admitted CRITICAL job '{next_job['name']}' into GPU Slice #{slot} ahead of {jumped} waiting low-priority job(s)!",
                )
            else:
                self._record_event(
                    "info",
                    f"Kueue admitted '{next_job['name']}' ({next_job['priority_class']}) into shared GPU Slice #{slot}.",
                )

        # 5. Update queue positions for remaining queued jobs
        for idx, qjob in enumerate(queued_jobs, start=1):
            qjob["queue_position"] = idx

    def submit_job(self, job_type: Literal["critical-retrain", "batch-sweep"], duration_seconds: int) -> Dict[str, object]:
        if self.mode == "live":
            return self._submit_k8s_job(job_type, duration_seconds)

        with self._lock:
            self._reconcile_sim_locked()
            self._seq += 1
            meta = JOB_CATALOG[job_type]
            prefix = "critical-retrain" if job_type == "critical-retrain" else "batch-sweep"
            job_name = f"{prefix}-{self._seq}"
            now = time.time()

            # Count how many low-priority queued jobs this critical job immediately jumps over
            waiting_low_priority = sum(
                1
                for j in self._jobs
                if j["status"] == "queued" and int(j["priority_value"]) < int(meta["priority_value"])
            )

            new_job: Dict[str, object] = {
                "name": job_name,
                "job_type": job_type,
                "title": meta["title"],
                "model_name": meta["model_name"],
                "priority_class": meta["priority_class"],
                "priority_value": meta["priority_value"],
                "tier_label": meta["tier_label"],
                "vram_mb": meta["vram_mb"],
                "status": "queued",
                "created_at": now,
                "started_at": None,
                "completed_at": None,
                "duration_seconds": duration_seconds,
                "progress_pct": 0,
                "gpu_slot": None,
                "queue_position": 0,
                "jumped_count": waiting_low_priority,
                "node_tier_id": self.sim_tier_id,
            }
            self._jobs.append(new_job)

            if waiting_low_priority > 0:
                self._record_event(
                    "critical",
                    f"Submitted CRITICAL job '{job_name}' (priority=1000). Jumped to front of queue ahead of {waiting_low_priority} pending batch job(s) without killing running jobs!",
                )
            else:
                self._record_event(
                    "info",
                    f"Submitted '{job_name}' ({meta['priority_class']}={meta['priority_value']}) to LocalQueue 'training-queue'.",
                )

            self._reconcile_sim_locked()
            return dict(new_job)

    def burst_jobs(
        self,
        job_type: Literal["critical-retrain", "batch-sweep"],
        count: int,
        duration_seconds: int,
    ) -> List[Dict[str, object]]:
        created = []
        for _ in range(count):
            created.append(self.submit_job(job_type, duration_seconds))
            time.sleep(0.002)
        return created

    def set_sim_tier(self, tier_id: Literal["on-demand-l4", "on-demand-t4", "spot-l4", "spot-t4"]) -> None:
        with self._lock:
            self.sim_tier_id = tier_id
            tier = COMPUTE_CLASS_TIERS[tier_id]
            for job in self._jobs:
                if job["status"] == "running":
                    job["node_tier_id"] = tier_id
            self._record_event(
                "info",
                f"ComputeClass 'cheap-gpu-timeshared' active node switched to Rule #{tier['rule_index']}: {tier['provisioning']} {tier['gpu_Title']} (priorityScore={tier['priority_score']}).",
            )

    def set_mode(self, mode: Literal["simulation", "live"]) -> None:
        with self._lock:
            if mode == "live":
                self._init_k8s_if_available()
                if not self.k8s_connected:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="No active Kubernetes/GKE cluster found in kubeconfig. Run './scripts/setup-gke-cluster.sh' first or stay in Simulation Mode.",
                    )
            self.mode = mode
            self._record_event("info", f"Switched dashboard telemetry mode to '{mode.upper()}'.")

    def reset_demo(self) -> None:
        if self.mode == "live":
            self._cleanup_k8s_jobs()
            return
        with self._lock:
            self._jobs.clear()
            self._events.clear()
            self._record_event(
                "info",
                "Demo state reset. All 4 shared GPU slices on 'cheap-gpu-timeshared' are now idle.",
            )

    def get_job_logs(self, job_name: str) -> Dict[str, object]:
        if not JOB_NAME_PATTERN.match(job_name):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job name format.")

        if self.mode == "live":
            return self._get_k8s_job_logs(job_name)

        with self._lock:
            self._reconcile_sim_locked()
            target = next((j for j in self._jobs if j["name"] == job_name), None)
            if not target:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

            tier = COMPUTE_CLASS_TIERS[str(target.get("node_tier_id", self.sim_tier_id))]
            running_jobs = [j for j in self._jobs if j["status"] == "running"]
            lines = [
                "================================================================================",
                f"JOB NAME       : {target['name']}",
                f"WORKLOAD TYPE  : {target['title']}",
                f"MODEL          : {target['model_name']}",
                f"KUEUE PRIORITY : {target['priority_class']} (priority={target['priority_value']})",
                f"COMPUTE CLASS  : cheap-gpu-timeshared -> Rule #{tier['rule_index']} (priorityScore={tier['priority_score']})",
                f"GKE NODE       : {tier['node_name']} ({tier['machine_type']} · {tier['provisioning']})",
                f"REGION / ZONE  : asia-southeast1 / {tier['zone']} (Dataplane V2 eBPF enabled)",
                "================================================================================",
            ]

            if target["status"] == "queued":
                lines.append("")
                lines.append(f"[KUEUE] Status: SUSPENDED (Waiting in LocalQueue 'training-queue', Position #{target['queue_position']})")
                lines.append("[KUEUE] Preemption Policy: withinClusterQueue=Never (Running jobs are protected from eviction)")
                if int(target.get("jumped_count", 0)) > 0:
                    lines.append(
                        f"[KUEUE] Priority Boost: Jumped ahead of {target['jumped_count']} older low-priority batch job(s)!"
                    )
                lines.append("[KUEUE] Waiting for the next 1/4 time-shared GPU slice to finish its current epoch...")
                return {"job_name": job_name, "status": target["status"], "logs": "\n".join(lines)}

            # Build realistic nvidia-smi output showing GPU Time-Sharing across active containers
            total_vram = int(tier["vram_total_mb"])
            used_vram = sum(int(j["vram_mb"]) for j in running_jobs)
            util_pct = min(98, len(running_jobs) * 24)
            lines.extend(
                [
                    "",
                    "$ nvidia-smi",
                    "+-----------------------------------------------------------------------------------------+",
                    "| NVIDIA-SMI 550.54.15              Driver Version: 550.54.15      CUDA Version: 12.4     |",
                    "|-----------------------------------------+------------------------+----------------------+",
                    "| GPU  Name                 Persistence-M | Bus-Id          Disp.A | Volatile Uncorr. ECC |",
                    "| Fan  Temp   Perf          Pwr:Usage/Cap |           Memory-Usage | GPU-Util  Compute M. |",
                    "|=========================================+========================+======================|",
                    f"|   0  {str(tier['gpu_Title'])[:22]:<22} On  |   00000000:00:04.0 Off |                    0 |",
                    f"| N/A   58C    P0             48W /  70W |   {used_vram:>5}MiB / {total_vram:>5}MiB |     {util_pct:>3}%      Default |",
                    "+-----------------------------------------+------------------------+----------------------+",
                    "| Processes (GKE GPU TIME_SHARING: maxSharedClientsPerGPU=4):                             |",
                    "|  GPU   GI   CI        PID   Type   Process name                              GPU Memory |",
                    "|        ID   ID                                                               Usage      |",
                    "|=========================================================================================|",
                ]
            )
            for idx, rjob in enumerate(running_jobs, start=1):
                pid = 14000 + int(str(rjob["name"]).split("-")[-1]) * 7
                proc_label = f"python3 train.py --job={rjob['name']} ({rjob['priority_class']})"
                lines.append(
                    f"|    0   N/A  N/A     {pid:>5}      C   {proc_label:<42} {int(rjob['vram_mb']):>5}MiB |"
                )
            lines.append("+-----------------------------------------------------------------------------------------+")
            lines.append("")

            # Epoch logs based on progress
            total_epochs = 10
            progress_pct = int(target.get("progress_pct", 0))
            completed_epochs = max(1, int((progress_pct / 100.0) * total_epochs))
            for ep in range(1, completed_epochs + 1):
                if target["job_type"] == "critical-retrain":
                    loss = 0.65 / (1.0 + 0.48 * ep)
                    metric = 84.5 + ep * 1.42
                    lines.append(
                        f"[Epoch {ep:02d}/{total_epochs:02d}] model=image-resnet18-v4 priority=CRITICAL(1000) loss={loss:.4f} val_auc={metric:.2f}% slice=TIME_SHARING(1/4)"
                    )
                else:
                    loss = 0.94 / (1.0 + 0.34 * ep)
                    metric = 71.2 + ep * 2.15
                    lines.append(
                        f"[Epoch {ep:02d}/{total_epochs:02d}] model=distilbert-tiny-sweep priority=LOW(100) loss={loss:.4f} val_acc={metric:.2f}% slice=TIME_SHARING(1/4)"
                    )

            if target["status"] == "succeeded":
                lines.append("")
                lines.append("[COMPLETE] Checkpoint saved & verified. Shared GPU slice released back to Kueue ClusterQueue.")

            return {"job_name": job_name, "status": target["status"], "logs": "\n".join(lines)}

    def get_state(self) -> Dict[str, object]:
        state = self._get_k8s_state() if self.mode == "live" else self._get_sim_state()
        state["gpu_metrics"] = self._metrics_snapshot()
        return state

    def _get_sim_state(self) -> Dict[str, object]:
        with self._lock:
            self._reconcile_sim_locked()
            now = time.time()
            tier = COMPUTE_CLASS_TIERS[self.sim_tier_id]

            running = [j for j in self._jobs if j["status"] == "running"]
            running.sort(key=lambda j: int(j["gpu_slot"] or 99))

            queued = [j for j in self._jobs if j["status"] == "queued"]
            queued.sort(key=lambda j: (-int(j["priority_value"]), float(j["created_at"])))

            completed = [j for j in self._jobs if j["status"] == "succeeded"]
            completed.sort(key=lambda j: float(j["completed_at"] or 0), reverse=True)

            slots = []
            for slot_idx in range(1, self.max_gpu_slots + 1):
                occupant = next((j for j in running if int(j["gpu_slot"] or 0) == slot_idx), None)
                if occupant:
                    rem = max(0, int(float(occupant["duration_seconds"]) - (now - float(occupant["started_at"] or now))))
                    slots.append(
                        {
                            "slot_id": slot_idx,
                            "occupied": True,
                            "job": {
                                **occupant,
                                "remaining_seconds": rem,
                            },
                        }
                    )
                else:
                    slots.append({"slot_id": slot_idx, "occupied": False, "job": None})

            return {
                "mode": self.mode,
                "k8s_connected": self.k8s_connected,
                "k8s_context": self.k8s_context_name,
                "cluster": {
                    "name": "gke-kueue-gpu-demo",
                    "region": "asia-southeast1 (Singapore)",
                    "dataplane": "Dataplane V2 (Cilium eBPF)",
                    "kueue_queue": "gpu-cluster-queue / training-queue",
                    "preemption_policy": "Never (Protects Running Training Jobs)",
                },
                "compute_class": {
                    "name": "cheap-gpu-timeshared",
                    "active_tier": tier,
                    "all_tiers": list(COMPUTE_CLASS_TIERS.values()),
                },
                "quota": {
                    "total_gpu_slices": self.max_gpu_slots,
                    "used_gpu_slices": len(running),
                    "waiting_jobs_count": len(queued),
                    "completed_jobs_count": len(completed),
                },
                "gpu_slots": slots,
                "queued_jobs": queued,
                "completed_jobs": completed[:8],
                "events": self._events[:12],
            }

    # ==========================================================================
    # Live Kubernetes / GKE Implementation
    # ==========================================================================

    def _submit_k8s_job(
        self,
        job_type: Literal["critical-retrain", "batch-sweep"],
        duration_seconds: int,
    ) -> Dict[str, object]:
        meta = JOB_CATALOG[job_type]
        batch_v1 = k8s_client.BatchV1Api()
        suffix = secrets.token_hex(3)
        job_name = f"{job_type}-{suffix}"
        epochs = max(3, duration_seconds // 5)

        waiting_low_priority = 0
        if job_type == "critical-retrain":
            try:
                existing = batch_v1.list_namespaced_job(
                    namespace="default",
                    label_selector="kueue.x-k8s.io/queue-name=training-queue",
                )
                waiting_low_priority = sum(
                    1
                    for item in existing.items
                    if item.spec.suspend
                    and (item.metadata.labels or {}).get("demo.gke.io/job-type") == "batch-sweep"
                )
            except Exception:
                waiting_low_priority = 0

        job_manifest = k8s_client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s_client.V1ObjectMeta(
                name=job_name,
                namespace="default",
                labels={
                    "kueue.x-k8s.io/queue-name": "training-queue",
                    "kueue.x-k8s.io/priority-class": str(meta["priority_class"]),
                    "demo.gke.io/job-type": job_type,
                    "demo.gke.io/priority-tier": "critical" if job_type == "critical-retrain" else "low",
                },
                annotations={
                    "demo.gke.io/duration-seconds": str(duration_seconds),
                    "demo.gke.io/jumped-count": str(waiting_low_priority),
                },
            ),
            spec=k8s_client.V1JobSpec(
                suspend=True,
                parallelism=1,
                completions=1,
                backoff_limit=1,
                template=k8s_client.V1PodTemplateSpec(
                    metadata=k8s_client.V1ObjectMeta(
                        labels={
                            "demo.gke.io/job-type": job_type,
                            "demo.gke.io/priority-tier": "critical" if job_type == "critical-retrain" else "low",
                        }
                    ),
                    spec=k8s_client.V1PodSpec(
                        restart_policy="Never",
                        node_selector={"cloud.google.com/compute-class": "cheap-gpu-timeshared"},
                        containers=[
                            k8s_client.V1Container(
                                name="trainer",
                                image=TRAINER_IMAGE,
                                command=["python", "-u", "-c", TRAIN_SCRIPT],
                                env=[
                                    k8s_client.V1EnvVar(name="JOB_NAME", value=job_name),
                                    k8s_client.V1EnvVar(name="JOB_TITLE", value=str(meta["title"])),
                                    k8s_client.V1EnvVar(
                                        name="PRIORITY_LABEL",
                                        value=f"{meta['priority_class']} ({meta['priority_value']})",
                                    ),
                                    k8s_client.V1EnvVar(name="DURATION_SECONDS", value=str(duration_seconds)),
                                ],
                                resources=k8s_client.V1ResourceRequirements(
                                    requests={"cpu": "500m", "memory": "2Gi", "nvidia.com/gpu": "1"},
                                    limits={"cpu": "1", "memory": "4Gi", "nvidia.com/gpu": "1"},
                                ),
                            )
                        ],
                    ),
                ),
            ),
        )

        try:
            batch_v1.create_namespaced_job(namespace="default", body=job_manifest)
            if waiting_low_priority > 0:
                self._record_event(
                    "critical",
                    f"[LIVE GKE] Dispatched CRITICAL Job '{job_name}' (priority=1000). Jumped ahead of {waiting_low_priority} queued batch job(s) in Kueue!",
                )
            else:
                self._record_event(
                    "critical" if job_type == "critical-retrain" else "info",
                    f"[LIVE GKE] Created Kubernetes Job '{job_name}' in queue 'training-queue' ({meta['priority_class']}).",
                )
            return {"name": job_name, "job_type": job_type, "status": "queued"}
        except ApiException as exc:
            logger.error("Kubernetes API error creating job: %s", exc.reason)
            raise HTTPException(status_code=500, detail="Failed to submit job to GKE cluster.")

    def _cleanup_k8s_jobs(self) -> None:
        batch_v1 = k8s_client.BatchV1Api()
        try:
            batch_v1.delete_collection_namespaced_job(
                namespace="default",
                label_selector="kueue.x-k8s.io/queue-name=training-queue",
                propagation_policy="Background",
            )
            self._record_event("info", "[LIVE GKE] Deleted all demo jobs in LocalQueue 'training-queue'.")
        except ApiException as exc:
            logger.error("Failed cleaning up GKE jobs: %s", exc.reason)
            raise HTTPException(status_code=500, detail="Failed to clean up jobs in GKE cluster.")

    def _get_k8s_job_logs(self, job_name: str) -> Dict[str, object]:
        core_v1 = k8s_client.CoreV1Api()
        try:
            pods = core_v1.list_namespaced_pod(
                namespace="default",
                label_selector=f"job-name={job_name}",
            )
            if not pods.items:
                return {
                    "job_name": job_name,
                    "status": "queued",
                    "logs": f"[LIVE GKE] Job '{job_name}' is suspended by Kueue in LocalQueue 'training-queue' waiting for an available GPU slice...",
                }
            pod = pods.items[0]
            pod_name = pod.metadata.name
            phase = pod.status.phase or "Pending"
            if phase == "Pending":
                return {
                    "job_name": job_name,
                    "status": "provisioning-gpu-node",
                    "logs": (
                        f"[LIVE GKE] Pod '{pod_name}' admitted by Kueue (1/4 shared GPU slice allocated).\n"
                        "GKE Custom ComputeClass 'cheap-gpu-timeshared' is provisioning/attaching an L4/T4 GPU node in asia-southeast1,\n"
                        "or the node is downloading the PyTorch image (first job on a new node takes ~1-3 minutes)...\n"
                        "  -> Priority Tier 1 (priorityScore: 20): On-Demand nvidia-l4 / nvidia-tesla-t4 (TIME_SHARING: 4x)\n"
                        "  -> Priority Tier 2 (priorityScore: 10): Spot nvidia-l4 / nvidia-tesla-t4 (TIME_SHARING: 4x)"
                    ),
                }
            log_text = core_v1.read_namespaced_pod_log(name=pod_name, namespace="default", tail_lines=100)
            return {"job_name": job_name, "status": phase.lower(), "logs": log_text}
        except ApiException as exc:
            return {"job_name": job_name, "status": "unknown", "logs": f"Waiting for pod logs ({exc.reason})..."}

    def _get_k8s_state(self) -> Dict[str, object]:
        batch_v1 = k8s_client.BatchV1Api()
        core_v1 = k8s_client.CoreV1Api()
        running_jobs: List[Dict[str, object]] = []
        queued_jobs: List[Dict[str, object]] = []
        completed_jobs: List[Dict[str, object]] = []
        now = time.time()

        try:
            jobs = batch_v1.list_namespaced_job(
                namespace="default",
                label_selector="kueue.x-k8s.io/queue-name=training-queue",
            )
            for item in jobs.items:
                labels = item.metadata.labels or {}
                annotations = item.metadata.annotations or {}
                jtype = labels.get("demo.gke.io/job-type", "batch-sweep")
                if jtype not in JOB_CATALOG:
                    jtype = "batch-sweep"
                meta = JOB_CATALOG[jtype]
                created_ts = item.metadata.creation_timestamp.timestamp() if item.metadata.creation_timestamp else now
                dur = int(annotations.get("demo.gke.io/duration-seconds", "25"))
                jumped = int(annotations.get("demo.gke.io/jumped-count", "0"))

                job_dict: Dict[str, object] = {
                    "name": item.metadata.name,
                    "job_type": jtype,
                    "title": meta["title"],
                    "model_name": meta["model_name"],
                    "priority_class": meta["priority_class"],
                    "priority_value": meta["priority_value"],
                    "tier_label": meta["tier_label"],
                    "vram_mb": meta["vram_mb"],
                    "created_at": created_ts,
                    "duration_seconds": dur,
                    "progress_pct": 15,
                    "remaining_seconds": dur,
                    "jumped_count": jumped,
                }

                if item.status.succeeded and item.status.succeeded >= 1:
                    job_dict["status"] = "succeeded"
                    job_dict["progress_pct"] = 100
                    job_dict["remaining_seconds"] = 0
                    completed_jobs.append(job_dict)
                elif item.spec.suspend:
                    job_dict["status"] = "queued"
                    job_dict["progress_pct"] = 0
                    queued_jobs.append(job_dict)
                else:
                    job_dict["status"] = "running"
                    if item.status.start_time:
                        elapsed = max(0.0, now - item.status.start_time.timestamp())
                        job_dict["progress_pct"] = min(98, max(5, int((elapsed / dur) * 100)))
                        job_dict["remaining_seconds"] = max(1, int(dur - elapsed))
                    running_jobs.append(job_dict)

            running_jobs.sort(key=lambda j: float(j["created_at"]))
            queued_jobs.sort(key=lambda j: (-int(j["priority_value"]), float(j["created_at"])))
            for idx, q in enumerate(queued_jobs, start=1):
                q["queue_position"] = idx

            # Inspect GPU node labels if present
            active_tier = dict(COMPUTE_CLASS_TIERS["on-demand-l4"])
            nodes = core_v1.list_node(label_selector="cloud.google.com/compute-class=cheap-gpu-timeshared")
            if nodes.items:
                node = nodes.items[0]
                nlabels = node.metadata.labels or {}
                acc = nlabels.get("cloud.google.com/gke-accelerator", "nvidia-l4")
                is_spot = nlabels.get("cloud.google.com/gke-spot", "false") == "true"
                tier_key = (
                    f"{'spot' if is_spot else 'on-demand'}-{'t4' if 't4' in acc else 'l4'}"
                )
                active_tier = dict(COMPUTE_CLASS_TIERS.get(tier_key, COMPUTE_CLASS_TIERS["on-demand-l4"]))
                active_tier["node_name"] = node.metadata.name
            else:
                active_tier["node_name"] = "Scale-from-0 Ready (Auto-provisions on Job admission)"

        except Exception as exc:
            logger.warning("Error reading live GKE state: %s", exc)
            active_tier = dict(COMPUTE_CLASS_TIERS[self.sim_tier_id])

        slots = []
        for slot_idx in range(1, self.max_gpu_slots + 1):
            occupant = running_jobs[slot_idx - 1] if slot_idx - 1 < len(running_jobs) else None
            if occupant:
                occupant["gpu_slot"] = slot_idx
                slots.append({"slot_id": slot_idx, "occupied": True, "job": occupant})
            else:
                slots.append({"slot_id": slot_idx, "occupied": False, "job": None})

        return {
            "mode": self.mode,
            "k8s_connected": self.k8s_connected,
            "k8s_context": self.k8s_context_name,
            "cluster": {
                "name": "gke-kueue-gpu-demo",
                "region": "asia-southeast1 (Singapore)",
                "dataplane": "Dataplane V2 (Cilium eBPF)",
                "kueue_queue": "gpu-cluster-queue / training-queue",
                "preemption_policy": "Never (Protects Running Training Jobs)",
            },
            "compute_class": {
                "name": "cheap-gpu-timeshared",
                "active_tier": active_tier,
                "all_tiers": list(COMPUTE_CLASS_TIERS.values()),
            },
            "quota": {
                "total_gpu_slices": self.max_gpu_slots,
                "used_gpu_slices": len(running_jobs),
                "waiting_jobs_count": len(queued_jobs),
                "completed_jobs_count": len(completed_jobs),
            },
            "gpu_slots": slots,
            "queued_jobs": queued_jobs,
            "completed_jobs": completed_jobs[:8],
            "events": self._events[:12],
        }


engine = DemoEngine()

# ==============================================================================
# FastAPI Application & Security Middleware
# ==============================================================================

app = FastAPI(
    title="GKE + Kueue + Custom Compute Class GPU Demo",
    description="Interactive demo UI for GKE Dataplane V2, Kueue Priority Scheduling, and L4/T4 GPU Time-Sharing in Singapore",
    version="1.0.0",
)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # 1. Enforce strict HTTP method allow-list
    if request.method not in ("GET", "POST", "HEAD"):
        return JSONResponse(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, content={"detail": "Method not allowed"})

    # 2. Enforce per-IP rate limiting
    client_ip = request.client.host if request.client else "127.0.0.1"
    limit = 60 if request.method == "POST" else 240
    if not rate_limiter.allow(f"{client_ip}:{request.method}", max_requests=limit):
        return JSONResponse(status_code=status.HTTP_429_TOO_MANY_REQUESTS, content={"detail": "Rate limit exceeded"})

    # 3. Validate CSRF token on all state-changing POST requests
    if request.method == "POST":
        session_id = request.cookies.get(SESSION_COOKIE_NAME, "")
        csrf_header = request.headers.get("X-CSRF-Token", "")
        if not session_id or not csrf_header:
            return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": "Missing CSRF token"})
        expected_csrf = _compute_csrf_token(session_id)
        if not hmac.compare_digest(csrf_header, expected_csrf):
            return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": "Invalid CSRF token"})

    response: Response = await call_next(request)

    # 4. Set strict security headers (CSP, nosniff, DENY framing, Permissions-Policy, no-store)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/session")
async def get_session(request: Request, response: Response) -> Dict[str, str]:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        session_id = secrets.token_urlsafe(32)
        is_https = request.url.scheme == "https"
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=session_id,
            httponly=True,
            secure=is_https,
            samesite="lax",
            max_age=3600 * 8,
            path="/",
        )
    return {"csrf_token": _compute_csrf_token(session_id)}


@app.get("/api/state")
async def get_cluster_state() -> Dict[str, object]:
    return engine.get_state()


@app.get("/api/manifests")
async def get_manifests() -> Dict[str, str]:
    result: Dict[str, str] = {}
    for fname in ("01-compute-class.yaml", "02-kueue-config.yaml", "03-job-templates.yaml", "04-ui-deployment.yaml"):
        fpath = (K8S_DIR / fname).resolve()
        if str(fpath).startswith(str(K8S_DIR.resolve()) + os.sep) and fpath.exists():
            result[fname] = fpath.read_text(encoding="utf-8")
    return result


@app.post("/api/jobs/submit")
async def submit_job(payload: SubmitJobRequest) -> Dict[str, object]:
    job = engine.submit_job(payload.job_type, payload.duration_seconds)
    return {"status": "ok", "job": job}


@app.post("/api/jobs/burst")
async def burst_jobs(payload: BurstJobsRequest) -> Dict[str, object]:
    jobs = engine.burst_jobs(payload.job_type, payload.count, payload.duration_seconds)
    return {"status": "ok", "created_count": len(jobs)}


@app.get("/api/jobs/{job_name}/logs")
async def get_job_logs(job_name: str) -> Dict[str, object]:
    return engine.get_job_logs(job_name)


@app.post("/api/sim/node-tier")
async def switch_sim_node_tier(payload: SimNodeTierRequest) -> Dict[str, str]:
    engine.set_sim_tier(payload.tier)
    return {"status": "ok", "active_tier": payload.tier}


@app.post("/api/mode")
async def switch_mode(payload: ModeSwitchRequest) -> Dict[str, str]:
    engine.set_mode(payload.mode)
    return {"status": "ok", "mode": payload.mode}


@app.post("/api/reset")
async def reset_demo() -> Dict[str, str]:
    engine.reset_demo()
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/resources")
async def resources_page() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "resources.html"))


@app.get("/slides")
async def slides_page() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "slides.html"))
