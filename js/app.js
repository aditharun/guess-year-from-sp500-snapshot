(function () {
  "use strict";

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var MONTHS_FULL = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  var WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

  var lede = document.getElementById("lede");
  var statusEl = document.getElementById("status");
  var board = document.getElementById("board");
  var tape = document.getElementById("tape");
  var form = document.getElementById("guess");
  var yearsEl = document.getElementById("years");
  var monthsEl = document.getElementById("months");
  var lockBtn = document.getElementById("lock");
  var pickers = document.getElementById("pickers");
  var result = document.getElementById("result");
  var resultWhen = document.getElementById("result-when");
  var resultDay = document.getElementById("result-day");
  var resultDistance = document.getElementById("result-distance");
  var resultAside = document.getElementById("result-aside");
  var resultYours = document.getElementById("result-yours");
  var againBtn = document.getElementById("again");
  var sortButtons = Array.prototype.slice.call(document.querySelectorAll(".sort button"));

  var state = {
    ids: [],
    deck: [],
    rows: [],
    iso: "",
    sort: "cap",
    year: null,
    month: null,
    locked: false,
    busy: false,
    yearMin: 2011,
    yearMax: 2026
  };

  function asset(path) {
    var url = new URL(window.location.href);
    var dir = url.pathname;
    if (dir.endsWith("/")) {
      /* directory URL */
    } else if (/\.html?$/i.test(dir)) {
      dir = dir.slice(0, dir.lastIndexOf("/") + 1);
    } else {
      dir += "/";
    }
    return dir + path.replace(/^\//, "");
  }

  function shuffle(list) {
    var copy = list.slice();
    for (var i = copy.length - 1; i > 0; i -= 1) {
      var j = Math.floor(Math.random() * (i + 1));
      var tmp = copy[i];
      copy[i] = copy[j];
      copy[j] = tmp;
    }
    return copy;
  }

  function parseISODate(iso) {
    var parts = iso.split("-");
    if (parts.length !== 3) return null;
    var y = Number(parts[0]);
    var m = Number(parts[1]);
    var d = Number(parts[2]);
    if (!y || !m || !d) return null;
    return new Date(y, m - 1, d);
  }

  function formatPrice(n) {
    return n.toLocaleString("en-US", {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  }

  function sortPhrase() {
    if (state.sort === "name") return "Alphabetical.";
    if (state.sort === "price") return "Highest share price first.";
    return "Largest companies first.";
  }

  function setLede() {
    var count = state.rows.length ? state.rows.length + " names. " : "";
    lede.textContent = count + sortPhrase();
  }

  function sortedRows() {
    var rows = state.rows.slice();
    if (state.sort === "name") {
      rows.sort(function (a, b) {
        return a.name.localeCompare(b.name) || b.cap - a.cap;
      });
    } else if (state.sort === "price") {
      rows.sort(function (a, b) {
        return b.price - a.price || a.name.localeCompare(b.name);
      });
    } else {
      rows.sort(function (a, b) {
        return b.cap - a.cap || a.name.localeCompare(b.name);
      });
    }
    return rows;
  }

  function renderTape() {
    var rows = sortedRows();
    var fragment = document.createDocumentFragment();
    rows.forEach(function (row, index) {
      var li = document.createElement("li");
      var rank = document.createElement("span");
      var name = document.createElement("span");
      var price = document.createElement("span");
      rank.className = "rank";
      name.className = "name";
      price.className = "price";
      rank.textContent = String(index + 1);
      name.textContent = row.name;
      price.textContent = formatPrice(row.price);
      li.appendChild(rank);
      li.appendChild(name);
      li.appendChild(price);
      fragment.appendChild(li);
    });
    tape.replaceChildren(fragment);
    setLede();
  }

  function asRow(raw) {
    if (!Array.isArray(raw) || raw.length < 3) return null;
    var name = typeof raw[0] === "string" ? raw[0] : "";
    var price = Number(raw[1]);
    var cap = Number(raw[2]);
    if (!name || !Number.isFinite(price) || !Number.isFinite(cap)) return null;
    return { name: name, price: price, cap: cap };
  }

  function syncLock() {
    lockBtn.disabled = state.locked || !state.year || !state.month;
  }

  function pressOnly(container, value) {
    Array.prototype.forEach.call(container.querySelectorAll("button"), function (button) {
      button.setAttribute("aria-pressed", String(button.dataset.value === String(value)));
    });
  }

  function buildPickers() {
    yearsEl.replaceChildren();
    monthsEl.replaceChildren();
    var y;
    for (y = state.yearMin; y <= state.yearMax; y += 1) {
      var yearBtn = document.createElement("button");
      yearBtn.type = "button";
      yearBtn.dataset.value = String(y);
      yearBtn.textContent = String(y);
      yearBtn.setAttribute("aria-pressed", "false");
      yearsEl.appendChild(yearBtn);
    }
    var midpoint = yearsEl.children[Math.floor(yearsEl.children.length / 2)];
    if (midpoint) {
      yearsEl.scrollLeft = midpoint.offsetLeft - (yearsEl.clientWidth - midpoint.offsetWidth) / 2;
    }
    MONTHS.forEach(function (label, index) {
      var monthBtn = document.createElement("button");
      monthBtn.type = "button";
      monthBtn.dataset.value = String(index + 1);
      monthBtn.textContent = label;
      monthBtn.setAttribute("aria-pressed", "false");
      monthsEl.appendChild(monthBtn);
    });
  }

  function spanWords(abs) {
    var years = Math.floor(abs / 12);
    var months = abs % 12;
    var yearText = years === 1 ? "1 year" : years + " years";
    var monthText = months === 1 ? "1 month" : months + " months";
    if (years && months) return yearText + " and " + monthText;
    if (years) return yearText;
    return monthText;
  }

  function distanceLine(delta) {
    if (delta === 0) return "Exact month.";
    return spanWords(Math.abs(delta)) + (delta > 0 ? " late." : " early.");
  }

  function asideLine(abs) {
    if (abs === 0) return "You were on the floor.";
    if (abs === 1) return "Close enough to smell the ink.";
    if (abs <= 3) return "Same season of the market.";
    if (abs <= 6) return "Same chapter, different page.";
    if (abs <= 12) return "Within a year. The tape still rhymes.";
    if (abs <= 24) return "A different year, a familiar market.";
    if (abs <= 48) return "Leadership had time to change.";
    return "That was a different era.";
  }

  function reveal() {
    var actual = parseISODate(state.iso);
    if (!actual) return;
    var actualYear = actual.getFullYear();
    var actualMonth = actual.getMonth() + 1;
    var delta = (state.year * 12 + (state.month - 1)) - (actualYear * 12 + (actualMonth - 1));
    var abs = Math.abs(delta);
    state.locked = true;
    resultWhen.textContent = MONTHS_FULL[actualMonth - 1] + " " + actualYear;
    resultDay.textContent = WEEKDAYS[actual.getDay()] + ", " + MONTHS[actualMonth - 1] + " " + actual.getDate();
    resultDistance.textContent = distanceLine(delta);
    resultAside.textContent = asideLine(abs);
    resultYours.textContent = "You said " + MONTHS_FULL[state.month - 1] + " " + state.year + ".";
    pickers.hidden = true;
    result.hidden = false;
    syncLock();
  }

  function resetGuess() {
    state.year = null;
    state.month = null;
    state.locked = false;
    pressOnly(yearsEl, "");
    pressOnly(monthsEl, "");
    pickers.hidden = false;
    result.hidden = true;
    resultWhen.textContent = "";
    resultDay.textContent = "";
    resultDistance.textContent = "";
    resultAside.textContent = "";
    resultYours.textContent = "";
    syncLock();
  }

  function showStatus(message) {
    statusEl.hidden = !message;
    statusEl.textContent = message || "";
  }

  async function loadSnapshot(id) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(id)) throw new Error("Bad session id");
    var response = await fetch(asset("data/snapshots/" + id + ".json"));
    if (!response.ok) throw new Error("Session " + response.status);
    var payload = await response.json();
    if (!payload || typeof payload.d !== "string" || !Array.isArray(payload.rows)) {
      throw new Error("Session was unreadable");
    }
    var rows = [];
    payload.rows.forEach(function (raw) {
      var row = asRow(raw);
      if (row) rows.push(row);
    });
    if (!rows.length) throw new Error("Session was empty");
    return { iso: payload.d, rows: rows };
  }

  async function deal() {
    if (state.busy) return;
    state.busy = true;
    board.setAttribute("aria-busy", "true");
    showStatus("Pulling a session…");
    tape.replaceChildren();
    resetGuess();
    state.sort = "cap";
    sortButtons.forEach(function (button) {
      button.setAttribute("aria-pressed", String(button.dataset.sort === "cap"));
    });
    var loaded = null;
    var attempts = 0;
    while (!loaded && attempts < 4) {
      attempts += 1;
      if (!state.deck.length) state.deck = shuffle(state.ids);
      var id = state.deck.pop();
      try {
        loaded = await loadSnapshot(id);
      } catch (err) {
        loaded = null;
      }
    }
    state.busy = false;
    board.removeAttribute("aria-busy");
    if (!loaded) {
      showStatus("Couldn't load a session. Serve this folder over HTTP and refresh. Opening the file directly blocks the data.");
      return;
    }
    state.iso = loaded.iso;
    state.rows = loaded.rows;
    showStatus("");
    renderTape();
    board.scrollTop = 0;
  }

  yearsEl.addEventListener("click", function (event) {
    var button = event.target.closest("button");
    if (!button || state.locked) return;
    state.year = Number(button.dataset.value);
    pressOnly(yearsEl, state.year);
    syncLock();
  });

  monthsEl.addEventListener("click", function (event) {
    var button = event.target.closest("button");
    if (!button || state.locked) return;
    state.month = Number(button.dataset.value);
    pressOnly(monthsEl, state.month);
    syncLock();
  });

  sortButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      state.sort = button.dataset.sort;
      sortButtons.forEach(function (other) {
        other.setAttribute("aria-pressed", String(other === button));
      });
      if (state.rows.length) renderTape();
    });
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (state.locked || !state.year || !state.month || !state.iso) return;
    reveal();
  });

  againBtn.addEventListener("click", function () {
    deal();
  });

  async function init() {
    var response = await fetch(asset("data/manifest.json"));
    if (!response.ok) throw new Error("Manifest " + response.status);
    var manifest = await response.json();
    var ids = Array.isArray(manifest.snapshots) ? manifest.snapshots : [];
    state.ids = ids.filter(function (id) { return /^\d{4}-\d{2}-\d{2}$/.test(id); });
    if (!state.ids.length) throw new Error("No sessions");
    if (/^\d{4}/.test(manifest.from || "")) state.yearMin = Number(String(manifest.from).slice(0, 4));
    if (/^\d{4}/.test(manifest.to || "")) state.yearMax = Number(String(manifest.to).slice(0, 4));
    buildPickers();
    await deal();
  }

  init().catch(function () {
    showStatus("Couldn't load a session. From this folder run python3 -m http.server 8080, then open the local address. Opening the file directly won't load the data.");
  });
})();
