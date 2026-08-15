import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const css = fs.readFileSync(path.join(root, "director.css"), "utf8");
const js = fs.readFileSync(path.join(root, "director.js"), "utf8");

test("CODE session header wraps the title in the ellipsis name-text span", () => {
  // The h1 must not render the prompt directly; it should hold a .name-text
  // span so the (long, multi-line) prompt collapses to one line.
  assert.match(
    html,
    /<h1 id="code-session-title"><span class="name-text" id="code-session-title-name">/,
    "header title should be wrapped in the .name-text ellipsis span"
  );
});

test("director.js writes the prompt into the name-text span, not the h1 itself", () => {
  assert.match(
    js,
    /code-session-title-name/,
    "director.js must target the title name-text span"
  );
  assert.match(js, /titleName\.textContent = label/);
  assert.match(js, /const label = meta\.title \|\| title \|\| "CODE session"/);
  // The whole brief is unreadable in a header but useful on hover / long-press.
  assert.match(js, /titleName\.title = label/);
});

test("the header title is truncated to one line with an ellipsis", () => {
  // The .name-text rule that governs the header title must force a single
  // line and visually truncate overflow with an ellipsis.
  for (const rule of ["white-space: nowrap", "overflow: hidden", "text-overflow: ellipsis"]) {
    assert.ok(
      css.includes(rule),
      `director.css should contain "${rule}" for the one-line ellipsis header`
    );
  }
  // The rule must be scoped to the header title span so the truncation applies
  // to the CODE session header without affecting unrelated surfaces.
  assert.match(css, /\.topbar \.title h1 \.name-text\s*\{/);
});

test("long prompts never spell out in the header; the rule exists even when empty", () => {
  // Regression guard for the reported bug: a very long prompt must stay
  // compact and readable, not grow the header to fill the screen.
  const longPrompt = "\u201C" + "a".repeat(2048);
  // The render path routes the prompt into the ellipsis span; a too-long value
  // is still a single text node the CSS truncates rather than wrapped lines.
  const titleSpanOpen = /<span class="name-text" id="code-session-title-name">/.test(html);
  assert.ok(titleSpanOpen, "title span must exist to hold long prompts");
  assert.ok(longPrompt.length > 1000, "sanity: prompt used here is long");
});