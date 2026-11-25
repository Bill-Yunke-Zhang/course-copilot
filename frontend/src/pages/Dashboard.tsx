import { useCallback, useEffect, useState } from "react";
import { api, type DashboardCard } from "../lib/api";
import { getRenderer, UnknownArchetype } from "../archetypes/registry";
import { CostPanel } from "../components/CostPanel";

type WeekFilter = "all" | "this" | "next";

function inWeek(dateString: string, which: "this" | "next"): boolean {
  const date = new Date(dateString + "T00:00:00");
  if (isNaN(date.getTime())) return true;
  const now = new Date();
  const day = (now.getDay() + 6) % 7; // Monday = 0
  const monday = new Date(now);
  monday.setDate(now.getDate() - day);
  monday.setHours(0, 0, 0, 0);
  const start = new Date(monday);
  if (which === "next") start.setDate(start.getDate() + 7);
  const end = new Date(start);
  end.setDate(end.getDate() + 7);
  return date >= start && date < end;
}

export function Dashboard() {
  const [cards, setCards] = useState<DashboardCard[]>([]);
  const [week, setWeek] = useState<WeekFilter>("all");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setCards(await api.dashboard());
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function runNow(agentId: string) {
    // The explicit button always forces a fresh run; cache hits still
    // happen on automatic (resource-change) triggers.
    await api.triggerRun(agentId, undefined, true);
    refresh();
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">This Week</h1>
        <div className="flex gap-1 rounded-lg bg-slate-100 dark:bg-slate-800 p-1 text-sm">
          {(["all", "this", "next"] as const).map((option) => (
            <button
              key={option}
              onClick={() => setWeek(option)}
              className={`rounded-md px-3 py-1 ${
                week === option ? "bg-white dark:bg-slate-700 shadow" : "text-slate-500 dark:text-slate-400"
              }`}
            >
              {option === "all" ? "All" : option === "this" ? "This week" : "Next week"}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="rounded border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/40 p-3 text-sm text-red-700 dark:text-red-300">
          {error}
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {cards.map((card) => {
          const Renderer = getRenderer(card.manifest.output_archetype);
          // The week toggle only applies to dated (timeline) items.
          const items =
            week === "all"
              ? card.items
              : card.items.filter(
                  (item) =>
                    card.manifest.output_archetype !== "timeline_item" ||
                    inWeek(String(item.payload.date ?? ""), week),
                );
          const summary = card.run?.verification_summary;
          return (
            <section
              key={card.manifest.agent_id}
              className="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-4 shadow-sm"
            >
              <header className="mb-2 flex items-start justify-between gap-2">
                <div>
                  <h2 className="font-semibold">{card.manifest.display_name}</h2>
                  <p className="text-xs text-slate-500 dark:text-slate-400">{card.manifest.description}</p>
                </div>
                <div className="flex items-center gap-2">
                  {card.stale && (
                    <span className="rounded-full bg-amber-100 dark:bg-amber-900/50 px-2 py-0.5 text-xs text-amber-700 dark:text-amber-300">
                      stale
                    </span>
                  )}
                  <button
                    onClick={() => runNow(card.manifest.agent_id)}
                    title="Run now (forces a fresh run, bypassing the cache)"
                    className="rounded bg-blue-600 px-2 py-1 text-xs text-white hover:bg-blue-700"
                  >
                    run
                  </button>
                </div>
              </header>

              {card.run?.status === "failed" && (
                <div className="mb-2 rounded border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/40 p-2 text-xs text-red-700 dark:text-red-300">
                  Last run failed: {card.run.error}
                </div>
              )}

              {items.length === 0 ? (
                <p className="py-4 text-sm text-slate-400 dark:text-slate-500">
                  {card.run ? "No items." : "No verified output yet — add resources or press run."}
                </p>
              ) : (
                <div>
                  {items.map((item) =>
                    Renderer ? (
                      <Renderer key={item.id} payload={item.payload} />
                    ) : (
                      <UnknownArchetype key={item.id} archetype={item.archetype} />
                    ),
                  )}
                </div>
              )}

              {summary && summary.items_dropped > 0 && (
                <footer className="mt-2 rounded bg-amber-50 dark:bg-amber-950/40 p-2 text-xs text-amber-800 dark:text-amber-300">
                  {summary.items_dropped} of {summary.items_total} items failed
                  grounding verification and were omitted.
                </footer>
              )}
            </section>
          );
        })}
      </div>

      <CostPanel />
    </div>
  );
}
