import { type ChangeEvent, type FormEvent, useRef, useState } from "react";

import { api, type Connection, type Project } from "../api";

interface Props {
  connection: Connection;
  projects: Project[];
  projectId: string | null;
  onSelect: (id: string) => void;
  onAdded: (project: Project) => void;
  onRemove: (id: string) => Promise<void>;
}

export function ProjectPicker({
  connection,
  projects,
  projectId,
  onSelect,
  onAdded,
  onRemove,
}: Props) {
  const [repository, setRepository] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  async function add(load: () => Promise<Project>) {
    setBusy(true);
    setError(null);
    try {
      onAdded(await load());
      setRepository("");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "That could not be added.");
    } finally {
      setBusy(false);
    }
  }

  function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) void add(() => api.upload(connection, file));
  }

  function importRepository(event: FormEvent) {
    event.preventDefault();
    if (repository.trim()) {
      void add(() => api.importGithub(connection, repository.trim()));
    }
  }

  return (
    <section className="panel">
      <h2>Project</h2>
      {projects.length > 0 && (
        <div className="project-row">
          <label className="sr-only" htmlFor="project">
            Current project
          </label>
          <select
            id="project"
            value={projectId ?? ""}
            onChange={(event) => onSelect(event.target.value)}
          >
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name} ({project.files} files)
              </option>
            ))}
          </select>
          {projectId && (
            <button
              className="ghost"
              title="Delete this project from the engine"
              aria-label="Delete this project from the engine"
              onClick={() => void onRemove(projectId)}
            >
              ✕
            </button>
          )}
        </div>
      )}

      <input
        ref={fileInput}
        type="file"
        accept=".zip"
        className="sr-only"
        aria-label="Project zip file"
        onChange={chooseFile}
        tabIndex={-1}
      />
      <button className="secondary wide" disabled={busy} onClick={() => fileInput.current?.click()}>
        {busy ? "Reading the project…" : "Upload a zip"}
      </button>

      <form className="inline" onSubmit={importRepository}>
        <label className="sr-only" htmlFor="repository">
          GitHub repository
        </label>
        <input
          id="repository"
          value={repository}
          onChange={(event) => setRepository(event.target.value)}
          placeholder="owner/repository"
        />
        <button className="secondary" disabled={busy || !repository.trim()}>
          Import
        </button>
      </form>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
