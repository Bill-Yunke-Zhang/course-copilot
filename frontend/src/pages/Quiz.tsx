import { CheckCircle2, Loader2, RefreshCw, XCircle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { SourceRefs } from "../archetypes/SourceRefs";
import {
  api,
  type AgentRun,
  type QuizAttempt,
  type QuizAttemptQuestion,
  type QuizDisclosure,
} from "../lib/api";

const FINISHED = new Set(["succeeded", "failed"]);
const POLL_MS = 1500;
const statusText: Record<string, string> = {
  queued: "Queued — waiting for the worker…",
  running: "Reading your request and picking material…",
  verifying: "Checking every question against its cited source…",
};

type Phase =
  | { kind: "idle" }
  | { kind: "generating"; run: AgentRun }
  | { kind: "failed"; run: AgentRun }
  | { kind: "empty"; run: AgentRun; disclosure: QuizDisclosure }
  | { kind: "taking"; attempt: QuizAttempt; disclosure: Partial<QuizDisclosure> }
  | { kind: "review"; attempt: QuizAttempt };

const panel =
  "rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900";
const primaryButton =
  "flex items-center gap-2 rounded-md bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-700 disabled:opacity-60 dark:bg-white dark:text-slate-900 dark:hover:bg-slate-200";
const secondaryButton =
  "rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800";
const warnBox =
  "rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950/50 dark:text-amber-300";
const errorBox =
  "rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/50 dark:text-red-300";

/**
 * Quiz (T6): one free-text request in, a verified quiz out. The agent parses
 * scope, count and format itself; this page only sends the text, shows the
 * run's progress and disclosure, and drives an attempt (answer → submit →
 * score → review wrong answers with their cited excerpts).
 */
export function Quiz() {
  const [text, setText] = useState("");
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<QuizAttempt[]>([]);

  const loadHistory = useCallback(async () => {
    try {
      setHistory(await api.quizAttempts());
    } catch {
      // History is a convenience; the quiz itself still works without it.
    }
  }, []);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  async function settle(run: AgentRun) {
    if (run.status === "failed") {
      setPhase({ kind: "failed", run });
      return;
    }
    const { disclosure } = await api.quizRun(run.id);
    if (disclosure.verified_count === 0) {
      setPhase({ kind: "empty", run, disclosure });
      return;
    }
    const attempt = await api.createQuizAttempt(run.id);
    setPhase({ kind: "taking", attempt, disclosure });
    loadHistory();
  }

  async function generate(force = false) {
    const request = text.trim();
    if (!request) return;
    setError(null);
    try {
      let { run } = await api.startQuiz(request, force);
      setPhase({ kind: "generating", run });
      while (!FINISHED.has(run.status)) {
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        run = await api.run(run.id);
        setPhase({ kind: "generating", run });
      }
      await settle(run);
    } catch (e) {
      setError(String(e));
      setPhase({ kind: "idle" });
    }
  }

  async function retake(runId: number | null) {
    if (runId === null) return;
    setError(null);
    try {
      const { run, disclosure } = await api.quizRun(runId);
      const attempt = await api.createQuizAttempt(run.id);
      setPhase({ kind: "taking", attempt, disclosure });
      loadHistory();
    } catch (e) {
      setError(String(e));
    }
  }

  async function openAttempt(id: number) {
    setError(null);
    try {
      const attempt = await api.quizAttempt(id);
      setText(attempt.raw_request);
      setPhase(
        attempt.completed_at
          ? { kind: "review", attempt }
          : { kind: "taking", attempt, disclosure: attempt.resolved_scope },
      );
    } catch (e) {
      setError(String(e));
    }
  }

  const busy = phase.kind === "generating";

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Quiz</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Say what to be quizzed on, how many questions, and multiple choice or true/false. Every
          question is drawn from your notes, slides and readings and checked against its source.
        </p>
      </div>

      <form
        className={`${panel} space-y-3`}
        onSubmit={(e) => {
          e.preventDefault();
          generate();
        }}
      >
        <label htmlFor="quiz-request" className="sr-only">
          Quiz request
        </label>
        <textarea
          id="quiz-request"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              generate();
            }
          }}
          rows={3}
          maxLength={1000}
          disabled={busy}
          placeholder="e.g. Quiz me on all content in unit 1 with 10 multiple choice questions"
          className="w-full rounded-md border border-slate-300 bg-white p-2 text-sm dark:border-slate-700 dark:bg-slate-950"
        />
        <div className="flex flex-wrap items-center gap-2">
          <button type="submit" disabled={busy || !text.trim()} className={primaryButton}>
            {busy && <Loader2 size={14} className="animate-spin" />}
            {busy ? "Generating…" : "Generate quiz"}
          </button>
          <span className="text-xs text-slate-500 dark:text-slate-400">⌘/Ctrl + Enter</span>
        </div>
      </form>

      {error && <div className={errorBox}>{error}</div>}

      {phase.kind === "generating" && (
        <div className={`${panel} flex items-center gap-3 text-sm`}>
          <Loader2 size={16} className="animate-spin text-slate-500" />
          <span>{statusText[phase.run.status] ?? phase.run.status}</span>
          <span className="ml-auto text-xs text-slate-400">run #{phase.run.id}</span>
        </div>
      )}

      {phase.kind === "failed" && (
        <div className={errorBox}>
          <div className="font-medium">No quiz was produced.</div>
          <div className="mt-1">{phase.run.error ?? "The quiz run failed."}</div>
        </div>
      )}

      {phase.kind === "empty" && (
        <div className={warnBox}>
          <div className="font-medium">
            0 of {phase.disclosure.requested_count ?? "the"} requested questions could be
            verified, so there is nothing to take.
          </div>
          <DisclosureNotes disclosure={phase.disclosure} />
          <button onClick={() => generate(true)} className={`${secondaryButton} mt-2`}>
            Try again with new questions
          </button>
        </div>
      )}

      {phase.kind === "taking" && (
        <TakeQuiz
          key={phase.attempt.id}
          attempt={phase.attempt}
          disclosure={phase.disclosure}
          onRegenerate={() => generate(true)}
          onSubmitted={(attempt) => {
            setPhase({ kind: "review", attempt });
            loadHistory();
          }}
        />
      )}

      {phase.kind === "review" && (
        <Review
          attempt={phase.attempt}
          onRetake={() => retake(phase.attempt.agent_run_id)}
          onNew={() => setPhase({ kind: "idle" })}
        />
      )}

      {history.length > 0 && (
        <section className={panel}>
          <h2 className="mb-2 text-sm font-semibold">Recent attempts</h2>
          <ul className="divide-y divide-slate-100 text-sm dark:divide-slate-800">
            {history.map((a) => (
              <li key={a.id}>
                <button
                  onClick={() => openAttempt(a.id)}
                  className="flex w-full items-center gap-3 py-1.5 text-left hover:text-blue-600 dark:hover:text-blue-400"
                >
                  <span className="min-w-0 flex-1 truncate">{a.raw_request}</span>
                  <span className="text-xs text-slate-500 dark:text-slate-400">
                    {a.completed_at ? `${a.score} / ${a.total}` : "in progress"}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function DisclosureNotes({ disclosure }: { disclosure: Partial<QuizDisclosure> }) {
  const notes = [...(disclosure.agent_notes ?? []), ...(disclosure.verifier_notes ?? [])];
  if (notes.length === 0) return null;
  return (
    <ul className="mt-1 list-disc pl-5 text-xs">
      {notes.map((n, i) => (
        <li key={i}>{n}</li>
      ))}
    </ul>
  );
}

function QuizHeader({ disclosure }: { disclosure: Partial<QuizDisclosure> }) {
  const requested = disclosure.requested_count;
  const verified = disclosure.verified_count;
  const asked = disclosure.asked_count;
  const shortfall = requested != null && verified != null && verified < requested;
  // requested_count is the count actually generated for (after the limit);
  // asked_count is what the student typed. Disclose the limit even when
  // every question passes verification.
  const capped = asked != null && requested != null && asked > requested;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {disclosure.scope_label && (
          <span className="rounded-full bg-blue-100 px-2 py-0.5 text-xs text-blue-700 dark:bg-blue-900/50 dark:text-blue-300">
            {disclosure.scope_label}
          </span>
        )}
        {disclosure.format && (
          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
            {disclosure.format === "true_false" ? "True / false" : "Multiple choice"}
          </span>
        )}
        {disclosure.from_cache && (
          <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-xs text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300">
            Reused verified quiz (no new LLM calls)
          </span>
        )}
      </div>
      {disclosure.resolution && (
        <p className="text-xs text-slate-500 dark:text-slate-400">
          Scope: {disclosure.resolution}
        </p>
      )}
      {(shortfall || capped) && (
        <div className={warnBox}>
          {capped && (
            <div className="font-medium">
              You asked for {asked} questions; quizzes are limited to {requested}, so{" "}
              {requested} were requested.
            </div>
          )}
          <div className={capped ? "text-xs" : "font-medium"}>
            {verified} of {requested} requested questions could be verified.
          </div>
          {(disclosure.dropped_count ?? 0) > 0 && (
            <div className="text-xs">
              {disclosure.dropped_count} generated question
              {disclosure.dropped_count === 1 ? " was" : "s were"} dropped because the cited text
              did not support them. Unverified questions are never shown.
            </div>
          )}
          {shortfall && <DisclosureNotes disclosure={disclosure} />}
        </div>
      )}
      {!shortfall && !capped && requested != null && verified != null && (
        <p className="text-xs text-slate-500 dark:text-slate-400">
          All {verified} requested questions passed verification.
        </p>
      )}
    </div>
  );
}

function TakeQuiz({
  attempt,
  disclosure,
  onSubmitted,
  onRegenerate,
}: {
  attempt: QuizAttempt;
  disclosure: Partial<QuizDisclosure>;
  onSubmitted: (attempt: QuizAttempt) => void;
  onRegenerate: () => void;
}) {
  const [answers, setAnswers] = useState<Record<number, number>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const unanswered = attempt.questions.filter((q) => answers[q.id] === undefined).length;

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      const result = await api.submitQuizAttempt(
        attempt.id,
        attempt.questions.map((q) => ({
          question_id: q.id,
          answer_index: answers[q.id] ?? null,
        })),
      );
      onSubmitted(result);
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className={`${panel} space-y-4`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h2 className="font-semibold">“{attempt.raw_request}”</h2>
        <button onClick={onRegenerate} className={`${secondaryButton} flex items-center gap-1`}>
          <RefreshCw size={14} /> New questions
        </button>
      </div>
      <QuizHeader disclosure={disclosure} />
      <ol className="space-y-4">
        {attempt.questions.map((q, n) => (
          <li key={q.id}>
            <fieldset>
              <legend className="font-medium">
                {n + 1}. {q.question}
              </legend>
              <div className="mt-1 space-y-1">
                {q.choices.map((choice, i) => (
                  <label
                    key={i}
                    className={`flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-sm ${
                      answers[q.id] === i
                        ? "bg-blue-50 dark:bg-blue-950/50"
                        : "hover:bg-slate-50 dark:hover:bg-slate-800"
                    }`}
                  >
                    <input
                      type="radio"
                      name={`q-${q.id}`}
                      checked={answers[q.id] === i}
                      onChange={() => setAnswers({ ...answers, [q.id]: i })}
                    />
                    <span>
                      {String.fromCharCode(65 + i)}. {choice}
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>
          </li>
        ))}
      </ol>
      {error && <div className={errorBox}>{error}</div>}
      <div className="flex flex-wrap items-center gap-3">
        <button onClick={submit} disabled={submitting} className={primaryButton}>
          {submitting && <Loader2 size={14} className="animate-spin" />}
          Submit answers
        </button>
        {unanswered > 0 && (
          <span className="text-xs text-slate-500 dark:text-slate-400">
            {unanswered} unanswered — unanswered questions count as wrong.
          </span>
        )}
      </div>
    </section>
  );
}

function Review({
  attempt,
  onRetake,
  onNew,
}: {
  attempt: QuizAttempt;
  onRetake: () => void;
  onNew: () => void;
}) {
  const wrong = attempt.questions.filter((q) => !q.is_correct);
  return (
    <section className={`${panel} space-y-4`}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-semibold">“{attempt.raw_request}”</h2>
          <div className="text-2xl font-semibold">
            {attempt.score} / {attempt.total}
          </div>
        </div>
        <div className="flex gap-2">
          <button onClick={onRetake} className={secondaryButton}>
            Retake this quiz
          </button>
          <button onClick={onNew} className={secondaryButton}>
            New quiz
          </button>
        </div>
      </div>
      <QuizHeader disclosure={attempt.resolved_scope} />
      {wrong.length === 0 ? (
        <p className="flex items-center gap-2 text-sm text-emerald-700 dark:text-emerald-300">
          <CheckCircle2 size={16} /> All correct.
        </p>
      ) : (
        <div className="space-y-3">
          <h3 className="text-sm font-semibold">Review wrong answers ({wrong.length})</h3>
          {wrong.map((q) => (
            <WrongAnswer key={q.id} question={q} number={attempt.questions.indexOf(q) + 1} />
          ))}
        </div>
      )}
      <details className="text-sm">
        <summary className="cursor-pointer text-slate-500 dark:text-slate-400">
          All questions
        </summary>
        <ol className="mt-2 space-y-1">
          {attempt.questions.map((q, n) => (
            <li key={q.id} className="flex items-start gap-2">
              {q.is_correct ? (
                <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-emerald-600" />
              ) : (
                <XCircle size={16} className="mt-0.5 shrink-0 text-red-600" />
              )}
              <span>
                {n + 1}. {q.question}{" "}
                <span className="text-xs text-slate-500">({q.topic})</span>
              </span>
            </li>
          ))}
        </ol>
      </details>
    </section>
  );
}

function WrongAnswer({ question: q, number }: { question: QuizAttemptQuestion; number: number }) {
  const letter = (i: number | null | undefined) =>
    i === null || i === undefined ? "—" : `${String.fromCharCode(65 + i)}. ${q.choices[i]}`;
  const excerpts = (q.source_refs ?? []).filter((r) => r.excerpt);
  return (
    <div className="rounded-md border border-red-100 p-3 dark:border-red-900/60">
      <div className="font-medium">
        {number}. {q.question}
      </div>
      <div className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">Topic: {q.topic}</div>
      <div className="mt-2 grid gap-1 text-sm">
        <div className="text-red-700 dark:text-red-300">
          Your answer: {q.student_answer_index == null ? "not answered" : letter(q.student_answer_index)}
        </div>
        <div className="text-emerald-700 dark:text-emerald-300">
          Correct answer: {letter(q.correct_index)}
        </div>
      </div>
      {excerpts.map((r, i) => (
        <blockquote
          key={i}
          className="mt-2 border-l-4 border-blue-300 bg-blue-50 p-2 text-sm italic dark:border-blue-800 dark:bg-blue-950/50"
        >
          “{r.excerpt}”
        </blockquote>
      ))}
      <SourceRefs refs={q.source_refs ?? []} />
    </div>
  );
}
