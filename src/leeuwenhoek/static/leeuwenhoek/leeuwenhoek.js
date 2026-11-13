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
