(function () {
  const STORAGE_KEY = "dairyDashboard.selectedFarms";

  function readStoredFarms() {
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return null;
      return parsed.filter((value) => typeof value === "string" && value);
    } catch (_err) {
      return null;
    }
  }

  function farmsFromUrl() {
    return new URLSearchParams(window.location.search).getAll("farm").filter(Boolean);
  }

  function saveFarms(farms) {
    if (!farms || !farms.length) return;
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(farms));
    } catch (_err) {
      /* Ignore quota / private-mode failures. */
    }
  }

  function selectedFarmsIn(group) {
    return [...group.querySelectorAll(".slicer-btn.selected")]
      .map((btn) => btn.dataset.value)
      .filter(Boolean);
  }

  function isAllSelected(group) {
    const buttons = [...group.querySelectorAll(".slicer-btn[data-value]")];
    return buttons.length > 0 && buttons.every((btn) => btn.classList.contains("selected"));
  }

  function applyFarms(group, farms) {
    const buttons = [...group.querySelectorAll(".slicer-btn[data-value]")];
    if (!buttons.length) return;
    const allowed = new Set(buttons.map((btn) => btn.dataset.value));
    const wanted = farms.filter((farm) => allowed.has(farm));
    if (!wanted.length) return;
    const wantedSet = new Set(wanted);
    buttons.forEach((btn) => {
      const on = wantedSet.has(btn.dataset.value);
      btn.classList.toggle("selected", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function restore() {
    const urlFarms = farmsFromUrl();
    const farms = urlFarms.length ? urlFarms : readStoredFarms();
    if (!farms || !farms.length) return;

    document.querySelectorAll(".farm-slicer").forEach((group) => {
      // Multi-select pages start with every farm selected. Exclusive
      // single-farm pages (parlour) keep their own default.
      if (urlFarms.length || isAllSelected(group)) {
        applyFarms(group, farms);
      }
    });

    if (urlFarms.length) saveFarms(urlFarms);
  }

  function saveFromPage() {
    const group = document.querySelector(".farm-slicer");
    if (!group) return;
    const farms = selectedFarmsIn(group);
    if (farms.length) saveFarms(farms);
  }

  restore();

  document.addEventListener("click", (event) => {
    if (
      !event.target.closest(".farm-slicer .slicer-btn") &&
      !event.target.closest("#clear-filters-btn")
    ) {
      return;
    }
    saveFromPage();
  });
})();
