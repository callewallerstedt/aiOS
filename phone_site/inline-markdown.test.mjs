import { test } from "node:test";
import assert from "node:assert/strict";
import { inlineMarkdown } from "./inline-markdown.js";

test("renders bold", () => {
  assert.equal(inlineMarkdown("**hello**"), "<strong>hello</strong>");
});

test("renders italic", () => {
  assert.equal(inlineMarkdown("a *world*"), "a <em>world</em>");
});

test("renders inline code", () => {
  assert.equal(inlineMarkdown("run `npm test`"), "run <code>npm test</code>");
});

test("renders http(s) links", () => {
  assert.equal(
    inlineMarkdown("[docs](https://example.com/a)"),
    '<a href="https://example.com/a" target="_blank" rel="noopener noreferrer">docs</a>'
  );
});

test("escapes script and markup so nothing untrusted injects HTML", () => {
  assert.equal(
    inlineMarkdown("<script>alert(1)</script>"),
    "&lt;script&gt;alert(1)&lt;/script&gt;"
  );
  assert.equal(inlineMarkdown('"><img src=x onerror=alert(1)>'),
    '&quot;&gt;&lt;img src=x onerror=alert(1)&gt;');
});

test("does not turn non-http targets into links", () => {
  assert.equal(
    inlineMarkdown("[x](javascript:alert(1))"),
    "[x](javascript:alert(1))"
  );
});

test("collapses newlines so the strip stays one line", () => {
  assert.equal(inlineMarkdown("line1\nline2\r\nline3"), "line1 line2 line3");
});

test("handles null/undefined as empty", () => {
  assert.equal(inlineMarkdown(null), "");
  assert.equal(inlineMarkdown(undefined), "");
});