(function () {
  const script = document.currentScript;
  const url = script && script.dataset.bullsUrl;
  const inputs = [...document.querySelectorAll("[data-sire-input]")];
  if (!inputs.length || !url) return;

  let bulls = [];

  function hiddenFor(input) {
    return input.parentElement.querySelector('input[type="hidden"]');
  }

  function listFor(input) {
    return input.parentElement.querySelector(".sire-matches");
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function render(input) {
    const list = listFor(input);
    if (!list) return;
    const query = input.value.trim().toLowerCase();
    if (query.length < 2 || !bulls.length) {
      list.hidden = true;
      list.innerHTML = "";
      return;
    }
    const matches = bulls.filter((bull) => {
      const name = String(bull.bull_name || "").toLowerCase();
      const full = String(bull.bull_name_full || "").toLowerCase();
      const hbn = String(bull.hbn || "").toLowerCase();
      return name.includes(query) || full.includes(query) || hbn.includes(query);
    }).slice(0, 8);
    if (!matches.length) {
      list.hidden = true;
      list.innerHTML = "";
      return;
    }
    list.innerHTML = matches.map((bull) => {
      const name = bull.bull_name || bull.hbn || "";
      return '<button type="button" data-hbn="' + escapeHtml(bull.hbn) + '">' + escapeHtml(name) + "</button>";
    }).join("");
    list.hidden = false;
  }

  inputs.forEach((input) => {
    input.setAttribute("autocomplete", "off");
    input.addEventListener("input", () => {
      const hidden = hiddenFor(input);
      if (hidden) hidden.value = "";
      render(input);
    });
    input.addEventListener("focus", () => render(input));
    input.addEventListener("blur", () => {
      const list = listFor(input);
      if (list) list.hidden = true;
    });
    const list = listFor(input);
    if (!list) return;
    list.addEventListener("mousedown", (event) => {
      const button = event.target.closest("button[data-hbn]");
      if (!button) return;
      event.preventDefault();
      input.value = button.textContent || "";
      const hidden = hiddenFor(input);
      if (hidden) hidden.value = button.dataset.hbn || "";
      list.hidden = true;
      list.innerHTML = "";
    });
  });

  fetch(url)
    .then((response) => response.json())
    .then((data) => {
      bulls = Array.isArray(data.rows) ? data.rows : [];
    })
    .catch(() => {
      bulls = [];
    });
})();
