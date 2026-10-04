/*
 * app/static/app.js
 * -----------------
 * The ONE JavaScript file for the whole app (no frameworks, no build step).
 * Pages never contain inline scripts (strict CSP). Instead, HTML elements
 * carry data-* attributes and this file wires up the behaviour:
 *
 *   data-action="toggle-theme"      light/dark switch
 *   data-action="toggle-sidebar"    staff menu on phones
 *   data-action="toggle-password"   show/hide password
 *   data-action="dismiss-toast"     close a toast
 *   data-open-sheet="id"            open a slide-up sheet (forms)
 *   data-action="close-sheet"       close the open sheet
 *   data-busy-form                  disable the submit button while sending
 *   data-confirm="message"          ask before submitting a form
 *   data-password-rules="listId"    live password rule ticks
 *   data-match="inputId"            "passwords must match" check
 *   data-count / data-ring          count-up numbers and progress rings
 *   data-colour="#hex"              sets --tint (e.g. subject colours)
 *   form[data-logout]               clears offline caches on logout
 *
 * Shared helpers are exposed as window.App (toast, fetchJSON, csrf).
 */
(function () {
  "use strict";

  var reduceMotion =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---------------------------------------------------------------------
  // Small helpers
  // ---------------------------------------------------------------------
  function $(selector, root) { return (root || document).querySelector(selector); }
  function $all(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }
  function csrfToken() { return document.body.getAttribute("data-csrf") || ""; }

  /** Indian number format: 1234567.5 -> "12,34,567.50" */
  function formatIndian(value, decimals) {
    return Number(value).toLocaleString("en-IN", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    });
  }

  function formatRupees(paise) {
    var rupees = paise / 100;
    return "\u20B9" + formatIndian(rupees, paise % 100 === 0 ? 0 : 2);
  }

  // ---------------------------------------------------------------------
  // Toasts
  // ---------------------------------------------------------------------
  var TOAST_MS = 4500;

  function dismissToast(toast) {
    if (!toast || toast.classList.contains("is-leaving")) return;
    toast.classList.add("is-leaving");
    setTimeout(function () { toast.remove(); }, reduceMotion ? 0 : 250);
  }

  function scheduleDismiss(toast) {
    // Errors stay until closed; other toasts disappear by themselves.
    if (!toast.classList.contains("toast-error")) {
      setTimeout(function () { dismissToast(toast); }, TOAST_MS);
    }
  }

  /** App.toast("Saved", "success" | "error" | "info") */
  function toast(text, kind) {
    kind = kind || "success";
    var stack = $("#toasts");
    if (!stack) return;
    var el = document.createElement("div");
    el.className = "toast toast-" + kind;
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    var span = document.createElement("span");
    span.textContent = text;                       // textContent = no HTML injection
    var close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("data-action", "dismiss-toast");
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "\u00D7";
    el.appendChild(span);
    el.appendChild(close);
    stack.appendChild(el);
    scheduleDismiss(el);
  }

  // ---------------------------------------------------------------------
  // JSON requests with the CSRF header (used by later phases, e.g. marks autosave)
  // ---------------------------------------------------------------------
  function fetchJSON(url, options) {
    options = options || {};
    var headers = options.headers || {};
    headers["Accept"] = "application/json";
    if (options.body && typeof options.body !== "string") {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }
    if ((options.method || "GET").toUpperCase() !== "GET") {
      headers["X-CSRF-Token"] = csrfToken();
    }
    options.headers = headers;
    options.credentials = "same-origin";
    return fetch(url, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (response.status === 401 && data.redirect) {
          window.location.href = data.redirect;       // session expired -> login
        }
        if (!response.ok) {
          var err = new Error(data.error || ("Request failed (" + response.status + ")"));
          err.status = response.status;
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  // ---------------------------------------------------------------------
  // Sidebar (staff, phones)
  // ---------------------------------------------------------------------
  function setSidebar(open) {
    var sidebar = $("#sidebar");
    var backdrop = $(".sidebar-backdrop");
    var button = $('[data-action="toggle-sidebar"]');
    if (!sidebar) return;
    sidebar.classList.toggle("is-open", open);
    if (backdrop) backdrop.hidden = !open;
    if (button) button.setAttribute("aria-expanded", open ? "true" : "false");
    document.body.classList.toggle("no-scroll", open);
  }

  // ---------------------------------------------------------------------
  // Slide-up sheets
  // ---------------------------------------------------------------------
  var openSheetEl = null;
  var lastFocus = null;

  function openSheet(id) {
    var sheet = document.getElementById(id);
    if (!sheet) return;
    closeSheet();
    lastFocus = document.activeElement;
    openSheetEl = sheet;
    sheet.hidden = false;
    $("#sheet-backdrop").hidden = false;
    document.body.classList.add("no-scroll");
    requestAnimationFrame(function () { sheet.classList.add("is-open"); });
    var first = sheet.querySelector("input, select, textarea, button");
    if (first) setTimeout(function () { first.focus(); }, reduceMotion ? 0 : 200);
  }

  function closeSheet() {
    if (!openSheetEl) return;
    var sheet = openSheetEl;
    openSheetEl = null;
    sheet.classList.remove("is-open");
    $("#sheet-backdrop").hidden = true;
    document.body.classList.remove("no-scroll");
    setTimeout(function () { sheet.hidden = true; }, reduceMotion ? 0 : 250);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  // ---------------------------------------------------------------------
  // One click handler for the whole page (event delegation)
  // ---------------------------------------------------------------------
  document.addEventListener("click", function (event) {
    var opener = event.target.closest("[data-open-sheet]");
    if (opener) {
      event.preventDefault();
      openSheet(opener.getAttribute("data-open-sheet"));
      return;
    }

    var el = event.target.closest("[data-action]");
    if (!el) return;
    var action = el.getAttribute("data-action");

    if (action === "toggle-theme" && window.TPATheme) {
      window.TPATheme.toggle();
    } else if (action === "toggle-sidebar") {
      setSidebar(!$("#sidebar").classList.contains("is-open"));
    } else if (action === "close-sidebar") {
      setSidebar(false);
    } else if (action === "close-sheet") {
      closeSheet();
    } else if (action === "dismiss-toast") {
      dismissToast(el.closest(".toast"));
    } else if (action === "toggle-password") {
      var input = el.parentElement.querySelector("input");
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      el.setAttribute("aria-pressed", show ? "true" : "false");
      el.setAttribute("aria-label", show ? "Hide password" : "Show password");
    } else if (action === "install-app") {
      installApp();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeSheet();
      setSidebar(false);
    }
  });

  // ---------------------------------------------------------------------
  // Forms: confirmation, busy state, logout cache clearing
  // ---------------------------------------------------------------------
  document.addEventListener("submit", function (event) {
    var form = event.target;

    var question = form.getAttribute("data-confirm");
    if (question && !window.confirm(question)) {
      event.preventDefault();
      return;
    }

    if (form.hasAttribute("data-logout") && window.caches) {
      // Remove saved pages/files so the next person on this phone can't see them.
      // (The form still submits normally; this runs in the background.)
      caches.keys().then(function (keys) {
        keys.forEach(function (key) { caches.delete(key); });
      }).catch(function () {});
    }

    if (form.hasAttribute("data-busy-form")) {
      if (form.classList.contains("is-busy")) {      // stop double submits
        event.preventDefault();
        return;
      }
      form.classList.add("is-busy");
      $all('button[type="submit"]', form).forEach(function (b) {
        b.setAttribute("aria-busy", "true");
      });
    }
  });

  // When the user comes "Back" to a page, undo the busy state.
  window.addEventListener("pageshow", function () {
    $all("form.is-busy").forEach(function (f) {
      f.classList.remove("is-busy");
      $all("[aria-busy]", f).forEach(function (b) { b.removeAttribute("aria-busy"); });
    });
  });

  // ---------------------------------------------------------------------
  // Password rules and "passwords match"
  // ---------------------------------------------------------------------
  function wirePasswordRules() {
    $all("[data-password-rules]").forEach(function (input) {
      var list = document.getElementById(input.getAttribute("data-password-rules"));
      if (!list) return;
      function check() {
        var v = input.value;
        var rules = {
          length: v.length >= 8,
          letter: /[A-Za-z\u00C0-\uFFFF]/.test(v),
          number: /[0-9]/.test(v)
        };
        Object.keys(rules).forEach(function (name) {
          var item = list.querySelector('[data-rule="' + name + '"]');
          if (item) item.classList.toggle("is-met", rules[name]);
        });
      }
      input.addEventListener("input", check);
      check();
    });

    $all("[data-match]").forEach(function (input) {
      var other = document.getElementById(input.getAttribute("data-match"));
      if (!other) return;
      function check() {
        var bad = input.value && input.value !== other.value;
        input.setCustomValidity(bad ? "The passwords do not match." : "");
        input.classList.toggle("is-invalid", !!bad);
      }
      input.addEventListener("input", check);
      other.addEventListener("input", check);
    });
  }

  // ---------------------------------------------------------------------
  // Count-up numbers and progress rings
  // ---------------------------------------------------------------------
  function animate(duration, step) {
    if (reduceMotion) { step(1); return; }
    var start = null;
    function frame(time) {
      if (start === null) start = time;
      var t = Math.min(1, (time - start) / duration);
      step(1 - Math.pow(1 - t, 3));          // ease-out
      if (t < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function wireCounters() {
    $all("[data-count]").forEach(function (el) {
      var target = parseFloat(el.getAttribute("data-count")) || 0;
      var format = el.getAttribute("data-format");
      animate(900, function (p) {
        var value = target * p;
        el.textContent = format === "inr"
          ? formatRupees(Math.round(value))
          : formatIndian(Math.round(value), 0);
      });
    });

    $all("[data-ring]").forEach(function (ring) {
      var target = parseFloat(ring.getAttribute("data-percent")) || 0;
      var circle = ring.querySelector(".ring-value");
      if (!circle) return;
      animate(1100, function (p) {
        circle.setAttribute("stroke-dasharray", (target * p).toFixed(2) + " 100");
      });
    });
  }

  // ---------------------------------------------------------------------
  // data-colour -> CSS variable (inline style attributes are blocked by CSP,
  // but setting styles from our own script is allowed)
  // ---------------------------------------------------------------------
  function wireColours() {
    $all("[data-colour]").forEach(function (el) {
      var colour = el.getAttribute("data-colour");
      if (/^#[0-9a-fA-F]{6}$/.test(colour)) el.style.setProperty("--tint", colour);
    });
  }

  // ---------------------------------------------------------------------
  // Online / offline banner
  // ---------------------------------------------------------------------
  function updateOnline() {
    var banner = $("#offline-banner");
    if (banner) banner.hidden = navigator.onLine;
  }
  window.addEventListener("online", updateOnline);
  window.addEventListener("offline", updateOnline);

  // ---------------------------------------------------------------------
  // "Install app" button (Android/desktop Chrome show the prompt event)
  // ---------------------------------------------------------------------
  var installEvent = null;
  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    installEvent = event;
    $all('[data-action="install-app"]').forEach(function (b) { b.hidden = false; });
  });

  function installApp() {
    if (!installEvent) return;
    installEvent.prompt();
    installEvent.userChoice.finally(function () {
      installEvent = null;
      $all('[data-action="install-app"]').forEach(function (b) { b.hidden = true; });
    });
  }

  // ---------------------------------------------------------------------
  // Start
  // ---------------------------------------------------------------------
  function init() {
    $all("#toasts .toast").forEach(scheduleDismiss);
    wirePasswordRules();
    wireCounters();
    wireColours();
    updateOnline();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Public helpers for later phases.
  window.App = {
    toast: toast,
    fetchJSON: fetchJSON,
    csrf: csrfToken,
    openSheet: openSheet,
    closeSheet: closeSheet,
    formatRupees: formatRupees
  };
})();

/*
 * ---------------------------------------------------------------------------
 * Part 2 - features from Phases 2-7
 *   select[data-autosubmit]        submit the filter form when changed
 *   [data-stream-for="classId"]    show the Stream field only for Class 11-12
 *   data-action="fill|copy|print"  icon picker, copy UPI ID, print receipt
 *   [data-method-select]           UPI payments require a reference number
 *   input[data-max-bytes]          warn before uploading a too-large file
 *   table[data-marks-grid]         marks grid: validation, live %, autosave
 *   data-action="save-offline"     keep a note file on the phone
 *   [data-saved-list]              list of files saved on this phone
 *   service worker registration    offline support (sw.js)
 * ---------------------------------------------------------------------------
 */
(function () {
  "use strict";

  var SAVED_KEY = "tpa-saved";        // localStorage: url -> {title, subject, size}
  var FILE_CACHE = "tpa-files";       // must match sw.js

  function $all(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }
  function toast(text, kind) { if (window.App) window.App.toast(text, kind); }
  function oneDecimal(n) { return String(Math.round(n * 10) / 10); }

  // ---------------------------------------------------------------------
  // Forms
  // ---------------------------------------------------------------------
  document.addEventListener("change", function (event) {
    var el = event.target;
    if (el.matches && el.matches("select[data-autosubmit]") && el.form) {
      el.form.submit();
    }
    if (el.matches && el.matches("input[type=file][data-max-bytes]") && el.files && el.files[0]) {
      var max = parseInt(el.getAttribute("data-max-bytes"), 10);
      if (el.files[0].size > max) {
        toast("That file is larger than " + Math.round(max / 1048576) + " MB.", "error");
        el.value = "";
      }
    }
    if (el.matches && el.matches("[data-method-select]")) updateReference(el);
  });

  function updateReference(select) {
    var form = select.closest("form");
    var input = form && form.querySelector("[data-reference-input]");
    if (!input) return;
    var upi = select.value === "UPI";
    input.required = upi;
    input.placeholder = upi ? "UPI reference / UTR (required)" : "Optional";
  }

  function wireStreamFields() {
    $all("[data-stream-for]").forEach(function (field) {
      var control = document.getElementById(field.getAttribute("data-stream-for"));
      if (!control) return;
      function update() {
        var show = control.value === "11" || control.value === "12";
        field.hidden = !show;
        if (!show) {
          var select = field.querySelector("select");
          if (select) select.value = "";
        }
      }
      control.addEventListener("change", update);
      update();
    });
  }

  document.addEventListener("click", function (event) {
    var el = event.target.closest("[data-action]");
    if (!el) return;
    var action = el.getAttribute("data-action");
    if (action === "fill") {
      var target = document.getElementById(el.getAttribute("data-target"));
      if (target) { target.value = el.getAttribute("data-value"); target.focus(); }
    } else if (action === "copy") {
      var value = el.getAttribute("data-value");
      if (navigator.clipboard) {
        navigator.clipboard.writeText(value).then(function () { toast("Copied " + value, "info"); });
      }
    } else if (action === "print") {
      window.print();
    } else if (action === "save-offline") {
      toggleSaved(el);
    } else if (action === "remove-offline") {
      removeSaved(el.getAttribute("data-url")).then(renderSavedList);
    }
  });

  // ---------------------------------------------------------------------
  // Marks grid
  // ---------------------------------------------------------------------
  function wireMarksGrid(table) {
    var url = table.getAttribute("data-save-url");
    var status = document.querySelector("[data-save-status]");
    var pending = {};
    var pendingRemarks = {};
    var timer = null;
    var saving = false;

    function setStatus(text, kind) {
      if (!status) return;
      status.textContent = text;
      status.className = "save-status" + (kind ? " is-" + kind : "");
    }

    function parse(input) {
      var v = input.value.trim().toUpperCase();
      var max = parseFloat(input.getAttribute("data-max"));
      if (v === "") return { ok: true, value: null, absent: false };
      if (v === "AB") return { ok: true, value: null, absent: true };
      if (!/^\d+(\.\d)?$/.test(v)) return { ok: false, message: "Use a number (1 decimal at most) or AB" };
      var n = parseFloat(v);
      if (n > max) return { ok: false, message: "Maximum is " + max };
      return { ok: true, value: n, absent: false };
    }

    function validate(input) {
      var result = parse(input);
      input.classList.toggle("is-invalid", !result.ok);
      input.setAttribute("aria-invalid", result.ok ? "false" : "true");
      input.title = result.ok ? "" : result.message;
      return result;
    }

    function rowPercent(tr) {
      var obtained = 0, max = 0, entered = false;
      $all(".mark-input", tr).forEach(function (input) {
        max += parseFloat(input.getAttribute("data-max"));
        var r = parse(input);
        if (r.ok && (r.value !== null || r.absent)) {
          entered = true;
          if (r.value !== null) obtained += r.value;
        }
      });
      var pct = entered && max > 0 ? (obtained / max) * 100 : null;
      var cell = tr.querySelector("[data-pct]");
      if (cell) cell.textContent = pct === null ? "-" : oneDecimal(pct);
      return pct;
    }

    function refreshTotals() {
      var values = [];
      $all("tbody tr", table).forEach(function (tr) {
        var p = rowPercent(tr);
        if (p !== null) values.push(p);
      });
      var avg = table.querySelector("[data-class-avg]");
      if (avg) {
        avg.textContent = values.length
          ? oneDecimal(values.reduce(function (a, b) { return a + b; }, 0) / values.length) : "-";
      }
    }

    function schedule() {
      setStatus("Saving soon\u2026");
      clearTimeout(timer);
      timer = setTimeout(flush, 700);
    }

    function flush() {
      if (saving) { schedule(); return; }
      var cells = Object.keys(pending).map(function (k) { return pending[k]; });
      var remarks = Object.keys(pendingRemarks).map(function (k) { return pendingRemarks[k]; });
      if (!cells.length && !remarks.length) return;
      pending = {};
      pendingRemarks = {};
      saving = true;
      setStatus("Saving\u2026");
      window.App.fetchJSON(url, { method: "POST", body: { cells: cells, remarks: remarks } })
        .then(function (res) {
          saving = false;
          if (res.errors && res.errors.length) {
            setStatus(res.errors.length + " cell(s) not saved - check the red cells", "bad");
            res.errors.forEach(function (e) {
              var input = table.querySelector('tr[data-student="' + e.student_id + '"] .mark-input[data-subject="' + e.subject_id + '"]');
              if (input) { input.classList.add("is-invalid"); input.title = e.error; }
            });
          } else {
            setStatus("All changes saved", "ok");
          }
        })
        .catch(function (err) {
          saving = false;
          // Put the unsaved changes back so the next edit retries them.
          cells.forEach(function (c) {
            var key = c.student_id + ":" + c.subject_id;
            if (!(key in pending)) pending[key] = c;
          });
          remarks.forEach(function (r) {
            if (!(r.student_id in pendingRemarks)) pendingRemarks[r.student_id] = r;
          });
          setStatus("Not saved: " + err.message, "bad");
          toast("Marks not saved: " + err.message, "error");
        });
    }

    table.addEventListener("input", function (event) {
      var input = event.target;
      var tr = input.closest("tr[data-student]");
      if (!tr) return;
      var studentId = parseInt(tr.getAttribute("data-student"), 10);
      if (input.classList.contains("mark-input")) {
        if (input.value.trim().toLowerCase() === "ab") input.value = "AB";
        var result = validate(input);
        refreshTotals();
        if (result.ok) {
          var subjectId = parseInt(input.getAttribute("data-subject"), 10);
          pending[studentId + ":" + subjectId] = { student_id: studentId, subject_id: subjectId, value: input.value.trim() };
          schedule();
        } else {
          setStatus("Fix the red cell to save it", "bad");
        }
      } else if (input.classList.contains("remark-input")) {
        pendingRemarks[studentId] = { student_id: studentId, remark: input.value };
        schedule();
      }
    });

    // Enter = next row (same subject), Shift+Enter = previous row.
    table.addEventListener("keydown", function (event) {
      var input = event.target;
      if (event.key !== "Enter" || !input.classList.contains("mark-input")) return;
      event.preventDefault();
      var tr = input.closest("tr");
      var next = event.shiftKey ? tr.previousElementSibling : tr.nextElementSibling;
      var subject = input.getAttribute("data-subject");
      while (next) {
        var target = next.querySelector('.mark-input[data-subject="' + subject + '"]');
        if (target) { target.focus(); target.select(); return; }
        next = event.shiftKey ? next.previousElementSibling : next.nextElementSibling;
      }
      flush();
    });

    table.addEventListener("focusin", function (event) {
      if (event.target.classList.contains("mark-input")) event.target.select();
    });

    window.addEventListener("beforeunload", function (event) {
      if (Object.keys(pending).length || Object.keys(pendingRemarks).length || saving) {
        flush();
        event.preventDefault();
        event.returnValue = "";
      }
    });

    $all(".mark-input", table).forEach(validate);
  }

  // ---------------------------------------------------------------------
  // Save files for offline use (students)
  // ---------------------------------------------------------------------
  function readSaved() {
    try { return JSON.parse(localStorage.getItem(SAVED_KEY) || "{}"); } catch (e) { return {}; }
  }
  function writeSaved(map) {
    try { localStorage.setItem(SAVED_KEY, JSON.stringify(map)); } catch (e) { /* ignore */ }
  }

  function markButton(button, saved) {
    button.classList.toggle("is-saved", saved);
    button.setAttribute("aria-pressed", saved ? "true" : "false");
    var label = button.querySelector("span");
    if (label) label.textContent = saved ? "Saved" : "Save offline";
  }

  function toggleSaved(button) {
    var url = button.getAttribute("data-url");
    var map = readSaved();
    if (map[url]) {
      removeSaved(url).then(function () { markButton(button, false); toast("Removed from this phone", "info"); });
      return;
    }
    button.setAttribute("aria-busy", "true");
    caches.open(FILE_CACHE)
      .then(function (cache) { return cache.add(url); })
      .then(function () {
        map = readSaved();
        map[url] = {
          title: button.getAttribute("data-title"),
          subject: button.getAttribute("data-subject"),
          size: parseInt(button.getAttribute("data-size"), 10) || 0
        };
        writeSaved(map);
        markButton(button, true);
        toast("Saved - it will open without internet", "success");
      })
      .catch(function () { toast("Could not save the file. Check your connection and phone storage.", "error"); })
      .finally(function () { button.removeAttribute("aria-busy"); });
  }

  function removeSaved(url) {
    var map = readSaved();
    delete map[url];
    writeSaved(map);
    return caches.open(FILE_CACHE).then(function (cache) { return cache.delete(url); });
  }

  function wireSaveButtons() {
    var buttons = $all('[data-action="save-offline"]');
    if (!buttons.length || !window.caches) return;
    caches.open(FILE_CACHE).then(function (cache) {
      buttons.forEach(function (button) {
        button.hidden = false;
        cache.match(button.getAttribute("data-url")).then(function (hit) { markButton(button, !!hit); });
      });
    });
  }

  function renderSavedList() {
    var list = document.querySelector("[data-saved-list]");
    if (!list) return;
    var empty = document.getElementById("saved-empty");
    var map = readSaved();
    var urls = Object.keys(map);
    list.textContent = "";
    if (!window.caches || !urls.length) {
      list.hidden = true;
      if (empty) empty.hidden = false;
      return;
    }
    caches.open(FILE_CACHE).then(function (cache) {
      return Promise.all(urls.map(function (url) {
        return cache.match(url).then(function (hit) { return hit ? url : null; });
      }));
    }).then(function (present) {
      var shown = 0;
      present.forEach(function (url) {
        if (!url) return;
        shown += 1;
        var item = map[url];
        var li = document.createElement("li");
        var meta = document.createElement("span");
        meta.className = "file-meta";
        var strong = document.createElement("strong");
        strong.textContent = item.title;
        var sub = document.createElement("span");
        sub.className = "muted";
        sub.textContent = item.subject + (item.size ? ", " + (item.size / 1048576).toFixed(1) + " MB" : "");
        meta.appendChild(strong);
        meta.appendChild(sub);
        var open = document.createElement("a");
        open.className = "btn btn-ghost btn-small";
        open.href = url;
        open.target = "_blank";
        open.rel = "noopener";
        open.textContent = "Open";
        var remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn btn-link";
        remove.setAttribute("data-action", "remove-offline");
        remove.setAttribute("data-url", url);
        remove.textContent = "Remove";
        li.appendChild(meta);
        li.appendChild(open);
        li.appendChild(remove);
        list.appendChild(li);
      });
      list.hidden = shown === 0;
      if (empty) empty.hidden = shown !== 0;
    });
  }

  // On logout: forget the saved-file list and tell the service worker to clear.
  document.addEventListener("submit", function (event) {
    if (!event.target.hasAttribute("data-logout")) return;
    try { localStorage.removeItem(SAVED_KEY); } catch (e) { /* ignore */ }
    if (navigator.serviceWorker && navigator.serviceWorker.controller) {
      navigator.serviceWorker.controller.postMessage({ type: "clear" });
    }
  });

  // ---------------------------------------------------------------------
  // Start
  // ---------------------------------------------------------------------
  function init() {
    wireStreamFields();
    $all("[data-method-select]").forEach(updateReference);
    $all("table[data-marks-grid]").forEach(function (t) {
      if (!t.hasAttribute("data-locked")) wireMarksGrid(t);
    });
    wireSaveButtons();
    renderSavedList();
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").catch(function () { /* offline support unavailable */ });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
