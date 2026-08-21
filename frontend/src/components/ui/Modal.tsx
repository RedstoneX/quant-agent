import { ReactNode, useEffect } from "react";

export function Modal({
  breadcrumb,
  onClose,
  children,
}: {
  breadcrumb: ReactNode;
  onClose: () => void;
  children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    document.body.classList.add("overflow-hidden");
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.classList.remove("overflow-hidden");
    };
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 bg-black/55 flex items-start justify-center p-4 sm:py-[4vh] z-[100] overflow-y-auto"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="bg-panel border border-border rounded-lg w-full max-w-4xl max-h-[92vh] sm:max-h-[92vh] flex flex-col overflow-hidden">
        <div className="flex items-center justify-between gap-3 px-3.5 py-2.5 border-b border-border sticky top-0 bg-panel">
          <div className="flex items-center gap-1.5 flex-wrap text-[0.875rem] min-w-0">{breadcrumb}</div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="border border-border rounded text-dim text-lg leading-none px-2 py-1 flex-shrink-0 hover:text-ink hover:border-accent"
          >
            &times;
          </button>
        </div>
        <div className="p-3.5 overflow-y-auto">{children}</div>
      </div>
    </div>
  );
}

export function CrumbLink({ text, onClick }: { text: string; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className="text-accent underline text-[0.875rem]">
      {text}
    </button>
  );
}
