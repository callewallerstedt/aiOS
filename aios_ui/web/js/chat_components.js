// Shared CODE/agent-chat UI primitives. These functions own the DOM shape;
// callers only provide selectors and labels for their different controllers.

export function promptShellMarkup({
  shellAttr = "",
  plusAttr = "",
  inputAttr = "",
  configAttr = "",
  configNameAttr = "",
  dictateAttr = "",
  sendAttr = "",
  placeholder = "Write a message&hellip;",
  configName = "Configuration",
  configLabel = "Choose configuration",
  sendTitle = "Send",
} = {}) {
  return `<div class="prompt-shell" ${shellAttr}>
    <div class="prompt-controls">
      <button type="button" class="prompt-icon-button prompt-plus" ${plusAttr} aria-label="Add attachments and sources" aria-expanded="false">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"></path></svg>
      </button>
      <textarea ${inputAttr} rows="1" placeholder="${placeholder}" aria-label="Prompt"></textarea>
      <button type="button" class="prompt-config-button" ${configAttr} aria-label="${configLabel}" aria-expanded="false">
        <span ${configNameAttr}>${configName}</span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"></path></svg>
      </button>
      <button type="button" class="prompt-icon-button prompt-dictate" ${dictateAttr} aria-label="Start dictation" title="Use aiOS dictation">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3"></path></svg>
      </button>
      <button type="button" class="prompt-icon-button prompt-send" ${sendAttr} aria-label="Send" title="${sendTitle}">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"></path></svg>
      </button>
    </div>
  </div>`;
}

export function promptConfigRowMarkup({ label, hint = "", selected = false, attrs = "" }) {
  return `<button type="button" class="prompt-config-row${selected ? " selected" : ""}" ${attrs}>
    <span><strong>${label}</strong>${hint ? `<small>${hint}</small>` : ""}</span>
    <svg class="prompt-config-check${selected ? "" : " hidden"}" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5"></path></svg>
  </button>`;
}

/** The shared CODE/agent composer expansion and height contract. */

// Below this width the one-row grid leaves the textarea only a sliver, so the
// stacked layout (text on its own row, controls underneath) kicks in instead.
const NARROW_SHELL_PX = 320;

// Width changes (sidebar drag, window resize) must re-run the sizing, or the
// textarea keeps its stale one-line height and grows a scrollbar.
const shellWatchers = new WeakMap();

export function autosizePromptShell(shell, input, maxHeight = 180) {
  if (!input) return;
  if (shell && typeof ResizeObserver === "function" && !shellWatchers.has(shell)) {
    let lastWidth = -1;
    const observer = new ResizeObserver((entries) => {
      const width = Math.round(entries[0].contentRect.width);
      if (width !== lastWidth) {
        lastWidth = width;
        autosizePromptShell(shell, input, maxHeight);
      }
    });
    observer.observe(shell);
    shellWatchers.set(shell, observer);
  }
  const style = getComputedStyle(input);
  const oneLine = (parseFloat(style.lineHeight) || 18)
    + parseFloat(style.paddingTop || 0) + parseFloat(style.paddingBottom || 0);
  const narrow = Boolean(shell && shell.clientWidth > 0 && shell.clientWidth < NARROW_SHELL_PX);
  let needsExpand = narrow || input.value.includes("\n");
  if (shell && !needsExpand) {
    const wasExpanded = shell.classList.contains("expanded");
    if (wasExpanded) shell.classList.remove("expanded");
    input.style.height = "auto";
    needsExpand = input.scrollHeight > oneLine + 2;
    if (wasExpanded && needsExpand) shell.classList.add("expanded");
  }
  shell?.classList.toggle("expanded", needsExpand);
  input.style.height = "auto";
  input.style.height = `${Math.min(maxHeight, input.scrollHeight)}px`;
}
