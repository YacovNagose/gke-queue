(() => {
  "use strict";

  const slides = Array.from(document.querySelectorAll(".slide"));
  const dotsWrap = document.getElementById("deck-dots");
  const counter = document.getElementById("deck-counter");
  const prevBtn = document.getElementById("btn-prev-slide");
  const nextBtn = document.getElementById("btn-next-slide");
  const fullscreenBtn = document.getElementById("btn-fullscreen");
  const dots = [];
  let index = 0;

  if (slides.length === 0) return;

  function clampIndex(i) {
    return Math.max(0, Math.min(slides.length - 1, i));
  }

  // The URL hash holds the slide number (#3), so a slide can be linked or reloaded.
  function indexFromHash() {
    const n = Number.parseInt(window.location.hash.slice(1), 10);
    return Number.isInteger(n) && n >= 1 && n <= slides.length ? n - 1 : 0;
  }

  function show(i) {
    index = clampIndex(i);
    slides.forEach((slide, k) => {
      slide.hidden = k !== index;
    });
    dots.forEach((dot, k) => {
      dot.classList.toggle("active", k === index);
      if (k === index) {
        dot.setAttribute("aria-current", "true");
      } else {
        dot.removeAttribute("aria-current");
      }
    });
    if (counter) counter.textContent = `${index + 1} / ${slides.length}`;
    if (prevBtn) prevBtn.disabled = index === 0;
    if (nextBtn) nextBtn.disabled = index === slides.length - 1;

    const hash = `#${index + 1}`;
    if (window.location.hash !== hash) {
      window.history.replaceState(null, "", hash);
    }
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else if (document.documentElement.requestFullscreen) {
      document.documentElement.requestFullscreen().catch(() => {
        // The browser can refuse full screen; the deck still works in the window.
      });
    }
  }

  function buildDots() {
    if (!dotsWrap) return;
    slides.forEach((slide, k) => {
      const dot = document.createElement("button");
      dot.type = "button";
      dot.className = "deck-dot";
      dot.id = `deck-dot-${k + 1}`;
      const heading = slide.querySelector(".slide-title");
      const title = heading ? heading.textContent.trim() : "";
      dot.setAttribute("aria-label", `Go to slide ${k + 1}: ${title}`);
      dot.addEventListener("click", () => show(k));
      dotsWrap.appendChild(dot);
      dots.push(dot);
    });
  }

  function onKeydown(ev) {
    if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
    const tag = ev.target && ev.target.tagName ? ev.target.tagName : "";
    const onControl = tag === "BUTTON" || tag === "A" || tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";

    switch (ev.key) {
      case "ArrowRight":
      case "PageDown":
        ev.preventDefault();
        show(index + 1);
        break;
      case "ArrowLeft":
      case "PageUp":
        ev.preventDefault();
        show(index - 1);
        break;
      case " ":
        // Space on a focused button or link should activate it, not change slide.
        if (onControl) return;
        ev.preventDefault();
        show(index + (ev.shiftKey ? -1 : 1));
        break;
      case "Home":
        ev.preventDefault();
        show(0);
        break;
      case "End":
        ev.preventDefault();
        show(slides.length - 1);
        break;
      case "f":
      case "F":
        if (onControl && tag !== "BUTTON" && tag !== "A") return;
        toggleFullscreen();
        break;
      default:
        break;
    }
  }

  buildDots();
  if (prevBtn) prevBtn.addEventListener("click", () => show(index - 1));
  if (nextBtn) nextBtn.addEventListener("click", () => show(index + 1));
  if (fullscreenBtn) fullscreenBtn.addEventListener("click", toggleFullscreen);
  document.addEventListener("keydown", onKeydown);
  window.addEventListener("hashchange", () => show(indexFromHash()));
  document.addEventListener("fullscreenchange", () => {
    if (fullscreenBtn) {
      fullscreenBtn.textContent = document.fullscreenElement ? "Exit full screen" : "Full screen";
    }
  });

  show(indexFromHash());
})();
