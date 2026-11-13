// Clickable rows and tabs, no dependencies.
document.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (tab) {
    const root = tab.closest(".lw-tabs");
    root.querySelectorAll("li").forEach((li) => li.classList.toggle("is-active", li === tab.parentElement));
    root.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== tab.dataset.tab; });
    return;
  }
  const row = event.target.closest("tr[data-href]");
  if (row && !event.target.closest("a, button, summary")) window.location = row.dataset.href;
});

// Theme toggle: cycles auto (follow the system) -> dark -> light -> auto.
// The choice is kept in localStorage; the inline script in <head> applies it on load.
(() => {
  const KEY = "leeuwenhoek-theme";
  const LABELS = { auto: "Theme: auto", dark: "Theme: dark", light: "Theme: light" };
  const NEXT = { auto: "dark", dark: "light", light: "auto" };
  const button = document.getElementById("lw-theme-toggle");
  if (!button) return;

  let current = "auto";
  try {
    const stored = localStorage.getItem(KEY);
    if (stored === "dark" || stored === "light") current = stored;
  } catch (e) { /* storage unavailable: start from the system preference */ }

  const apply = (theme, persist) => {
    current = theme;
    const root = document.documentElement;
    if (theme === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    button.querySelector("[data-theme-label]").textContent = LABELS[theme];
    if (!persist) return;
    try {
      if (theme === "auto") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, theme);
    } catch (e) { /* storage unavailable: the choice lasts for this page only */ }
  };

  apply(current, false);
  button.addEventListener("click", () => apply(NEXT[current], true));
})();

// Live polling: new rows go on top of the listing and the menu counters follow /api/status.
// It only runs when LEEUWENHOEK_POLL_SECONDS > 0 (data-lw-poll on <body>). The rows URL is only
// set on the newest page of a listing, so an older page stays put. Paused while the tab is hidden.
(() => {
  const seconds = parseFloat(document.body.dataset.lwPoll || "0");
  if (!(seconds > 0)) return;
  const statusUrl = document.body.dataset.lwStatusUrl;
  const tbody = document.getElementById("lw-listing");
  const rowsUrl = tbody ? tbody.dataset.rowsUrl : null;
  let timer = null;
  let busy = false;

  const pollRows = async () => {
    const url = new URL(rowsUrl, window.location.href);
    const latest = tbody.querySelector("tr[data-id]");
    if (latest) url.searchParams.set("after", latest.dataset.id);
    const response = await fetch(url, { headers: { Accept: "text/html" } });
    if (!response.ok) return;
    const template = document.createElement("template");
    template.innerHTML = `<table><tbody>${await response.text()}</tbody></table>`;
    const fresh = template.content.querySelectorAll("tr[data-id]");
    if (!fresh.length) return;
    // The rows come newest first: insert them in order above the current first row.
    const first = tbody.firstElementChild;
    fresh.forEach((row) => tbody.insertBefore(row, first));
    tbody.closest(".table-container").hidden = false;
    const empty = document.getElementById("lw-empty");
    if (empty) empty.hidden = true;
  };

  const pollStatus = async () => {
    const response = await fetch(statusUrl, { headers: { Accept: "application/json" } });
    if (!response.ok) return;
    const { counts } = await response.json();
    document.querySelectorAll("[data-count]").forEach((tag) => {
      tag.textContent = String(counts[tag.dataset.count] || 0);
    });
  };

  const tick = async () => {
    if (busy || document.visibilityState !== "visible") return;
    busy = true;
    try {
      await Promise.all([rowsUrl ? pollRows() : null, pollStatus()]);
    } catch (e) {
      /* network hiccup or the server is restarting: try again on the next tick */
    } finally {
      busy = false;
    }
  };

  const start = () => { if (timer === null) timer = window.setInterval(tick, seconds * 1000); };
  const stop = () => { window.clearInterval(timer); timer = null; };

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      tick();
      start();
    } else {
      stop();
    }
  });
  if (document.visibilityState === "visible") start();
})();
