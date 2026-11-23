// sonde's only script, served from /static so the CSP can say
// script-src 'self' (sonde/web/headers.py). No inline script or on…= handlers
// in templates: put behaviour here.

// An avatar that fails to load hides itself, leaving the grey placeholder
// behind it. `error` doesn't bubble, so listen in the capture phase; and an
// image can fail before this script runs, so sweep for those once as well.
(function () {
  const hide = (img) => { img.style.visibility = "hidden"; };
  document.addEventListener("error", function (event) {
    const el = event.target;
    if (el instanceof HTMLImageElement && el.hasAttribute("data-hide-if-broken")) hide(el);
  }, true);
  document.querySelectorAll("img[data-hide-if-broken]").forEach(function (img) {
    if (img.complete && img.naturalWidth === 0 && img.currentSrc) hide(img);
  });
})();

(function () {
  const dot  = document.getElementById("activity-dot");
  const text = document.getElementById("activity-text");
  const bar  = document.getElementById("activity-bar");
  const fill = document.getElementById("activity-fill");
  const next = document.getElementById("activity-next");

  // 3s while something runs, 15s when idle. A static page can't show a
  // 40-second sweep; a 3s poll when nothing is happening is just waste.
  const BUSY = 3000, IDLE = 15000;
  let timer = null;

  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  function humanise(seconds) {
    if (seconds == null) return "";
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
    return `${Math.round(seconds / 86400)}d`;
  }

  function idle(status) {
    dot.className = "h-2 w-2 shrink-0 rounded-full bg-emerald-500";
    bar.classList.add("hidden");
    const age = status.last_sync_age_seconds;
    text.textContent = age == null
      ? "Idle — nothing has run yet"
      : `Idle — last sync ${humanise(age)} ago`;
    if (!status.scheduler) {
      dot.className = "h-2 w-2 shrink-0 rounded-full bg-amber-500";
      text.textContent += " · scheduler not running";
    }
  }

  function busy(jobs) {
    dot.className = "h-2 w-2 shrink-0 animate-pulse rounded-full bg-red-700";
    // Jobs run in parallel, so name all of them. They share one rate limiter,
    // which is why running several is safe but each one is slower.
    const parts = jobs.map((job) => {
      let label = job.kind;
      if (job.total) label += ` ${job.current}/${job.total} ${job.unit}`;
      else if (job.detail) label += ` ${job.detail}`;
      return `${label} (${humanise(job.elapsed)})`;
    });
    text.textContent = `Running ${plural(jobs.length, "job")} — ${parts.join(" · ")}`;
    // The bar tracks whichever job has furthest to go.
    const measured = jobs.filter((j) => j.total);
    if (measured.length) {
      const slowest = measured.reduce((a, b) => (a.pct <= b.pct ? a : b));
      bar.classList.remove("hidden");
      fill.style.width = `${slowest.pct}%`;
    } else {
      bar.classList.add("hidden");
    }
  }

  function stopped(message) {
    dot.className = "h-2 w-2 shrink-0 rounded-full bg-amber-500";
    text.textContent = message;
    bar.classList.add("hidden");
    next.textContent = "";
    if (timer) clearTimeout(timer);
  }

  async function poll() {
    let status;
    try {
      const resp = await fetch("/api/status", {
        headers: { "Accept": "application/json" },
        redirect: "error",   // an Authelia redirect must not be followed
      });
      if (!resp.ok) throw new Error(resp.status);
      status = await resp.json();
    } catch (err) {
      // Most likely the Authelia session expired, so /api/status answered
      // with a redirect to the login page rather than JSON. Say so instead
      // of silently showing nothing.
      stopped("Session expired — reload to sign in again");
      return;
    }

    const jobs = status.running || [];
    if (jobs.length) busy(jobs); else idle(status);

    const soonest = (status.scheduled || [])[0];
    next.textContent = soonest
      ? `next: ${soonest.id} in ${humanise(soonest.in_seconds)}`
      : "";

    timer = setTimeout(poll, jobs.length ? BUSY : IDLE);
  }

  poll();
  // Don't poll a tab nobody is looking at; refresh the moment it returns.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (timer) clearTimeout(timer);
    } else {
      poll();
    }
  });
})();

(function () {
  // Toggling a circle in place. Without this every chip still works — it is a
  // plain form, it posts, and the redirect lands back on the chips — so the
  // only thing lost is the reload. Which is worth losing: the block sits well
  // down a long profile, and tagging three people is six page loads.
  const grid = document.querySelector("[data-circle-toggles]");

  const ON = ["border-red-800", "bg-red-50", "text-red-800"];
  const OFF = ["border-slate-300", "bg-white", "text-slate-500",
               "hover:border-red-800", "hover:text-red-800"];

  if (grid) grid.addEventListener("submit", function (event) {
    const form = event.target.closest("form");
    const chip = form && form.querySelector("[data-circle-chip]");
    if (!chip) return;
    event.preventDefault();
    if (chip.disabled) return;

    const action = form.elements.action.value;
    chip.disabled = true;
    chip.style.opacity = "0.5";

    // getAttribute, NOT form.action. This form has a control named "action",
    // and a named control shadows the form's own property — so `form.action`
    // returns that <input> rather than the URL, and every toggle posted to
    // /followers/[object HTMLInputElement] and 405'd. Server-side tests all
    // passed: the markup was right and the endpoint was right, and only a
    // browser joins the two up.
    fetch(form.getAttribute("action"), {
      method: "POST",
      body: new FormData(form),
      // manual, NOT follow. The endpoint answers 303 to a full page, and
      // following it costs a complete render this code then discards —
      // measured at 1.8s for /circles. An opaqueredirect IS the success
      // signal here: the browser will not show us the 303, but reaching one
      // means the POST was accepted.
      redirect: "manual",
    }).then(function (response) {
      if (!response.ok && response.type !== "opaqueredirect")
        throw new Error(response.status);
      const on = action === "add";
      chip.classList.remove.apply(chip.classList, on ? OFF : ON);
      chip.classList.add.apply(chip.classList, on ? ON : OFF);
      chip.setAttribute("aria-pressed", on ? "true" : "false");
      form.elements.action.value = on ? "remove" : "add";
      const mark = chip.querySelector("[data-chip-mark]");
      mark.textContent = on ? "×" : "+";
      mark.className = on ? "text-red-800" : "text-slate-300";
    }).catch(function () {
      // Say nothing false. The page is now out of step with the database, and
      // a reload is the honest way to find out what it actually says.
      chip.title = "That did not save — reload the page.";
      chip.style.outline = "2px solid #991b1b";
    }).finally(function () {
      chip.disabled = false;
      chip.style.opacity = "";
    });
  });
})();

(function () {
  // The same idea one table over. A row chip only ever removes, so the chip
  // goes away rather than flipping. Adding from a row still posts normally:
  // building the new chip client-side would mean duplicating its markup in
  // JavaScript, and a reload after adding one circle is not the tedium this
  // set out to fix.
  document.querySelectorAll("form[data-row-tag]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      const chip = form.querySelector("[data-row-chip]");
      if (!chip) return;
      event.preventDefault();
      chip.disabled = true;
      chip.style.opacity = "0.5";
      // getAttribute, not form.action — a control named "action" shadows it.
      fetch(form.getAttribute("action"), {
        method: "POST", body: new FormData(form), redirect: "manual",
      }).then(function (response) {
        if (!response.ok && response.type !== "opaqueredirect")
          throw new Error(response.status);
        form.remove();
      }).catch(function () {
        chip.disabled = false;
        chip.style.opacity = "";
        chip.title = "That did not save — reload the page.";
        chip.style.outline = "2px solid #991b1b";
      });
    });
  });
})();

(function () {
  // Fetched when the panel is first opened, not on page load. Nobody needs
  // this except in the moment they wonder, and polling it would spend GitHub
  // rate limit answering a question nobody asked.
  const panel = document.getElementById("build");
  if (!panel) return;
  const body = panel.querySelector("[data-build-body]");
  const dot  = panel.querySelector("[data-build-dot]");
  let asked = false;

  const escape = (s) => String(s == null ? "" : s).replace(/[<>&"]/g,
    (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));

  function render(d) {
    if (d.status === "current") {
      body.innerHTML = '<span class="text-emerald-700">Up to date with '
        + escape(d.branch) + ".</span>";
      return;
    }
    if (d.status === "unknown") {
      body.textContent = d.detail || "Cannot tell.";
      return;
    }
    const parts = [];
    if (d.behind) {
      parts.push('<span class="font-semibold text-red-800">' + d.behind
        + " commit" + (d.behind === 1 ? "" : "s") + " behind " + escape(d.branch)
        + ".</span>");
      dot.textContent = " ●";
      dot.className = "text-red-700";
    }
    if (d.ahead) {
      parts.push('<span class="text-amber-700">Running ' + d.ahead
        + " commit" + (d.ahead === 1 ? "" : "s") + " that " + escape(d.branch)
        + " does not have.</span>");
    }
    if (d.commits && d.commits.length) {
      parts.push('<ul class="mt-2 space-y-1">' + d.commits.map((c) =>
        '<li class="truncate"><span class="font-mono text-[10px] text-slate-400">'
        + escape(c.sha) + "</span> " + escape(c.message) + "</li>").join("")
        + "</ul>");
    }
    if (d.compare_url) {
      parts.push('<a class="mt-2 inline-block text-red-800 hover:underline" '
        + 'target="_blank" rel="noreferrer" href="' + escape(d.compare_url)
        + '">see the diff →</a>');
    }
    body.innerHTML = parts.join("");
  }

  panel.addEventListener("toggle", function () {
    if (!panel.open || asked) return;
    asked = true;
    fetch("/build", { headers: { "Accept": "application/json" } })
      .then((r) => r.json())
      .then(render)
      .catch(() => { body.textContent = "Could not check."; });
  });
})();

(function () {
  // Progressive enhancement only. Without this the checkboxes and both submit
  // buttons still work exactly as they do with it; only the running count and
  // the select-all box are lost. Nothing here is load-bearing.
  document.querySelectorAll("form[data-batch]").forEach(function (form) {
    // form.elements, NOT form.querySelectorAll. The row checkboxes are not
    // descendants of the form — they join it with form="batch" so the table
    // is never wrapped and the follow-back form inside who() is never nested.
    // A descendant query finds nothing and the count sits at zero forever.
    const boxes = Array.from(form.elements).filter((el) => el.name === "did");
    const count = form.querySelector("[data-selection-count]");
    // Also outside the form, for the same reason.
    const all = document.querySelector("[data-select-all]");

    function recount() {
      if (count) count.textContent = boxes.filter((b) => b.checked).length;
    }

    boxes.forEach((box) => box.addEventListener("change", recount));
    if (all) {
      all.addEventListener("change", function () {
        boxes.forEach((box) => { box.checked = all.checked; });
        recount();
      });
    }
    recount();
  });
})();
