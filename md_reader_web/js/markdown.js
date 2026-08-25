// Standalone markdown → HTML for the aiOS reader.
// Same core grammar as aios_ui/web/js/markdown.js, plus heading ids for TOC.

const INLINE = /(?<code>`[^`]+`)|(?<link>\[[^\]]+\]\([^)]+\))|(?<bold>\*\*[^*]+\*\*|__[^_]+__)|(?<italic>\*[^*]+\*|_[^_]+_)/g;

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
}

function slugify(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/<[^>]+>/g, "")
    .replace(/&[a-z]+;/g, "")
    .replace(/[^a-z0-9\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-")
    .slice(0, 80) || "section";
}

function inlineHtml(text) {
  const source = String(text ?? "");
  let html = "";
  let cursor = 0;
  INLINE.lastIndex = 0;
  let match;
  while ((match = INLINE.exec(source)) !== null) {
    if (match.index > cursor) html += escapeHtml(source.slice(cursor, match.index));
    const groups = match.groups;
    if (groups.code) {
      html += `<code>${escapeHtml(groups.code.slice(1, -1))}</code>`;
    } else if (groups.link) {
      const split = groups.link.slice(1).indexOf("](");
      const label = groups.link.slice(1, split + 1);
      const target = groups.link.slice(split + 3, -1);
      const href = escapeHtml(target);
      html += `<a href="${href}" data-link="${href}">${escapeHtml(label)}</a>`;
    } else if (groups.bold) {
      html += `<strong>${escapeHtml(groups.bold.slice(2, -2))}</strong>`;
    } else {
      const raw = groups.italic;
      html += `<em>${escapeHtml(raw.slice(1, -1))}</em>`;
    }
    cursor = match.index + match[0].length;
  }
  if (cursor < source.length) html += escapeHtml(source.slice(cursor));
  return html;
}

export function renderMarkdown(text) {
  const lines = String(text ?? "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
  const out = [];
  const toc = [];
  const usedIds = new Map();
  let fence = null;
  let buffer = [];
  let list = null;
  let paragraph = [];

  const uniqueId = (label) => {
    const base = slugify(label);
    const count = usedIds.get(base) || 0;
    usedIds.set(base, count + 1);
    return count ? `${base}-${count + 1}` : base;
  };

  const closeParagraph = () => {
    if (!paragraph.length) return;
    out.push(`<p>${paragraph.join("<br>")}</p>`);
    paragraph = [];
  };
  const closeList = () => {
    if (!list) return;
    out.push(list === "ul" ? "</ul>" : "</ol>");
    list = null;
  };
  const flush = () => {
    closeParagraph();
    closeList();
  };
  const openList = (kind) => {
    closeParagraph();
    if (list === kind) return;
    closeList();
    list = kind;
    out.push(kind === "ul" ? "<ul>" : "<ol>");
  };
  const emitCode = (lang) => {
    const cls = lang ? ` class="language-${escapeHtml(lang)}"` : "";
    out.push(`<pre><code${cls}>${escapeHtml(buffer.join("\n"))}</code></pre>`);
    buffer = [];
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const stripped = line.trim();

    if (stripped.startsWith("```") || stripped.startsWith("~~~")) {
      const marker = stripped.slice(0, 3);
      if (fence === null) {
        flush();
        fence = marker;
        buffer = [];
        buffer._lang = stripped.slice(3).trim();
      } else if (stripped.startsWith(fence)) {
        emitCode(buffer._lang || "");
        fence = null;
      } else {
        buffer.push(line);
      }
      continue;
    }
    if (fence !== null) {
      buffer.push(line);
      continue;
    }
    if (!stripped) {
      flush();
      continue;
    }

    if (stripped.includes("|") && index + 1 < lines.length) {
      const header = stripped.replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
      const divider = lines[index + 1].trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
      if (header.length >= 2 && divider.length === header.length && divider.every((cell) => /^:?-{3,}:?$/.test(cell))) {
        flush();
        const rows = [header];
        index += 2;
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
          const row = lines[index].trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
          while (row.length < header.length) row.push("");
          rows.push(row.slice(0, header.length));
          index += 1;
        }
        index -= 1;
        const head = rows[0].map((cell) => `<th>${inlineHtml(cell)}</th>`).join("");
        const body = rows.slice(1)
          .map((row) => `<tr>${row.map((cell) => `<td>${inlineHtml(cell)}</td>`).join("")}</tr>`)
          .join("");
        out.push(`<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`);
        continue;
      }
    }

    const heading = /^(#{1,6})\s+(.*?)(?:\s+#*)?$/.exec(stripped);
    if (heading) {
      flush();
      const level = Math.min(6, heading[1].length);
      const label = heading[2].trim();
      const id = uniqueId(label);
      toc.push({ id, level, text: label.replace(/[*_`\[\]]/g, "") });
      out.push(`<h${level} id="${id}">${inlineHtml(label)}</h${level}>`);
      continue;
    }
    if (/^([-*_])\s*\1\s*\1[-*_\s]*$/.test(stripped)) {
      flush();
      out.push("<hr>");
      continue;
    }
    if (/^>\s?/.test(stripped)) {
      flush();
      out.push(`<blockquote>${inlineHtml(stripped.replace(/^>\s?/, ""))}</blockquote>`);
      continue;
    }

    const bullet = /^[-*+]\s+(.*)$/.exec(stripped);
    if (bullet) {
      const task = /^\[([ xX])\]\s+(.*)$/.exec(bullet[1]);
      openList("ul");
      if (task) {
        const checked = task[1].toLowerCase() === "x" ? " checked" : "";
        out.push(`<li><input type="checkbox" disabled${checked}> ${inlineHtml(task[2])}</li>`);
      } else {
        out.push(`<li>${inlineHtml(bullet[1])}</li>`);
      }
      continue;
    }

    const numbered = /^(\d+)[.)]\s+(.*)$/.exec(stripped);
    if (numbered) {
      openList("ol");
      out.push(`<li>${inlineHtml(numbered[2])}</li>`);
      continue;
    }

    closeList();
    paragraph.push(inlineHtml(stripped));
  }

  if (fence !== null && buffer.length) emitCode(buffer._lang || "");
  flush();
  return { html: out.join(""), toc };
}
