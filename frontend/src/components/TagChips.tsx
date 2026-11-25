import { useState } from "react";

/**
 * Editable tag chips for one resource item. Tags always come from the fixed
 * vocabulary — the classifier's assignment is a suggestion the user may
 * correct, and the backend treats both identically.
 */
export function TagChips({
  tags,
  vocabulary,
  onChange,
}: {
  tags: string[];
  vocabulary: string[];
  onChange: (tags: string[]) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<string[]>(tags);

  if (!editing) {
    return (
      <span className="inline-flex flex-wrap items-center gap-1">
        {tags.length === 0 && (
          <span className="text-xs italic text-slate-400 dark:text-slate-500">no tags</span>
        )}
        {tags.map((tag) => (
          <span
            key={tag}
            className="rounded-full bg-blue-50 dark:bg-blue-950/50 px-2 py-0.5 text-xs text-blue-700 dark:text-blue-300"
          >
            {tag}
          </span>
        ))}
        <button
          className="text-xs text-slate-400 underline decoration-dotted dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300"
          title="Choose tags from the fixed vocabulary"
          onClick={() => {
            setDraft(tags);
            setEditing(true);
          }}
        >
          edit tags
        </button>
      </span>
    );
  }

  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <span className="mr-1 text-xs italic text-slate-400 dark:text-slate-500">
        click to toggle:
      </span>
      {vocabulary.map((tag) => {
        const active = draft.includes(tag);
        return (
          <button
            key={tag}
            onClick={() =>
              setDraft(active ? draft.filter((t) => t !== tag) : [...draft, tag])
            }
            className={`rounded-full px-2 py-0.5 text-xs ${
              active
                ? "bg-blue-600 text-white"
                : "bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-700"
            }`}
          >
            {tag}
          </button>
        );
      })}
      <button
        className="ml-1 text-xs font-medium text-emerald-600 dark:text-emerald-400"
        onClick={() => {
          onChange(draft);
          setEditing(false);
        }}
      >
        save
      </button>
      <button
        className="text-xs text-slate-400 dark:text-slate-500"
        onClick={() => setEditing(false)}
      >
        cancel
      </button>
    </span>
  );
}
