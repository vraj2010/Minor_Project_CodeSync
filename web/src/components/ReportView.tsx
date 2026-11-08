import type { Attempt, CheckResult, Report, Verdict } from "../api";

const HEADLINES: Record<Verdict, { title: string; text: string }> = {
  safe: {
    title: "Safe to keep",
    text: "Every check passed, including the tests that cover this function.",
  },
  unsafe: {
    title: "Refused",
    text: "This change would break the project.",
  },
  unconfirmed: {
    title: "Not confirmed",
    text: "Nothing failed, but the change could not be proved safe.",
  },
};

const CHECK_NAMES: Record<string, string> = {
  contract: "Contract",
  "call-sites": "Call sites",
  names: "Names",
  types: "Types",
  tests: "Tests",
};

const STATUS_WORDS: Record<CheckResult["status"], string> = {
  passed: "passed",
  failed: "failed",
  skipped: "not run",
};

interface Props {
  report: Report;
  attempts?: Attempt[];
  reason?: string;
}

export function ReportView({ report, attempts, reason }: Props) {
  const headline = HEADLINES[report.verdict];
  const findings = report.checks.flatMap((item) => item.findings);

  return (
    <section className={`report report-${report.verdict}`} aria-live="polite">
      <header>
        <h3>{headline.title}</h3>
        <p>{reason ?? headline.text}</p>
      </header>

      {attempts && attempts.length > 1 && (
        <ol className="attempts" aria-label="Attempts">
          {attempts.map((item) => (
            <li key={item.number}>
              <strong>Attempt {item.number}</strong>{" "}
              {item.caught_by
                ? `rejected by the ${CHECK_NAMES[item.caught_by] ?? item.caught_by} check`
                : (item.verdict ?? item.error ?? item.outcome)}
            </li>
          ))}
        </ol>
      )}

      <ul className="checks" aria-label="Checks">
        {report.checks.map((item) => (
          <li key={item.check} className={`check check-${item.status}`}>
            <span className="check-name">{CHECK_NAMES[item.check] ?? item.check}</span>
            <span className="check-status">{STATUS_WORDS[item.status]}</span>
            {item.seconds > 0 && <span className="check-time">{timing(item.seconds)}</span>}
          </li>
        ))}
      </ul>

      {report.places.length > 0 && (
        <div className="damage">
          <h4>
            {report.places.length === 1
              ? "1 place would have stopped working"
              : `${report.places.length} places would have stopped working`}
          </h4>
          <ul>
            {report.places.map((place) => (
              <li key={place}>
                <code>{place}</code>
              </li>
            ))}
          </ul>
        </div>
      )}

      {findings.length > 0 && (
        <div>
          <h4>What is wrong</h4>
          <ul className="findings">
            {findings.map((item, index) => (
              <li key={`${item.code}:${index}`}>
                {item.path && (
                  <code className="where">
                    {item.path}
                    {item.lineno ? `:${item.lineno}` : ""}
                  </code>
                )}
                {item.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      {report.notes.length > 0 && (
        <div>
          <h4>Worth knowing</h4>
          <ul className="findings">
            {report.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function timing(seconds: number): string {
  return seconds < 1 ? `${Math.max(Math.round(seconds * 1000), 1)} ms` : `${seconds.toFixed(1)} s`;
}
