import type { SourceRef } from "../lib/api";
import type { ArchetypeRendererProps } from "./registry";
import { SourceRefs } from "./SourceRefs";

// {date, title, description, source_refs}
export function TimelineItemView({ payload }: ArchetypeRendererProps) {
  return (
    <div className="flex gap-3 border-b border-slate-100 dark:border-slate-800 py-2 last:border-b-0">
      <div className="w-24 shrink-0 text-sm font-medium text-slate-500 dark:text-slate-400">
        {String(payload.date ?? "")}
      </div>
      <div className="min-w-0">
        <div className="font-medium">{String(payload.title ?? "")}</div>
        <div className="text-sm text-slate-600 dark:text-slate-300">{String(payload.description ?? "")}</div>
        <SourceRefs refs={payload.source_refs as SourceRef[]} />
      </div>
    </div>
  );
}
