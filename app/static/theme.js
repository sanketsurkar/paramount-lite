/*
 * app/static/theme.js
 * -------------------
 * Loaded in <head> WITHOUT "defer", so it runs before the page is drawn.
 * It sets <html data-theme="light|dark"> from the saved choice or, if none,
 * from the phone/PC setting (prefers-color-scheme). This avoids a white
 * flash when dark mode is on. app.js calls TPATheme.toggle() for the button.
 */
(function () {
  "use strict";

  var KEY = "tpa-theme";
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function saved() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }  // private mode etc.
  }

  function systemTheme() {
    return media && media.matches ? "dark" : "light";
  }

  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    // Colour of the phone's status bar / browser toolbar.
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? "#0f1729" : "#1e3a8a");
  }

  apply(saved() || systemTheme());

  // If the user never chose, follow the system when it changes (e.g. night mode).
  if (media && media.addEventListener) {
    media.addEventListener("change", function () {
      if (!saved()) apply(systemTheme());
    });
  }

  // Small public API used by app.js.
  window.TPATheme = {
    current: function () {
      return document.documentElement.getAttribute("data-theme") || "light";
    },
    toggle: function () {
      var next = this.current() === "dark" ? "light" : "dark";
      try { localStorage.setItem(KEY, next); } catch (e) { /* ignore */ }
      apply(next);
      return next;
    }
  };
})();
