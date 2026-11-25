import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { SourceRefs } from "../archetypes/SourceRefs";
import {
  api,
  type AgentOutput,
  type OutputItem,
  type SourceRef,
  type ToolCallTrace,
} from "../lib/api";

const AGENT_ID = "repo_progress_agent";
// Run statuses after which polling stops.
const FINISHED = new Set(["succeeded", "failed"]);
const POLL_MS = 2000;

const stateStyles: Record<string, string> = {
  done: "bg-emerald-100 dark:bg-emerald-900/50 text-emerald-700 dark:text-emerald-300",
  on_track: "bg-emerald-100 dark:bg-emerald-900/50 text-emerald-700 dark:text-emerald-300",
  behind: "bg-red-100 dark:bg-red-900/50 text-red-700 dark:text-red-300",
  not_started: "bg-amber-100 dark:bg-amber-900/50 text-amber-700 dark:text-amber-300",
  unknown: "bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400",
};

const stateLabels: Record<string, string> = {
  done: "Done",
  on_track: "On track",
  behind: "Behind",
  not_started: "Not started",
  unknown: "Unknown",
};

const callStyles: Record<string, string> = {
  ok: "text-emerald-700 dark:text-emerald-300",
  refused: "text-red-700 dark:text-red-300",
  not_allowed: "text-red-700 dark:text-red-300",
  error: "text-amber-700 dark:text-amber-300",
};

// The backend stores naive UTC timestamps; mark them as UTC before parsing.
function asUtc(timestamp: string): Date {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(timestamp) ? timestamp : timestamp + "Z");
}

/**
 * Assignment Tracker (T5): one card per assignment from the Repo Progress
 * agent, each expandable to the evidence it rests on and the run's
 * tool-call trace. "Check now" forces a fresh run past the cache.
 */
export function AssignmentTracker() {
  const [output, setOutput] = useState<AgentOutput | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The run's trace is shared by every card, so it is loaded once per run.
  const [trace, setTrace] = useState<ToolCallTrace[] | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api.agentOutput(AGENT_ID);
      setOutput(next);
      setTrace(null);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function checkNow() {
    setChecking(true);
    setError(null);
    try {
      let run = await api.triggerRun(AGENT_ID, undefined, true);
      while (!FINISHED.has(run.status)) {
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        run = await api.run(run.id);
      }
      if (run.status === "failed") {
        setError(run.error ?? "The check failed.");
      }
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setChecking(false);
    }
  }

  async function loadTrace() {
    if (trace !== null || !output?.run) return;
    try {
      setTrace(await api.toolCalls(output.run.id));
    } catch {
      setTrace([]);
    }
  }

  const run = output?.run ?? null;
  const summary = run?.verification_summary ?? null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Assignment Tracker</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Progress on each assignment, judged from your team&apos;s project repo.
            {run?.finished_at && (
              <> Last checked {asUtc(run.finished_at).toLocaleString()}.</>
            )}
          </p>
        </div>
        <button
          onClick={checkNow}
          disabled={checking}
          className="flex items-center gap-2 rounded-md bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-700 disabled:opacity-60 dark:bg-white dark:text-slate-900 dark:hover:bg-slate-200"
        >
          <RefreshCw size={14} className={checking ? "animate-spin" : ""} />
          {checking ? "Checking…" : "Check now"}
        </button>
      </div>

      {error && (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/50 dark:text-red-300">
          {error}
        </div>
      )}

      {summary && summary.items_dropped > 0 && (
        <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950/50 dark:text-amber-300">
          {summary.items_total - summary.items_dropped} of {summary.items_total} statuses
          verified; {summary.items_dropped} dropped because the evidence did not support them.
          {summary.notes.length > 0 && (
            <ul className="mt-1 list-disc pl-5">
              {summary.notes.map((note, i) => (
                <li key={i}>{note}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {output && output.items.length === 0 && !checking && (
        <div className="rounded-lg border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500 dark:border-slate-700 dark:text-slate-400">
          {run
            ? "The last check found nothing to report."
            : "No check has run yet. Add your project repo on the Resources page (tagged project-code), then press Check now."}
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        {output?.items.map((item) => (
          <AssignmentCard key={item.id} item={item} trace={trace} onExpand={loadTrace} />
        ))}
      </div>
    </div>
  );
}

function AssignmentCard({
  item,
  trace,
  onExpand,
}: {
  item: OutputItem;
  trace: ToolCallTrace[] | null;
  onExpand: () => void;
}) {
  const [open, setOpen] = useState(false);
  const p = item.payload;
  const state = typeof p.state === "string" ? p.state : "unknown";
  const refs = p.source_refs as SourceRef[];
  const evidence = (refs.find((r) => r.evidence)?.evidence ?? {}) as {
    files?: string[];
    commits?: string[];
    tool_steps?: number[];
  };
  const usedSteps = new Set(evidence.tool_steps ?? []);

  function toggle() {
    if (!open) onExpand();
    setOpen(!open);
  }

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="font-medium">{String(p.assignment ?? p.label ?? "")}</div>
          {typeof p.due_date === "string" && (
            <div className="text-xs text-slate-500 dark:text-slate-400">Due {p.due_date}</div>
          )}
        </div>
        <span
          className={`shrink-0 rounded-full px-2 py-0.5 text-xs ${stateStyles[state] ?? stateStyles.unknown}`}
        >
          {stateLabels[state] ?? state}
        </span>
      </div>
      <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{String(p.detail ?? "")}</p>
      <SourceRefs refs={refs} />

      <button
        onClick={toggle}
        className="mt-3 flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        Evidence and tool-call trace
      </button>

      {open && (
        <div className="mt-2 space-y-3 text-xs">
          <EvidenceList title="Files" values={evidence.files} />
          <EvidenceList title="Commits" values={evidence.commits} />
          <div>
            <div className="mb-1 font-medium text-slate-700 dark:text-slate-200">
              Tool calls in this check
            </div>
            {trace === null ? (
              <div className="text-slate-500">Loading…</div>
            ) : trace.length === 0 ? (
              <div className="text-slate-500">No tool calls were recorded.</div>
            ) : (
              <ol className="space-y-1">
                {trace.map((call) => (
                  <li
                    key={call.id}
                    className={`rounded p-1.5 ${
                      usedSteps.has(call.step)
                        ? "bg-blue-50 dark:bg-blue-950/50"
                        : "bg-slate-50 dark:bg-slate-800"
                    }`}
                  >
                    <div className="flex flex-wrap items-center gap-2 font-mono">
                      <span className="text-slate-400">#{call.step}</span>
                      <span>{call.name}</span>
                      <span className="text-slate-500">{JSON.stringify(call.args)}</span>
                      <span className={`ml-auto ${callStyles[call.status] ?? ""}`}>
                        {call.status}
                      </span>
                    </div>
                    {call.result_preview && (
                      <div className="mt-0.5 truncate text-slate-500" title={call.result_preview}>
                        {call.result_preview}
                      </div>
                    )}
                  </li>
                ))}
              </ol>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function EvidenceList({ title, values }: { title: string; values?: string[] }) {
  if (!values || values.length === 0) return null;
  return (
    <div>
      <div className="mb-1 font-medium text-slate-700 dark:text-slate-200">{title}</div>
      <ul className="space-y-0.5 font-mono text-slate-600 dark:text-slate-300">
        {values.map((v) => (
          <li key={v} className="truncate" title={v}>
            {v}
          </li>
        ))}
      </ul>
    </div>
  );
}
