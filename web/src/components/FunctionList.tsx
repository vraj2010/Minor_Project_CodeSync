import { useMemo, useState } from "react";

import type { SymbolSummary } from "../api";

interface Props {
  symbols: SymbolSummary[];
  selected: string | null;
  error: string | null;
  hasProject: boolean;
  onSelect: (qualname: string) => void;
}

export function FunctionList({ symbols, selected, error, hasProject, onSelect }: Props) {
  const [query, setQuery] = useState("");

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return needle
      ? symbols.filter((item) => item.qualname.toLowerCase().includes(needle))
      : symbols;
  }, [symbols, query]);

  if (!hasProject) return null;

  return (
    <section className="panel functions">
      <h2>
        Functions <span className="count">{symbols.length}</span>
      </h2>
      <label className="sr-only" htmlFor="search">
        Search functions
      </label>
      <input
        id="search"
        type="search"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="Search by name"
      />
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <ul>
        {shown.map((item) => (
          <li key={item.qualname}>
            <button
              className={item.qualname === selected ? "symbol active" : "symbol"}
              aria-current={item.qualname === selected}
              onClick={() => onSelect(item.qualname)}
            >
              <span className="symbol-name">{item.name}</span>
              <span className="symbol-path">
                {item.path}:{item.lineno}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {!error && shown.length === 0 && <p className="muted">Nothing matches.</p>}
    </section>
  );
}
