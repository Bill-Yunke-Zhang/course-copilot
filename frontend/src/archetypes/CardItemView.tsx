import { useState } from "react";
import type { SourceRef } from "../lib/api";
import type { ArchetypeRendererProps } from "./registry";
import { SourceRefs } from "./SourceRefs";

// {front, back, source_refs} — click to flip
export function CardItemView({ payload }: ArchetypeRendererProps) {
  const [flipped, setFlipped] = useState(false);
  return (
    <div className="border-b border-slate-100 dark:border-slate-800 py-2 last:border-b-0">
      <button
        className="w-full rounded border border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-800 p-3 text-left hover:bg-slate-100 dark:hover:bg-slate-700"
        onClick={() => setFlipped((f) => !f)}
      >
        <div className="text-xs uppercase text-slate-400 dark:text-slate-500">
          {flipped ? "back" : "front"} — click to flip
        </div>
        <div className="mt-1">
          {String((flipped ? payload.back : payload.front) ?? "")}
        </div>
      </button>
      <SourceRefs refs={payload.source_refs as SourceRef[]} />
    </div>
  );
}
