/* The hosted reviewer site's only script. It holds no rule and no data: when an answer changes it
   sends the form as it stands to the server, puts the form the server returns in its place, and
   keeps the focus where it was. Without it the "Refresh the form" button does the same. */
(function () {
  "use strict";
  document.documentElement.classList.add("js");
  var form = document.getElementById("reading-form");
  var body = document.getElementById("form-body");
  if (!form || !body || !window.fetch || !window.URLSearchParams) { return; }
  var url = form.getAttribute("data-state");
  var changes = 0;   /* counts every change: a reply for an older state is dropped */
  var timer = null;

  function busy(text) {
    var el = document.getElementById("busy");
    if (el) { el.textContent = text; }
  }

  function typed(el) {
    return el && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && el.type === "text"));
  }

  function refresh() {
    var mine = changes;
    window.clearTimeout(timer);
    busy("Updating the form…");
    window.fetch(url, {
      method: "POST",
      body: new URLSearchParams(new FormData(form)),
      credentials: "same-origin",
      headers: { "X-Fragment": "form" }
    }).then(function (reply) {
      if (reply.status === 409 || (reply.ok && reply.headers.get("X-Fragment") !== "form")) {
        window.location.reload();   /* the form moved on, or the session ended */
        return null;
      }
      return reply.ok ? reply.text() : Promise.reject(new Error("refused"));
    }).then(function (html) {
      if (html === null || mine !== changes) { return; }
      var active = document.activeElement;
      var id = active && body.contains(active) ? active.id : "";
      var top = id ? active.getBoundingClientRect().top : 0;
      var from = typed(active) ? active.selectionStart : null;
      var to = typed(active) ? active.selectionEnd : null;
      body.innerHTML = html;
      var again = id ? document.getElementById(id) : null;
      if (again) {
        again.focus({ preventScroll: true });
        if (typed(again) && from !== null) { again.setSelectionRange(from, to); }
        window.scrollBy(0, again.getBoundingClientRect().top - top);
      }
      busy("");
    }).catch(function () {
      if (mine === changes) { busy("The form could not be updated. Check the connection and change an answer again."); }
    });
  }

  form.addEventListener("change", function () {
    changes += 1;
    refresh();
  });
  form.addEventListener("input", function (event) {
    if (!typed(event.target)) { return; }
    changes += 1;
    window.clearTimeout(timer);
    timer = window.setTimeout(refresh, 700);
  });
  form.addEventListener("keydown", function (event) {
    /* Enter in a one-line box applies the value; it never saves. */
    if (event.key === "Enter" && event.target.tagName === "INPUT" && event.target.type === "text") {
      event.preventDefault();
      changes += 1;
      refresh();
    }
  });
}());
