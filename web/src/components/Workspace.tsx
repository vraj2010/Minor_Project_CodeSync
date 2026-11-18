import { DiffEditor } from "@monaco-editor/react";
import type { editor } from "monaco-editor/esm/vs/editor/editor.api";
import { useEffect, useRef, useState } from "react";

import {
  api,
  type AppliedChange,
  type Capabilities,
  type Connection,
  type Impact,
  type Improvement,
  type Report,
  type SymbolDetail,
} from "../api";
import { ChangeActions } from "./ChangeActions";
import { ImpactView } from "./ImpactView";
import { ReportView } from "./ReportView";

const DEFAULT_GOAL = "Improve readability and robustness without changing behaviour.";
const POLL_INTERVAL = 1000;

interface Props {
  connection: Connection;
  capabilities: Capabilities;
  projectId: string;
  qualname: string;
}

type Tab = "impact" | "rewrite";

export function Workspace({ connection, capabilities, projectId, qualname }: Props) {
  const [symbol, setSymbol] = useState<SymbolDetail | null>(null);
  const [impact, setImpact] = useState<Impact | null>(null);
  const [tab, setTab] = useState<Tab>("impact");
  const [goal, setGoal] = useState(DEFAULT_GOAL);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [improvement, setImprovement] = useState<Improvement | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  // The checked rewrite on screen, as the engine knows it, and whether the
  // project has changed since it was checked.
  const [change, setChange] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  // True when the editor no longer holds the version that was checked.
  const [edited, setEdited] = useState(false);
  const [history, setHistory] = useState<AppliedChange[]>([]);
  // Bumped after an apply or undo, so the function is read again.
  const [revision, setRevision] = useState(0);
  // What the editable side starts from. It is set once per function, because a
  // new value would overwrite whatever the user has typed.
  const [starting, setStarting] = useState<string | null>(null);
  const loadedFor = useRef<string | null>(null);
  const editorRef = useRef<editor.IStandaloneDiffEditor | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    const key = `${projectId}:${qualname}`;
    Promise.all([
      api.symbol(connection, projectId, qualname),
      api.impact(connection, projectId, qualname),
    ])
      .then(([detail, reach]) => {
        if (!alive.current) return;
        setSymbol(detail);
        setImpact(reach);
        if (loadedFor.current !== key) {
          loadedFor.current = key;
          setStarting(detail.source);
        }
      })
      .catch((failure: Error) => alive.current && setError(failure.message));
    return () => {
      alive.current = false;
    };
  }, [connection, projectId, qualname, revision]);

  useEffect(() => {
    let current = true;
    api
      .applied(connection, projectId)
      .then((items) => current && setHistory(items))
      .catch(() => current && setHistory([]));
    return () => {
      current = false;
    };
  }, [connection, projectId]);

  function clearResult() {
    setError(null);
    setReport(null);
    setImprovement(null);
    setNote(null);
    setSuccess(null);
    setChange(null);
    setStale(false);
  }

  function fail(failure: unknown, fallback: string) {
    if (alive.current) setError(failure instanceof Error ? failure.message : fallback);
  }

  async function applyChange() {
    if (!change) return;
    setError(null);
    setSuccess(null);
    setStatus("Applying the change…");
    try {
      const result = await api.apply(connection, projectId, change);
      if (!alive.current) return;
      setHistory(result.history);
      setStale(true);
      setSuccess(result.message);
      setRevision((value) => value + 1);
    } catch (failure) {
      fail(failure, "The change could not be applied.");
    } finally {
      if (alive.current) setStatus(null);
    }
  }

  async function undoChange() {
    setError(null);
    setSuccess(null);
    setStatus("Undoing the last change…");
    try {
      const result = await api.undo(connection, projectId);
      if (!alive.current) return;
      setHistory(result.history);
      setStale(true);
      setSuccess(result.message);
      setRevision((value) => value + 1);
    } catch (failure) {
      fail(failure, "The change could not be undone.");
    } finally {
      if (alive.current) setStatus(null);
    }
  }

  async function downloadPatch() {
    if (!change) return;
    setError(null);
    try {
      const file = await api.patch(connection, projectId, change);
      const url = URL.createObjectURL(new Blob([file.text], { type: "text/x-diff" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = file.name;
      link.click();
      URL.revokeObjectURL(url);
    } catch (failure) {
      fail(failure, "The patch could not be downloaded.");
    }
  }

  async function checkMine() {
    const code = editorRef.current?.getModifiedEditor().getValue() ?? "";
    clearResult();
    setStatus("Running the checks…");
    try {
      const result = await api.verify(connection, projectId, qualname, code);
      if (!alive.current) return;
      if (result.report) setReport(result.report);
      else setNote("This is the same as the original, so there is nothing to check.");
      setChange(result.change ?? null);
      // Typing while the checks ran means the editor is not what was checked.
      setEdited(editorRef.current?.getModifiedEditor().getValue() !== code);
    } catch (failure) {
      if (alive.current) setError(failure instanceof Error ? failure.message : "The check failed.");
    } finally {
      if (alive.current) setStatus(null);
    }
  }

  async function askModel() {
    clearResult();
    setStatus("Asking the model…");
    try {
      let job = await api.improve(connection, projectId, qualname, goal);
      while (alive.current && (job.status === "queued" || job.status === "running")) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL));
        job = await api.job(connection, job.id);
        setStatus(
          job.progress.done
            ? `Checking attempt ${job.progress.done + 1}…`
            : "Asking the model and checking its answer…",
        );
      }
      if (!alive.current) return;

      if (job.result) {
        setImprovement(job.result);
        if (job.result.report) setReport(job.result.report);
        if (job.result.after) {
          editorRef.current?.getModifiedEditor().setValue(job.result.after);
        }
        setChange(job.result.change);
        setEdited(false);
        if (job.result.decision === "unchanged") setNote(job.result.reason);
      }
      if (job.status === "failed") setError(job.error ?? "The model could not be reached.");
    } catch (failure) {
      if (alive.current) setError(failure instanceof Error ? failure.message : "The request failed.");
    } finally {
      if (alive.current) setStatus(null);
    }
  }

  if (error && !symbol) {
    return (
      <p className="error" role="alert">
        {error}
      </p>
    );
  }
  if (!symbol || !impact || starting === null) {
    return <p className="muted pad">Reading the project…</p>;
  }

  const busy = status !== null;

  return (
    <div className="workspace">
      <header className="function-head">
        <div>
          <h2>{symbol.name}</h2>
          <p>
            <code>{symbol.contract?.signature ?? symbol.qualname}</code>
          </p>
        </div>
        <code className="where">
          {symbol.path}:{symbol.lineno}
        </code>
      </header>

      <div className="tabs" role="tablist">
        {(["impact", "rewrite"] as const).map((name) => (
          <button
            key={name}
            role="tab"
            aria-selected={tab === name}
            className={tab === name ? "tab active" : "tab"}
            onClick={() => setTab(name)}
          >
            {name === "impact" ? "What breaks if this changes" : "Rewrite and check"}
          </button>
        ))}
      </div>

      {tab === "impact" && <ImpactView impact={impact} />}

      {/* Kept mounted so a rewrite in progress survives a look at the other tab. */}
      <div className="rewrite" hidden={tab !== "rewrite"}>
        <div className="diff-heads" aria-hidden="true">
          <span>Original</span>
          <span>Your version (editable)</span>
        </div>
        <div className="diff">
          <DiffEditor
            language="python"
            original={symbol.source}
            modified={starting}
            onMount={(instance) => {
              editorRef.current = instance;
              instance.getModifiedEditor().onDidChangeModelContent(() => setEdited(true));
            }}
            options={{
              originalEditable: false,
              readOnly: false,
              renderSideBySide: true,
              wordWrap: "on",
              minimap: { enabled: false },
              scrollBeyondLastLine: false,
              fontSize: 13,
              automaticLayout: true,
            }}
          />
        </div>

        <div className="actions">
          <button className="primary" disabled={busy} onClick={() => void checkMine()}>
            Check my version
          </button>
          <span className="or">or</span>
          <label className="sr-only" htmlFor="goal">
            What the model should aim for
          </label>
          <input
            id="goal"
            value={goal}
            onChange={(event) => setGoal(event.target.value)}
            disabled={!capabilities.model_configured}
          />
          <button
            className="secondary"
            disabled={busy || !capabilities.model_configured}
            title={
              capabilities.model_configured
                ? "Rewrite this with the model, with up to three retries"
                : "The engine has no model key for its provider"
            }
            onClick={() => void askModel()}
          >
            Ask the AI
          </button>
        </div>
        {!capabilities.model_configured && (
          <p className="muted">
            Asking the AI needs a model on the engine: a GEMINI_API_KEY, a DEEPSEEK_API_KEY, or
            CODESYNC_PROVIDER=ollama. Checking your own version does not.
          </p>
        )}

        {status && (
          <p className="status" role="status">
            {status}
          </p>
        )}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {success && (
          <p className="success" role="status">
            {success}
          </p>
        )}
        {note && <p className="callout">{note}</p>}
        {report && (
          <ReportView
            report={report}
            attempts={improvement?.attempts}
            reason={improvement?.reason}
          />
        )}
        <ChangeActions
          verdict={lastVerdict(report, improvement)}
          change={change}
          stale={stale}
          edited={edited}
          history={history}
          busy={busy}
          runsTests={capabilities.runs_tests}
          onApply={() => void applyChange()}
          onDownload={() => void downloadPatch()}
          onUndo={() => void undoChange()}
        />
      </div>
    </div>
  );
}

/** The verdict that belongs to the rewrite on screen. A model's last attempt can
 * end without a report of its own, and then an earlier attempt's verdict does
 * not describe it. */
function lastVerdict(report: Report | null, improvement: Improvement | null) {
  if (improvement) {
    return improvement.attempts[improvement.attempts.length - 1]?.verdict ?? null;
  }
  return report?.verdict ?? null;
}
