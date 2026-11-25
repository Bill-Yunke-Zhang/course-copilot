import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Resource } from "../lib/api";
import { ItemTree } from "../components/ItemTree";
import { TagChips } from "../components/TagChips";

const statusStyles: Record<string, string> = {
  pending: "bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400",
  ingesting: "bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300",
  ready: "bg-emerald-100 dark:bg-emerald-900/50 text-emerald-700 dark:text-emerald-300",
  error: "bg-red-100 dark:bg-red-900/50 text-red-700 dark:text-red-300",
};

export function Resources() {
  const [resources, setResources] = useState<Resource[]>([]);
  const [vocabulary, setVocabulary] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [form, setForm] = useState({ kind: "url", locator: "", title: "" });
  const [privateRepo, setPrivateRepo] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const filePicker = useRef<HTMLInputElement>(null);
  const folderPicker = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      setResources(await api.resources());
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    api.tags().then(setVocabulary).catch(() => {});
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function add() {
    if (!form.locator.trim()) return;
    try {
      await api.createResource({
        ...form,
        locator: form.locator.trim(),
        // A private repo is fetched with the stored PAT via SecretStore; a
        // public repo is cloned anonymously and needs no credential.
        credential_ref:
          form.kind === "repo" && privateRepo ? "gitlab-pat" : undefined,
      });
      setForm({ ...form, locator: "", title: "" });
      setError(null);
      refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  async function upload(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return;
    try {
      await api.uploadResource(Array.from(fileList), form.title);
      setForm({ ...form, title: "" });
      setError(null);
      refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      if (filePicker.current) filePicker.current.value = "";
      if (folderPicker.current) folderPicker.current.value = "";
    }
  }

  async function saveTags(itemId: number, tags: string[]) {
    await api.updateItemTags(itemId, tags);
    refresh();
  }

  function toggle(id: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Resources</h1>

      <section className="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-4 shadow-sm">
        <h2 className="mb-2 font-semibold">Add a resource</h2>
        <div className="flex flex-wrap gap-2">
          <select
            className="rounded border border-slate-300 dark:border-slate-600 dark:bg-slate-800 px-2 py-1.5 text-sm"
            value={form.kind}
            onChange={(e) => setForm({ ...form, kind: e.target.value })}
          >
            <option value="url">URL (schedule, syllabus, reading…)</option>
            <option value="file">Local file (notes, PDF…)</option>
            <option value="repo">GitLab repo</option>
          </select>
          {form.kind !== "file" && (
            <input
              className="min-w-64 flex-1 rounded border border-slate-300 dark:border-slate-600 dark:bg-slate-800 px-2 py-1.5 text-sm"
              placeholder={
                form.kind === "url"
                  ? "https://…"
                  : "https://git.doit.wisc.edu/…/lectures-netid.git"
              }
              value={form.locator}
              onChange={(e) => setForm({ ...form, locator: e.target.value })}
            />
          )}
          <input
            className="rounded border border-slate-300 dark:border-slate-600 dark:bg-slate-800 px-2 py-1.5 text-sm"
            placeholder="title (optional)"
            value={form.title}
            onChange={(e) => setForm({ ...form, title: e.target.value })}
          />
          {form.kind === "file" ? (
            <>
              <input
                ref={filePicker}
                type="file"
                multiple
                className="hidden"
                onChange={(e) => upload(e.target.files)}
              />
              <input
                ref={folderPicker}
                type="file"
                className="hidden"
                // Non-standard but universally supported attribute enabling
                // whole-directory selection.
                {...({ webkitdirectory: "" } as Record<string, string>)}
                onChange={(e) => upload(e.target.files)}
              />
              <button
                onClick={() => filePicker.current?.click()}
                className="rounded bg-blue-600 px-4 py-1.5 text-sm text-white hover:bg-blue-700"
              >
                Choose file(s)…
              </button>
              <button
                onClick={() => folderPicker.current?.click()}
                className="rounded bg-blue-600 px-4 py-1.5 text-sm text-white hover:bg-blue-700"
              >
                Choose folder…
              </button>
            </>
          ) : (
            <button
              onClick={add}
              className="rounded bg-blue-600 px-4 py-1.5 text-sm text-white hover:bg-blue-700"
            >
              Add
            </button>
          )}
        </div>
        {form.kind === "file" && (
          <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
            Picked files are uploaded into the app as a snapshot; a folder
            becomes one resource with a taggable item per file. Re-ingest
            re-reads the uploaded snapshot — if you edit the originals,
            upload again. Advanced: files already mounted into the
            containers (e.g. /data/sample_resources/…) can also be added by
            path via the API; for those, re-ingest picks up live edits.
          </p>
        )}
        {form.kind === "repo" && (
          <label className="mt-2 flex items-center gap-2 text-xs text-slate-600 dark:text-slate-300">
            <input
              type="checkbox"
              checked={privateRepo}
              onChange={(e) => setPrivateRepo(e.target.checked)}
            />
            Private repo (fetched with your GitLab PAT via SecretStore)
          </label>
        )}
        {form.kind === "repo" && privateRepo && (
          <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
            Private repos are fetched with the GitLab PAT stored on the{" "}
            <Link to="/settings" className="underline">
              Settings
            </Link>{" "}
            page. Add it there first, or ingestion reports an error (public
            repos need no token: uncheck the box).
          </p>
        )}
        {error && <p className="mt-2 text-xs text-red-600 dark:text-red-400">{error}</p>}
      </section>

      <section className="space-y-3">
        {resources.map((resource) => (
          <div
            key={resource.id}
            className="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-4 shadow-sm"
          >
            <div className="flex flex-wrap items-center gap-2">
              <button
                onClick={() => toggle(resource.id)}
                className="text-slate-400 dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300"
              >
                {expanded.has(resource.id) ? "▾" : "▸"}
              </button>
              <span className="rounded bg-slate-100 dark:bg-slate-800 px-1.5 py-0.5 text-xs uppercase text-slate-500 dark:text-slate-400">
                {resource.kind}
              </span>
              <span className="font-medium">
                {resource.title || resource.locator}
              </span>
              <span
                className={`rounded-full px-2 py-0.5 text-xs ${
                  statusStyles[resource.ingest_status] ?? ""
                }`}
              >
                {resource.ingest_status}
              </span>
              {resource.discovered_from_id !== null && (
                <span
                  className="rounded-full bg-purple-100 dark:bg-purple-900/50 px-2 py-0.5 text-xs text-purple-700 dark:text-purple-300"
                  title={`Auto-discovered by crawling resource #${resource.discovered_from_id}`}
                >
                  discovered
                </span>
              )}
              <span className="ml-auto flex gap-2">
                <button
                  onClick={() => api.reingestResource(resource.id).then(refresh)}
                  className="text-xs text-slate-400 dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300"
                >
                  re-ingest
                </button>
                <button
                  onClick={() => api.deleteResource(resource.id).then(refresh)}
                  className="text-xs text-red-400 dark:text-red-500 hover:text-red-600 dark:hover:text-red-400"
                >
                  delete
                </button>
              </span>
            </div>
            <div className="mt-1 truncate text-xs text-slate-400 dark:text-slate-500">
              {resource.locator}
            </div>
            {resource.ingest_error && (
              <div className="mt-2 rounded border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/40 p-2 text-xs text-red-700 dark:text-red-300">
                {resource.ingest_error}
              </div>
            )}

            {/* Single-item resources show tags inline; repos expand to a
                per-file list (repo resources are tagged per file/subfolder). */}
            {resource.items.length === 1 && (
              <div className="mt-2">
                <TagChips
                  tags={resource.items[0].tags}
                  vocabulary={vocabulary}
                  onChange={(tags) => saveTags(resource.items[0].id, tags)}
                />
              </div>
            )}
            {expanded.has(resource.id) && resource.items.length !== 1 && (
              <div className="mt-3 max-h-96 overflow-y-auto border-t border-slate-100 dark:border-slate-800 pt-2">
                {resource.items.length === 0 ? (
                  <p className="text-xs text-slate-400 dark:text-slate-500">no items yet</p>
                ) : (
                  <ItemTree
                    items={resource.items}
                    vocabulary={vocabulary}
                    onSaveTags={saveTags}
                    onBulkTags={(prefix, tags) =>
                      api.bulkUpdateTags(resource.id, prefix, tags).then(refresh)
                    }
                  />
                )}
              </div>
            )}
            {expanded.has(resource.id) && resource.items.length === 1 && (
              <pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap border-t border-slate-100 dark:border-slate-800 pt-2 text-xs text-slate-500 dark:text-slate-400">
                {resource.items[0].content_preview}
              </pre>
            )}
          </div>
        ))}
        {resources.length === 0 && (
          <p className="text-sm text-slate-400 dark:text-slate-500">
            No resources yet. Add your course's schedule URL to get started —
            the crawler will discover readings, videos, and project specs
            from it automatically.
          </p>
        )}
      </section>
    </div>
  );
}
