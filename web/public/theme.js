// Applied before first paint so the page never flashes the wrong theme (kept external for a strict CSP).
try {
  var t = localStorage.getItem("omen-theme");
  var dark = t ? t === "dark" : !window.matchMedia("(prefers-color-scheme: light)").matches;
  document.documentElement.classList.toggle("dark", dark);
} catch (e) {}
