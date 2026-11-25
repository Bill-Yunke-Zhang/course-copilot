import type { SourceRef } from "../lib/api";
import type { ArchetypeRendererProps } from "./registry";
import { SourceRefs } from "./SourceRefs";

// {heading, body, source_refs}
export function TextBlockView({ payload }: ArchetypeRendererProps) {
  return (
    <div className="border-b border-slate-100 dark:border-slate-800 py-2 last:border-b-0">
      <h4 className="font-medium">{String(payload.heading ?? "")}</h4>
      <p className="whitespace-pre-wrap text-sm text-slate-600 dark:text-slate-300">
        {String(payload.body ?? "")}
      </p>
      <SourceRefs refs={payload.source_refs as SourceRef[]} />
    </div>
  );
}
