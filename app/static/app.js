(() => {
  "use strict";

  let csrfToken = "";
  let currentMode = "simulation";
  let selectedJobName = null;
  let cachedManifests = {};
  let toastTimeout = null;

  // Plain-language labels for the two job types
  const JOB_LABELS = {
    "critical-retrain": { short: "Urgent", title: "Image model retraining" },
    "batch-sweep": { short: "Regular", title: "Research" },
  };

  function jobLabel(job) {
    return JOB_LABELS[job.job_type] || JOB_LABELS["batch-sweep"];
  }

  function gpuShortName(gpuType) {
    return String(gpuType || "").includes("t4") ? "NVIDIA T4" : "NVIDIA L4";
  }

  function vmKind(tier) {
    return tier.spot ? "Spot VM (cheaper)" : "On-demand VM";
  }

  function showToast(message) {
    const banner = document.getElementById("toast-banner");
    if (!banner) return;
    banner.textContent = message;
    banner.classList.remove("hidden");
    if (toastTimeout) clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => {
      banner.classList.add("hidden");
    }, 4500);
  }

  async function initSession() {
    try {
      const res = await fetch("/api/session", { credentials: "same-origin" });
      if (res.ok) {
        const data = await res.json();
        csrfToken = data.csrf_token || "";
      }
    } catch (_err) {
      showToast("Can't reach the demo server. Check that it's running, then reload the page.");
    }
  }

  async function apiPost(path, bodyObj, isRetry) {
    const res = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify(bodyObj || {}),
    });
    if (res.status === 403 && !isRetry) {
      // The server restarted and the token changed: get a new one and try again.
      await initSession();
      return apiPost(path, bodyObj, true);
    }
    if (!res.ok) {
      const errData = await res.json().catch(() => ({ detail: "Request failed" }));
      throw new Error(errData.detail || "Request failed");
    }
    return res.json();
  }

  function getSelectedDuration() {
    const sel = document.getElementById("job-duration-select");
    return sel ? parseInt(sel.value, 10) || 45 : 45;
  }

  function openJobOutput(jobName) {
    selectedJobName = jobName;
    const details = document.getElementById("details-gpu");
    if (details) details.open = true;
    fetchAndRenderLogs(jobName);
  }

  function renderComputeClassRules(computeClass) {
    const container = document.getElementById("ccc-rules-container");
    if (!container) return;
    container.replaceChildren();

    const activeId = computeClass.active_tier ? computeClass.active_tier.id : "";
    (computeClass.all_tiers || []).forEach((tier) => {
      const row = document.createElement("div");
      row.className = "ccc-rule-item" + (tier.id === activeId ? " active" : "");

      const left = document.createElement("div");
      const titleSpan = document.createElement("strong");
      titleSpan.textContent = `${tier.priority_score === 20 ? "First choice" : "Backup"}: ${gpuShortName(tier.gpu_type)}, ${vmKind(tier)}`;
      const subSpan = document.createElement("div");
      subSpan.className = "mono-tag";
      subSpan.textContent = `gpu.type: ${tier.gpu_type} · spot: ${String(tier.spot)}`;
      left.appendChild(titleSpan);
      left.appendChild(subSpan);

      const scoreBadge = document.createElement("span");
      scoreBadge.className = "rule-score-pill";
      scoreBadge.textContent = `score ${tier.priority_score}`;

      row.appendChild(left);
      row.appendChild(scoreBadge);

      row.addEventListener("click", async () => {
        if (currentMode !== "simulation") {
          showToast("On the live cluster, GKE picks the machine automatically.");
          return;
        }
        try {
          await apiPost("/api/sim/node-tier", { tier: tier.id });
          showToast(`Now simulating ${gpuShortName(tier.gpu_type)} on a ${vmKind(tier)}.`);
          await refreshState();
        } catch (err) {
          showToast(err.message);
        }
      });

      container.appendChild(row);
    });
  }

  function renderQuotaMeter(quota, gpuSlots) {
    const usageText = document.getElementById("quota-usage-text");
    const waitingText = document.getElementById("queue-waiting-count");
    const track = document.getElementById("quota-bar-track");

    if (usageText) usageText.textContent = `${quota.used_gpu_slices} of ${quota.total_gpu_slices}`;
    if (waitingText) waitingText.textContent = String(quota.waiting_jobs_count);
    if (track) {
      track.replaceChildren();
      (gpuSlots || []).forEach((slot) => {
        const seg = document.createElement("div");
        let cls = "quota-segment";
        if (slot.occupied && slot.job) {
          cls += slot.job.job_type === "critical-retrain" ? " occupied-critical" : " occupied-low";
        }
        seg.className = cls;
        track.appendChild(seg);
      });
    }
  }

  function renderQueuedJobs(queuedJobs) {
    const container = document.getElementById("queued-jobs-container");
    if (!container) return;
    container.replaceChildren();

    if (!queuedJobs || queuedJobs.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-queue-box";
      empty.textContent = "No jobs waiting. Once all 4 GPU seats are busy, new jobs line up here.";
      container.appendChild(empty);
      return;
    }

    queuedJobs.forEach((job) => {
      const isCritical = job.job_type === "critical-retrain";
      const label = jobLabel(job);
      const card = document.createElement("div");
      card.className = "queued-job-card " + (isCritical ? "critical" : "low");

      const left = document.createElement("div");
      const topLine = document.createElement("div");
      const posSpan = document.createElement("strong");
      posSpan.textContent = `#${job.queue_position}  ${label.title}`;
      topLine.appendChild(posSpan);

      if (isCritical && job.jumped_count > 0) {
        const jumpBadge = document.createElement("span");
        jumpBadge.className = "jump-callouts";
        jumpBadge.textContent = `Skipped ahead of ${job.jumped_count}`;
        topLine.appendChild(jumpBadge);
      }

      const subLine = document.createElement("div");
      subLine.className = "mono-tag";
      subLine.textContent = job.queue_position === 1 ? "Runs next" : "Waiting for a free seat";

      left.appendChild(topLine);
      left.appendChild(subLine);

      const right = document.createElement("span");
      right.className = "priority-badge " + (isCritical ? "priority-critical" : "priority-low");
      right.textContent = label.short;

      card.appendChild(left);
      card.appendChild(right);
      card.addEventListener("click", () => openJobOutput(job.name));
      container.appendChild(card);
    });
  }

  function renderEvents(events) {
    const container = document.getElementById("events-container");
    if (!container) return;
    container.replaceChildren();

    (events || []).forEach((ev) => {
      const row = document.createElement("div");
      row.className = "event-row " + (ev.level || "info");
      const timeSpan = document.createElement("span");
      timeSpan.className = "event-time";
      timeSpan.textContent = `[${ev.timestamp}]`;
      const msgSpan = document.createElement("span");
      msgSpan.textContent = ev.message;
      row.appendChild(timeSpan);
      row.appendChild(msgSpan);
      container.appendChild(row);
    });
  }

  function renderGpuSlots(gpuSlots, activeTier) {
    if (activeTier) {
      const pill = document.getElementById("active-gpu-pill");
      const nodeNameEl = document.getElementById("node-name-val");
      const nodeMachineEl = document.getElementById("node-machine-val");
      const nodeScoreEl = document.getElementById("node-score-val");
      if (pill) pill.textContent = `${gpuShortName(activeTier.gpu_type)} · ${vmKind(activeTier)}`;
      if (nodeNameEl) nodeNameEl.textContent = activeTier.node_name;
      if (nodeMachineEl) nodeMachineEl.textContent = `${activeTier.machine_type} (${activeTier.gpu_type})`;
      if (nodeScoreEl) nodeScoreEl.textContent = `priorityScore ${activeTier.priority_score}`;
    }

    const container = document.getElementById("gpu-slots-container");
    if (!container) return;
    container.replaceChildren();

    (gpuSlots || []).forEach((slot) => {
      const card = document.createElement("div");
      const top = document.createElement("div");
      top.className = "slot-top-row";
      const idLabel = document.createElement("span");
      idLabel.className = "slot-id-label";
      idLabel.textContent = `Seat ${slot.slot_id}`;
      top.appendChild(idLabel);

      if (!slot.occupied || !slot.job) {
        card.className = "gpu-slot-card idle";
        const freeTag = document.createElement("span");
        freeTag.className = "mono-tag";
        freeTag.textContent = "Free";
        top.appendChild(freeTag);
        card.appendChild(top);
        container.appendChild(card);
        return;
      }

      const job = slot.job;
      const label = jobLabel(job);
      const isCritical = job.job_type === "critical-retrain";
      card.className = "gpu-slot-card " + (isCritical ? "occupied-critical" : "occupied-low");

      const prioTag = document.createElement("span");
      prioTag.className = "priority-badge " + (isCritical ? "priority-critical" : "priority-low");
      prioTag.textContent = label.short;
      top.appendChild(prioTag);

      const nameDiv = document.createElement("div");
      nameDiv.className = "slot-job-name";
      nameDiv.textContent = label.title;

      const timeDiv = document.createElement("div");
      timeDiv.className = "slot-model-name";
      timeDiv.textContent = `${job.remaining_seconds}s left`;

      const track = document.createElement("div");
      track.className = "progress-track";
      const fill = document.createElement("div");
      fill.className = "progress-fill" + (isCritical ? " critical" : "");
      fill.style.width = `${job.progress_pct || 5}%`;
      track.appendChild(fill);

      card.appendChild(top);
      card.appendChild(nameDiv);
      card.appendChild(timeDiv);
      card.appendChild(track);
      card.addEventListener("click", () => openJobOutput(job.name));
      container.appendChild(card);
    });
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function usageLevel(pct) {
    if (pct >= 85) return "high";
    if (pct >= 40) return "mid";
    return "low";
  }

  function renderGpuUsage(metrics) {
    const container = document.getElementById("gpu-usage-container");
    if (!container || !metrics) return;
    container.replaceChildren();

    const header = el("div", "usage-header");
    header.appendChild(el("span", "usage-title", "GPU usage"));
    let sourceText = "Starting…";
    if (metrics.source === "live") sourceText = "Live from the GPU (nvidia-smi)";
    if (metrics.source === "simulated") sourceText = "Estimate (simulation)";
    header.appendChild(el("span", "usage-source", sourceText));
    container.appendChild(header);

    const gpus = metrics.gpus || [];
    if (gpus.length === 0) {
      const idle = el("div", "usage-row");
      idle.appendChild(el("div", "usage-pct", "0%"));
      idle.appendChild(
        el(
          "div",
          "usage-idle-text",
          metrics.source === "live"
            ? "No jobs running right now, so the GPU is idle. GKE removes idle GPU machines after a few minutes to save cost."
            : "No jobs running right now, so the GPU is idle."
        )
      );
      container.appendChild(idle);
    }

    gpus.forEach((gpu) => {
      const pct = Math.max(0, Math.min(100, Number(gpu.utilization_pct) || 0));
      const row = el("div", "usage-row");

      const left = el("div", "usage-pct usage-" + usageLevel(pct), `${pct}%`);
      const right = el("div", "usage-detail");
      right.appendChild(el("div", "usage-gpu-name", `${gpu.name} · ${gpu.running_jobs} job${gpu.running_jobs === 1 ? "" : "s"} sharing it`));

      const bar = el("div", "usage-bar");
      const fill = el("div", "usage-bar-fill usage-" + usageLevel(pct));
      fill.style.width = `${pct}%`;
      bar.appendChild(fill);
      right.appendChild(bar);

      const usedGb = (Number(gpu.memory_used_mb) / 1024).toFixed(1);
      const totalGb = (Number(gpu.memory_total_mb) / 1024).toFixed(1);
      right.appendChild(el("div", "usage-meta", `Memory ${usedGb} of ${totalGb} GB · ${gpu.temperature_c}°C · ${gpu.power_w} W`));

      row.appendChild(left);
      row.appendChild(right);
      container.appendChild(row);
    });

    // History: last ~2 minutes, one bar every 2 seconds
    const history = metrics.history || [];
    if (history.length > 1) {
      const chart = el("div", "usage-history");
      chart.setAttribute("role", "img");
      chart.setAttribute("aria-label", `GPU usage over the last ${history.length * 2} seconds`);
      history.forEach((value) => {
        const v = Math.max(0, Math.min(100, Number(value) || 0));
        const b = el("div", "usage-history-bar usage-" + usageLevel(v));
        b.style.height = `${Math.max(2, v)}%`;
        chart.appendChild(b);
      });
      container.appendChild(chart);
      container.appendChild(el("div", "usage-history-label", "Last 2 minutes"));
    }
  }

  async function fetchAndRenderLogs(jobName) {
    if (!jobName) return;
    const titleEl = document.getElementById("log-inspector-title");
    const statusEl = document.getElementById("log-inspector-status");
    const termEl = document.getElementById("terminal-log-output");
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(jobName)}/logs`, {
        credentials: "same-origin",
      });
      if (!res.ok) return;
      const data = await res.json();
      if (titleEl) titleEl.textContent = `Job output: ${data.job_name}`;
      if (statusEl) statusEl.textContent = String(data.status);
      if (termEl) termEl.textContent = data.logs;
    } catch (_err) {
      // Keep the last output on a transient error
    }
  }

  function setSvgClass(id, className) {
    const node = document.getElementById(id);
    if (node) node.setAttribute("class", className);
  }

  function setText(id, value) {
    const node = document.getElementById(id);
    if (node) node.textContent = value;
  }

  function renderArchitectureDiagram(state) {
    const quota = state.quota || { used_gpu_slices: 0, total_gpu_slices: 4, waiting_jobs_count: 0 };
    const queuedJobs = state.queued_jobs || [];
    const gpuSlots = state.gpu_slots || [];
    const activeTier = state.compute_class ? state.compute_class.active_tier : null;
    const gpus = state.gpu_metrics && Array.isArray(state.gpu_metrics.gpus) ? state.gpu_metrics.gpus : [];

    const used = Number(quota.used_gpu_slices) || 0;
    const total = Number(quota.total_gpu_slices) || 4;
    const waiting = Number(quota.waiting_jobs_count) || 0;
    const urgentQueued = queuedJobs.some((j) => j.job_type === "critical-retrain");
    const regularQueued = queuedJobs.some((j) => j.job_type === "batch-sweep");
    const urgentRunning = gpuSlots.some((s) => s.occupied && s.job && s.job.job_type === "critical-retrain");
    const regularRunning = gpuSlots.some((s) => s.occupied && s.job && s.job.job_type === "batch-sweep");

    setText(
      "arch-live-summary-pill",
      urgentQueued
        ? `${used} / ${total} seats active · ${waiting} waiting (Urgent #1)`
        : `${used} / ${total} seats active · ${waiting} waiting`
    );

    // Stage 2: Kueue queue & quota
    if (waiting === 0) {
      setText("arch-queue-live", "Waiting line: 0 jobs");
      setSvgClass("arch-queue-live", "arch-item-title arch-fill-cyan");
      setSvgClass("arch-kueue-queue-card", "arch-subcard");
    } else if (urgentQueued) {
      setText("arch-queue-live", `Waiting: ${waiting} · Urgent is #1`);
      setSvgClass("arch-queue-live", "arch-item-title arch-fill-amber");
      setSvgClass("arch-kueue-queue-card", "arch-subcard arch-queue-urgent");
    } else {
      setText("arch-queue-live", `Waiting line: ${waiting} regular job${waiting === 1 ? "" : "s"}`);
      setSvgClass("arch-queue-live", "arch-item-title arch-fill-cyan");
      setSvgClass("arch-kueue-queue-card", "arch-subcard");
    }
    setText("arch-quota-live", `Quota in use: ${used} / ${total} GPU slices`);

    // Stage 3: ComputeClass active tier (score 20 On-Demand vs score 10 Spot)
    if (activeTier) {
      const gpuName = gpuShortName(activeTier.gpu_type);
      setText("arch-gpu-chip", gpuName);
      if (activeTier.spot) {
        setSvgClass("arch-tier-od", "arch-subcard");
        setSvgClass("arch-tier-spot", "arch-subcard arch-tier-active");
        setText("arch-tier-od-state", "Primary tier · Fallback triggered");
        setText("arch-tier-spot-state", `Active: ${gpuName} Spot VM`);
      } else {
        setSvgClass("arch-tier-od", "arch-subcard arch-tier-active");
        setSvgClass("arch-tier-spot", "arch-subcard");
        setText("arch-tier-od-state", `Active: ${gpuName} On-Demand VM`);
        setText("arch-tier-spot-state", "Fast backup capacity if OD full");
      }
    }

    // Stage 4: 4x Time-Shared GPU Seats & Telemetry
    for (let i = 1; i <= 4; i += 1) {
      const slot = gpuSlots.find((s) => Number(s.slot_id) === i);
      if (slot && slot.occupied && slot.job) {
        const isCritical = slot.job.job_type === "critical-retrain";
        const cls = isCritical ? "occupied-critical" : "occupied-low";
        const kindText = isCritical ? "Urgent" : "Regular";
        setSvgClass(`arch-seat-${i}`, `arch-seat-rect ${cls}`);
        setSvgClass(`arch-seat-${i}-label`, `arch-seat-state ${cls}`);
        setText(`arch-seat-${i}-label`, `${kindText} · ${slot.job.remaining_seconds}s`);
      } else {
        setSvgClass(`arch-seat-${i}`, "arch-seat-rect");
        setSvgClass(`arch-seat-${i}-label`, "arch-seat-state");
        setText(`arch-seat-${i}-label`, "Free");
      }
    }

    if (gpus.length > 0) {
      const pct = Math.max(0, Math.min(100, Number(gpus[0].utilization_pct) || 0));
      const src = state.gpu_metrics && state.gpu_metrics.source === "live" ? "nvidia-smi" : "sim";
      setText("arch-gpu-util", `${src}: ${pct}% util · ${gpus[0].temperature_c}°C · ${gpus[0].power_w}W`);
    } else {
      setText("arch-gpu-util", "GPU idle · 0% utilization");
    }

    // Animated flow wires when paths are active
    setSvgClass(
      "arch-wire-urgent",
      "arch-wire arch-wire-amber" + (urgentQueued || urgentRunning ? " arch-wire-live" : "")
    );
    setSvgClass(
      "arch-wire-regular",
      "arch-wire arch-wire-cyan" + (regularQueued || regularRunning ? " arch-wire-live" : "")
    );
    setSvgClass(
      "arch-wire-admit",
      "arch-wire arch-wire-emerald" + (used > 0 ? " arch-wire-live" : "")
    );
    setSvgClass(
      "arch-wire-gpu",
      "arch-wire arch-wire-emerald" + (used > 0 ? " arch-wire-live" : "")
    );
  }

  async function refreshState() {
    try {
      const res = await fetch("/api/state", { credentials: "same-origin" });
      if (!res.ok) return;
      const state = await res.json();

      currentMode = state.mode;
      const modeLabel = document.getElementById("mode-label");
      const modeDot = document.getElementById("mode-dot");
      const toggleBtn = document.getElementById("btn-toggle-mode");
      if (modeLabel) modeLabel.textContent = state.mode === "live" ? "Live cluster · Singapore" : "Simulation";
      if (modeDot) modeDot.className = "status-dot" + (state.mode === "live" ? " live" : "");
      if (toggleBtn) toggleBtn.textContent = state.mode === "live" ? "Switch to simulation" : "Switch to live cluster";

      renderComputeClassRules(state.compute_class);
      renderQuotaMeter(state.quota, state.gpu_slots);
      renderQueuedJobs(state.queued_jobs);
      renderEvents(state.events);
      renderGpuSlots(state.gpu_slots, state.compute_class.active_tier);
      renderGpuUsage(state.gpu_metrics);
      renderArchitectureDiagram(state);

      if (selectedJobName) await fetchAndRenderLogs(selectedJobName);
    } catch (_err) {
      // Ignore transient poll errors
    }
  }

  function bindEvents() {
    const bind = (id, handler) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("click", handler);
    };

    const bindKeyOrClick = (id, handler) => {
      const node = document.getElementById(id);
      if (!node) return;
      node.addEventListener("click", handler);
      node.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          handler();
        }
      });
    };

    bind("btn-submit-critical", async () => {
      try {
        await apiPost("/api/jobs/submit", { job_type: "critical-retrain", duration_seconds: getSelectedDuration() });
        showToast("Urgent job sent. It goes to the front of the line.");
        await refreshState();
      } catch (err) {
        showToast(err.message);
      }
    });

    bind("btn-submit-low", async () => {
      try {
        await apiPost("/api/jobs/submit", { job_type: "batch-sweep", duration_seconds: getSelectedDuration() });
        showToast("Regular job sent.");
        await refreshState();
      } catch (err) {
        showToast(err.message);
      }
    });

    bind("btn-burst-low", async () => {
      try {
        await apiPost("/api/jobs/burst", { job_type: "batch-sweep", count: 6, duration_seconds: getSelectedDuration() });
        showToast("6 regular jobs sent. Up to 4 run at once; the rest wait. Now try sending an urgent job.");
        await refreshState();
      } catch (err) {
        showToast(err.message);
      }
    });

    bind("btn-reset-demo", async () => {
      try {
        await apiPost("/api/reset", {});
        selectedJobName = null;
        const termEl = document.getElementById("terminal-log-output");
        if (termEl) termEl.textContent = "Click a job to see its output here.";
        showToast("Demo reset. All jobs cleared.");
        await refreshState();
      } catch (err) {
        showToast(err.message);
      }
    });

    bind("btn-toggle-mode", async () => {
      const targetMode = currentMode === "simulation" ? "live" : "simulation";
      try {
        await apiPost("/api/mode", { mode: targetMode });
        showToast(targetMode === "live" ? "Now using the live cluster." : "Now using the simulation.");
        await refreshState();
      } catch (err) {
        showToast(err.message);
      }
    });

    const modal = document.getElementById("manifests-modal");
    const codeView = document.getElementById("manifest-code-content");
    const tabButtons = document.querySelectorAll(".tab-btn");

    async function openManifestModal(fileKey) {
      if (!modal) return;
      modal.classList.remove("hidden");
      if (Object.keys(cachedManifests).length === 0) {
        const res = await fetch("/api/manifests", { credentials: "same-origin" });
        if (res.ok) cachedManifests = await res.json();
      }
      const targetFile = fileKey || "01-compute-class.yaml";
      tabButtons.forEach((b) => {
        b.classList.toggle("active", b.getAttribute("data-file") === targetFile);
      });
      if (codeView) codeView.textContent = cachedManifests[targetFile] || "File not found.";
    }

    bind("btn-view-manifests", () => openManifestModal("01-compute-class.yaml"));

    bind("btn-close-modal", () => {
      if (modal) modal.classList.add("hidden");
    });

    tabButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const fileKey = btn.getAttribute("data-file");
        if (fileKey) openManifestModal(fileKey);
      });
    });

    // Architecture diagram stage interactions
    bindKeyOrClick("arch-stage-jobs", () => openManifestModal("03-job-templates.yaml"));
    bindKeyOrClick("arch-stage-kueue", () => openManifestModal("02-kueue-config.yaml"));
    bindKeyOrClick("arch-stage-ccc", () => openManifestModal("01-compute-class.yaml"));
    bindKeyOrClick("arch-stage-gpu", () => {
      const details = document.getElementById("details-gpu");
      if (details) {
        details.open = true;
        details.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    });
  }

  document.addEventListener("DOMContentLoaded", async () => {
    bindEvents();
    await initSession();
    await refreshState();
    setInterval(refreshState, 1000);
  });
})();
