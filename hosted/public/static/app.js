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
  var remembered = null; /* the control the reviewer was at when they left for the post (keyOf) */
  var wide = window.matchMedia ? window.matchMedia("(min-width: 80rem)") : null;
  var panel = document.getElementById("nav-panel");

  /* The panel of sequences: open next to the page on a wide screen, one line above it on a narrow one. */
  function fitPanel() {
    if (panel && wide) { panel.open = wide.matches; }
  }
  fitPanel();
  if (wide && wide.addEventListener) { wide.addEventListener("change", fitPanel); }

  /* The region that scrolls: the page's own one when a bottom bar is shown, else the window. */
  function scroller() {
    var region = document.getElementById("page-scroll");
    return region && region.scrollHeight > region.clientHeight ? region : window;
  }

  /* A control with the focus stands wholly within the region that scrolls, clear of both edges. */
  function keepInView(el) {
    if (!el || !el.getBoundingClientRect) { return; }
    var region = scroller();
    var top = 0;
    var bottom = window.innerHeight;
    if (region !== window) {
      var r = region.getBoundingClientRect();
      top = r.top;
      bottom = r.bottom;
    }
    var box = el.getBoundingClientRect();
    var margin = 24;
    if (box.top < top + margin) {
      region.scrollBy(0, box.top - top - margin);
    } else if (box.bottom > bottom - margin) {
      region.scrollBy(0, Math.min(box.bottom - bottom + margin, box.top - top - margin));
    }
  }

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

  function usable(c) {
    return c.type !== "hidden" && !c.disabled && c.name !== "ui.later" && c.offsetParent !== null;
  }

  /* The control of a question that still needs its answer: one marked wrong, else the first
     empty box or list, else the first group of choices with nothing chosen (in a number row:
     the missing part). None: null. */
  function missingControl(box) {
    if (!box) { return null; }
    var all = [].filter.call(box.querySelectorAll("input, select, textarea"), usable);
    var empty = function (c) {
      if (c.type === "radio") { return !box.querySelector('input[type="radio"][name="' + c.name + '"]:checked'); }
      if (c.type === "checkbox") { return false; }
      return !c.value;
    };
    var wrong = all.filter(function (c) { return c.getAttribute("aria-invalid") === "true" && empty(c); });
    if (wrong.length) { return wrong[0]; }
    var blank = all.filter(empty);
    return blank.length ? blank[0] : null;
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
    var control = box.matches("input, select, textarea, button") ? box
      : (missingControl(box) || firstControl(box));
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
    var last = lastField();
    if (last && last.value && document.getElementById(last.value)) { return last.value; }
    var state = document.getElementById("form-state");
    var next = state ? state.getAttribute("data-next") : "";
    return next && document.getElementById(next) ? next : "";
  }

  function back() {
    setDock(false);
    var el = body ? findKey(remembered) : null;
    var id = el ? "" : backTarget();
    if (!el && !id) { moveTo(form, "start"); return; }
    el = el || document.getElementById(id);
    if (el && !id) {
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
    if (body && active && body.contains(active)) { remembered = keyOf(active); }
    setDock(true);
    moveTo(post, "start");
    /* The focus waits in the post's own "Back to my question": one key press returns. */
    var stay = post ? post.querySelector("[data-back]") : null;
    if (stay) { stay.focus({ preventScroll: true }); } else if (post) { post.focus({ preventScroll: true }); }
  }

  /* The images of the post: loaded at once; "Loading…" until there, a plain message and "Try
     again" if one fails. */
  [].forEach.call(document.querySelectorAll("[data-media]"), function (fig) {
    var img = fig.querySelector("img");
    var source = img.getAttribute("src");
    var tries = 0;
    function loaded() { fig.classList.remove("is-failed"); fig.classList.add("is-loaded"); }
    function failed() { fig.classList.remove("is-loaded"); fig.classList.add("is-failed"); }
    img.addEventListener("load", loaded);
    img.addEventListener("error", failed);
    if (img.complete) { if (img.naturalWidth > 0) { loaded(); } else { failed(); } }
    var again = fig.querySelector(".media__retry");
    if (again) {
      again.addEventListener("click", function () {
        tries += 1;
        fig.classList.remove("is-failed");
        img.setAttribute("src", source + "?try=" + tries);
      });
    }
  });

  document.addEventListener("click", function (event) {
    var skip = event.target.closest ? event.target.closest("[data-skip]") : null;
    if (skip && body) {
      var first = body.querySelector("[data-q]");
      if (first) { event.preventDefault(); goTo(first.id); return; }
    }
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
    if (event.target && event.target !== document.body && !event.target.closest(".dock")) {
      window.setTimeout(function () { keepInView(document.activeElement); }, reduce ? 0 : 350);
    }
    if (!body || !body.contains(event.target)) { return; }
    /* (The focus put back after a replacement is the same place: marking it again is harmless.) */
    var q = questionOf(event.target);
    if (!q) { return; }
    markCurrent(q);
    var last = lastField();
    if (last) { last.value = q.id; }
    remembered = keyOf(event.target);
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
  var version = 0;       /* counts every edit of the reviewer */
  var inFlight = false;  /* one request at a time */
  var replacing = false; /* while the form is being replaced, nothing it fires is an edit */
  var held = false;      /* a pointer press or a key is down */
  var pending = null;    /* a reply waiting for that release */
  var lastHtml = null;   /* the server's form last put in place */
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
    /* A kept draft makes a form not started In progress. */
    var now = document.querySelector(".badge-now");
    var then = document.querySelector(".badge-if-draft");
    if (now && then) { now.hidden = true; then.hidden = false; }
    var time = at.slice(11, 16);
    var words = saveState.querySelector("span:last-child");
    if (words) { words.textContent = saveState.getAttribute("data-draft").replace("{time}", time); }
    saveState.classList.remove("is-editing");
    saveState.classList.add("save-state--draft");
  }

  function edited() {
    if (!saveState || saveState.classList.contains("is-editing")) { return; }
    saveState.classList.remove("save-state--draft");
    saveState.classList.add("is-editing");
    var words = saveState.querySelector("span:last-child");
    if (words) { words.textContent = saveState.getAttribute("data-editing"); }
  }

  /* A control, found again in a new copy of the form: by its id, else by what it is (its tag,
     name, value and words) within the nearest element with an id, and its place among equals. */
  function keyOf(el) {
    if (!el || !body.contains(el) || el === body) { return null; }
    if (el.id) { return { id: el.id }; }
    var box = el.parentNode;
    while (box && box !== body && !box.id) { box = box.parentNode; }
    var k = { box: box && box !== body ? box.id : "", tag: el.tagName, name: el.getAttribute("name"),
              value: el.getAttribute("value"), words: (el.textContent || "").trim() };
    k.n = equals(k).indexOf(el);
    return k;
  }

  function equals(k) {
    var root = k.box ? document.getElementById(k.box) : body;
    if (!root) { return []; }
    return [].filter.call(root.querySelectorAll(k.tag), function (x) {
      return x.getAttribute("name") === k.name && x.getAttribute("value") === k.value &&
        (x.textContent || "").trim() === k.words;
    });
  }

  function findKey(k) {
    if (!k) { return null; }
    if (k.id) { return document.getElementById(k.id); }
    var found = equals(k);
    return found[k.n] || found[0] || null;
  }

  /* Put the server's form in place. Nothing the replacement fires (a "change" of a removed box,
     a blur) is an edit, and the form is never read while it is being replaced. The focus, the
     caret, the open disclosures and the place on the screen stay where they were. */
  function replace(html) {
    var active = document.activeElement;
    var key = keyOf(active);
    var top = key ? active.getBoundingClientRect().top : 0;
    var from = typed(active) ? active.selectionStart : null;
    var to = typed(active) ? active.selectionEnd : null;
    var open = [].map.call(body.querySelectorAll("details[open] > summary"), keyOf);
    replacing = true;
    try {
      body.innerHTML = html;
    } finally {
      replacing = false;
    }
    open.forEach(function (k) {
      var s = findKey(k);
      if (s && s.parentNode.tagName === "DETAILS") { s.parentNode.open = true; }
    });
    var again = findKey(key);
    if (again) {
      again.focus({ preventScroll: true });
      if (typed(again) && from !== null) { again.setSelectionRange(from, to); }
      scroller().scrollBy(0, again.getBoundingClientRect().top - top);
      markCurrent(questionOf(again));
    }
  }

  /* Send the form as it stands now. One request at a time; a reply is put in place only when
     nothing changed since its request was built; otherwise the newest state is sent at once,
     so the server always ends with the newest state as the draft. */
  function send() {
    window.clearTimeout(timer);
    timer = null;
    if (inFlight || replacing) { return; }
    var mine = version;
    var data = new URLSearchParams(new FormData(form));
    inFlight = true;
    busy("Updating the form…");
    window.fetch(url, {
      method: "POST", body: data, credentials: "same-origin", headers: { "X-Fragment": "form" }
    }).then(function (reply) {
      if (reply.status === 409 || (reply.ok && reply.headers.get("X-Fragment") !== "form")) {
        window.location.reload();   /* the form moved on, or the session ended */
        return null;
      }
      if (!reply.ok) { return Promise.reject(new Error("refused")); }
      return reply.text().then(function (html) { return { html: html, at: reply.headers.get("X-Draft-Kept") }; });
    }).then(function (got) {
      inFlight = false;
      if (got === null) { return; }
      got.mine = mine;
      if (held) { pending = got; return; }   /* a press or a key is down: after its release */
      apply(got);
    }).catch(function () {
      inFlight = false;
      if (mine !== version) { send(); return; }
      busy("The form could not be updated. Check the connection and change an answer again.");
    });
  }

  /* A reply is applied only when nothing changed since its request (else the newest state is
     sent), and the form is replaced only when the server's form differs from the one on the
     screen in more than the entered values (most typed entries change nothing else). */
  function apply(got) {
    if (got.mine !== version) { send(); return; }   /* the reviewer went on: send the newest */
    if (lastHtml !== null && shape(got.html) === shape(lastHtml)) {
      var made = document.createElement("template");
      made.innerHTML = got.html;
      var fresh = made.content.getElementById("form-state");
      var here = document.getElementById("form-state");
      if (fresh && here) {
        here.setAttribute("data-ready", fresh.getAttribute("data-ready") || "");
        here.setAttribute("data-next", fresh.getAttribute("data-next") || "");
      }
    } else {
      replace(got.html);
    }
    lastHtml = got.html;
    kept(got.at);
    busy("");
  }

  function shape(html) {
    return html.replace(/ value="[^"]*"/g, "").replace(/ (checked|selected)(?=[ >])/g, "")
      .replace(/(<textarea[^>]*>)[\s\S]*?(<\/textarea>)/g, "$1$2")
      .replace(/ data-(ready|next)="[^"]*"/g, "");
  }

  /* A press of the pointer or a key that is down holds every replacement until its release
     (the click lands on the control it began on). */
  function release() {
    held = false;
    if (pending) {
      window.setTimeout(function () {
        if (held || !pending) { return; }
        var got = pending;
        pending = null;
        apply(got);
      }, 0);
    }
  }
  document.addEventListener("pointerdown", function () { held = true; }, true);
  document.addEventListener("pointerup", release, true);
  document.addEventListener("pointercancel", release, true);
  document.addEventListener("keydown", function (event) {
    if (!/^(Shift|Control|Alt|Meta)$/.test(event.key)) { held = true; }
  }, true);
  document.addEventListener("keyup", release, true);
  window.addEventListener("blur", release);

  function later(ms) {
    window.clearTimeout(timer);
    timer = window.setTimeout(send, ms);
  }

  form.addEventListener("change", function (event) {
    if (replacing) { return; }
    version += 1;
    if ((event.target.name || "").indexOf("ui.") !== 0) { edited(); }
    send();
  });
  form.addEventListener("input", function (event) {
    if (replacing || !typed(event.target)) { return; }
    version += 1;
    edited();
    later(700);
  });
  form.addEventListener("keydown", function (event) {
    /* Enter in a one-line box applies the value; it never saves. */
    if (event.key === "Enter" && event.target.tagName === "INPUT" && event.target.type === "text") {
      event.preventDefault();
      version += 1;
      send();
    }
  });
}());
