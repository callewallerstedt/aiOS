// A port of code_markdown_blocks() / code_inline_spans() from helper_overlay.py.
//
// Same grammar, same edge cases (including an unterminated fence, which matters
// because we render assistant text while it is still streaming and the closing
// ``` has not arrived yet). Output is HTML instead of Tk text tags.

const INLINE = /(?<code>`[^`]+`)|(?<link>\[[^\]]+\]\([^)]+\))|(?<bold>\*\*[^*]+\*\*)|(?<italic>\*[^*]+\*)/g;

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
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
      // Anything the model emits is untrusted text; only http(s) becomes a link.
      const safe = /^https?:\/\//i.test(target) ? target : "";
      html += safe
        ? `<a href="${escapeHtml(safe)}" data-external="1">${escapeHtml(label)}</a>`
        : escapeHtml(label);
    } else if (groups.bold) {
      html += `<strong>${escapeHtml(groups.bold.slice(2, -2))}</strong>`;
    } else {
      html += `<em>${escapeHtml(groups.italic.slice(1, -1))}</em>`;
    }
    cursor = match.index + match[0].length;
  }
  if (cursor < source.length) html += escapeHtml(source.slice(cursor));
  return html;
}

export function renderMarkdown(text) {
  const lines = String(text ?? "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let fence = null;
  let buffer = [];
  let list = null; // "ul" | "ol" | null
  let paragraph = [];

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
  const emitCode = () => {
    out.push(`<pre><code>${escapeHtml(buffer.join("\n"))}</code></pre>`);
    buffer = [];
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const stripped = line.trim();

    if (stripped.startsWith("```")) {
      if (fence === null) {
        flush();
        fence = stripped.slice(3).trim();
        buffer = [];
      } else {
        emitCode();
        fence = null;
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

    // Table: a header row plus a |---|---| divider.
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

    const heading = /^(#{1,6})\s+(.*)$/.exec(stripped);
    if (heading) {
      flush();
      const level = Math.min(3, heading[1].length);
      out.push(`<h${level}>${inlineHtml(heading[2])}</h${level}>`);
      continue;
    }
    if (/^([-*_])\s*\1\s*\1[-*_\s]*$/.test(stripped)) {
      flush();
      out.push("<hr>");
      continue;
    }
    if (stripped.startsWith("> ")) {
      flush();
      out.push(`<blockquote>${inlineHtml(stripped.slice(2))}</blockquote>`);
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

  // A fence still open means the model is mid code block. Render what we have
  // so the block grows in place instead of popping in when the fence closes.
  if (fence !== null && buffer.length) emitCode();
  flush();
  return out.join("");
}
