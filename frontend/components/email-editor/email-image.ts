import { mergeAttributes, Node } from "@tiptap/core";

// Inline image for email bodies. Uploaded images are referenced from the stored
// HTML as <img src="cid:CONTENT_ID"> (the file travels inside the message), but
// a browser cannot display cid: URLs. While editing, the node therefore carries
// a temporary display URL in `src` and the content id in `data-cid`;
// editorHtmlToCid() (email-html.ts) turns it back before anything is saved.
//
// Size and position use plain HTML attributes (width, align, hspace) rather than
// CSS, because those are what the backend sanitizer keeps and what Gmail and
// Outlook honour: `align="left"` lets the text wrap to the right of the image,
// the usual signature-with-logo layout.

export const EMAIL_IMAGE_MIN_WIDTH = 24;
export const EMAIL_IMAGE_MAX_WIDTH = 600;
// Width given to a freshly inserted image so a large photo can't blow the email
// open; the user can resize it afterwards.
export const EMAIL_IMAGE_DEFAULT_WIDTH = 240;
export const EMAIL_IMAGE_SIZES = [
  { label: "S", title: "Small", width: 100 },
  { label: "M", title: "Medium", width: 240 },
  { label: "L", title: "Large", width: 480 },
] as const;
export const EMAIL_IMAGE_ALIGNMENTS = [
  { value: null, label: "Inline", title: "In line with the text" },
  { value: "left", label: "Left", title: "Image on the left, text wraps on its right" },
  { value: "right", label: "Right", title: "Image on the right, text wraps on its left" },
] as const;

// Gap between a floated image and the text beside it.
const FLOAT_GAP = "8";

function parseWidth(value: unknown): number | null {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) && parsed > 0 && !String(value).includes("%") ? parsed : null;
}

function clampWidth(width: number): number {
  return Math.min(EMAIL_IMAGE_MAX_WIDTH, Math.max(EMAIL_IMAGE_MIN_WIDTH, Math.round(width)));
}

export const EmailImage = Node.create({
  name: "image",
  group: "inline",
  inline: true,
  atom: true,
  draggable: true,

  addAttributes() {
    return {
      src: { default: null },
      alt: { default: "" },
      width: { default: null },
      align: {
        default: null,
        parseHTML: (element) => {
          const value = element.getAttribute("align")?.toLowerCase();
          return value === "left" || value === "right" ? value : null;
        },
        renderHTML: (attributes) => {
          const align = attributes.align as string | null;
          if (align !== "left" && align !== "right") return {};
          return { align, hspace: FLOAT_GAP };
        },
      },
      dataCid: {
        default: null,
        parseHTML: (element) => element.getAttribute("data-cid"),
        renderHTML: (attributes) =>
          attributes.dataCid ? { "data-cid": attributes.dataCid as string } : {},
      },
    };
  },

  parseHTML() {
    return [{ tag: "img[src]" }];
  },

  renderHTML({ HTMLAttributes }) {
    return ["img", mergeAttributes(HTMLAttributes)];
  },

  // Editing view only (getHTML() uses renderHTML): the image plus a drag handle
  // to resize it and a small bar to pick a size and where the text goes.
  addNodeView() {
    return ({ node, editor, getPos }) => {
      let current = node;

      const dom = document.createElement("span");
      dom.className = "email-image";
      const img = document.createElement("img");
      img.draggable = false;
      const handle = document.createElement("span");
      handle.className = "email-image-handle";
      handle.setAttribute("role", "separator");
      handle.setAttribute("aria-label", "Drag to resize the image");
      handle.title = "Drag to resize";
      const bar = document.createElement("span");
      bar.className = "email-image-bar";
      bar.setAttribute("role", "toolbar");
      bar.setAttribute("aria-label", "Image options");
      dom.append(img, handle, bar);

      function update(attrs: Record<string, unknown>) {
        const pos = getPos();
        if (pos === undefined || !editor.isEditable) return;
        editor.view.dispatch(
          editor.state.tr.setNodeMarkup(pos, undefined, { ...current.attrs, ...attrs }),
        );
      }

      const buttons: { el: HTMLButtonElement; isActive: () => boolean }[] = [];
      function addButton(label: string, title: string, run: () => void, isActive: () => boolean) {
        const el = document.createElement("button");
        el.type = "button";
        el.textContent = label;
        el.title = title;
        el.setAttribute("aria-label", title);
        // Keep the editor selection (the image) while a button is pressed.
        el.addEventListener("mousedown", (event) => event.preventDefault());
        el.addEventListener("click", run);
        bar.append(el);
        buttons.push({ el, isActive });
      }
      for (const size of EMAIL_IMAGE_SIZES) {
        addButton(
          size.label,
          `${size.title} image`,
          () => update({ width: String(size.width) }),
          () => parseWidth(current.attrs.width) === size.width,
        );
      }
      bar.append(Object.assign(document.createElement("span"), { className: "email-image-sep" }));
      for (const option of EMAIL_IMAGE_ALIGNMENTS) {
        addButton(
          option.label,
          option.title,
          () => update({ align: option.value }),
          () => (current.attrs.align ?? null) === option.value,
        );
      }

      function render() {
        img.src = (current.attrs.src as string | null) ?? "";
        img.alt = (current.attrs.alt as string | null) ?? "";
        const width = parseWidth(current.attrs.width);
        img.style.width = width ? `${width}px` : "";
        dom.dataset.align = (current.attrs.align as string | null) ?? "";
        for (const { el, isActive } of buttons) {
          el.setAttribute("aria-pressed", String(isActive()));
        }
      }
      render();

      handle.addEventListener("pointerdown", (event) => {
        if (!editor.isEditable) return;
        event.preventDefault();
        event.stopPropagation();
        const startX = event.clientX;
        const startWidth = img.getBoundingClientRect().width || parseWidth(current.attrs.width) || 0;
        // A right-floated image grows to the left, so dragging left enlarges it.
        const direction = current.attrs.align === "right" ? -1 : 1;
        let width = clampWidth(startWidth);

        const move = (e: PointerEvent) => {
          width = clampWidth(startWidth + (e.clientX - startX) * direction);
          img.style.width = `${width}px`;
        };
        const end = () => {
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", end);
          window.removeEventListener("pointercancel", end);
          update({ width: String(width) });
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", end);
        window.addEventListener("pointercancel", end);
      });

      return {
        dom,
        update(updated) {
          if (updated.type !== current.type) return false;
          current = updated;
          render();
          return true;
        },
        // Clicks on the handle and the bar belong to them, not to the editor.
        stopEvent: (event) => event.target instanceof Element && (bar.contains(event.target) || handle.contains(event.target)),
        // The width is written straight to the element while dragging.
        ignoreMutation: () => true,
      };
    };
  },
});
