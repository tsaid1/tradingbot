/* The hosted reviewer site's only script. It holds no rule and no data. It adds comfort only:
   the page works the same without it.
   - When an answer changes it sends the form as it stands to the server, puts the form the
     server returns in its place, and keeps the focus where it was (without a script the
     "Refresh the form" button does the same).
   - "View post" moves to the post under review and becomes "Back to my question", which
     returns to the exact control, focused, with everything typed still there.
   - "Go to question" (and every jump of the form) moves to the question in place, opens what
     holds it, and focuses its first control. Without a script these buttons post the form to
     the server with the place in the address, and the page opens there.
   - The last question the reviewer was at travels with the form (a field named ui.last):
     interface state only, never stored, never checked. */
(function () {
  "use strict";
  document.documentElement.classList.add("js");
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var form = document.getElementById("reading-form");
  var body = document.getElementById("form-body");
  var dock = document.getElementById("view-post");
  var post = document.getElementById("post-under-review");
  var remembered = "";   /* the id of the control the reviewer was at when they left for the post */
  var wide = window.matchMedia ? window.matchMedia("(min-width: 80rem)") : null;
  var panel = document.getElementById("nav-panel");

  /* The panel of sequences: open next to the page on a wide screen, one line above it on a narrow one. */
  function fitPanel() {
    if (panel && wide) { panel.open = wide.matches; }
  }
  fitPanel();
  if (wide && wide.addEventListener) { wide.addEventListener("change", fitPanel); }

  function moveTo(el, where) {
    if (!el) { return; }
    el.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: where || "center" });
  }

  function questionOf(el) {
    while (el && el !== document.body) {
      if (el.hasAttribute && el.hasAttribute("data-q")) { return el; }
      el = el.parentNode;
    }
    return null;
  }

  function firstControl(box) {
    if (!box) { return null; }
    var all = box.querySelectorAll("input, select, textarea, button");
    for (var i = 0; i < all.length; i += 1) {
      var c = all[i];
      if (c.type === "hidden" || c.disabled || c.name === "ui.later") { continue; }
      if (c.type === "radio" && !c.checked) {
        var chosen = box.querySelector('input[type="radio"][name="' + c.name + '"]:checked');
        if (chosen) { return chosen; }
      }
      return c;
    }
    return null;
  }

  function openAround(el) {
    var up = el;
    while (up && up !== document.body) {
      if (up.tagName === "DETAILS" && !up.open) { up.open = true; }
      up = up.parentNode;
    }
  }

  /* Go to an element of the form by id: open what holds it, show it, focus its control. */
  function goTo(id) {
    var box = document.getElementById(id);
    if (!box) { return false; }
    openAround(box);
    var control = box.matches("input, select, textarea, button") ? box : firstControl(box);
    moveTo(box);
    if (control) {
      control.focus({ preventScroll: true });
    } else {
      if (!box.hasAttribute("tabindex")) { box.setAttribute("tabindex", "-1"); }
      box.focus({ preventScroll: true });
    }
    return true;
  }

  function markCurrent(q) {
    var old = document.querySelectorAll(".question.is-current");
    for (var i = 0; i < old.length; i += 1) { if (old[i] !== q) { old[i].classList.remove("is-current"); } }
    if (q) { q.classList.add("is-current"); }
  }

  function lastField() { return document.getElementById("ui-last"); }

  function setDock(back) {
    if (!dock) { return; }
    var label = dock.querySelector("span");
    if (label) { label.textContent = back ? dock.getAttribute("data-back-text") : dock.getAttribute("data-view"); }
    dock.classList.toggle("is-back", back);
    dock.setAttribute("data-mode", back ? "back" : "view");
    var inPost = post ? post.querySelector(".post-back") : null;
    if (inPost) { inPost.classList.toggle("is-shown", back); }
  }

  /* Where "Back to my question" goes: the remembered control, else the question last
     visited, else the next unanswered question after it (the server names it). */
  function backTarget() {
    if (remembered && document.getElementById(remembered)) { return remembered; }
    var last = lastField();
    if (last && last.value && document.getElementById(last.value)) { return last.value; }
    var state = document.getElementById("form-state");
    var next = state ? state.getAttribute("data-next") : "";
    return next && document.getElementById(next) ? next : "";
  }

  function back() {
    var id = backTarget();
    setDock(false);
    if (!id) { moveTo(form, "start"); return; }
    var el = document.getElementById(id);
    if (el && el.matches("input, select, textarea")) {
      openAround(el);
      moveTo(el);
      el.focus({ preventScroll: true });
      markCurrent(questionOf(el));
    } else {
      goTo(id);
    }
  }

  function viewPost() {
    var active = document.activeElement;
    if (body && active && body.contains(active) && active.id) { remembered = active.id; }
    setDock(true);
    moveTo(post, "start");
    /* The focus waits in the post's own "Back to my question": one key press returns. */
    var stay = post ? post.querySelector("[data-back]") : null;
    if (stay) { stay.focus({ preventScroll: true }); } else if (post) { post.focus({ preventScroll: true }); }
  }

  document.addEventListener("click", function (event) {
    var el = event.target.closest ? event.target.closest("[data-go], [data-back], #view-post") : null;
    if (!el) { return; }
    if (el.id === "view-post") {
      if (!post) { return; }
      event.preventDefault();
      if (el.getAttribute("data-mode") === "back") { back(); } else { viewPost(); }
      return;
    }
    if (el.hasAttribute("data-back")) {
      event.preventDefault();
      back();
      return;
    }
    if (goTo(el.getAttribute("data-go"))) { event.preventDefault(); }
  });

  /* The question the reviewer is at: marked, and carried with the form. */
  document.addEventListener("focusin", function (event) {
    if (!body || !body.contains(event.target)) { return; }
    var q = questionOf(event.target);
    if (!q) { return; }
    markCurrent(q);
    var last = lastField();
    if (last) { last.value = q.id; }
    if (event.target.id) { remembered = event.target.id; }
  });

  /* After a refused submission: the summary takes the focus (the page has already moved to it). */
  var problems = document.getElementById("problems");
  if (problems) {
    problems.focus({ preventScroll: true });
  } else if (document.getElementById("saved")) {
    document.getElementById("saved").focus({ preventScroll: true });
  }
  /* A page opened at a place (a jump made without a script, then this script loaded): focus it. */
  if (window.location.hash.length > 1 && !problems) {
    var target = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
    if (target && target !== post && body && body.contains(target)) {
      var c = firstControl(target);
      if (c) { c.focus({ preventScroll: true }); }
    }
  }

  if (!form || !body || !window.fetch || !window.URLSearchParams) { return; }
  var url = form.getAttribute("data-state");
  var changes = 0;   /* counts every change: a reply for an older state is dropped */
  var timer = null;
  var saveState = document.getElementById("save-state");

  function busy(text) {
    var el = document.getElementById("busy");
    if (el) { el.textContent = text; }
  }

  function typed(el) {
    return el && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && el.type === "text"));
  }

  /* A draft the server kept (it says when, in a header): "Draft kept at HH:MM, not submitted". */
  function kept(at) {
    if (!saveState || !at) { return; }
    var time = at.slice(11, 16);
    var words = saveState.querySelector("span");
    if (words) { words.textContent = saveState.getAttribute("data-draft").replace("{time}", time); }
    saveState.classList.remove("is-editing");
    saveState.classList.add("save-state--draft");
  }

  function edited() {
    if (!saveState || saveState.classList.contains("is-editing")) { return; }
    saveState.classList.remove("save-state--draft");
    saveState.classList.add("is-editing");
    var words = saveState.querySelector("span");
    if (words) { words.textContent = saveState.getAttribute("data-editing"); }
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
      if (reply.ok && mine === changes) { kept(reply.headers.get("X-Draft-Kept")); }
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
        markCurrent(questionOf(again));
      }
      busy("");
    }).catch(function () {
      if (mine === changes) { busy("The form could not be updated. Check the connection and change an answer again."); }
    });
  }

  form.addEventListener("change", function (event) {
    changes += 1;
    if ((event.target.name || "").indexOf("ui.") !== 0) { edited(); }
    refresh();
  });
  form.addEventListener("input", function (event) {
    if (!typed(event.target)) { return; }
    changes += 1;
    edited();
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
