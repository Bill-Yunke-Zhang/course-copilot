import type { SourceRef } from "../lib/api";
import type { ArchetypeRendererProps } from "./registry";
import { SourceRefs } from "./SourceRefs";

const stateStyles: Record<string, string> = {
  covered: "bg-emerald-100 dark:bg-emerald-900/50 text-emerald-700 dark:text-emerald-300",
  on_track: "bg-emerald-100 dark:bg-emerald-900/50 text-emerald-700 dark:text-emerald-300",
  upcoming: "bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400",
  behind: "bg-red-100 dark:bg-red-900/50 text-red-700 dark:text-red-300",
  unavailable: "bg-amber-100 dark:bg-amber-900/50 text-amber-700 dark:text-amber-300",
};

// {label, detail, source_refs} + optional extra "state" badge field
export function StatusItemView({ payload }: ArchetypeRendererProps) {
  const state = typeof payload.state === "string" ? payload.state : null;
  return (
    <div className="border-b border-slate-100 dark:border-slate-800 py-2 last:border-b-0">
      <div className="flex items-center gap-2">
        <span className="font-medium">{String(payload.label ?? "")}</span>
        {state && (
          <span
            className={`rounded-full px-2 py-0.5 text-xs ${
              stateStyles[state] ?? "bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400"
            }`}
          >
            {state}
          </span>
        )}
      </div>
      <div className="text-sm text-slate-600 dark:text-slate-300">{String(payload.detail ?? "")}</div>
      <SourceRefs refs={payload.source_refs as SourceRef[]} />
    </div>
  );
}
