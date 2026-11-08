import { type FormEvent, useState } from "react";

import type { Connection } from "../api";

interface Props {
  initial: Connection;
  error: string | null;
  onConnect: (connection: Connection) => Promise<void>;
}

export function ConnectionForm({ initial, error, onConnect }: Props) {
  const [baseUrl, setBaseUrl] = useState(initial.baseUrl);
  const [apiKey, setApiKey] = useState(initial.apiKey);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    await onConnect({ baseUrl: baseUrl.trim(), apiKey: apiKey.trim() });
    setBusy(false);
  }

  return (
    <form className="card connect" onSubmit={submit}>
      <h2>Connect to the engine</h2>
      <p>Leave both fields empty for an engine running on this computer.</p>

      <label>
        Engine address
        <input
          value={baseUrl}
          onChange={(event) => setBaseUrl(event.target.value)}
          placeholder="https://your-service.onrender.com"
          autoComplete="url"
        />
      </label>
      <label>
        API key
        <input
          type="password"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
          placeholder="Not needed for a local engine"
          autoComplete="off"
        />
      </label>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <button className="primary" disabled={busy}>
        {busy ? "Connecting…" : "Connect"}
      </button>
    </form>
  );
}
