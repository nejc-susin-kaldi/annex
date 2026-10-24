// Annex: reports uncaught errors and unhandled promise rejections in this page to the app's own server
// (POST /api/_errors, behind the same login), where they are logged as errors and grouped like server errors.
// Add to a page:  <script src="/annex-errors.js"></script>   Keep this file as is.
(function () {
  var sent = 0;
  var MAX_PER_PAGE = 5;
  function report(msg, stack) {
    if (sent >= MAX_PER_PAGE) return;
    sent += 1;
    try {
      fetch("/api/_errors", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        keepalive: true,
        body: JSON.stringify({
          msg: String(msg || "unknown error").slice(0, 500),
          stack: String(stack || "").slice(0, 2000),
          url: location.pathname, // never the query string: it can hold what the user searched for
        }),
      }).catch(function () {});
    } catch (e) {}
  }
  window.addEventListener("error", function (e) {
    report(e.message, e.error && e.error.stack);
  });
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    report(r && r.message ? r.message : r, r && r.stack);
  });
})();
