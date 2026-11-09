"use client";

import * as React from "react";
import { EditorContent, useEditor, type Editor } from "@tiptap/react";
import {
  AlignCenter,
  AlignLeft,
  AlignRight,
  Bold,
  Code2,
  Eraser,
  Italic,
  Link2,
  List,
  ListOrdered,
  MoreHorizontal,
  Underline,
} from "lucide-react";

import {
  buildEmailExtensions,
  canonicalizeHtml,
  cidToEditorHtml,
  editorHtmlToCid,
  EMAIL_FONT_SIZES,
  EMAIL_FONTS,
  insertAtCursor,
  normalizeLinkUrl,
} from "@/components/email-editor/email-html";
import { EMAIL_IMAGE_DEFAULT_WIDTH } from "@/components/email-editor/email-image";
import { VariablePicker } from "@/components/email-editor/variable-picker";
import { Popover } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

export type EditorMode = "visual" | "source";

type Props = {
  value: string;
  onChange: (html: string) => void;
  mode: EditorMode;
  onModeChange: (mode: EditorMode) => void;
  readOnly?: boolean;
  placeholder?: string;
  // Extra toolbar controls (template picker, attachment/image buttons, ...).
  toolbarExtras?: React.ReactNode;
  // Fired when switching source -> visual dropped markup the visual editor
  // can't represent (e.g. a hand-written <table>).
  onLossy?: () => void;
  // content id -> temporary display URL for uploaded inline images. The stored
  // HTML keeps cid: references; URLs are only used while editing.
  imageUrls?: Record<string, string>;
  id?: string;
};

export type EmailBodyEditorHandle = {
  // Insert an uploaded inline image at the cursor (both editor modes).
  insertImage: (contentId: string, alt: string, displayUrl?: string) => void;
};

function ToolbarButton({
  label,
  active,
  disabled,
  onClick,
  children,
}: {
  label: string;
  active?: boolean;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      aria-pressed={active === undefined ? undefined : active}
      disabled={disabled}
      // Keep the editor selection while a toolbar button is pressed.
      onMouseDown={(event) => event.preventDefault()}
      onClick={onClick}
      className={cn(
        "inline-flex h-8 w-8 items-center justify-center rounded-md text-slate-600 hover:bg-slate-100 hover:text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600 disabled:pointer-events-none disabled:opacity-40",
        active && "bg-brand-50 text-brand-700",
      )}
    >
      {children}
    </button>
  );
}

function Divider() {
  return <span className="mx-1 h-5 w-px bg-slate-200" aria-hidden="true" />;
}

function LinkControl({ editor, disabled }: { editor: Editor; disabled: boolean }) {
  const [url, setUrl] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  return (
    <Popover
      label="Link"
      className="w-72"
      trigger={({ toggle, open, ariaProps }) => (
        <button
          type="button"
          aria-label="Link"
          title="Link"
          disabled={disabled}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => {
            if (!open) {
              setUrl((editor.getAttributes("link").href as string | undefined) ?? "");
              setError(null);
            }
            toggle();
          }}
          {...ariaProps}
          className={cn(
            "inline-flex h-8 w-8 items-center justify-center rounded-md text-slate-600 hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600 disabled:pointer-events-none disabled:opacity-40",
            editor.isActive("link") && "bg-brand-50 text-brand-700",
          )}
        >
          <Link2 className="h-4 w-4" aria-hidden="true" />
        </button>
      )}
    >
      {(close) => (
        <form
          className="space-y-2"
          onSubmit={(event) => {
            event.preventDefault();
            const href = normalizeLinkUrl(url);
            if (!href) {
              setError("Enter a web address (https://…) or an email link (mailto:…).");
              return;
            }
            const chain = editor.chain().focus();
            if (editor.state.selection.empty && !editor.isActive("link")) {
              chain
                .insertContent({
                  type: "text",
                  text: href.replace(/^mailto:/i, ""),
                  marks: [{ type: "link", attrs: { href } }],
                })
                .run();
            } else {
              chain.extendMarkRange("link").setLink({ href }).run();
            }
            close();
          }}
        >
          <label className="block text-xs font-medium text-slate-600">
            Link address
            <input
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://example.com"
              className="mt-1 h-8 w-full rounded-md border border-slate-200 px-2 text-sm"
            />
          </label>
          {error ? <p className="text-xs text-red-600">{error}</p> : null}
          <div className="flex justify-between">
            <button
              type="button"
              onClick={() => {
                editor.chain().focus().extendMarkRange("link").unsetLink().run();
                close();
              }}
              className="text-xs text-slate-500 hover:text-red-600"
            >
              Remove link
            </button>
            <button
              type="submit"
              className="h-8 rounded-md bg-slate-900 px-3 text-xs font-medium text-white"
            >
              Apply
            </button>
          </div>
        </form>
      )}
    </Popover>
  );
}

function VisualToolbar({ editor, disabled }: { editor: Editor; disabled: boolean }) {
  const textStyle = editor.getAttributes("textStyle") as {
    fontFamily?: string;
    fontSize?: string;
    color?: string;
  };
  const currentSize = (textStyle.fontSize ?? "").replace("px", "");

  return (
    <>
      <select
        aria-label="Font"
        disabled={disabled}
        value={EMAIL_FONTS.find((font) => font.value === (textStyle.fontFamily ?? ""))?.value ?? ""}
        onChange={(event) => {
          const value = event.target.value;
          const chain = editor.chain().focus();
          if (value) chain.setFontFamily(value).run();
          else chain.unsetFontFamily().run();
        }}
        className="h-8 max-w-[8rem] rounded-md border border-slate-200 bg-white px-1.5 text-xs"
      >
        {EMAIL_FONTS.map((font) => (
          <option key={font.label} value={font.value}>
            {font.label}
          </option>
        ))}
      </select>
      <select
        aria-label="Font size"
        disabled={disabled}
        value={EMAIL_FONT_SIZES.includes(currentSize) ? currentSize : ""}
        onChange={(event) => {
          const value = event.target.value;
          const chain = editor.chain().focus();
          if (value) chain.setFontSize(`${value}px`).run();
          else chain.unsetFontSize().run();
        }}
        className="h-8 w-16 rounded-md border border-slate-200 bg-white px-1.5 text-xs"
      >
        <option value="">Size</option>
        {EMAIL_FONT_SIZES.map((size) => (
          <option key={size} value={size}>
            {size}px
          </option>
        ))}
      </select>
      <Divider />
      <ToolbarButton
        label="Bold"
        active={editor.isActive("bold")}
        disabled={disabled}
        onClick={() => editor.chain().focus().toggleBold().run()}
      >
        <Bold className="h-4 w-4" aria-hidden="true" />
      </ToolbarButton>
      <ToolbarButton
        label="Italic"
        active={editor.isActive("italic")}
        disabled={disabled}
        onClick={() => editor.chain().focus().toggleItalic().run()}
      >
        <Italic className="h-4 w-4" aria-hidden="true" />
      </ToolbarButton>
      <ToolbarButton
        label="Underline"
        active={editor.isActive("underline")}
        disabled={disabled}
        onClick={() => editor.chain().focus().toggleUnderline().run()}
      >
        <Underline className="h-4 w-4" aria-hidden="true" />
      </ToolbarButton>
      <label
        title="Text color"
        className={cn(
          "relative inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-md hover:bg-slate-100",
          disabled && "pointer-events-none opacity-40",
        )}
      >
        <span
          aria-hidden="true"
          className="flex flex-col items-center text-sm font-semibold leading-none text-slate-700"
        >
          A
          <span
            className="mt-0.5 h-0.5 w-4"
            style={{ backgroundColor: textStyle.color || "#0f172a" }}
          />
        </span>
        <input
          type="color"
          aria-label="Text color"
          disabled={disabled}
          value={/^#[0-9a-fA-F]{6}$/.test(textStyle.color ?? "") ? textStyle.color : "#0f172a"}
          onChange={(event) => editor.chain().focus().setColor(event.target.value).run()}
          className="absolute inset-0 h-full w-full cursor-pointer opacity-0"
        />
      </label>
      <ToolbarButton
        label="Clear formatting"
        disabled={disabled}
        onClick={() => editor.chain().focus().unsetAllMarks().clearNodes().run()}
      >
        <Eraser className="h-4 w-4" aria-hidden="true" />
      </ToolbarButton>
      <Divider />
      <ToolbarButton
        label="Align left"
        active={editor.isActive({ textAlign: "left" })}
        disabled={disabled}
        onClick={() => editor.chain().focus().setTextAlign("left").run()}
      >
        <AlignLeft className="h-4 w-4" aria-hidden="true" />
      </ToolbarButton>
      <ToolbarButton
        label="Align center"
        active={editor.isActive({ textAlign: "center" })}
        disabled={disabled}
        onClick={() => editor.chain().focus().setTextAlign("center").run()}
      >
        <AlignCenter className="h-4 w-4" aria-hidden="true" />
      </ToolbarButton>
      <ToolbarButton
        label="Align right"
        active={editor.isActive({ textAlign: "right" })}
        disabled={disabled}
        onClick={() => editor.chain().focus().setTextAlign("right").run()}
      >
        <AlignRight className="h-4 w-4" aria-hidden="true" />
      </ToolbarButton>
      <Divider />
      <ToolbarButton
        label="Numbered list"
        active={editor.isActive("orderedList")}
        disabled={disabled}
        onClick={() => editor.chain().focus().toggleOrderedList().run()}
      >
        <ListOrdered className="h-4 w-4" aria-hidden="true" />
      </ToolbarButton>
      <ToolbarButton
        label="Bulleted list"
        active={editor.isActive("bulletList")}
        disabled={disabled}
        onClick={() => editor.chain().focus().toggleBulletList().run()}
      >
        <List className="h-4 w-4" aria-hidden="true" />
      </ToolbarButton>
      <Divider />
      <LinkControl editor={editor} disabled={disabled} />
    </>
  );
}

function MoreMenu({ editor, disabled }: { editor: Editor; disabled: boolean }) {
  const items: { label: string; run: () => void; active?: boolean }[] = [
    {
      label: "Strikethrough",
      active: editor.isActive("strike"),
      run: () => editor.chain().focus().toggleStrike().run(),
    },
    {
      label: "Quote",
      active: editor.isActive("blockquote"),
      run: () => editor.chain().focus().toggleBlockquote().run(),
    },
    {
      label: "Heading",
      active: editor.isActive("heading", { level: 2 }),
      run: () => editor.chain().focus().toggleHeading({ level: 2 }).run(),
    },
    { label: "Divider line", run: () => editor.chain().focus().setHorizontalRule().run() },
    { label: "Undo", run: () => editor.chain().focus().undo().run() },
    { label: "Redo", run: () => editor.chain().focus().redo().run() },
  ];
  return (
    <Popover
      label="More formatting"
      className="w-44 p-1"
      trigger={({ toggle, ariaProps }) => (
        <button
          type="button"
          aria-label="More formatting"
          title="More formatting"
          disabled={disabled}
          onMouseDown={(event) => event.preventDefault()}
          onClick={toggle}
          {...ariaProps}
          className="inline-flex h-8 w-8 items-center justify-center rounded-md text-slate-600 hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-600 disabled:pointer-events-none disabled:opacity-40"
        >
          <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
        </button>
      )}
    >
      {(close) => (
        <ul>
          {items.map((item) => (
            <li key={item.label}>
              <button
                type="button"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  item.run();
                  close();
                }}
                className={cn(
                  "w-full rounded px-2 py-1.5 text-left text-sm text-slate-700 hover:bg-slate-100",
                  item.active && "bg-brand-50 text-brand-700",
                )}
              >
                {item.label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </Popover>
  );
}

export const EmailBodyEditor = React.forwardRef<EmailBodyEditorHandle, Props>(
  function EmailBodyEditor(
    {
      value,
      onChange,
      mode,
      onModeChange,
      readOnly = false,
      placeholder,
      toolbarExtras,
      onLossy,
      imageUrls,
      id,
    },
    ref,
  ) {
    const urlsRef = React.useRef<Record<string, string>>(imageUrls ?? {});
    urlsRef.current = imageUrls ?? {};
    const valueRef = React.useRef(value);
    valueRef.current = value;
    const extensions = React.useMemo(() => buildEmailExtensions(placeholder), [placeholder]);
    // The last HTML the visual editor and `value` are known to agree on. Lets us
    // tell an external change (template inserted, source edited) from the echo of
    // our own onChange, without which the editor would reset the caret on every
    // keystroke.
    // null = unknown: the source textarea was edited (or the editor was created
    // from HTML while hidden), so the next visual switch must re-sync and check
    // for markup the visual editor can't keep.
    const synced = React.useRef<string | null>(mode === "visual" ? value : null);
    const onChangeRef = React.useRef(onChange);
    onChangeRef.current = onChange;
    const onLossyRef = React.useRef(onLossy);
    onLossyRef.current = onLossy;
    const sourceRef = React.useRef<HTMLTextAreaElement>(null);

    const editor = useEditor({
      extensions,
      content: cidToEditorHtml(value, urlsRef.current),
      editable: !readOnly,
      immediatelyRender: false,
      shouldRerenderOnTransaction: true,
      editorProps: {
        attributes: { "aria-label": "Email body", ...(id ? { id } : {}) },
      },
      onUpdate: ({ editor: e }) => {
        const html = editorHtmlToCid(e.getHTML());
        synced.current = html;
        onChangeRef.current(html);
      },
    });

    React.useEffect(() => {
      // emitUpdate=false: toggling editability is not a content change, and an
      // update event here would rewrite the stored HTML and mark the step dirty.
      editor?.setEditable(!readOnly, false);
    }, [editor, readOnly]);

    React.useEffect(() => {
      if (!editor || mode !== "visual" || value === synced.current) return;
      editor.commands.setContent(cidToEditorHtml(value, urlsRef.current), {
        emitUpdate: false,
      });
      const normalized = editorHtmlToCid(editor.getHTML());
      synced.current = normalized;
      if (normalized !== value) {
        if (canonicalizeHtml(normalized) !== canonicalizeHtml(value)) onLossyRef.current?.();
        onChangeRef.current(normalized);
      }
    }, [editor, mode, value]);

    // Display URLs arrive after the first render (they are fetched); re-render
    // the images without touching the value, selection-independent content or the
    // dirty state.
    const urlsKey = JSON.stringify(imageUrls ?? {});
    React.useEffect(() => {
      if (!editor || mode !== "visual" || !valueRef.current.includes("cid:")) return;
      editor.commands.setContent(cidToEditorHtml(valueRef.current, urlsRef.current), {
        emitUpdate: false,
      });
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [editor, mode, urlsKey]);

    React.useImperativeHandle(
      ref,
      () => ({
        insertImage(contentId, alt, displayUrl) {
          if (readOnly) return;
          if (mode === "source") {
            const el = sourceRef.current;
            const tag = `<img src="cid:${contentId}" alt="${alt.replace(/"/g, "&quot;")}" width="${EMAIL_IMAGE_DEFAULT_WIDTH}">`;
            const result = insertAtCursor(
              valueRef.current,
              el?.selectionStart ?? null,
              el?.selectionEnd ?? null,
              tag,
            );
            synced.current = null;
            onChangeRef.current(result.value);
            return;
          }
          editor
            ?.chain()
            .focus()
            .insertContent({
              type: "image",
              attrs: {
                src: displayUrl ?? urlsRef.current[contentId] ?? "",
                alt,
                width: String(EMAIL_IMAGE_DEFAULT_WIDTH),
                dataCid: contentId,
              },
            })
            .run();
        },
      }),
      [editor, mode, readOnly],
    );

    function insertVariable(code: string) {
      if (readOnly) return;
      if (mode === "source") {
        const el = sourceRef.current;
        const result = insertAtCursor(
          value,
          el?.selectionStart ?? null,
          el?.selectionEnd ?? null,
          code,
        );
        synced.current = null;
        onChange(result.value);
        requestAnimationFrame(() => {
          el?.focus();
          el?.setSelectionRange(result.caret, result.caret);
        });
        return;
      }
      editor?.chain().focus().insertContent(code).run();
    }

    return (
      <div className="rounded-md border border-slate-200 bg-white">
        <div
          role="toolbar"
          aria-label="Email formatting"
          className="flex flex-wrap items-center gap-0.5 border-b border-slate-200 px-2 py-1.5"
        >
          {mode === "visual" && editor ? (
            <VisualToolbar editor={editor} disabled={readOnly} />
          ) : (
            <span className="px-2 text-xs font-medium text-slate-500">HTML source</span>
          )}
          {toolbarExtras}
          <Divider />
          <ToolbarButton
            label={mode === "source" ? "Switch to visual editor" : "Edit HTML source"}
            active={mode === "source"}
            onClick={() => onModeChange(mode === "source" ? "visual" : "source")}
          >
            <Code2 className="h-4 w-4" aria-hidden="true" />
          </ToolbarButton>
          {mode === "visual" && editor ? <MoreMenu editor={editor} disabled={readOnly} /> : null}
          <VariablePicker
            onInsert={insertVariable}
            disabled={readOnly}
            label="Insert variable in body"
          />
        </div>
        {mode === "visual" ? (
          <EditorContent editor={editor} className="email-editor" />
        ) : (
          <textarea
            ref={sourceRef}
            id={id}
            aria-label="HTML source"
            spellCheck={false}
            readOnly={readOnly}
            value={value}
            onChange={(event) => {
              synced.current = null;
              onChange(event.target.value);
            }}
            className="block min-h-[16rem] w-full resize-y rounded-b-md p-3 font-mono text-xs leading-5 text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-600"
          />
        )}
      </div>
    );
  },
);
