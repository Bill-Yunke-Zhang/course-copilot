import { KeyRound } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, type CredentialStatus, type Resource } from "../lib/api";

// The one credential private repos use (GitLab PATs are account-scoped).
const PAT_REF = "gitlab-pat";

// The backend stores naive UTC timestamps; mark them as UTC before parsing.
function asUtc(timestamp: string): Date {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(timestamp) ? timestamp : timestamp + "Z");
}

/**
 * Settings (T4): enter, replace or remove the GitLab PAT. The token is sent
 * once to the backend, which encrypts it; this page never reads it back and
 * clears the field as soon as it is saved.
 */
export function Settings() {
  const [status, setStatus] = useState<CredentialStatus | null>(null);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Private repos whose last ingestion failed, e.g. for want of a token.
  const [failedRepos, setFailedRepos] = useState<Resource[]>([]);

  const load = useCallback(async () => {
    try {
      setStatus(await api.credentialStatus(PAT_REF));
      const resources = await api.resources();
      setFailedRepos(
        resources.filter(
          (r) => r.kind === "repo" && r.credential_ref === PAT_REF && r.ingest_status === "error",
        ),
      );
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!token.trim()) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      setStatus(await api.saveCredential(PAT_REF, token));
      setMessage("Token saved. It is stored encrypted and will not be shown again.");
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setToken(""); // never keep the token in page state longer than needed
      setBusy(false);
    }
  }

  async function remove() {
    if (!confirm("Remove the stored GitLab PAT? Private repos will stop updating.")) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await api.deleteCredential(PAT_REF);
      setMessage("Token removed.");
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function reingestFailed() {
    setBusy(true);
    setError(null);
    try {
      await Promise.all(failedRepos.map((r) => api.reingestResource(r.id)));
      setMessage(`Re-ingesting ${failedRepos.length} private repo(s).`);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Settings</h1>

      <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900">
        <div className="flex items-center gap-2">
          <KeyRound size={16} />
          <h2 className="font-semibold">GitLab personal access token</h2>
        </div>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
          Used only to clone and pull private repos (your lecture repo and team project). Create a
          new token just for Course Copilot with the <code>read_repository</code> scope and a near
          expiry date, and enter it here, not anywhere else.
        </p>

        <div className="mt-3 text-sm">
          {status === null ? (
            <span className="text-slate-500">Checking…</span>
          ) : status.stored ? (
            <span className="text-emerald-700 dark:text-emerald-300">
              A token is stored
              {status.updated_at && <> (saved {asUtc(status.updated_at).toLocaleString()})</>}.
            </span>
          ) : (
            <span className="text-amber-700 dark:text-amber-300">
              No token stored. Private repos can&apos;t be fetched until you add one.
            </span>
          )}
        </div>

        <form onSubmit={save} className="mt-3 flex flex-wrap gap-2">
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={status?.stored ? "Paste a new token to replace it" : "glpat-…"}
            autoComplete="off"
            spellCheck={false}
            aria-label="GitLab personal access token"
            className="min-w-0 flex-1 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-950"
          />
          <button
            type="submit"
            disabled={busy || !token.trim()}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-700 disabled:opacity-50 dark:bg-white dark:text-slate-900 dark:hover:bg-slate-200"
          >
            {status?.stored ? "Replace token" : "Save token"}
          </button>
          {status?.stored && (
            <button
              type="button"
              onClick={remove}
              disabled={busy}
              className="rounded-md border border-red-300 px-3 py-1.5 text-sm text-red-700 hover:bg-red-50 disabled:opacity-50 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950/50"
            >
              Remove
            </button>
          )}
        </form>

        {message && <p className="mt-2 text-sm text-emerald-700 dark:text-emerald-300">{message}</p>}
        {error && <p className="mt-2 text-sm text-red-600 dark:text-red-400">{error}</p>}

        {status?.stored && failedRepos.length > 0 && (
          <div className="mt-3 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950/50 dark:text-amber-300">
            {failedRepos.length} private repo(s) failed to ingest earlier:
            <ul className="mt-1 list-disc pl-5">
              {failedRepos.map((r) => (
                <li key={r.id} className="break-all">
                  {r.title || r.locator}
                </li>
              ))}
            </ul>
            <button
              onClick={reingestFailed}
              disabled={busy}
              className="mt-2 rounded-md bg-amber-600 px-3 py-1 text-xs text-white hover:bg-amber-700 disabled:opacity-50"
            >
              Re-ingest them now
            </button>
          </div>
        )}
      </section>
    </div>
  );
}
