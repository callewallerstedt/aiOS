"use strict";
/* Operator flow viewer.
 * Consumes the operator event stream (SSE) and renders:
 *   left  — scrollable gallery of screenshots + LocateAnything outputs
 *   right — live activity feed (steps, thoughts, actions, la_click details)
 */
(function () {
  const galleryList = document.getElementById("op-gallery-list");
  const galleryEmpty = document.getElementById("op-gallery-empty");
  const feedList = document.getElementById("op-feed-list");
  const stepChip = document.getElementById("op-step");
  const stateChip = document.getElementById("op-state");
  const followBox = document.getElementById("op-follow");
  const countEl = document.getElementById("op-count");
  const clearBtn = document.getElementById("op-clear-gallery");
  const lightbox = document.getElementById("op-lightbox");
  const lightboxImg = document.getElementById("op-lightbox-img");
  const lightboxCap = document.getElementById("op-lightbox-cap");

  let feedCount = 0;
  let galleryCount = 0;

  const frameUrl = (id) => `/api/phone/operator/frame/${id}`;

  function autoScroll(el) {
    if (followBox.checked) el.scrollTop = el.scrollHeight;
  }

  function clearAll() {
    galleryList.innerHTML = "";
    feedList.innerHTML = "";
    feedCount = 0;
    galleryCount = 0;
    galleryEmpty.style.display = "";
    countEl.textContent = "";
  }

  function openLightbox(src, caption) {
    lightboxImg.src = src;
    lightboxCap.textContent = caption || "";
    lightbox.classList.remove("hidden");
  }
  lightbox.addEventListener("click", () => lightbox.classList.add("hidden"));

  // ---------- gallery ----------
  function addGalleryCard({ frame, tag, tagClass, desc, meta, sub, cardClass }) {
    galleryEmpty.style.display = "none";
    const card = document.createElement("div");
    card.className = "gcard" + (cardClass ? " " + cardClass : "");

    const cap = document.createElement("div");
    cap.className = "gcard-cap";
    if (tag) {
      const t = document.createElement("span");
      t.className = "tag " + (tagClass || "");
      t.textContent = tag;
      cap.appendChild(t);
    }
    if (desc) {
      const d = document.createElement("span");
      d.className = "desc";
      d.textContent = desc;
      cap.appendChild(d);
    }
    if (meta) {
      const m = document.createElement("span");
      m.className = "meta";
      m.textContent = meta;
      cap.appendChild(m);
    }
    card.appendChild(cap);

    if (frame != null) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.src = frameUrl(frame);
      const capText = [tag, desc, meta].filter(Boolean).join("  ·  ");
      img.addEventListener("click", () => openLightbox(img.src, capText));
      card.appendChild(img);
    }
    if (sub) {
      const s = document.createElement("div");
      s.className = "gcard-sub";
      s.textContent = sub;
      card.appendChild(s);
    }

    galleryList.appendChild(card);
    galleryCount += 1;
    autoScroll(galleryList);
  }

  // ---------- feed ----------
  function addFeed(node) {
    feedList.appendChild(node);
    feedCount += 1;
    countEl.textContent = `${feedCount} events`;
    autoScroll(feedList);
  }
  function feedDiv(cls, html) {
    const d = document.createElement("div");
    d.className = cls;
    d.innerHTML = html;
    addFeed(d);
    return d;
  }
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const tokens = (n) => Number(n || 0).toLocaleString();

  function usageHtml(ev) {
    const u = ev.usage || {};
    if (!u.requests && !u.total_tokens && !u.input_tokens && !u.output_tokens) return "";
    const input = Number(u.input_tokens || 0);
    const cached = Math.min(Number(u.cached_input_tokens || 0), input);
    const cacheWrite = Math.min(Number(u.cache_write_input_tokens || 0), Math.max(0, input - cached));
    const fresh = Math.max(0, input - cached - cacheWrite);
    const output = Number(u.output_tokens || 0);
    const total = Number(u.total_tokens || input + output);
    const c = ev.cost || {};
    const usd = Number(c.usd_exact || c.usd || 0).toFixed(9);
    let bill = `$${usd} API charge`;
    if (Number(c.plan_requests || 0)) {
      const plan = c.plan_type
        ? `ChatGPT ${String(c.plan_type).replace(/^./, (x) => x.toUpperCase())} plan`
        : "ChatGPT plan";
      bill += ` · ${tokens(c.plan_tokens)} tokens on ${esc(plan)}`;
    }
    if ((c.unpriced || []).length) bill += ` · no price set for ${esc(c.unpriced.join(", "))}`;
    return (
      `<div class="usage"><div><b>TOKENS</b> ${tokens(input)} input ` +
      `(${tokens(fresh)} fresh, ${tokens(cached)} cached, ${tokens(cacheWrite)} cache-write) + ` +
      `${tokens(output)} output = ${tokens(total)} total · ${tokens(u.requests)} call(s)</div>` +
      `<div><b>COST</b> ${bill}</div></div>`
    );
  }

  function setState(cls, text) {
    stateChip.className = "op-chip " + cls;
    stateChip.textContent = text;
  }

  // ---------- event dispatch ----------
  function handle(ev) {
    const t = ev.type;
    switch (t) {
      case "run_start":
      case "cleared":
        clearAll();
        if (t === "run_start") setState("state-running", "running");
        break;

      case "step_begin":
        stepChip.textContent = "step " + (ev.n ?? "—");
        setState("state-running", "running");
        feedDiv("fitem fstep", "Step " + esc(ev.n));
        break;

      case "screenshot":
        if (ev.frame != null) {
          addGalleryCard({
            frame: ev.frame, tag: "screen", tagClass: "shot",
            desc: "what the brain sees", cardClass: "shot",
          });
        }
        break;

      case "thought": {
        let html = "";
        if (ev.thought) html += `<div class="fthought">${esc(ev.thought)}</div>`;
        if (ev.say) html += `<div class="fsay">🔊 ${esc(ev.say)}</div>`;
        if (ev.message) html += `<div class="fmsg">${esc(ev.message)}</div>`;
        html += `<div class="op-muted">${esc(ev.actions || 0)} action(s) · status=${esc(ev.status)} · ${esc(ev.elapsed_ms)}ms</div>`;
        feedDiv("fitem", html);
        break;
      }

      case "la_click_begin":
        feedDiv("fitem fla",
          `<div class="head">🔎 Locate: ${esc(ev.description)}</div>` +
          `<div class="sub">region ${esc(ev.region)} — sending crop to LocateAnything…</div>`);
        if (ev.crop_frame != null) {
          addGalleryCard({
            frame: ev.crop_frame, tag: "region", tagClass: "la",
            desc: ev.description, meta: ev.region,
            sub: "Region crop sent to LocateAnything",
            cardClass: "la",
          });
        }
        break;

      case "la_click_result": {
        const ok = !!ev.ok;
        const src = ev.source || "";
        let tag, tagClass, cardClass, head;
        if (ok && src === "locate_anything") {
          tag = "LA ✓"; tagClass = "la"; cardClass = "la";
          head = "LocateAnything found it";
        } else if (ok && src === "gpt5.5_fallback") {
          tag = "GPT-5.5 ⤷"; tagClass = "fallback"; cardClass = "la fallback";
          head = "LocateAnything missed → GPT-5.5 fallback found it";
        } else {
          tag = "miss ✗"; tagClass = "miss"; cardClass = "la miss";
          head = "Not found by LocateAnything or GPT-5.5";
        }
        const coords = (ev.x != null && ev.y != null) ? `(${ev.x}, ${ev.y})` : "—";
        feedDiv("fitem fla " + (cardClass.includes("fallback") ? "fallback" : cardClass.includes("miss") ? "miss" : ""),
          `<div class="head">${esc(head)}</div>` +
          `<div class="sub">${esc(ev.description)} · ${coords} · ${esc(ev.elapsed_ms)}ms` +
          (ev.detail ? ` · ${esc(ev.detail)}` : "") + `</div>`);
        if (ev.annotated_frame != null) {
          addGalleryCard({
            frame: ev.annotated_frame, tag, tagClass, cardClass,
            desc: ev.description,
            meta: `${ev.region || ""} · ${coords}`,
            sub: head + (ev.answer ? `  —  ${ev.answer}` : ""),
          });
        }
        break;
      }

      case "action_done": {
        const ok = !!ev.ok;
        feedDiv("fitem faction",
          `<span class="${ok ? "ok" : "err"}">${ok ? "✓" : "✗"}</span> ` +
          `<span class="name">${esc(ev.action || "?")}</span> ` +
          `<span class="detail">${esc(ev.detail || "")}${ev.elapsed_ms != null ? " (" + ev.elapsed_ms + "ms)" : ""}</span>`);
        break;
      }

      case "ask":
        setState("state-asking", "asking");
        feedDiv("fitem fmsg", "❓ " + esc(ev.message));
        break;

      case "done":
        setState(ev.ok ? "state-done" : "state-fail", ev.ok ? "done" : "failed");
        feedDiv("fdone " + (ev.ok ? "ok" : "fail"),
          (ev.ok ? "✓ DONE" : "✗ ENDED") +
          ` · steps ${esc(ev.steps)} · ${esc(ev.message || "")}` + usageHtml(ev));
        break;

      case "log":
        if (ev.msg) feedDiv("fitem flog", esc(ev.msg));
        break;

      default:
        break;
    }
  }

  clearBtn.addEventListener("click", clearAll);

  // ---------- connect ----------
  function connect() {
    const src = new EventSource("/api/phone/operator/events");
    src.addEventListener("reset", clearAll);
    src.onmessage = (e) => {
      if (!e.data) return;
      let ev;
      try { ev = JSON.parse(e.data); } catch (_) { return; }
      try { handle(ev); } catch (err) { console.error("handle", err, ev); }
    };
    src.onerror = () => { /* EventSource auto-reconnects */ };
  }

  // Replay any existing events on load (so a refresh keeps history), then stream.
  fetch("/api/phone/operator/log")
    .then((r) => r.json())
    .then((data) => {
      (data.events || []).forEach((ev) => { try { handle(ev); } catch (_) {} });
    })
    .catch(() => {})
    .finally(connect);
})();
