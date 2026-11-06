(function () {
  const STORAGE_PREFIX = "dairyDashboard.filtersCollapsed:";

  function storageKey() {
    return STORAGE_PREFIX + window.location.pathname;
  }

  function readCollapsed() {
    try {
      return sessionStorage.getItem(storageKey()) === "1";
    } catch (_err) {
      return false;
    }
  }

  function writeCollapsed(collapsed) {
    try {
      sessionStorage.setItem(storageKey(), collapsed ? "1" : "0");
    } catch (_err) {
      /* Ignore quota / private-mode failures. */
    }
  }

  function enhance(layout) {
    const panel = layout.querySelector(".filter-panel");
    if (!panel) return;
    const heading = panel.querySelector(":scope > h2");
    if (!heading || heading.querySelector(".filter-panel-toggle")) return;

    const label = heading.textContent.trim() || "Filters";
    heading.classList.add("filter-panel-head");
    heading.textContent = "";

    const button = document.createElement("button");
    button.type = "button";
    button.className = "filter-panel-toggle";

    const text = document.createElement("span");
    text.textContent = label;

    const chevron = document.createElement("span");
    chevron.className = "filter-panel-chevron";
    chevron.setAttribute("aria-hidden", "true");

    button.append(text, chevron);
    heading.append(button);

    function apply(collapsed, persist) {
      layout.classList.toggle("is-filters-collapsed", collapsed);
      button.setAttribute("aria-expanded", collapsed ? "false" : "true");
      button.setAttribute("aria-label", (collapsed ? "Expand " : "Collapse ") + label);
      if (persist) writeCollapsed(collapsed);
      if (persist) {
        window.requestAnimationFrame(function () {
          window.dispatchEvent(new Event("resize"));
        });
      }
    }

    button.addEventListener("click", function () {
      apply(!layout.classList.contains("is-filters-collapsed"), true);
    });

    apply(readCollapsed(), false);
  }

  document.querySelectorAll(".page-layout").forEach(enhance);
})();
