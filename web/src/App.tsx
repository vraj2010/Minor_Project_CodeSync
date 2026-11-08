import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type Capabilities, type Connection, type Project, type SymbolSummary } from "./api";
import { ConnectionForm } from "./components/ConnectionForm";
import { FunctionList } from "./components/FunctionList";
import { ProjectPicker } from "./components/ProjectPicker";
import { Workspace } from "./components/Workspace";

const STORAGE_KEY = "codesync.connection";

function loadConnection(): Connection {
  const fallback = { baseUrl: import.meta.env.VITE_API_URL ?? "", apiKey: "" };
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    return saved ? { ...fallback, ...JSON.parse(saved) } : fallback;
  } catch {
    return fallback;
  }
}

function saveConnection(connection: Connection) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(connection));
  } catch {
    // Without storage the connection simply lasts until the page is closed.
  }
}

export default function App() {
  const [connection, setConnection] = useState<Connection>(loadConnection);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [connectError, setConnectError] = useState<string | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [symbols, setSymbols] = useState<SymbolSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);

  const connect = useCallback(async (next: Connection) => {
    setConnection(next);
    saveConnection(next);
    setConnectError(null);
    try {
      const [abilities, held] = await Promise.all([api.capabilities(next), api.projects(next)]);
      setCapabilities(abilities);
      setProjects(held);
      setProjectId((current) =>
        held.some((item) => item.id === current) ? current : (held[0]?.id ?? null),
      );
    } catch (error) {
      setCapabilities(null);
      setConnectError(error instanceof Error ? error.message : "Could not connect.");
    }
  }, []);

  useEffect(() => {
    void connect(connection);
    // Only the connection the page opened with is tried automatically.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    setSelected(null);
    setSymbols([]);
    setListError(null);
    if (!projectId) return;

    let current = true;
    api
      .symbols(connection, projectId)
      .then((data) => current && setSymbols(data.items))
      .catch((error: Error) => current && setListError(error.message));
    return () => {
      current = false;
    };
  }, [connection, projectId]);

  const addProject = useCallback((project: Project) => {
    setProjects((held) => [...held, project]);
    setProjectId(project.id);
  }, []);

  const removeProject = useCallback(
    async (id: string) => {
      await api.deleteProject(connection, id);
      setProjects((held) => held.filter((item) => item.id !== id));
      setProjectId((current) => (current === id ? null : current));
    },
    [connection],
  );

  const mode = useMemo(() => {
    if (!capabilities) return null;
    return capabilities.runs_tests
      ? { label: "Local: tests run", tone: "good" }
      : { label: "Static checks only: tests are not run", tone: "warn" };
  }, [capabilities]);

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            CS
          </span>
          <div>
            <h1>CodeSync</h1>
            <p>AI code changes, checked against the whole project</p>
          </div>
        </div>
        {mode && <span className={`pill pill-${mode.tone}`}>{mode.label}</span>}
      </header>

      {!capabilities ? (
        <main className="welcome">
          <ConnectionForm initial={connection} error={connectError} onConnect={connect} />
        </main>
      ) : (
        <div className="layout">
          <aside className="sidebar" aria-label="Project and functions">
            <ProjectPicker
              connection={connection}
              projects={projects}
              projectId={projectId}
              onSelect={setProjectId}
              onAdded={addProject}
              onRemove={removeProject}
            />
            <FunctionList
              symbols={symbols}
              selected={selected}
              error={listError}
              hasProject={projectId !== null}
              onSelect={setSelected}
            />
          </aside>
          <main className="main">
            {projectId && selected ? (
              <Workspace
                key={`${projectId}:${selected}`}
                connection={connection}
                capabilities={capabilities}
                projectId={projectId}
                qualname={selected}
              />
            ) : (
              <div className="empty">
                <h2>{projectId ? "Choose a function" : "Add a project to begin"}</h2>
                <p>
                  {projectId
                    ? "Pick one from the list to see what depends on it, then rewrite it and check the result."
                    : "Upload a zip of a Python project, or import a public GitHub repository."}
                </p>
              </div>
            )}
          </main>
        </div>
      )}
    </div>
  );
}
