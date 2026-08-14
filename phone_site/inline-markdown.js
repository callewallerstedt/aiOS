// Inline-only markdown for the one-line agent preview strip on the Director
// homepage. Everything is escaped first, so nothing untrusted can inject
// markup — the tags below are the only ones that exist. No block elements
// (no <p>, <ul>, <pre>, headings, tables), so the ellipsized single-line
// strip keeps its shape and styling.

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

export function inlineMarkdown(source) {
  return escapeHtml(source)
    .replace(/\r?\n/g, " ")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}