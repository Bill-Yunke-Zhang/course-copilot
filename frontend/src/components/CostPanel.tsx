import { useEffect, useState } from "react";
import { api, type CostSummary } from "../lib/api";

/** The §13 cost-accounting report: cumulative LLM spend and how much the
 * caching policy saved ("N of M requested runs served from cache"). */
export function CostPanel() {
  const [summary, setSummary] = useState<CostSummary | null>(null);

  useEffect(() => {
    const load = () => api.costs().then(setSummary).catch(() => {});
    load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, []);

  if (!summary) return null;

  return (
    <section className="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-4 shadow-sm">
      <h2 className="mb-3 font-semibold">Token &amp; Cost Accounting</h2>
      <div className="grid gap-4 sm:grid-cols-3">
        <div>
          <div className="text-2xl font-semibold">
            ${summary.total_cost_usd.toFixed(4)}
          </div>
          <div className="text-xs text-slate-500 dark:text-slate-400">
            total spend · {summary.total_calls} LLM calls
          </div>
        </div>
        <div>
          <div className="text-2xl font-semibold">
            {summary.runs_from_cache}
            <span className="text-base font-normal text-slate-400 dark:text-slate-500">
              {" "}
              / {summary.runs_requested}
            </span>
          </div>
          <div className="text-xs text-slate-500 dark:text-slate-400">
            requested runs served from cache
          </div>
        </div>
        <div className="text-xs text-slate-500 dark:text-slate-400">
          {Object.entries(summary.by_purpose).map(([purpose, cost]) => (
            <div key={purpose} className="flex justify-between gap-2">
              <span>{purpose}</span>
              <span>${cost.toFixed(4)}</span>
            </div>
          ))}
        </div>
      </div>
      {summary.by_agent.length > 0 && (
        <table className="mt-4 w-full text-left text-xs">
          <thead className="text-slate-400 dark:text-slate-500">
            <tr>
              <th className="py-1 font-normal">agent</th>
              <th className="py-1 font-normal">calls</th>
              <th className="py-1 font-normal">tokens in/out</th>
              <th className="py-1 font-normal">cost</th>
            </tr>
          </thead>
          <tbody>
            {summary.by_agent.map((row) => (
              <tr key={row.agent_id} className="border-t border-slate-100 dark:border-slate-800">
                <td className="py-1">{row.agent_id}</td>
                <td className="py-1">{row.calls}</td>
                <td className="py-1">
                  {row.input_tokens.toLocaleString()} /{" "}
                  {row.output_tokens.toLocaleString()}
                </td>
                <td className="py-1">${row.cost_usd.toFixed(4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
