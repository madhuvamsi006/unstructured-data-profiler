/* Demo console front end. No framework and no build step: the whole point of a
   pre-sales asset is that it runs from a clean checkout on someone else's
   laptop without a toolchain. */

(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var state = { runId: null, health: null, sessionId: null, busy: false, scanning: false };

  // ---------------- formatting ----------------
  function num(n) { return (n == null ? 0 : n).toLocaleString("en-US"); }
  function compact(n) {
    n = n || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (n >= 1e4) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
    return num(n);
  }
  function bytes(b) {
    b = b || 0;
    if (b >= 1073741824) return (b / 1073741824).toFixed(2) + " GB";
    if (b >= 1048576) return (b / 1048576).toFixed(1) + " MB";
    if (b >= 1024) return (b / 1024).toFixed(1) + " KB";
    return b + " B";
  }
  function lookup(list, key) {
    for (var i = 0; i < (list || []).length; i++) if (list[i].key === key) return list[i].count;
    return 0;
  }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // ---------------- theme ----------------
  var themeToggle = $("theme-toggle");
  try {
    var saved = localStorage.getItem("profiler-theme");
    if (saved) document.documentElement.setAttribute("data-theme", saved);
  } catch (e) { /* private window - fall back to the OS setting */ }
  themeToggle.addEventListener("click", function () {
    var root = document.documentElement;
    var current = root.getAttribute("data-theme");
    if (!current) {
      current = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    var next = current === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("profiler-theme", next); } catch (e) { /* ignore */ }
    // The dashboard lives in an iframe with its own document; keep it in step.
    var frame = $("dashboard-frame");
    if (frame.contentDocument) frame.contentDocument.documentElement.setAttribute("data-theme", next);
  });

  // ---------------- tabs ----------------
  var TABS = ["overview", "dashboard", "agent"];
  function selectTab(name) {
    TABS.forEach(function (t) {
      $("tab-" + t).setAttribute("aria-selected", String(t === name));
      $("panel-" + t).setAttribute("data-active", String(t === name));
    });
    if (name === "agent") setTimeout(function () { $("chat-input").focus(); }, 0);
  }
  TABS.forEach(function (t) {
    $("tab-" + t).addEventListener("click", function () { selectTab(t); });
  });

  // ---------------- health ----------------
  function applyHealth(health) {
    state.health = health;
    $("model-name").textContent = health.model;
    $("model-pill").classList.add("ok");

    var pill = $("auth-pill");
    var text = $("auth-text");
    pill.classList.toggle("ok", !!health.auth.ok);
    pill.classList.toggle("bad", !health.auth.ok);
    text.textContent = health.auth.ok
      ? (health.auth.mode === "vertex" ? "Vertex AI connected" : "API key connected")
      : "Agent offline";
    pill.title = health.auth.detail + (health.auth.fix ? "  →  " + health.auth.fix : "");

    $("max-files").value = health.defaults.max_files;

    var hosted = health.environment === "cloud_run";
    if (hosted) {
      // Show the drop-zone as the primary action; collapse the text source field.
      var dz = $("drop-zone");
      if (dz) dz.hidden = false;

      // Collapse the source text section to a small "or scan gs:// / sample" toggle
      var srcSection = $("source-section");
      if (srcSection) {
        srcSection.hidden = true;
        var toggle = document.createElement("div");
        toggle.className = "source-toggle";
        toggle.textContent = "Or scan a gs:// bucket / sample estate…";
        toggle.addEventListener("click", function () {
          srcSection.hidden = !srcSection.hidden;
          $("run-scan").hidden = srcSection.hidden;
          toggle.textContent = srcSection.hidden
            ? "Or scan a gs:// bucket / sample estate…"
            : "Hide path input";
        });
        dz.insertAdjacentElement("afterend", toggle);
      }

      $("source").placeholder = "gs://bucket/prefix";
      $("dlp-hint").insertAdjacentHTML(
        "afterend",
        ‘<p class="hint">Hosted on Cloud Run’ +
          (health.revision ? " (<code>" + health.revision + "</code>)" : "") +
          ". Drop or upload a local file, or scan a <code>gs://</code> bucket. Run history resets when the instance recycles.</p>"
      );
      // Hide Run scan — upload auto-starts the scan
      $("run-scan").hidden = true;
    }

    var allowed = health.allowed_sources || [];
    if (allowed.length) {
      $("source").placeholder = "must be within the allowed sources below";
      $("dlp-hint").insertAdjacentHTML(
        "afterend",
        '<p class="hint">This deployment can only scan: ' +
          allowed.map(function (a) { return "<code>" + a.replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</code>"; }).join(", ") +
          "</p>"
      );
    }

    var presets = $("presets");
    presets.innerHTML = "";
    var options = [];
    if (health.sample_estate) options.push(["Sample estate", health.sample_estate]);
    // Only offer sources this deployment is actually allowed to scan.
    allowed.forEach(function (entry) {
      if (health.sample_estate && entry === health.sample_estate) return;
      options.push([entry.replace(/^gs:\/\//, ""), entry]);
    });
    if (!hosted && !allowed.length) options.push(["Home folder", "~"]);
    options.forEach(function (pair) {
      var chip = el("button", "chip", pair[0]);
      chip.type = "button";
      chip.addEventListener("click", function () { $("source").value = pair[1]; });
      presets.appendChild(chip);
    });
    if (health.sample_estate && !$("source").value) $("source").value = health.sample_estate;

    if (!health.dlp_configured) {
      $("use-dlp").disabled = true;
      $("dlp-switch").classList.add("disabled");
      $("dlp-hint").textContent = "Set GOOGLE_CLOUD_PROJECT to enable Cloud DLP. Local detection still runs.";
    }

    if (!health.auth.ok) {
      var banner = el("div", "banner err");
      var body = el("div");
      body.appendChild(el("b", null, "The agent cannot reach a model yet"));
      body.appendChild(document.createTextNode(health.auth.detail + " "));
      body.appendChild(el("br"));
      body.appendChild(document.createTextNode("Scanning, the dashboard and the report all still work."));
      if (health.auth.fix) {
        var fix = el("code", null, health.auth.fix.replace(/^Run:\s*/, ""));
        body.appendChild(el("br"));
        body.appendChild(fix);
      }
      banner.appendChild(body);
      $("chat-inner").appendChild(banner);
    }
    renderSuggestions();
  }

  // ---------------- runs ----------------
  function loadRuns() {
    return fetch("/api/runs").then(function (r) { return r.json(); }).then(function (data) {
      var box = $("runs");
      box.innerHTML = "";
      if (!data.runs.length) {
        box.appendChild(el("p", "empty", "No scans yet."));
        return;
      }
      data.runs.forEach(function (run) {
        var btn = el("button", "run");
        btn.type = "button";
        if (run.run_id === state.runId) btn.setAttribute("aria-current", "true");
        var leaf = String(run.source_uri || "").replace(/\\/g, "/").replace(/\/$/, "").split("/").pop();
        btn.appendChild(el("span", "run-src", leaf || run.source_uri));
        btn.appendChild(el("span", "run-meta",
          num(run.total_files) + " files · readiness " + run.mean_readiness +
          " · " + new Date(run.generated_at).toLocaleString()));
        btn.addEventListener("click", function () { openRun(run.run_id); });
        box.appendChild(btn);
      });
    });
  }

  function openRun(runId) {
    return fetch("/api/runs/" + encodeURIComponent(runId) + "/summary")
      .then(function (r) {
        if (!r.ok) throw new Error("Run not found");
        return r.json();
      })
      .then(function (summary) {
        showSummary(summary);
        selectTab("dashboard");
      });
  }

  function showSummary(summary) {
    state.runId = summary.run_id;
    renderKpis(summary);

    var frame = $("dashboard-frame");
    frame.src = "/api/runs/" + encodeURIComponent(summary.run_id) + "/dashboard";
    frame.addEventListener("load", function () {
      var theme = document.documentElement.getAttribute("data-theme");
      if (theme && frame.contentDocument) {
        frame.contentDocument.documentElement.setAttribute("data-theme", theme);
      }
    }, { once: true });

    var report = $("dl-report");
    report.href = "/api/runs/" + encodeURIComponent(summary.run_id) + "/report";
    report.hidden = false;
    var manifest = $("dl-manifest");
    manifest.href = "/api/runs/" + encodeURIComponent(summary.run_id) + "/manifest";
    manifest.hidden = false;

    // A new scan is a new subject; start the agent on a fresh session seeded with it.
    state.sessionId = null;
    renderSuggestions();
    loadRuns();
  }

  function renderKpis(summary) {
    var total = summary.inventory.total_files || 0;
    var ingestNow = lookup(summary.readiness.by_disposition, "ingest_now");
    var creds = summary.sensitivity.credential_exposures || 0;
    var tiles = [
      ["Files", compact(total), false],
      ["Estate size", bytes(summary.inventory.total_bytes), false],
      ["Ingest-ready", (total ? (100 * ingestNow / total).toFixed(1) : "0") + "%", false],
      ["Needs Doc AI", compact(summary.readiness.needs_document_ai), false],
      ["Reclaimable", bytes(summary.duplication.redundant_bytes), false],
      ["High sensitivity", compact(summary.sensitivity.high_or_critical), false],
      ["Credentials", compact(creds), creds > 0],
      ["Mean readiness", summary.readiness.mean_score, false]
    ];
    var box = $("kpis");
    box.innerHTML = "";
    tiles.forEach(function (t) {
      var tile = el("div", "kpi" + (t[2] ? " alarm" : ""));
      tile.appendChild(el("div", "kpi-label", t[0]));
      tile.appendChild(el("div", "kpi-value", String(t[1])));
      box.appendChild(tile);
    });
    box.hidden = false;
  }

  // ---------------- scanning ----------------
  function logLine(text, cls) {
    var log = $("scanlog");
    log.appendChild(el("div", cls || null, text));
    log.scrollTop = log.scrollHeight;
  }

  function isWindowsPath(s) {
    // Matches C:\... D:/... \\server\share  and quoted variants
    var stripped = s.replace(/^["']+|["']+$/g, "").trim();
    return /^[A-Za-z]:[/\\]/.test(stripped) || /^\\\\/.test(stripped);
  }

  function runScan() {
    if (state.scanning) return;
    var source = $("source").value.trim();
    if (!source) { $("source").focus(); return; }

    // Catch local Windows paths before they hit the server — they never exist
    // on Cloud Run; the user must upload the file first.
    if (isWindowsPath(source)) {
      var log = $("scanlog");
      $("overview-idle").hidden = true;
      log.hidden = false;
      log.innerHTML = "";
      selectTab("overview");
      logLine("$ scan " + source, "muted");
      logLine("crawling\u2026");
      logLine(
        "\u2717 That looks like a path on your local machine — it does not exist on the server.\n" +
        "  \u25b6 Use the \u201cUpload file\u201d button to upload your file first, then click Run scan.",
        "err"
      );
      // Flash the upload button to draw attention
      var lbl = $("upload-label");
      if (lbl) {
        lbl.classList.add("upload-pulse");
        setTimeout(function () { lbl.classList.remove("upload-pulse"); }, 2000);
      }
      return;
    }

    state.scanning = true;
    $("run-scan").disabled = true;
    $("run-label").textContent = "Scanning…";
    $("overview-idle").hidden = true;
    var log = $("scanlog");
    log.hidden = false;
    log.innerHTML = "";
    selectTab("overview");

    logLine("$ scan " + source, "muted");

    var params = new URLSearchParams({
      source: source,
      max_files: $("max-files").value || "0",
      sample_bytes: $("sample-bytes").value || "0",
      use_dlp: $("use-dlp").checked ? "true" : "false"
    });
    var es = new EventSource("/api/scan?" + params.toString());

    es.addEventListener("started", function () { logLine("crawling…"); });
    es.addEventListener("progress", function (e) {
      var d = JSON.parse(e.data);
      var log = $("scanlog");
      var last = log.lastElementChild;
      var text = "  " + num(d.seen) + " files profiled";
      if (last && last.dataset.progress === "1") last.textContent = text;
      else {
        var line = el("div", null, text);
        line.dataset.progress = "1";
        log.appendChild(line);
      }
      log.scrollTop = log.scrollHeight;
    });
    // EventSource routes BOTH a server-sent `event: error` and a native
    // transport error here. Always close: an unhandled native error makes
    // EventSource reconnect, which would silently re-run the whole scan.
    var settled = false;
    es.addEventListener("error", function (e) {
      es.close();
      if (settled) return;
      settled = true;
      var message = null;
      try { message = JSON.parse(e.data).message; } catch (err) { /* native error has no data */ }
      logLine("✗ " + (message || "Lost connection to the server."), "err");
      finishScan();
    });
    es.addEventListener("done", function (e) {
      settled = true;
      var summary = JSON.parse(e.data);
      var inv = summary.inventory, dup = summary.duplication, sens = summary.sensitivity;
      logLine("✓ " + num(inv.total_files) + " files · " + bytes(inv.total_bytes) +
              " · " + summary.scan.elapsed_seconds + "s", "ok");
      logLine("  " + summary.readiness.ingest_now_pct + "% ingest-ready · " +
              num(summary.readiness.needs_document_ai) + " need Document AI");
      logLine("  " + num(dup.duplicate_files) + " duplicates · " +
              bytes(dup.redundant_bytes) + " reclaimable");
      logLine("  " + num(sens.high_or_critical) + " high/critical sensitivity" +
              (sens.credential_exposures ? " · " + num(sens.credential_exposures) + " credential exposures" : ""),
              sens.credential_exposures ? "err" : null);
      logLine("  report + dashboard written", "muted");
      showSummary(summary);
      finishScan();
      es.close();
      selectTab("dashboard");
    });

    function finishScan() {
      state.scanning = false;
      $("run-scan").disabled = false;
      $("run-label").textContent = "Run scan";
    }
  }
  $("run-scan").addEventListener("click", runScan);
  $("source").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); runScan(); }
  });

  // ---------------- chat ----------------
  function renderSuggestions() {
    var existing = document.querySelector(".suggestions");
    if (existing) existing.remove();
    var box = el("div", "suggestions");
    var prompts = state.runId
      ? [
          "Summarise this estate for a migration steering group.",
          "Which files are quarantined, and why?",
          "What would it cost us to skip deduplication?",
          "Show me everything that needs OCR before indexing."
        ]
      : [
          // Carry the resolved path so a click always scans something real.
          "Profile the estate at " + ((state.health && state.health.sample_estate) || "sample_estate") +
            " and tell me what it would take to make it RAG-ready.",
          "What do you actually check for?"
        ];
    prompts.forEach(function (text) {
      var chip = el("button", "chip", text);
      chip.type = "button";
      chip.addEventListener("click", function () { send(text); });
      box.appendChild(chip);
    });
    $("chat-inner").appendChild(box);
  }

  function addMessage(role, text) {
    var msg = el("div", "msg " + role);
    msg.appendChild(el("div", "msg-role", role === "user" ? "You" : "Profiler"));
    var body = el("div", "msg-body");
    if (text != null) body.textContent = text;
    msg.appendChild(body);
    $("chat-inner").appendChild(msg);
    scrollChat();
    return body;
  }

  function scrollChat() {
    var scroll = $("chat-scroll");
    scroll.scrollTop = scroll.scrollHeight;
  }

  /* Minimal inline markdown: the agent writes tables and bold, and rendering
     them is the difference between a demo that reads like a product and one
     that reads like a terminal. Text is escaped first, so this never injects. */
  function renderMarkdown(target, raw) {
    var escaped = raw
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    var lines = escaped.split("\n");
    var html = "";
    var table = null;

    function flushTable() {
      if (!table) return;
      html += "<table>";
      table.forEach(function (row, i) {
        var tag = i === 0 ? "th" : "td";
        html += "<tr>" + row.map(function (c) { return "<" + tag + ">" + c + "</" + tag + ">"; }).join("") + "</tr>";
      });
      html += "</table>";
      table = null;
    }

    lines.forEach(function (line) {
      var trimmed = line.trim();
      if (/^\|.*\|$/.test(trimmed)) {
        var cells = trimmed.slice(1, -1).split("|").map(function (c) { return c.trim(); });
        if (cells.every(function (c) { return /^:?-{2,}:?$/.test(c); })) return;  // separator row
        table = table || [];
        table.push(cells);
        return;
      }
      flushTable();
      html += line + "\n";
    });
    flushTable();

    html = html
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
    target.innerHTML = html;
  }

  function send(text) {
    if (state.busy) return;
    text = (text || $("chat-input").value).trim();
    if (!text) return;

    var suggestions = document.querySelector(".suggestions");
    if (suggestions) suggestions.remove();

    $("chat-input").value = "";
    $("chat-input").style.height = "auto";
    addMessage("user", text);

    state.busy = true;
    $("chat-send").disabled = true;

    var msg = el("div", "msg agent");
    msg.appendChild(el("div", "msg-role", "Profiler"));
    var tools = el("div", "toolcalls");
    var body = el("div", "msg-body");
    var pending = el("div", "toolcall");
    pending.appendChild(el("span", "spinner"));
    pending.appendChild(el("span", "name", "thinking"));
    msg.appendChild(tools);
    msg.appendChild(body);
    tools.appendChild(pending);
    $("chat-inner").appendChild(msg);
    scrollChat();

    var answer = "";
    var toolNodes = {};

    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: state.sessionId, run_id: state.runId })
    }).then(function (response) {
      if (!response.ok || !response.body) throw new Error("Request failed (" + response.status + ")");
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";

      function pump() {
        return reader.read().then(function (result) {
          if (result.done) return finish();
          buffer += decoder.decode(result.value, { stream: true });
          var chunks = buffer.split("\n\n");
          buffer = chunks.pop();
          chunks.forEach(handleChunk);
          return pump();
        });
      }

      function handleChunk(chunk) {
        var event = null, data = null;
        chunk.split("\n").forEach(function (line) {
          if (line.indexOf("event: ") === 0) event = line.slice(7).trim();
          else if (line.indexOf("data: ") === 0) data = line.slice(6);
        });
        if (!event || data == null) return;
        var payload;
        try { payload = JSON.parse(data); } catch (e) { return; }

        if (event === "session") {
          state.sessionId = payload.session_id;
        } else if (event === "tool_call") {
          if (pending.parentNode) pending.remove();
          var node = el("div", "toolcall");
          node.appendChild(el("span", "spinner"));
          node.appendChild(el("span", "name", payload.name));
          var args = Object.keys(payload.args || {})
            .filter(function (k) { return payload.args[k] !== "" && payload.args[k] !== 0 && payload.args[k] !== false; })
            .map(function (k) { return k + "=" + payload.args[k]; })
            .join("  ");
          node.appendChild(el("span", "args", args));
          node.appendChild(el("span", "state running", "running"));
          tools.appendChild(node);
          toolNodes[payload.name] = node;
          scrollChat();
        } else if (event === "tool_result") {
          var target = toolNodes[payload.name];
          if (target) {
            var spin = target.querySelector(".spinner");
            if (spin) spin.remove();
            var stateEl = target.querySelector(".state");
            var failed = payload.status === "error";
            stateEl.className = "state " + (failed ? "err" : "ok");
            stateEl.textContent = failed ? "failed" : "done";
            if (failed && payload.error) target.title = payload.error;
          }
          if (payload.run_id && payload.run_id !== state.runId) {
            openRun(payload.run_id).then(function () { selectTab("agent"); }).catch(function () {});
          }
        } else if (event === "text") {
          if (pending.parentNode) pending.remove();
          answer += payload.text;
          renderMarkdown(body, answer);
          scrollChat();
        } else if (event === "error") {
          if (pending.parentNode) pending.remove();
          var banner = el("div", "banner err");
          var inner = el("div");
          inner.appendChild(el("b", null, "The agent could not answer"));
          inner.appendChild(document.createTextNode(payload.message || "Unknown error."));
          if (payload.fix) {
            inner.appendChild(el("br"));
            inner.appendChild(el("code", null, payload.fix.replace(/^Run:\s*/, "")));
          }
          banner.appendChild(inner);
          body.appendChild(banner);
          scrollChat();
        }
      }

      function finish() {
        if (pending.parentNode) pending.remove();
        tools.querySelectorAll(".spinner").forEach(function (s) { s.remove(); });
        if (!answer && !body.firstChild) {
          body.appendChild(el("div", "empty", "No response."));
        }
        state.busy = false;
        $("chat-send").disabled = false;
        loadRuns();
        scrollChat();
      }

      return pump();
    }).catch(function (err) {
      if (pending.parentNode) pending.remove();
      var banner = el("div", "banner err");
      banner.appendChild(el("b", null, "Could not reach the server"));
      banner.appendChild(document.createTextNode(String(err.message || err)));
      body.appendChild(banner);
      state.busy = false;
      $("chat-send").disabled = false;
      scrollChat();
    });
  }

  $("chat-send").addEventListener("click", function () { send(); });
  var input = $("chat-input");
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  input.addEventListener("input", function () {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  });

  // ---------------- file upload + drag-and-drop ----------------
  var dropZone = $("drop-zone");
  var uploadInput = $("upload-input");

  function setDropState(cls, title, sub, status) {
    dropZone.className = "drop-zone" + (cls ? " " + cls : "");
    if (title != null) $("drop-title").textContent = title;
    if (sub != null)   $("drop-sub").innerHTML = sub;
    if (status != null) $("drop-status").textContent = status;
  }

  function uploadAndScan(file) {
    setDropState("uploading", "Uploading…", "", file.name);
    var form = new FormData();
    form.append("file", file);
    fetch("/api/upload", { method: "POST", body: form })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || r.statusText); });
        return r.json();
      })
      .then(function (data) {
        setDropState("done",
          data.filename,
          "Drop another file or <span class=\"drop-link\" id=\"drop-browse\">browse</span>",
          bytes(data.size_bytes) + " — starting scan…"
        );
        // Re-wire the new drop-browse span
        var nb = $("drop-browse");
        if (nb) nb.addEventListener("click", function () { uploadInput.click(); });
        // Set source and auto-start the scan
        $("source").value = data.path;
        setTimeout(runScan, 300);
      })
      .catch(function (err) {
        setDropState("",
          "Drop a file here to scan",
          "or <span class=\"drop-link\" id=\"drop-browse\">click to browse</span>",
          "Upload failed: " + (err.message || err)
        );
        var nb = $("drop-browse");
        if (nb) nb.addEventListener("click", function () { uploadInput.click(); });
      });
  }

  if (dropZone && uploadInput) {
    // The <label for="upload-input"> inside the drop-zone handles all clicks —
    // no JS .click() needed. Just handle the change event and drag-and-drop.
    uploadInput.addEventListener("change", function () {
      var file = uploadInput.files && uploadInput.files[0];
      if (file) uploadAndScan(file);
      uploadInput.value = "";
    });

    // Drag-and-drop (bypasses the label)
    dropZone.addEventListener("dragover", function (e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      dropZone.classList.add("drag-over");
    });
    ["dragleave", "dragend"].forEach(function (evt) {
      dropZone.addEventListener(evt, function () { dropZone.classList.remove("drag-over"); });
    });
    dropZone.addEventListener("drop", function (e) {
      e.preventDefault();
      dropZone.classList.remove("drag-over");
      var file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) uploadAndScan(file);
    });
  }

  // ---------------- boot ----------------
  fetch("/api/health")
    .then(function (r) { return r.json(); })
    .then(applyHealth)
    .then(loadRuns)
    .catch(function (err) {
      $("auth-text").textContent = "server unreachable";
      $("auth-pill").classList.add("bad");
      console.error(err);
    });
})();
