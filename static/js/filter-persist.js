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

  const CLICK_HOLD_MS = 300;
  const multiSelectGroups = new Set(
    [...document.querySelectorAll(".farm-slicer")].filter(isAllSelected)
  );
  multiSelectGroups.forEach((group) => {
    group.querySelectorAll(".slicer-btn[data-value]").forEach((btn) => {
      btn.title = "Double-click to show only this farm";
    });
  });

  let committingClick = false;
  let pendingClick = null;

  function farmButton(event) {
    const btn = event.target.closest(".farm-slicer .slicer-btn");
    if (!btn) return null;
    const group = btn.closest(".farm-slicer");
    if (!multiSelectGroups.has(group)) return null;
    return { btn, group };
  }

  function commitClick(btn) {
    committingClick = true;
    btn.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    committingClick = false;
  }

  function selectAllFarms(group) {
    if (isAllSelected(group)) return;
    const buttons = [...group.querySelectorAll(".slicer-btn[data-value]")];
    const stillOff = buttons.find((btn) => !btn.classList.contains("selected"));
    if (!stillOff) return;
    buttons.forEach((btn) => {
      if (btn === stillOff) return;
      btn.classList.add("selected");
      btn.setAttribute("aria-pressed", "true");
    });
    commitClick(stillOff);
  }

  multiSelectGroups.forEach((group) => {
    if (!group.parentElement) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary small farm-select-all";
    button.textContent = "Select all";
    button.addEventListener("click", () => selectAllFarms(group));
    group.parentElement.insertBefore(button, group);
  });

  restore();

  document.addEventListener("click", (event) => {
    if (committingClick) return;
    const hit = farmButton(event);
    if (!hit) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    clearTimeout(pendingClick);
    pendingClick = setTimeout(() => {
      pendingClick = null;
      commitClick(hit.btn);
    }, CLICK_HOLD_MS);
  }, true);

  document.addEventListener("dblclick", (event) => {
    const hit = farmButton(event);
    if (!hit) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    clearTimeout(pendingClick);
    pendingClick = null;
    hit.group.querySelectorAll(".slicer-btn").forEach((btn) => {
      btn.classList.remove("selected");
      btn.setAttribute("aria-pressed", "false");
    });
    commitClick(hit.btn);
  }, true);

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
