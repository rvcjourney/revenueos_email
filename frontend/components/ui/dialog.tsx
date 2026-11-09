"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

import { cn } from "@/lib/utils";

type DialogProps = {
  open: boolean;
  // Escape, backdrop click and the close button all call this. The parent
  // decides whether to actually close, so unsaved-changes guards live there.
  onRequestClose: () => void;
  title: string;
  headerContent?: React.ReactNode;
  footer?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  // "stretch" fills the viewport height (editors); "center" sizes to content
  // (confirmations, short forms).
  align?: "stretch" | "center";
};

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

export function Dialog({
  open,
  onRequestClose,
  title,
  headerContent,
  footer,
  children,
  className,
  align = "stretch",
}: DialogProps) {
  const panelRef = React.useRef<HTMLDivElement>(null);
  const titleId = React.useId();
  const [mounted, setMounted] = React.useState(false);
  const closeRef = React.useRef(onRequestClose);
  closeRef.current = onRequestClose;

  React.useEffect(() => setMounted(true), []);

  React.useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    panelRef.current?.focus();
    return () => {
      document.body.style.overflow = previousOverflow;
      previouslyFocused?.focus?.();
    };
  }, [open]);

  if (!open || !mounted) return null;

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.stopPropagation();
      closeRef.current();
      return;
    }
    if (event.key !== "Tab" || !panelRef.current) return;
    const items = Array.from(panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (items.length === 0) {
      event.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === panelRef.current)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return createPortal(
    <div
      className={cn(
        "fixed inset-0 z-50 flex justify-center bg-ink-950/60 backdrop-blur-[2px] animate-fade-in",
        align === "center" ? "items-center p-4" : "items-stretch p-0 sm:p-4",
      )}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) closeRef.current();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className={cn(
          "flex w-full max-w-6xl flex-col overflow-hidden bg-white shadow-overlay outline-none animate-slide-up sm:rounded-2xl",
          // "stretch" already fills the fixed-inset overlay's height via
          // items-stretch above. "center" sizes to content instead, so
          // without a cap here a tall form (e.g. Add Lead) grows past the
          // viewport -- and body scroll is locked while open, making the
          // overflow unreachable -- instead of scrolling inside itself.
          align === "center" ? "max-h-[85vh]" : null,
          className,
        )}
      >
        <div className="flex items-center justify-between gap-3 border-b border-slate-200 px-4 py-3">
          <div className="flex min-w-0 items-center gap-3">
            <h2 id={titleId} className="truncate text-base font-bold text-slate-900">
              {title}
            </h2>
            {headerContent}
          </div>
          <button
            type="button"
            onClick={() => closeRef.current()}
            aria-label="Close"
            className="rounded-md p-1.5 text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
          >
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
        {footer ? (
          <div className="border-t border-slate-200 bg-white px-4 py-3">{footer}</div>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}
