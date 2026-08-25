// Live reload for the aiOS GUI (opt-in; not started by default).
//
// Disabled because CODE editing the GUI mid-session was reloading the running
// window and crashing it. Keep this module for deliberate GUI iteration —
// call startLiveReload() from app.js when you want it.
//
// When enabled: the backend watches web/*.{html,css,js}. CSS is hot-swapped by
// bumping its query string; HTML/JS takes a real reload.

import { stream } from "./bridge.js";

function swapStylesheets() {
  const stamp = Date.now();
  for (const link of document.querySelectorAll('link[rel="stylesheet"]')) {
    const url = new URL(link.href, location.href);
    url.searchParams.set("v", stamp);
    // Swapping href on the live element makes the browser fetch the new sheet
    // and cross-fade it in; removing and re-adding would flash unstyled.
    link.href = url.pathname + url.search;
  }
}

function flash(message) {
  const node = document.createElement("div");
  node.className = "toast show";
  node.style.cssText = "bottom:14px;font-size:10px;opacity:.85";
  node.textContent = message;
  document.body.appendChild(node);
  setTimeout(() => node.remove(), 1400);
}

export function startLiveReload() {
  stream(() => "/sse/dev/reload", {
    reload: (payload) => {
      if (payload.cssOnly) {
        swapStylesheets();
        flash(`styles reloaded · ${(payload.files || []).join(", ")}`);
        return;
      }
      location.reload();
    },
  });
}
