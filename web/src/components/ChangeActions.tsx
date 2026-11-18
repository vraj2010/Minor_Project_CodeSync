import type { AppliedChange, Verdict } from "../api";

interface Props {
  /** Verdict of the rewrite on screen, if one was checked. */
  verdict: Verdict | null;
  /** Id of that rewrite on the engine, when there is one to apply or download. */
  change: string | null;
  /** True once the project changed after the rewrite was checked. */
  stale: boolean;
  /** True when the editor no longer holds the version that was checked. */
  edited: boolean;
  history: AppliedChange[];
  busy: boolean;
  runsTests: boolean;
  onApply: () => void;
  onDownload: () => void;
  onUndo: () => void;
}

export function ChangeActions({
  verdict,
  change,
  stale,
  edited,
  history,
  busy,
  runsTests,
  onApply,
  onDownload,
  onUndo,
}: Props) {
  if (!change && history.length === 0) return null;

  const canApply = !busy && change !== null && verdict === "safe" && !stale && !edited;
  const last = history[history.length - 1];
  const hint = change ? applyHint(verdict, stale, edited, runsTests) : null;

  return (
    <div className="change-actions">
      <div className="actions">
        {change && (
          <>
            <button className="primary" disabled={!canApply} onClick={onApply}>
              Apply to Project
            </button>
            <button className="secondary" disabled={busy} onClick={onDownload}>
              Download .patch
            </button>
          </>
        )}
        {last && (
          <button
            className="secondary"
            disabled={busy}
            title={`Put ${last.path} back exactly as it was before ${last.target} was changed`}
            onClick={onUndo}
          >
            Undo Last Change
          </button>
        )}
      </div>
      {hint && <p className="muted">{hint}</p>}
    </div>
  );
}

function applyHint(
  verdict: Verdict | null,
  stale: boolean,
  edited: boolean,
  runsTests: boolean,
): string {
  if (edited) {
    return "The editor has changed since this version was checked. Check it again to apply it; the patch is of the checked version.";
  }
  if (stale) {
    return "The project changed after this version was checked. Check it again before applying it.";
  }
  if (verdict === "unsafe") {
    return "This version would break the project, so it cannot be applied.";
  }
  if (verdict !== "safe") {
    return runsTests
      ? "Only a version every check passed can be applied. Nothing failed, but no test confirmed this one."
      : "Only a version every check passed can be applied, and this engine does not run tests. Download the patch to apply it yourself.";
  }
  return "Apply changes the engine's copy of the project. To change the files on your computer, download the patch and run git apply in the project folder.";
}
