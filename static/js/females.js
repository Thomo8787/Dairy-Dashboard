const PAGE = JSON.parse(document.getElementById("page-config").textContent);
const FIXED_COLUMNS = PAGE.mode === "embryo" ? [
  { key: "visual_id", label: "Calf", type: "text" },
  { key: "dob", label: "DOB", type: "date" },
  { key: "recipient_id", label: "Recipient", type: "text" },
  { key: "dam", label: "Dam", type: "text" },
  { key: "due_date", label: "Due", type: "date" },
  { key: "flush_sire", label: "Flush sire", type: "text" },
  { key: "sire_name", label: "Genomic sire", type: "text" },
  { key: "eartag", label: "Eartag", type: "text" },
] : [
  { key: "visual_id", label: "Visual ID", type: "text" },
  { key: "eartag", label: "Eartag", type: "text" },
  { key: "dob", label: "Date of birth", type: "date" },
  { key: "sire_name", label: "Sire", type: "text" },
];
const SEARCH_KEYS = PAGE.mode === "embryo"
  ? ["visual_id", "eartag", "sire_name", "flush_sire", "recipient_id", "dam", "hbn"]
  : ["visual_id", "eartag", "sire_name", "cow_id", "hbn"];
const PAGE_SIZE = 100;
const sortState = { key: "visual_id", dir: 1 };
let rows = [];
let catalog = [];
let displayedKeys = loadDisplayed();
let draftKeys = [];
let selectedAvailable = new Set();
let selectedDisplayed = new Set();
let dragKey = "";
let currentPage = 1;
let renderTimer = 0;

function loadDisplayed() {
  try {
    const saved = JSON.parse(localStorage.getItem(PAGE.storageKey) || "[]");
    return Array.isArray(saved) ? saved.filter((key) => typeof key === "string") : [];
  } catch (err) {
    return [];
  }
}

function catalogByKey(key) {
  return catalog.find((col) => col.key === key);
}

function activeColumns() {
  const traits = displayedKeys.map((key) => {
    const col = catalogByKey(key);
    if (!col) return null;
    return { key: col.key, label: col.label, type: col.numeric ? "num" : "text" };
  }).filter(Boolean);
  return FIXED_COLUMNS.concat(traits);
}

function escapeHtml(str) {
  return String(str == null ? "" : str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function showMessage(text, kind) {
  const el = document.getElementById("status-message");
  el.textContent = text || "";
  el.className = "message" + (kind ? " " + kind : "");
}

function searchQuery() {
  return document.getElementById("search-input").value.trim().toLowerCase();
}

function readTraitBounds() {
  const bounds = [];
  document.querySelectorAll("#trait-filters .trait-filter-row").forEach((row) => {
    const minInput = row.querySelector('input[data-bound="min"]');
    const maxInput = row.querySelector('input[data-bound="max"]');
    const min = minInput && minInput.value !== "" ? Number(minInput.value) : null;
    const max = maxInput && maxInput.value !== "" ? Number(maxInput.value) : null;
    if (Number.isFinite(min) || Number.isFinite(max)) {
      bounds.push({
        key: minInput.dataset.key,
        min: Number.isFinite(min) ? min : null,
        max: Number.isFinite(max) ? max : null,
      });
    }
  });
  return bounds;
}

function passesTraitFilters(row, bounds) {
  return bounds.every((bound) => {
    const value = Number(row[bound.key]);
    if (!Number.isFinite(value)) return false;
    if (bound.min !== null && value < bound.min) return false;
    if (bound.max !== null && value > bound.max) return false;
    return true;
  });
}

function passesDob(row) {
  const from = document.getElementById("dob-from").value;
  const to = document.getElementById("dob-to").value;
  if (!from && !to) return true;
  if (!row.dob) return false;
  if (from && row.dob < from) return false;
  if (to && row.dob > to) return false;
  return true;
}

function filteredRows() {
  const q = searchQuery();
  const bounds = readTraitBounds();
  return rows.filter((row) => {
    if (!passesDob(row) || !passesTraitFilters(row, bounds)) return false;
    if (!q) return true;
    return SEARCH_KEYS.some((key) => String(row[key] || "").toLowerCase().includes(q));
  });
}

function sortValue(row, key) {
  const column = activeColumns().find((col) => col.key === key);
  const value = row[key];
  if (value === null || value === undefined || value === "") return -Infinity;
  if (column && column.type === "num") {
    const num = Number(value);
    return Number.isFinite(num) ? num : -Infinity;
  }
  return String(value).toLowerCase();
}

function sortedRows(source) {
  const copy = source.slice();
  copy.sort((a, b) => {
    const av = sortValue(a, sortState.key);
    const bv = sortValue(b, sortState.key);
    if (av < bv) return -1 * sortState.dir;
    if (av > bv) return 1 * sortState.dir;
    return String(a.visual_id || a.eartag || "").localeCompare(String(b.visual_id || b.eartag || ""));
  });
  return copy;
}

function fmtNum(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "—";
  return num.toLocaleString("en-GB", { maximumFractionDigits: 2 });
}

function cellHtml(row, col) {
  if (col.type === "date") {
    if (col.key === "due_date") return escapeHtml(row.due_label || "—");
    return escapeHtml(row.dob_label || "—");
  }
  if (col.type === "num") return fmtNum(row[col.key]);
  const text = row[col.key];
  return escapeHtml(text == null || text === "" ? "—" : text);
}

function scheduleRender(resetPage) {
  if (resetPage) currentPage = 1;
  window.clearTimeout(renderTimer);
  renderTimer = window.setTimeout(renderTable, 80);
}

function renderTraitFilters() {
  const host = document.getElementById("trait-filters");
  const kept = readTraitBounds();
  const numeric = displayedKeys.map(catalogByKey).filter((col) => col && col.numeric);
  host.innerHTML = numeric.map((trait) => {
    const bound = kept.find((item) => item.key === trait.key) || {};
    const min = bound.min == null ? "" : bound.min;
    const max = bound.max == null ? "" : bound.max;
    return '<div class="trait-filter-row">' +
      "<label>" + escapeHtml(trait.label) + "</label>" +
      '<input type="number" data-key="' + escapeHtml(trait.key) + '" data-bound="min" step="any" placeholder="—" value="' + min + '">' +
      '<input type="number" data-key="' + escapeHtml(trait.key) + '" data-bound="max" step="any" placeholder="—" value="' + max + '">' +
      "</div>";
  }).join("");
}

function renderHead() {
  const columns = activeColumns();
  document.getElementById("females-head").innerHTML = columns.map((col) => {
    const cls = [
      col.type === "num" ? "num" : "",
      sortState.key === col.key ? (sortState.dir > 0 ? "sorted-asc" : "sorted-desc") : "",
    ].filter(Boolean).join(" ");
    return '<th data-sort="' + col.key + '" class="' + cls + '">' + escapeHtml(col.label) + "</th>";
  }).join("");
}

function renderTable() {
  renderHead();
  const columns = activeColumns();
  const visible = sortedRows(filteredRows());
  const pageCount = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  if (currentPage > pageCount) currentPage = pageCount;
  const start = (currentPage - 1) * PAGE_SIZE;
  const pageRows = visible.slice(start, start + PAGE_SIZE);
  const noun = PAGE.noun;
  const parts = [visible.length.toLocaleString("en-GB") + " " + (visible.length === 1 ? noun.one : noun.many)];
  if (visible.length !== rows.length) parts.push("of " + rows.length.toLocaleString("en-GB"));
  if (visible.length) parts.push("showing " + (start + 1) + "–" + (start + pageRows.length));
  document.getElementById("meta-line").textContent = parts.join(" · ");
  document.getElementById("page-label").textContent = "Page " + currentPage + " of " + pageCount;
  document.getElementById("page-prev").disabled = currentPage <= 1;
  document.getElementById("page-next").disabled = currentPage >= pageCount;
  const body = document.getElementById("table-body");
  if (!visible.length) {
    body.innerHTML = '<tr><td colspan="' + columns.length + '" class="empty-state">No ' +
      escapeHtml(noun.many) + " match the current filters.</td></tr>";
    return;
  }
  body.innerHTML = pageRows.map((row) => {
    return "<tr>" + columns.map((col) => {
      const classes = [col.type === "num" ? "num" : ""];
      if (col.type === "num" && Number(row[col.key]) < 0) classes.push("negative");
      const cls = classes.filter(Boolean).join(" ");
      return "<td" + (cls ? ' class="' + cls + '"' : "") + ">" + cellHtml(row, col) + "</td>";
    }).join("") + "</tr>";
  }).join("");
}

function applyPayload(data) {
  rows = data.rows || [];
  catalog = data.columns || [];
  displayedKeys = displayedKeys.filter((key) => catalogByKey(key));
  renderTraitFilters();
  renderTable();
}

function listItems(keys, selected, search, draggable) {
  const needle = search.trim().toLowerCase();
  return keys.filter((key) => {
    const col = catalogByKey(key);
    return col && (!needle || col.label.toLowerCase().includes(needle));
  }).map((key) => {
    const col = catalogByKey(key);
    const classes = selected.has(key) ? "selected" : "";
    return '<li data-key="' + escapeHtml(key) + '" class="' + classes + '"' +
      (draggable ? ' draggable="true"' : "") + ">" + escapeHtml(col.label) + "</li>";
  }).join("");
}

function renderColumnLists() {
  const available = catalog.map((col) => col.key).filter((key) => !draftKeys.includes(key));
  document.getElementById("available-list").innerHTML = listItems(
    available,
    selectedAvailable,
    document.getElementById("available-search").value,
    false
  );
  document.getElementById("displayed-list").innerHTML = listItems(
    draftKeys,
    selectedDisplayed,
    document.getElementById("displayed-search").value,
    true
  );
}

function openColumns() {
  draftKeys = displayedKeys.slice();
  selectedAvailable = new Set();
  selectedDisplayed = new Set();
  document.getElementById("columns-note").textContent = PAGE.mode === "embryo"
    ? "Calf, dates, recipient, dam, and both sires stay on the table. Drag the displayed traits to change their order."
    : "Visual ID, eartag, date of birth, and sire stay on the table. Drag the displayed traits to change their order.";
  document.getElementById("columns-overlay").hidden = false;
  renderColumnLists();
}

function closeColumns() {
  document.getElementById("columns-overlay").hidden = true;
}

async function loadRows() {
  document.getElementById("meta-line").textContent = "Loading…";
  try {
    const res = await fetch(PAGE.apiUrl);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Failed to load females");
    applyPayload(data);
    if (data.imported) {
      showMessage(
        "Loaded " + data.imported.rows_imported.toLocaleString("en-GB") + " females from " + data.imported.filename + ".",
        "ok"
      );
    }
  } catch (err) {
    showMessage(err.message || "Failed to load females", "err");
    document.getElementById("meta-line").textContent = "";
  }
}

document.getElementById("females-head").addEventListener("click", (event) => {
  const th = event.target.closest("th[data-sort]");
  if (!th) return;
  const key = th.dataset.sort;
  if (sortState.key === key) sortState.dir *= -1;
  else {
    sortState.key = key;
    const column = activeColumns().find((col) => col.key === key);
    sortState.dir = column && column.type === "text" ? 1 : -1;
  }
  renderTable();
});
document.getElementById("search-input").addEventListener("input", () => scheduleRender(true));
document.getElementById("dob-from").addEventListener("change", () => scheduleRender(true));
document.getElementById("dob-to").addEventListener("change", () => scheduleRender(true));
document.getElementById("trait-filters").addEventListener("input", () => scheduleRender(true));
document.getElementById("clear-filters-btn").addEventListener("click", () => {
  document.querySelectorAll("#trait-filters input").forEach((input) => { input.value = ""; });
  document.getElementById("dob-from").value = "";
  document.getElementById("dob-to").value = "";
  document.getElementById("search-input").value = "";
  scheduleRender(true);
});
document.getElementById("page-prev").addEventListener("click", () => {
  if (currentPage > 1) {
    currentPage -= 1;
    renderTable();
  }
});
document.getElementById("page-next").addEventListener("click", () => {
  currentPage += 1;
  renderTable();
});
document.getElementById("columns-btn").addEventListener("click", openColumns);
document.getElementById("columns-close").addEventListener("click", closeColumns);
document.getElementById("columns-default").addEventListener("click", () => {
  draftKeys = [];
  selectedDisplayed = new Set();
  renderColumnLists();
});
document.getElementById("columns-update").addEventListener("click", () => {
  displayedKeys = draftKeys.slice();
  localStorage.setItem(PAGE.storageKey, JSON.stringify(displayedKeys));
  closeColumns();
  renderTraitFilters();
  renderTable();
});
document.getElementById("columns-add").addEventListener("click", () => {
  selectedAvailable.forEach((key) => {
    if (!draftKeys.includes(key)) draftKeys.push(key);
  });
  selectedAvailable = new Set();
  renderColumnLists();
});
document.getElementById("columns-remove").addEventListener("click", () => {
  draftKeys = draftKeys.filter((key) => !selectedDisplayed.has(key));
  selectedDisplayed = new Set();
  renderColumnLists();
});
document.getElementById("available-search").addEventListener("input", renderColumnLists);
document.getElementById("displayed-search").addEventListener("input", renderColumnLists);
document.getElementById("available-list").addEventListener("click", (event) => {
  const item = event.target.closest("li[data-key]");
  if (!item) return;
  if (selectedAvailable.has(item.dataset.key)) selectedAvailable.delete(item.dataset.key);
  else selectedAvailable.add(item.dataset.key);
  renderColumnLists();
});
document.getElementById("displayed-list").addEventListener("click", (event) => {
  const item = event.target.closest("li[data-key]");
  if (!item) return;
  if (selectedDisplayed.has(item.dataset.key)) selectedDisplayed.delete(item.dataset.key);
  else selectedDisplayed.add(item.dataset.key);
  renderColumnLists();
});
document.getElementById("displayed-list").addEventListener("dragstart", (event) => {
  const item = event.target.closest("li[data-key]");
  if (!item) return;
  dragKey = item.dataset.key;
});
document.getElementById("displayed-list").addEventListener("dragover", (event) => {
  if (event.target.closest("li[data-key]")) event.preventDefault();
});
document.getElementById("displayed-list").addEventListener("drop", (event) => {
  const item = event.target.closest("li[data-key]");
  if (!item || !dragKey || item.dataset.key === dragKey) return;
  event.preventDefault();
  const from = draftKeys.indexOf(dragKey);
  const to = draftKeys.indexOf(item.dataset.key);
  if (from < 0 || to < 0) return;
  draftKeys.splice(from, 1);
  draftKeys.splice(to, 0, dragKey);
  dragKey = "";
  renderColumnLists();
});

const uploadButton = document.getElementById("upload-btn");
if (uploadButton) {
  const uploadInput = document.getElementById("upload-input");
  uploadButton.addEventListener("click", () => uploadInput.click());
  uploadInput.addEventListener("change", async () => {
    const file = uploadInput.files && uploadInput.files[0];
    uploadInput.value = "";
    if (!file) return;
    showMessage("Uploading…", "");
    const body = new FormData();
    body.append("file", file);
    try {
      const res = await fetch(PAGE.uploadUrl, { method: "POST", body });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "Upload failed");
      applyPayload(data);
      showMessage(
        "Loaded " + data.imported.rows_imported.toLocaleString("en-GB") + " females from " + data.imported.filename + ".",
        "ok"
      );
    } catch (err) {
      showMessage(err.message || "Upload failed", "err");
    }
  });
}

loadRows();
