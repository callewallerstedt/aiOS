(() => {
  const COLORS = [
    "#e0a45a", "#6fbfa3", "#7eb6ff", "#e07a6a", "#c9a0ff",
    "#f0d76a", "#5fd0d0", "#ff8f6b", "#9ad37f", "#f5a3c7",
    "#8ec5ff", "#d4b483",
  ];

  const els = {
    apiKey: document.getElementById("apiKey"),
    toggleKey: document.getElementById("toggleKey"),
    pasteBtn: document.getElementById("pasteBtn"),
    fileInput: document.getElementById("fileInput"),
    clearImage: document.getElementById("clearImage"),
    dropzone: document.getElementById("dropzone"),
    stage: document.getElementById("stage"),
    emptyState: document.getElementById("emptyState"),
    imageMeta: document.getElementById("imageMeta"),
    runMeta: document.getElementById("runMeta"),
    target: document.getElementById("target"),
    fastMode: document.getElementById("fastMode"),
    runBtn: document.getElementById("runBtn"),
    refreshModels: document.getElementById("refreshModels"),
    selectDefaults: document.getElementById("selectDefaults"),
    clearModels: document.getElementById("clearModels"),
    modelSearch: document.getElementById("modelSearch"),
    modelSort: document.getElementById("modelSort"),
    maxInputPrice: document.getElementById("maxInputPrice"),
    selectCheaper: document.getElementById("selectCheaper"),
    modelCount: document.getElementById("modelCount"),
    modelList: document.getElementById("modelList"),
    results: document.getElementById("results"),
    summaryStats: document.getElementById("summaryStats"),
  };

  const state = {
    imageDataUrl: null,
    width: 0,
    height: 0,
    models: [],
    defaults: [],
    selected: new Set(),
    results: [],
    colorByModel: new Map(),
    running: false,
    filter: "vision",
    sort: "popular",
  };

  const ctx = els.stage.getContext("2d");

  function loadKey() {
    els.apiKey.value = localStorage.getItem("openrouter_api_key") || "";
  }

  function saveKey() {
    localStorage.setItem("openrouter_api_key", els.apiKey.value.trim());
  }

  function updateRunEnabled() {
    els.runBtn.disabled = !(
      state.imageDataUrl &&
      state.selected.size > 0 &&
      els.target.value.trim() &&
      els.apiKey.value.trim() &&
      !state.running
    );
    els.clearImage.disabled = !state.imageDataUrl;
    els.modelCount.textContent = `${state.selected.size} selected`;
  }

  function setImageFromFile(file) {
    if (!file || !file.type.startsWith("image/")) return;
    const reader = new FileReader();
    reader.onload = () => setImageDataUrl(reader.result);
    reader.readAsDataURL(file);
  }

  function setImageDataUrl(dataUrl) {
    const img = new Image();
    img.onload = () => {
      state.imageDataUrl = dataUrl;
      state.width = img.naturalWidth;
      state.height = img.naturalHeight;
      els.stage.width = state.width;
      els.stage.height = state.height;
      els.emptyState.classList.add("hidden");
      els.dropzone.classList.add("has-image");
      els.imageMeta.textContent = `${state.width}×${state.height}px`;
      state.results = [];
      renderResults();
      drawStage();
      updateRunEnabled();
    };
    img.src = dataUrl;
  }

  function clearImage() {
    state.imageDataUrl = null;
    state.width = 0;
    state.height = 0;
    state.results = [];
    ctx.clearRect(0, 0, els.stage.width, els.stage.height);
    els.stage.width = 1280;
    els.stage.height = 720;
    els.emptyState.classList.remove("hidden");
    els.dropzone.classList.remove("has-image");
    els.imageMeta.textContent = "No image";
    els.runMeta.textContent = "";
    renderResults();
    updateRunEnabled();
  }

  function drawStage() {
    if (!state.imageDataUrl) return;
    const img = new Image();
    img.onload = () => {
      ctx.clearRect(0, 0, state.width, state.height);
      ctx.drawImage(img, 0, 0);
      const okResults = state.results.filter(
        (r) => !r.disabled && r.ok && Number.isFinite(r.x) && Number.isFinite(r.y)
      );
      okResults.forEach((r, i) => drawMarker(r, colorFor(r.model), i + 1));
    };
    img.src = state.imageDataUrl;
  }

  function colorFor(model) {
    if (!state.colorByModel.has(model)) {
      state.colorByModel.set(model, COLORS[state.colorByModel.size % COLORS.length]);
    }
    return state.colorByModel.get(model);
  }

  function drawMarker(result, color, index) {
    const x = result.x;
    const y = result.y;
    const scale = Math.max(state.width, state.height) / 1200;
    const r = Math.max(8, 10 * scale);
    const arm = Math.max(14, 18 * scale);

    ctx.save();
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = Math.max(2, 2.5 * scale);

    // Crosshair
    ctx.beginPath();
    ctx.moveTo(x - arm, y);
    ctx.lineTo(x + arm, y);
    ctx.moveTo(x, y - arm);
    ctx.lineTo(x, y + arm);
    ctx.stroke();

    // Outer ring
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.stroke();

    // Center dot
    ctx.beginPath();
    ctx.arc(x, y, Math.max(2.5, 3 * scale), 0, Math.PI * 2);
    ctx.fill();

    // Label bubble
    const label = String(index);
    ctx.font = `600 ${Math.max(11, 12 * scale)}px IBM Plex Mono, monospace`;
    const padX = 6 * scale;
    const padY = 4 * scale;
    const tw = ctx.measureText(label).width;
    const bx = x + r + 4 * scale;
    const by = y - r - 4 * scale;
    ctx.fillStyle = "rgba(10,12,14,0.82)";
    roundRect(ctx, bx, by - 12 * scale, tw + padX * 2, 16 * scale, 6);
    ctx.fill();
    ctx.fillStyle = color;
    ctx.fillText(label, bx + padX, by);
    ctx.restore();
  }

  function roundRect(c, x, y, w, h, radius) {
    c.beginPath();
    c.moveTo(x + radius, y);
    c.arcTo(x + w, y, x + w, y + h, radius);
    c.arcTo(x + w, y + h, x, y + h, radius);
    c.arcTo(x, y + h, x, y, radius);
    c.arcTo(x, y, x + w, y, radius);
    c.closePath();
  }

  async function fetchModels() {
    els.modelList.innerHTML = `<div class="loading">Loading OpenRouter models…</div>`;
    try {
      const headers = {};
      const key = els.apiKey.value.trim();
      if (key) headers["X-OpenRouter-Key"] = key;
      const res = await fetch("/api/models", { headers });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "Failed to load models");
      state.models = data.models || [];
      state.defaults = data.defaults || [];
      if (state.selected.size === 0) {
        state.defaults.forEach((id) => state.selected.add(id));
      }
      renderModels();
      updateRunEnabled();
    } catch (err) {
      els.modelList.innerHTML = `<div class="loading">Could not load models: ${escapeHtml(err.message)}</div>`;
    }
  }

  function sortedFilteredModels() {
    const q = els.modelSearch.value.trim().toLowerCase();
    let list = state.models.filter((m) => {
      if (state.filter === "vision" && !m.vision) return false;
      if (state.filter === "selected" && !state.selected.has(m.id)) return false;
      if (!q) return true;
      return (m.id || "").toLowerCase().includes(q) || (m.name || "").toLowerCase().includes(q);
    });

    const prompt = (m) => Number(m.pricing?.prompt || 0);
    const completion = (m) => Number(m.pricing?.completion || 0);
    const sort = state.sort || "popular";

    list = [...list].sort((a, b) => {
      if (sort === "input-asc") return prompt(a) - prompt(b) || (a.rank ?? 0) - (b.rank ?? 0);
      if (sort === "input-desc") return prompt(b) - prompt(a) || (a.rank ?? 0) - (b.rank ?? 0);
      if (sort === "output-asc") return completion(a) - completion(b) || (a.rank ?? 0) - (b.rank ?? 0);
      if (sort === "output-desc") return completion(b) - completion(a) || (a.rank ?? 0) - (b.rank ?? 0);
      if (sort === "name") return String(a.name || a.id).localeCompare(String(b.name || b.id));
      // popular = OpenRouter top-weekly rank
      return (a.rank ?? 0) - (b.rank ?? 0);
    });
    return list;
  }

  function renderModels() {
    const filtered = sortedFilteredModels();

    if (!filtered.length) {
      els.modelList.innerHTML = `<div class="loading">No models match. Try All filter or clear search.</div>`;
      return;
    }

    els.modelList.innerHTML = "";
    for (const model of filtered) {
      const item = document.createElement("label");
      item.className = "model-item" + (state.selected.has(model.id) ? " selected" : "");
      const prompt = model.pricing?.prompt ?? 0;
      const completion = model.pricing?.completion ?? 0;
      const badge = model.vision
        ? `<span class="badge vision">vision</span>`
        : `<span class="badge text">text</span>`;
      item.innerHTML = `
        <input type="checkbox" ${state.selected.has(model.id) ? "checked" : ""} />
        <div>
          <div class="name">${escapeHtml(model.name || model.id)}${badge}</div>
          <div class="id">${escapeHtml(model.id)}</div>
        </div>
        <div class="price" title="Input / output per 1M tokens">
          ${formatPrice(prompt)} in<br>${formatPrice(completion)} out
        </div>
      `;
      const checkbox = item.querySelector("input");
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.selected.add(model.id);
        else state.selected.delete(model.id);
        item.classList.toggle("selected", checkbox.checked);
        updateRunEnabled();
        if (state.filter === "selected") renderModels();
      });
      els.modelList.appendChild(item);
    }
  }

  function formatPrice(perToken) {
    if (!perToken && perToken !== 0) return "—";
    const perM = Number(perToken) * 1_000_000;
    if (perM === 0) return "$0";
    if (perM < 0.01) return `$${perM.toFixed(4)}`;
    if (perM < 1) return `$${perM.toFixed(3)}`;
    return `$${perM.toFixed(2)}`;
  }

  function formatUsd(amount) {
    if (amount == null || !Number.isFinite(amount)) return "—";
    if (amount === 0) return "$0";
    if (amount < 0.000001) return `$${amount.toExponential(2)}`;
    if (amount < 0.01) return `$${amount.toFixed(6)}`;
    if (amount < 1) return `$${amount.toFixed(4)}`;
    return `$${amount.toFixed(3)}`;
  }

  function modelById(id) {
    return state.models.find((m) => m.id === id) || null;
  }

  function estimateRunCost(result) {
    if (result?.usage?.cost != null && Number.isFinite(Number(result.usage.cost))) {
      return Number(result.usage.cost);
    }
    const usage = result?.usage || {};
    const promptTokens = Number(usage.prompt_tokens);
    const completionTokens = Number(usage.completion_tokens);
    if (!Number.isFinite(promptTokens) && !Number.isFinite(completionTokens)) return null;
    const model = modelById(result.model);
    if (!model?.pricing) return null;
    const promptRate = Number(model.pricing.prompt || 0);
    const completionRate = Number(model.pricing.completion || 0);
    const p = Number.isFinite(promptTokens) ? promptTokens : 0;
    const c = Number.isFinite(completionTokens) ? completionTokens : 0;
    return p * promptRate + c * completionRate;
  }

  function selectCheaperThan(maxPerMillion) {
    if (!Number.isFinite(maxPerMillion) || maxPerMillion < 0) {
      return { count: 0, total: 0, capped: false };
    }
    const maxPerToken = maxPerMillion / 1_000_000;
    // Use current modality scope (vision/all). Ignore search text and "selected" filter.
    const pool = state.models.filter((m) => (state.filter === "all" ? true : !!m.vision));
    // Strictly cheaper than the threshold, so pasting Luna's rate excludes Luna itself.
    const cheaper = pool.filter((m) => Number(m.pricing?.prompt || 0) < maxPerToken);
    // Cap to run limit; keep most popular first.
    const ordered = [...cheaper].sort((a, b) => (a.rank ?? 0) - (b.rank ?? 0));
    const capped = ordered.length > 24;
    state.selected = new Set((capped ? ordered.slice(0, 24) : ordered).map((m) => m.id));
    return { count: state.selected.size, total: cheaper.length, capped };
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function disableResult(modelId) {
    const result = state.results.find((r) => r.model === modelId);
    if (result) result.disabled = true;
    state.selected.delete(modelId);
    renderModels();
    renderResults();
    drawStage();
    updateRunEnabled();
  }

  function renderResults() {
    const visible = state.results.filter((r) => !r.disabled);
    if (!visible.length) {
      els.results.innerHTML = state.results.length
        ? `<div class="empty-inline">All results disabled. Run again or re-select models.</div>`
        : `<div class="empty-inline">Run a comparison to see latency, coords, and click markers.</div>`;
      const hidden = state.results.filter((r) => r.disabled).length;
      els.summaryStats.innerHTML = hidden
        ? `<span>${hidden} disabled</span>`
        : "";
      return;
    }

    const done = visible.filter((r) => r.status !== "pending");
    const ok = done.filter((r) => r.ok);
    const latencies = ok.map((r) => r.latency_ms).filter((n) => Number.isFinite(n));
    const avg = latencies.length
      ? Math.round(latencies.reduce((a, b) => a + b, 0) / latencies.length)
      : null;
    const hidden = state.results.filter((r) => r.disabled).length;
    const costs = done.map(estimateRunCost).filter((n) => n != null && Number.isFinite(n));
    const totalCost = costs.length ? costs.reduce((a, b) => a + b, 0) : null;

    els.summaryStats.innerHTML = `
      <span>${ok.length}/${done.length} ok</span>
      ${avg != null ? `<span>avg ${avg} ms</span>` : ""}
      ${latencies.length ? `<span>fastest ${Math.min(...latencies)} ms</span>` : ""}
      ${totalCost != null ? `<span>total ${formatUsd(totalCost)}</span>` : ""}
      ${hidden ? `<span>${hidden} disabled</span>` : ""}
    `;

    // Keep visual order stable: selected model order, then arrival extras.
    const order = [...state.selected];
    const sorted = [...visible].sort((a, b) => {
      const ai = order.indexOf(a.model);
      const bi = order.indexOf(b.model);
      return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
    });

    els.results.innerHTML = "";
    sorted.forEach((result, idx) => {
      const color = colorFor(result.model);
      const card = document.createElement("article");
      const klass = result.status === "pending" ? "pending" : result.ok ? "ok" : "err";
      card.className = `card ${klass}`;

      const disableBtn = result.status === "pending"
        ? ""
        : `<button type="button" class="ghost danger disable-btn" data-model="${escapeHtml(result.model)}">Disable</button>`;

      if (result.status === "pending") {
        card.innerHTML = `
          <div class="card-top">
            <div class="card-title">${escapeHtml(result.model)}</div>
            <div class="card-swatch" style="background:${color}"></div>
          </div>
          <div class="card-error" style="color:var(--muted)">Running…</div>
        `;
      } else if (!result.ok) {
        const cost = estimateRunCost(result);
        card.innerHTML = `
          <div class="card-top">
            <div class="card-title">${escapeHtml(result.model)}</div>
            <div class="card-actions">
              ${disableBtn}
              <div class="card-swatch" style="background:${color}"></div>
            </div>
          </div>
          <div class="card-grid">
            <div><span class="k">Latency</span>${result.latency_ms ?? "—"} ms</div>
            <div><span class="k">Cost</span><span class="cost-value">${formatUsd(cost)}</span></div>
            <div><span class="k">Status</span>error</div>
            <div><span class="k">Tokens</span>${formatTokens(result.usage)}</div>
          </div>
          <div class="card-error" style="margin-top:10px">${escapeHtml(result.error || "Unknown error")}</div>
          ${result.raw ? `<div class="card-raw">${escapeHtml(result.raw)}</div>` : ""}
        `;
      } else {
        const markerIndex = ok.findIndex((r) => r.model === result.model) + 1;
        const cost = estimateRunCost(result);
        card.innerHTML = `
          <div class="card-top">
            <div class="card-title">#${markerIndex || idx + 1} · ${escapeHtml(result.model)}</div>
            <div class="card-actions">
              ${disableBtn}
              <div class="card-swatch" style="background:${color}"></div>
            </div>
          </div>
          <div class="card-grid">
            <div><span class="k">Click</span>(${result.x}, ${result.y})</div>
            <div><span class="k">Cost</span><span class="cost-value">${formatUsd(cost)}</span></div>
            <div><span class="k">Latency</span>${result.latency_ms} ms</div>
            <div><span class="k">Tokens</span>${formatTokens(result.usage)}</div>
          </div>
          ${result.label ? `<div class="card-raw">${escapeHtml(result.label)}</div>` : ""}
          ${result.raw ? `<div class="card-raw">${escapeHtml(result.raw)}</div>` : ""}
        `;
      }

      const btn = card.querySelector(".disable-btn");
      if (btn) {
        btn.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          disableResult(result.model);
        });
      }
      els.results.appendChild(card);
    });
  }

  function formatTokens(usage) {
    if (!usage) return "—";
    const total = usage.total_tokens;
    if (total != null) return String(total);
    const p = usage.prompt_tokens;
    const c = usage.completion_tokens;
    if (p != null || c != null) return `${p ?? "?"}→${c ?? "?"}`;
    return "—";
  }

  async function runComparison() {
    saveKey();
    const apiKey = els.apiKey.value.trim();
    const target = els.target.value.trim();
    const models = [...state.selected];
    if (!apiKey || !target || !state.imageDataUrl || !models.length) return;

    state.running = true;
    state.colorByModel = new Map();
    state.results = models.map((model) => ({ model, status: "pending", ok: false }));
    els.runMeta.textContent = `Running ${models.length} models…`;
    updateRunEnabled();
    renderResults();
    drawStage();

    try {
      const res = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api_key: apiKey,
          image: state.imageDataUrl,
          target,
          models,
          width: state.width,
          height: state.height,
          fast: !!els.fastMode?.checked,
        }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || `HTTP ${res.status}`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() || "";
        for (const chunk of chunks) {
          const line = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          const payload = JSON.parse(line.slice(6));
          if (payload.type === "result" && payload.result) {
            upsertResult(payload.result);
            els.runMeta.textContent = `${payload.index}/${payload.total} finished`;
            renderResults();
            drawStage();
          } else if (payload.type === "done") {
            els.runMeta.textContent = `Done · ${payload.total} models`;
          }
        }
      }
    } catch (err) {
      els.runMeta.textContent = `Failed: ${err.message}`;
      alert(err.message);
    } finally {
      state.running = false;
      updateRunEnabled();
    }
  }

  function upsertResult(result) {
    const idx = state.results.findIndex((r) => r.model === result.model);
    const next = { ...result, status: "done" };
    if (idx >= 0) state.results[idx] = next;
    else state.results.push(next);
  }

  async function pasteFromClipboard() {
    try {
      const items = await navigator.clipboard.read();
      for (const item of items) {
        const type = item.types.find((t) => t.startsWith("image/"));
        if (!type) continue;
        const blob = await item.getType(type);
        setImageFromFile(new File([blob], "clipboard.png", { type }));
        return;
      }
      alert("No image found on clipboard.");
    } catch (err) {
      alert("Clipboard paste blocked. Focus the page and try Ctrl+V, or upload a file.");
      console.warn(err);
    }
  }

  // Events
  els.apiKey.addEventListener("input", () => {
    saveKey();
    updateRunEnabled();
  });
  els.toggleKey.addEventListener("click", () => {
    const show = els.apiKey.type === "password";
    els.apiKey.type = show ? "text" : "password";
    els.toggleKey.textContent = show ? "Hide" : "Show";
  });
  els.pasteBtn.addEventListener("click", pasteFromClipboard);
  els.fileInput.addEventListener("change", () => {
    const file = els.fileInput.files?.[0];
    if (file) setImageFromFile(file);
    els.fileInput.value = "";
  });
  els.clearImage.addEventListener("click", clearImage);
  els.target.addEventListener("input", updateRunEnabled);
  els.runBtn.addEventListener("click", runComparison);
  els.refreshModels.addEventListener("click", fetchModels);
  els.selectDefaults.addEventListener("click", () => {
    state.selected = new Set(state.defaults);
    renderModels();
    updateRunEnabled();
  });
  els.clearModels.addEventListener("click", () => {
    state.selected.clear();
    renderModels();
    updateRunEnabled();
  });
  els.selectCheaper.addEventListener("click", () => {
    const max = Number(els.maxInputPrice.value);
    if (!Number.isFinite(max) || max <= 0) {
      alert("Enter a max input price in $/M tokens (e.g. 0.10 for Luna).");
      return;
    }
    const result = selectCheaperThan(max);
    renderModels();
    updateRunEnabled();
    const scope = state.filter === "all" ? "all" : "vision";
    if (result.capped) {
      els.runMeta.textContent = `Selected cheapest 24 of ${result.total} ${scope} models under $${max}/M input`;
    } else {
      els.runMeta.textContent = `Selected ${result.count} ${scope} models under $${max}/M input`;
    }
  });
  els.maxInputPrice.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      els.selectCheaper.click();
    }
  });
  els.modelSearch.addEventListener("input", renderModels);
  els.modelSort.addEventListener("change", () => {
    state.sort = els.modelSort.value;
    renderModels();
  });
  document.querySelectorAll(".seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.filter = btn.dataset.filter || "vision";
      renderModels();
    });
  });

  window.addEventListener("paste", (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.startsWith("image/")) {
        e.preventDefault();
        setImageFromFile(item.getAsFile());
        return;
      }
    }
  });

  ["dragenter", "dragover"].forEach((evt) => {
    els.dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      els.dropzone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((evt) => {
    els.dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      els.dropzone.classList.remove("dragover");
    });
  });
  els.dropzone.addEventListener("drop", (e) => {
    const file = e.dataTransfer?.files?.[0];
    if (file) setImageFromFile(file);
  });

  loadKey();
  updateRunEnabled();
  fetchModels();
})();
