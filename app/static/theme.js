(() => {
  "use strict";

  // Loaded in <head> so the theme is applied before first paint.
  // Only a non-sensitive display preference is stored.
  const STORAGE_KEY = "gke-demo-theme";
  const THEMES = ["dark", "light"];
  const root = document.documentElement;

  function isTheme(value) {
    return THEMES.includes(value);
  }

  function readSaved() {
    try {
      const value = window.localStorage.getItem(STORAGE_KEY);
      return isTheme(value) ? value : null;
    } catch (_err) {
      return null;
    }
  }

  function save(theme) {
    try {
      window.localStorage.setItem(STORAGE_KEY, theme);
    } catch (_err) {
      // Storage can be blocked; the theme still applies for this page view.
    }
  }

  // ?theme=light or ?theme=dark overrides the saved choice (handy for shared links).
  const fromUrl = new URLSearchParams(window.location.search).get("theme");
  let current = isTheme(fromUrl) ? fromUrl : readSaved() || "dark";

  function apply() {
    root.setAttribute("data-theme", current);
    const btn = document.getElementById("btn-theme-toggle");
    if (btn) {
      btn.textContent = current === "dark" ? "Light theme" : "Dark theme";
    }
  }

  apply();

  document.addEventListener("DOMContentLoaded", () => {
    apply();
    const btn = document.getElementById("btn-theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", () => {
      current = current === "dark" ? "light" : "dark";
      save(current);
      apply();
    });
  });
})();
