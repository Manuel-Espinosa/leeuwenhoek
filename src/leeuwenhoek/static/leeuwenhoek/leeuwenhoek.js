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
