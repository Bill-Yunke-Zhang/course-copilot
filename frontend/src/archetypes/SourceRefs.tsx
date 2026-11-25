import { useState } from "react";
import { api, type SourceRef } from "../lib/api";

/**
 * Renders an item's citations. Clicking a citation loads the cited resource
 * item and shows the excerpt in context — every archetype uses this, which
 * is why grounding is uniform across agents.
 */
export function SourceRefs({ refs }: { refs: SourceRef[] }) {
  const [open, setOpen] = useState<SourceRef | null>(null);
  const [content, setContent] = useState<string>("");
  const [title, setTitle] = useState<string>("");

  async function show(ref: SourceRef) {
    setOpen(ref);
    setContent("Loading…");
    try {
      const item = await api.resourceItem(ref.resource_item_id);
      setTitle(item.title || item.path || item.locator);
      setContent(item.content || "(no text content)");
    } catch {
      setContent("Could not load source.");
    }
  }

  return (
    <div className="mt-1">
      <div className="flex flex-wrap gap-1">
        {refs.map((ref, i) => (
          <button
            key={i}
            onClick={() => show(ref)}
            className="rounded bg-slate-100 dark:bg-slate-800 px-1.5 py-0.5 text-xs text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-700"
            title={ref.excerpt ?? "structured evidence"}
          >
            source #{ref.resource_item_id}
          </button>
        ))}
      </div>
      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6"
          onClick={() => setOpen(null)}
        >
          <div
            className="max-h-[80vh] w-full max-w-2xl overflow-auto rounded-lg bg-white dark:bg-slate-900 p-4 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-2 flex items-start justify-between gap-4">
              <h3 className="font-semibold">{title}</h3>
              <button
                className="text-slate-400 dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300"
                onClick={() => setOpen(null)}
              >
                ✕
              </button>
            </div>
            {open.excerpt && (
              <blockquote className="mb-3 border-l-4 border-blue-300 dark:border-blue-800 bg-blue-50 dark:bg-blue-950/50 p-2 text-sm italic">
                “{open.excerpt}”
              </blockquote>
            )}
            {open.evidence && (
              <pre className="mb-3 overflow-x-auto rounded bg-slate-50 dark:bg-slate-800 p-2 text-xs">
                {JSON.stringify(open.evidence, null, 2)}
              </pre>
            )}
            <pre className="whitespace-pre-wrap text-xs text-slate-600 dark:text-slate-300">{content}</pre>
          </div>
        </div>
      )}
    </div>
  );
}
