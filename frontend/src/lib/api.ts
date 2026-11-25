// API client + shared types (mirrors backend/app/schemas/api.py)

export interface SourceRef {
  resource_item_id: number;
  excerpt?: string;
  evidence?: Record<string, unknown>;
}

// Archetype payloads all carry source_refs plus their own fields; extra
// fields (like status_item's optional `state`) are allowed by design.
export interface ItemPayload {
  source_refs: SourceRef[];
  [key: string]: unknown;
}

export interface OutputItem {
  id: number;
  archetype: string;
  position: number;
  payload: ItemPayload;
  verification_status: string;
  verifier_note: string | null;
}

export interface Manifest {
  agent_id: string;
  display_name: string;
  description: string;
  required_resource_tags: string[];
  optional_upstream_agents: string[];
  output_archetype: string;
  trigger: string;
  cacheable: boolean;
  dashboard_widget: boolean;
}

export interface AgentRun {
  id: number;
  agent_id: string;
  status: string;
  trigger: string;
  from_cache: boolean;
  reused_run_id: number | null;
  verification_summary: {
    items_total: number;
    items_dropped: number;
    notes: string[];
  } | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface DashboardCard {
  manifest: Manifest;
  run: AgentRun | null;
  items: OutputItem[];
  stale: boolean;
}

export interface ResourceItem {
  id: number;
  path: string;
  title: string;
  tags: string[];
  meta: Record<string, unknown>;
  content_preview: string;
}

export interface Resource {
  id: number;
  kind: string;
  locator: string;
  title: string;
  discovered_from_id: number | null;
  credential_ref: string | null;
  ingest_status: string;
  ingest_error: string | null;
  created_at: string;
  items: ResourceItem[];
}

export interface CostSummary {
  total_cost_usd: number;
  total_calls: number;
  by_purpose: Record<string, number>;
  by_agent: {
    agent_id: string;
    calls: number;
    input_tokens: number;
    output_tokens: number;
    cost_usd: number;
  }[];
  runs_requested: number;
  runs_from_cache: number;
}

// GET /api/agents/{id}/output: the latest verified output (cache hits
// resolve to the run that produced the items).
export interface AgentOutput {
  run: AgentRun | null;
  items: OutputItem[];
}

// One row of GET /api/runs/{id}/tool-calls (refused calls included).
export interface ToolCallTrace {
  id: number;
  step: number;
  name: string;
  args: Record<string, unknown>;
  status: string; // ok | not_allowed | refused | error
  result_preview: string;
  created_at: string;
}

// GET/PUT /api/credentials/{ref}: whether a token is stored, never the token.
export interface CredentialStatus {
  credential_ref: string;
  stored: boolean;
  updated_at: string | null;
}

export interface ResourceItemFull {
  id: number;
  resource_id: number;
  title: string;
  path: string;
  locator: string;
  kind: string;
  tags: string[];
  meta: Record<string, unknown>;
  content: string;
}

// ── Quiz (T6): mirrors backend/app/quiz_service.py ──────────────────────────

// Requested vs. verified counts and how the scope resolved, for one run.
export interface QuizDisclosure {
  raw_request: string;
  scope: string;
  scope_label: string;
  resolution: string;
  format: string | null;
  material_item_ids: number[];
  requested_count: number | null;
  asked_count: number | null;
  generated_count: number;
  verified_count: number;
  dropped_count: number;
  verifier_notes: string[];
  agent_notes: string[];
  shortfall: boolean;
  output_run_id: number;
  from_cache: boolean;
}

export interface QuizRunStatus {
  run: AgentRun;
  disclosure: QuizDisclosure;
}

// Answers and citations are present only once the attempt is submitted.
export interface QuizAttemptQuestion {
  id: number;
  question: string;
  choices: string[];
  topic: string;
  correct_index?: number;
  source_refs?: SourceRef[];
  student_answer_index?: number | null;
  is_correct?: boolean | null;
}

export interface QuizAttempt {
  id: number;
  raw_request: string;
  resolved_scope: Partial<QuizDisclosure>;
  agent_run_id: number | null;
  score: number | null;
  total: number;
  created_at: string;
  completed_at: string | null;
  questions: QuizAttemptQuestion[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${body}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  tags: () => request<string[]>("/api/tags"),
  resources: () => request<Resource[]>("/api/resources"),
  createResource: (body: {
    kind: string;
    locator: string;
    title?: string;
    credential_ref?: string;
  }) => request<Resource>("/api/resources", { method: "POST", body: JSON.stringify(body) }),
  // File-picker path: uploads picked files (or a whole picked folder) and
  // registers them as one `file` resource.
  uploadResource: async (files: File[], title: string) => {
    const form = new FormData();
    for (const file of files) form.append("files", file);
    form.append(
      "relative_paths",
      JSON.stringify(
        files.map((f) => (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name),
      ),
    );
    form.append("title", title);
    const response = await fetch("/api/resources/upload", { method: "POST", body: form });
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}: ${await response.text()}`);
    }
    return response.json() as Promise<Resource>;
  },
  deleteResource: (id: number) =>
    request<void>(`/api/resources/${id}`, { method: "DELETE" }),
  reingestResource: (id: number) =>
    request<Resource>(`/api/resources/${id}/reingest`, { method: "POST" }),
  bulkUpdateTags: (resourceId: number, pathPrefix: string, tags: string[]) =>
    request<Resource>(`/api/resources/${resourceId}/bulk-tags`, {
      method: "PUT",
      body: JSON.stringify({ path_prefix: pathPrefix, tags }),
    }),
  updateItemTags: (itemId: number, tags: string[]) =>
    request<ResourceItem>(`/api/resource-items/${itemId}/tags`, {
      method: "PUT",
      body: JSON.stringify({ tags }),
    }),
  resourceItem: (id: number) => request<ResourceItemFull>(`/api/resource-items/${id}`),
  dashboard: () => request<DashboardCard[]>("/api/dashboard"),
  triggerRun: (agentId: string, payload?: Record<string, unknown>, force = false) =>
    request<AgentRun>(`/api/agents/${agentId}/runs`, {
      method: "POST",
      body: JSON.stringify({ payload: payload ?? null, force }),
    }),
  agentOutput: (agentId: string) => request<AgentOutput>(`/api/agents/${agentId}/output`),
  run: (runId: number) => request<AgentRun>(`/api/runs/${runId}`),
  toolCalls: (runId: number) => request<ToolCallTrace[]>(`/api/runs/${runId}/tool-calls`),
  costs: () => request<CostSummary>("/api/costs/summary"),
  credentialStatus: (ref: string) => request<CredentialStatus>(`/api/credentials/${ref}`),
  saveCredential: (ref: string, token: string) =>
    request<CredentialStatus>(`/api/credentials/${ref}`, {
      method: "PUT",
      body: JSON.stringify({ token }),
    }),
  deleteCredential: (ref: string) =>
    request<void>(`/api/credentials/${ref}`, { method: "DELETE" }),
  startQuiz: (text: string, force = false) =>
    request<{ run: AgentRun }>("/api/quiz/runs", {
      method: "POST",
      body: JSON.stringify({ text, force }),
    }),
  quizRun: (runId: number) => request<QuizRunStatus>(`/api/quiz/runs/${runId}`),
  createQuizAttempt: (runId: number) =>
    request<QuizAttempt>("/api/quiz/attempts", {
      method: "POST",
      body: JSON.stringify({ run_id: runId }),
    }),
  quizAttempts: () => request<QuizAttempt[]>("/api/quiz/attempts"),
  quizAttempt: (attemptId: number) => request<QuizAttempt>(`/api/quiz/attempts/${attemptId}`),
  submitQuizAttempt: (
    attemptId: number,
    answers: { question_id: number; answer_index: number | null }[],
  ) =>
    request<QuizAttempt>(`/api/quiz/attempts/${attemptId}/submit`, {
      method: "POST",
      body: JSON.stringify({ answers }),
    }),
};
