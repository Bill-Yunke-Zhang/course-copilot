import { useState } from "react";
import type { SourceRef } from "../lib/api";
import type { ArchetypeRendererProps } from "./registry";
import { SourceRefs } from "./SourceRefs";

// {question, choices[], correct_index, source_refs}
// This is the *generic* display (used e.g. on dashboard cards). The
// interactive quiz-taking flow (spec §9.1) is student-built UI on top.
export function QaItemView({ payload }: ArchetypeRendererProps) {
  const [revealed, setRevealed] = useState(false);
  const choices = (payload.choices as string[]) ?? [];
  const correct = payload.correct_index as number;

  return (
    <div className="border-b border-slate-100 dark:border-slate-800 py-2 last:border-b-0">
      <div className="font-medium">{String(payload.question ?? "")}</div>
      <ol className="mt-1 space-y-0.5">
        {choices.map((choice, i) => (
          <li
            key={i}
            className={`rounded px-2 py-0.5 text-sm ${
              revealed && i === correct
                ? "bg-emerald-100 dark:bg-emerald-900/50 text-emerald-800 dark:text-emerald-300"
                : "text-slate-600 dark:text-slate-300"
            }`}
          >
            {String.fromCharCode(65 + i)}. {choice}
          </li>
        ))}
      </ol>
      <button
        className="mt-1 text-xs text-blue-600 dark:text-blue-400 hover:underline"
        onClick={() => setRevealed((r) => !r)}
      >
        {revealed ? "hide answer" : "show answer"}
      </button>
      <SourceRefs refs={payload.source_refs as SourceRef[]} />
    </div>
  );
}
