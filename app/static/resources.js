(() => {
  "use strict";

  let toastTimeout = null;

  function setText(id, text) {
    const node = document.getElementById(id);
    if (node) node.textContent = text;
  }

  function gpuShortName(gpuType) {
    return String(gpuType || "").includes("t4") ? "NVIDIA T4" : "NVIDIA L4";
  }

  function showToast(message) {
    const banner = document.getElementById("toast-banner");
    if (!banner) return;
    banner.textContent = message;
    banner.classList.remove("hidden");
    if (toastTimeout) clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => {
      banner.classList.add("hidden");
    }, 3200);
  }

  function applyCategoryFilter(category) {
    const filterBtns = document.querySelectorAll(".res-filter-btn");
    filterBtns.forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-category") === category);
    });

    const cards = document.querySelectorAll(".res-card");
    cards.forEach((card) => {
      const cardCat = card.getAttribute("data-category");
      const visible = category === "all" || cardCat === category;
      card.classList.toggle("hidden", !visible);
    });
  }

  async function refreshLiveStatus() {
    try {
      const res = await fetch("/api/state", { credentials: "same-origin" });
      if (!res.ok) return;
      const state = await res.json();

      const modeDot = document.getElementById("res-mode-dot");
      if (modeDot) {
        modeDot.className = "status-dot" + (state.mode === "live" ? " live" : "");
      }
      setText("res-mode-label", state.mode === "live" ? "Live cluster · Singapore" : "Simulation");

      if (state.quota) {
        const used = Number(state.quota.used_gpu_slices) || 0;
        const total = Number(state.quota.total_gpu_slices) || 4;
        const waiting = Number(state.quota.waiting_jobs_count) || 0;
        setText("res-live-waiting", `namespace: default · ${waiting} waiting`);
        setText("res-live-quota", `${used} / ${total} GPU slices`);
      }

      const activeTier = state.compute_class ? state.compute_class.active_tier : null;
      if (activeTier) {
        const vmType = activeTier.spot ? "Spot VM" : "On-Demand VM";
        setText("res-live-gpu", `${gpuShortName(activeTier.gpu_type)} · ${vmType}`);
        setText("res-live-tier", `cheap-gpu-timeshared · score ${activeTier.priority_score}`);
      }
    } catch (_err) {
      // Ignore transient poll errors
    }
  }

  function bindInteractions() {
    // Category filter pills
    const filterBtns = document.querySelectorAll(".res-filter-btn");
    filterBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        const cat = btn.getAttribute("data-category") || "all";
        applyCategoryFilter(cat);
      });
    });

    // Clickable SVG resource blocks -> scroll to corresponding card
    const svgGroups = document.querySelectorAll("[data-scroll-target]");
    svgGroups.forEach((group) => {
      const jump = () => {
        const targetId = group.getAttribute("data-scroll-target");
        if (!targetId) return;
        applyCategoryFilter("all");
        const targetEl = document.getElementById(targetId);
        if (targetEl) {
          targetEl.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      };
      group.addEventListener("click", jump);
      group.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          jump();
        }
      });
    });

    // Copy YAML / CLI command buttons
    const copyBtns = document.querySelectorAll(".res-copy-btn");
    copyBtns.forEach((btn) => {
      btn.addEventListener("click", async () => {
        const targetId = btn.getAttribute("data-copy-target");
        const targetEl = targetId ? document.getElementById(targetId) : null;
        if (!targetEl || !navigator.clipboard) return;
        const originalLabel = btn.textContent || "Copy";
        try {
          await navigator.clipboard.writeText(targetEl.textContent || "");
          btn.textContent = "Copied!";
          setTimeout(() => {
            btn.textContent = originalLabel;
          }, 1600);
        } catch (_err) {
          showToast("Clipboard copy wasn't permitted by the browser.");
        }
      });
    });
  }

  document.addEventListener("DOMContentLoaded", async () => {
    bindInteractions();
    await refreshLiveStatus();
    setInterval(refreshLiveStatus, 2000);
  });
})();
