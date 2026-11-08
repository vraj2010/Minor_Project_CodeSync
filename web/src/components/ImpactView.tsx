import type { Impact } from "../api";

export function ImpactView({ impact }: { impact: Impact }) {
  const further = impact.affected.filter((item) => item.depth > 1);
  const usages = impact.direct.filter((item) => item.kind !== "import");

  return (
    <div className="impact">
      <div className="stats">
        <Stat value={usages.length} label="direct uses" />
        <Stat value={further.length} label="reached indirectly" />
        <Stat value={impact.files.length} label="other files affected" />
        <Stat value={impact.tests.length} label="tests cover it" />
      </div>

      {impact.tests.length === 0 && (
        <p className="callout">
          No test reaches this function, so a change here can never be confirmed safe.
        </p>
      )}

      <section>
        <h3>Used directly</h3>
        {usages.length === 0 ? (
          <p className="muted">Nothing else in the project calls it.</p>
        ) : (
          <ul className="rows">
            {usages.map((item) => (
              <li key={`${item.path}:${item.lineno}:${item.text}`}>
                <code className="where">
                  {item.path}:{item.lineno}
                </code>
                <code>{item.text}</code>
                {item.confidence === "name-only" && <span className="tag">name only</span>}
              </li>
            ))}
          </ul>
        )}
      </section>

      {further.length > 0 && (
        <section>
          <h3>Reached through other code</h3>
          <ul className="rows">
            {further.map((item) => (
              <li key={item.qualname}>
                <code>{item.qualname}</code>
                <span className="muted">via {item.via.slice(1).reverse().join(" ← ")}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <h3>Tests that would notice</h3>
        {impact.tests.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <ul className="rows">
            {impact.tests.map((item) => (
              <li key={item.nodeid}>
                <code>{item.nodeid}</code>
              </li>
            ))}
          </ul>
        )}
      </section>

      {impact.truncated && (
        <p className="muted">The search stopped early, so more code may be affected.</p>
      )}
    </div>
  );
}

function Stat({ value, label }: { value: number; label: string }) {
  return (
    <div className="stat">
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  );
}
