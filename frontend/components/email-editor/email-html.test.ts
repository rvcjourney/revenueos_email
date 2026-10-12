import { Editor } from "@tiptap/core";
import { describe, expect, it } from "vitest";

import {
  buildEmailExtensions,
  canonicalizeHtml,
  cidToEditorHtml,
  cidToPreviewHtml,
  editorHtmlToCid,
  extractCids,
  insertAtCursor,
  normalizeLinkUrl,
  removeCidImages,
  roundTripsCleanly,
} from "@/components/email-editor/email-html";

describe("normalizeLinkUrl", () => {
  it.each([
    ["https://example.com/a?b=1", "https://example.com/a?b=1"],
    ["http://example.com", "http://example.com"],
    ["mailto:a@b.co", "mailto:a@b.co"],
    ["example.com", "https://example.com"],
    ["  example.com/path ", "https://example.com/path"],
  ])("accepts %s", (input, expected) => {
    expect(normalizeLinkUrl(input)).toBe(expected);
  });

  it.each([
    "",
    "   ",
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "data:text/html;base64,AAAA",
    "{{first_name}}",
    "not a url",
    "/relative/path",
  ])("rejects %s", (input) => {
    expect(normalizeLinkUrl(input)).toBeNull();
  });
});

describe("insertAtCursor", () => {
  it("inserts at the caret and returns the new caret", () => {
    expect(insertAtCursor("Hello world", 5, 5, ",")).toEqual({ value: "Hello, world", caret: 6 });
  });
  it("replaces a selection", () => {
    expect(insertAtCursor("Hi NAME!", 3, 7, "{{first_name}}")).toEqual({
      value: "Hi {{first_name}}!",
      caret: 17,
    });
  });
  it("appends when there is no selection info", () => {
    expect(insertAtCursor("abc", null, null, "X")).toEqual({ value: "abcX", caret: 4 });
  });
});

describe("canonicalizeHtml", () => {
  it("treats editor and hand-written equivalents as equal", () => {
    expect(canonicalizeHtml("<p><b>a</b></p>")).toBe(canonicalizeHtml("<p><strong>a</strong></p>"));
    expect(canonicalizeHtml("<ul><li>x</li></ul>")).toBe(
      canonicalizeHtml("<ul><li><p>x</p></li></ul>"),
    );
    expect(canonicalizeHtml('<p style="color: red;  font-size:14px">a</p>')).toBe(
      canonicalizeHtml('<p style="font-size: 14px; color:RED">a</p>'),
    );
  });
  it("ignores whitespace between block elements", () => {
    expect(canonicalizeHtml("<p>a</p>\n  <p>b</p>")).toBe(canonicalizeHtml("<p>a</p><p>b</p>"));
  });
  it("still tells different content apart", () => {
    expect(canonicalizeHtml("<p>a</p>")).not.toBe(canonicalizeHtml("<p>b</p>"));
  });
});

describe("roundTripsCleanly", () => {
  it("accepts what the editor produces and supports", () => {
    expect(roundTripsCleanly("")).toBe(true);
    expect(roundTripsCleanly("<p>Hi {{first_name|there}},</p><p></p>")).toBe(true);
    expect(roundTripsCleanly('<p style="text-align: center">Hi <strong>you</strong></p>')).toBe(
      true,
    );
    expect(roundTripsCleanly("<ul><li>one</li><li>two</li></ul>")).toBe(true);
    expect(roundTripsCleanly('<p><a href="https://example.com">link</a></p>')).toBe(true);
  });
  it("rejects markup the visual editor would drop", () => {
    expect(roundTripsCleanly("<table><tr><td>cell</td></tr></table>")).toBe(false);
    expect(roundTripsCleanly('<div class="x"><span>hi</span></div>')).toBe(false);
  });
});

describe("inline image conversions", () => {
  const stored = '<p>Hi <img src="cid:abc12345" alt="logo"> there</p>';

  it("shows a display URL in the editor and remembers the content id", () => {
    const html = cidToEditorHtml(stored, { abc12345: "https://files.example/x?sig=1&exp=2" });
    expect(html).toContain('src="https://files.example/x?sig=1&exp=2"');
    expect(html).toContain('data-cid="abc12345"');
    expect(html).not.toContain("cid:abc12345");
  });

  it("uses a transparent placeholder until the URL is known", () => {
    const html = cidToEditorHtml(stored, {});
    expect(html).toContain("data:image/gif;base64,");
    expect(html).toContain('data-cid="abc12345"');
  });

  it("round-trips back to the exact cid reference", () => {
    const shown = cidToEditorHtml(stored, { abc12345: "https://files.example/x" });
    expect(editorHtmlToCid(shown)).toBe(stored);
  });

  it("restores the reference even if the display URL changed while editing", () => {
    const html = '<p><img src="https://new.example/other" alt="" data-cid="abc12345"></p>';
    expect(editorHtmlToCid(html)).toBe('<p><img src="cid:abc12345" alt=""></p>');
  });

  it("leaves HTML without cid images byte-for-byte untouched", () => {
    const plain = '<p>Hi&nbsp;there <img src="https://e.com/a.png"></p>';
    expect(cidToEditorHtml(plain, {})).toBe(plain);
    expect(editorHtmlToCid(plain)).toBe(plain);
    expect(cidToPreviewHtml(plain, {})).toBe(plain);
  });

  it("only rewrites the images it knows about", () => {
    const html = '<img src="cid:one11111"><img src="cid:two22222">';
    expect(cidToPreviewHtml(html, { one11111: "https://u/1" })).toBe(
      '<img src="https://u/1"><img src="cid:two22222">',
    );
  });

  it("extracts and removes references by content id", () => {
    const html = '<p><img src="cid:one11111"><img src="cid:two22222" alt="x"></p>';
    expect(extractCids(html)).toEqual(["one11111", "two22222"]);
    expect(removeCidImages(html, "one11111")).toBe('<p><img src="cid:two22222" alt="x"></p>');
    expect(removeCidImages(html, "nothere1")).toBe(html);
  });

  it("keeps images through the visual editor round trip", () => {
    expect(roundTripsCleanly(stored)).toBe(true);
  });
});

describe("inline image size and position", () => {
  const sized = '<p><img src="cid:abc12345" alt="logo" width="240" align="left" hspace="8">Text</p>';

  it("keeps width, align and the float gap through the visual editor", () => {
    expect(roundTripsCleanly(sized)).toBe(true);
  });

  it("does not add align or a gap to an inline image", () => {
    const html = '<p><img src="cid:abc12345" alt="" width="100"></p>';
    expect(roundTripsCleanly(html)).toBe(true);
  });

  it("resizes and repositions the selected image from its option bar", () => {
    const editor = new Editor({
      extensions: buildEmailExtensions(),
      content: '<p><img src="https://e.com/a.png" alt="a" width="240">Hi</p>',
    });
    editor.commands.setNodeSelection(1);
    const click = (title: string) =>
      (editor.view.dom.querySelector(`button[aria-label="${title}"]`) as HTMLButtonElement).click();

    click("Large image");
    expect(editor.getHTML()).toContain('width="480"');

    click("Image on the left, text wraps on its right");
    expect(editor.getHTML()).toContain('align="left"');
    expect(editor.getHTML()).toContain('hspace="8"');

    click("In line with the text");
    expect(editor.getHTML()).not.toContain("align=");
    editor.destroy();
  });
});
