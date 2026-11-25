import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { SourceRefs } from "../archetypes/SourceRefs";
import { api, type AgentOutput, type OutputItem, type SourceRef } from "../lib/api";

const AGENT_ID = "spec_checklist_agent";
const FINISHED = new Set(["succeeded", "failed"]);
const POLL_MS = 2000;
// Per-student UI state lives in this browser only (see docs/custom-agent.md).
const TICKS_KEY = "spec-checklist:ticks";
const SEEN_KEY = "spec-checklist:seen";

type Kind = "must" | "must_not" | "graded";
const KINDS: Kind[] = ["must", "must_not", "graded"];
const kindLabels: Record<Kind, string> = {
  must: "Must",
  must_not: "Must not",
  graded: "Graded",
};
const kindStyles: Record<string, string> = {
  must: "bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300",
  must_not: "bg-red-100 dark:bg-red-900/50 text-red-700 dark:text-red-300",
  graded: "bg-violet-100 dark:bg-violet-900/50 text-violet-700 dark:text-violet-300",
};

interface Requirement {
  item: OutputItem;
  key: string; // hash of the normalized excerpt: stable across runs
  task: string;
  kind: string;
  dueDate: string | null;
  excerpt: string;
}

interface Group {
  task: string;
  dueDate: string | null;
  requirements: Requirement[];
}

// FNV-1a over the normalized excerpt, so a tick follows the requirement
// itself rather than a run-specific item id.
function excerptKey(excerpt: string): string {
  const text = excerpt.replace(/\s+/g, " ").trim().toLowerCase();
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16);
}

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage unavailable (private window, blocked): ticks just won't persist.
  }
}

function toRequirement(item: OutputItem): Requirement {
  const p = item.payload;
  const refs = p.source_refs as SourceRef[];
  const excerpt = refs.find((r) => r.excerpt)?.excerpt ?? String(p.label ?? "");
  return {
    item,
    key: excerptKey(excerpt),
    task: typeof p.task === "string" && p.task ? p.task : "General",
    kind: typeof p.state === "string" ? p.state : "must",
    dueDate: typeof p.due_date === "string" ? p.due_date : null,
    excerpt,
  };
}

// Groups ordered by their earliest due date; undated groups last, then by name.
function groupByTask(requirements: Requirement[]): Group[] {
  const groups = new Map<string, Group>();
  for (const r of requirements) {
    const group = groups.get(r.task) ?? { task: r.task, dueDate: null, requirements: [] };
    group.requirements.push(r);
    if (r.dueDate && (!group.dueDate || r.dueDate < group.dueDate)) group.dueDate = r.dueDate;
    groups.set(r.task, group);
  }
  return [...groups.values()].sort((a, b) => {
    if (a.dueDate && b.dueDate && a.dueDate !== b.dueDate) return a.dueDate < b.dueDate ? -1 : 1;
    if (a.dueDate && !b.dueDate) return -1;
    if (!a.dueDate && b.dueDate) return 1;
    return a.task.localeCompare(b.task, undefined, { numeric: true });
  });
}

/**
 * Spec Checklist (T8): every hard requirement from the project spec and
 * instructions, grouped by task, ticked off by the student. Ticks and the
 * "new since last check" marker are kept in this browser.
 */
export function Checklist() {
  const [output, setOutput] = useState<AgentOutput | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ticks, setTicks] = useState<Set<string>>(
    () => new Set(readJson<string[]>(TICKS_KEY, [])),
  );
  const [kinds, setKinds] = useState<Set<string>>(() => new Set(KINDS));
  const [hideDone, setHideDone] = useState(false);
  const [newKeys, setNewKeys] = useState<Set<string>>(new Set());

  const load = useCallback(async () => {
    try {
      setOutput(await api.agentOutput(AGENT_ID));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const requirements = useMemo(() => (output?.items ?? []).map(toRequirement), [output]);

  // Mark requirements this browser had not seen in the previous run's output.
  useEffect(() => {
    const run = output?.run;
    if (!run) return;
    const current = requirements.map((r) => r.key);
    const seen = readJson<{ runId: number; keys: string[]; prevKeys: string[] | null } | null>(
      SEEN_KEY,
      null,
    );
    let prevKeys: string[] | null;
    if (seen && seen.runId === run.id) {
      prevKeys = seen.prevKeys;
    } else {
      prevKeys = seen ? seen.keys : null;
      writeJson(SEEN_KEY, { runId: run.id, keys: current, prevKeys });
    }
    const before = new Set(prevKeys ?? current);
    setNewKeys(new Set(current.filter((k) => !before.has(k))));
  }, [output, requirements]);

  function toggleTick(key: string) {
    const next = new Set(ticks);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setTicks(next);
    writeJson(TICKS_KEY, [...next]);
  }

  function toggleKind(kind: string) {
    const next = new Set(kinds);
    if (next.has(kind)) next.delete(kind);
    else next.add(kind);
    setKinds(next);
  }

  async function recheck() {
    setChecking(true);
    setError(null);
    try {
      let run = await api.triggerRun(AGENT_ID, undefined, true);
      while (!FINISHED.has(run.status)) {
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        run = await api.run(run.id);
      }
      if (run.status === "failed") setError(run.error ?? "The check failed.");
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setChecking(false);
    }
  }

  const visible = requirements.filter(
    (r) => kinds.has(r.kind) && !(hideDone && ticks.has(r.key)),
  );
  const groups = groupByTask(visible);
  const doneCount = requirements.filter((r) => ticks.has(r.key)).length;
  const summary = output?.run?.verification_summary ?? null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Spec Checklist</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Every requirement in the project spec and instructions, each quoting its source.
            {requirements.length > 0 && (
              <>
                {" "}
                {doneCount} of {requirements.length} done.
              </>
            )}
          </p>
        </div>
        <button
          onClick={recheck}
          disabled={checking}
          className="flex items-center gap-2 rounded-md bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-700 disabled:opacity-60 dark:bg-white dark:text-slate-900 dark:hover:bg-slate-200"
        >
          <RefreshCw size={14} className={checking ? "animate-spin" : ""} />
          {checking ? "Checking…" : "Re-check spec"}
        </button>
      </div>

      {error && (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/50 dark:text-red-300">
          {error}
        </div>
      )}

      {summary && summary.items_dropped > 0 && (
        <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950/50 dark:text-amber-300">
          {summary.items_total - summary.items_dropped} of {summary.items_total} requirements
          verified; {summary.items_dropped} dropped because the cited text did not support them.
        </div>
      )}

      {requirements.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {KINDS.map((kind) => (
            <button
              key={kind}
              onClick={() => toggleKind(kind)}
              aria-pressed={kinds.has(kind)}
              className={`rounded-full px-3 py-1 text-xs ${
                kinds.has(kind)
                  ? kindStyles[kind]
                  : "bg-slate-100 text-slate-400 line-through dark:bg-slate-800 dark:text-slate-500"
              }`}
            >
              {kindLabels[kind]}
            </button>
          ))}
          <label className="ml-2 flex items-center gap-1.5 text-xs text-slate-600 dark:text-slate-300">
            <input
              type="checkbox"
              checked={hideDone}
              onChange={(e) => setHideDone(e.target.checked)}
            />
            Hide done
          </label>
        </div>
      )}

      {output && requirements.length === 0 && !checking && (
        <div className="rounded-lg border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500 dark:border-slate-700 dark:text-slate-400">
          {output.run
            ? "The last check found no requirements."
            : "No checklist yet. Add the project spec or instructions on the Resources page (tagged project-spec or project-instructions), then press Re-check spec."}
        </div>
      )}

      {groups.map((group) => {
        const all = requirements.filter((r) => r.task === group.task);
        const done = all.filter((r) => ticks.has(r.key)).length;
        return (
          <section
            key={group.task}
            className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
          >
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="font-semibold">
                {group.task}
                {group.dueDate && (
                  <span className="ml-2 text-xs font-normal text-slate-500 dark:text-slate-400">
                    Due {group.dueDate}
                  </span>
                )}
              </h2>
              <span className="text-xs text-slate-500 dark:text-slate-400">
                {done} / {all.length} done
              </span>
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
              <div
                className="h-full rounded-full bg-emerald-500"
                style={{ width: `${all.length ? (100 * done) / all.length : 0}%` }}
              />
            </div>
            <ul className="mt-3 divide-y divide-slate-100 dark:divide-slate-800">
              {group.requirements.map((r) => (
                <RequirementRow
                  key={r.item.id}
                  requirement={r}
                  done={ticks.has(r.key)}
                  isNew={newKeys.has(r.key)}
                  onToggle={() => toggleTick(r.key)}
                />
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

function RequirementRow({
  requirement,
  done,
  isNew,
  onToggle,
}: {
  requirement: Requirement;
  done: boolean;
  isNew: boolean;
  onToggle: () => void;
}) {
  const [open, setOpen] = useState(false);
  const p = requirement.item.payload;
  return (
    <li className="py-2">
      <div className="flex items-start gap-3">
        <input
          type="checkbox"
          checked={done}
          onChange={onToggle}
          aria-label={`Mark "${String(p.label ?? "")}" done`}
          className="mt-1"
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`font-medium ${done ? "text-slate-400 line-through" : ""}`}>
              {String(p.label ?? "")}
            </span>
            <span
              className={`rounded-full px-2 py-0.5 text-xs ${
                kindStyles[requirement.kind] ?? "bg-slate-100 text-slate-500 dark:bg-slate-800"
              }`}
            >
              {kindLabels[requirement.kind as Kind] ?? requirement.kind}
            </span>
            {isNew && (
              <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-700 dark:bg-amber-900/50 dark:text-amber-300">
                New since last check
              </span>
            )}
          </div>
          <div className="text-sm text-slate-600 dark:text-slate-300">{String(p.detail ?? "")}</div>
          <button
            onClick={() => setOpen(!open)}
            className="mt-1 flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
          >
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            Source
          </button>
          {open && (
            <div className="mt-1">
              <blockquote className="border-l-4 border-blue-300 bg-blue-50 p-2 text-sm italic dark:border-blue-800 dark:bg-blue-950/50">
                “{requirement.excerpt}”
              </blockquote>
              <SourceRefs refs={p.source_refs as SourceRef[]} />
            </div>
          )}
        </div>
      </div>
    </li>
  );
}
