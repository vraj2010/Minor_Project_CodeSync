export interface Connection {
  baseUrl: string;
  apiKey: string;
}

export interface Capabilities {
  runs_tests: boolean;
  model_configured: boolean;
}

export interface Project {
  id: string;
  name: string;
  files: number;
  expires: string;
}

export interface SymbolSummary {
  qualname: string;
  name: string;
  kind: string;
  path: string;
  lineno: number;
}

export interface SymbolDetail extends SymbolSummary {
  source: string;
  contract: { signature: string } | null;
}

export interface Usage {
  path: string;
  lineno: number;
  scope: string | null;
  kind: string;
  confidence: string;
  text: string;
}

export interface Impact {
  target: string;
  files: string[];
  direct: Usage[];
  affected: { qualname: string; path: string; depth: number; via: string[] }[];
  tests: { nodeid: string; depth: number }[];
  truncated: boolean;
}

export interface Finding {
  check: string;
  code: string;
  message: string;
  path: string | null;
  lineno: number | null;
  text: string | null;
}

export interface CheckResult {
  check: string;
  status: "passed" | "failed" | "skipped";
  seconds: number;
  detail: string | null;
  findings: Finding[];
}

export type Verdict = "safe" | "unsafe" | "unconfirmed";

export interface Report {
  target: string;
  verdict: Verdict;
  caught_by: string | null;
  would_break: string[];
  places: string[];
  notes: string[];
  tests_run: number;
  checks: CheckResult[];
}

export interface VerifyResult {
  changed: boolean;
  /** Id of the checked rewrite, for applying it or downloading it as a patch. */
  change?: string;
  path?: string;
  diff: string;
  report: Report | null;
}

export interface Attempt {
  number: number;
  outcome: string;
  error: string | null;
  verdict: Verdict | null;
  caught_by: string | null;
}

export interface Improvement {
  target: string;
  change: string | null;
  path: string | null;
  decision: "kept" | "refused" | "unchanged" | "failed";
  reason: string;
  before: string;
  after: string | null;
  diff: string | null;
  report: Report | null;
  attempts: Attempt[];
}

export interface Job {
  id: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  progress: { done: number; total: number };
  result: Improvement | null;
  error: string | null;
}

export interface AppliedChange {
  id: string;
  path: string;
  target: string;
  applied_at: string;
}

export interface ApplyResult {
  applied: AppliedChange;
  message: string;
  history: AppliedChange[];
}

export interface UndoResult {
  undone: AppliedChange;
  message: string;
  history: AppliedChange[];
}

export interface PatchFile {
  name: string;
  text: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(
  connection: Connection,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await send(connection, path, init);
  return response.status === 204 ? (undefined as T) : response.json();
}

async function send(connection: Connection, path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  if (connection.apiKey) headers.set("X-API-Key", connection.apiKey);

  let response: Response;
  try {
    const base = connection.baseUrl.replace(/\/+$/, "");
    response = await fetch(`${base}${path}`, { ...init, headers });
  } catch {
    throw new ApiError("The engine could not be reached. Is it running?", 0);
  }

  if (!response.ok) {
    throw new ApiError(await errorMessage(response), response.status);
  }
  return response;
}

function attachmentName(response: Response, fallback: string): string {
  const match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") ?? "");
  return match?.[1] ?? fallback;
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return body.detail[0]?.msg ?? "The request was refused.";
  } catch {
    // The body was not JSON, so the status is all there is to go on.
  }
  return `The engine answered with status ${response.status}.`;
}

function json(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

const encode = (qualname: string) => qualname.split("/").map(encodeURIComponent).join("/");

export const api = {
  capabilities: (c: Connection) => request<Capabilities>(c, "/v1/capabilities"),

  projects: (c: Connection) => request<Project[]>(c, "/v1/projects"),

  upload(c: Connection, file: File) {
    const form = new FormData();
    form.append("file", file);
    return request<Project>(c, "/v1/projects/upload", { method: "POST", body: form });
  },

  importGithub: (c: Connection, repository: string) =>
    request<Project>(c, "/v1/projects/github", json({ repository })),

  deleteProject: (c: Connection, id: string) =>
    request<void>(c, `/v1/projects/${id}`, { method: "DELETE" }),

  symbols: (c: Connection, id: string) =>
    request<{ total: number; items: SymbolSummary[] }>(
      c,
      `/v1/projects/${id}/symbols?limit=1000`,
    ),

  symbol: (c: Connection, id: string, qualname: string) =>
    request<SymbolDetail>(c, `/v1/projects/${id}/symbols/${encode(qualname)}`),

  impact: (c: Connection, id: string, qualname: string) =>
    request<Impact>(c, `/v1/projects/${id}/impact/${encode(qualname)}`),

  verify: (c: Connection, id: string, target: string, code: string) =>
    request<VerifyResult>(c, `/v1/projects/${id}/verify`, json({ target, code })),

  improve: (c: Connection, id: string, target: string, goal: string) =>
    request<Job>(c, `/v1/projects/${id}/improve`, json({ target, goal })),

  job: (c: Connection, id: string) => request<Job>(c, `/v1/jobs/${id}`),

  apply: (c: Connection, id: string, change: string) =>
    request<ApplyResult>(c, `/v1/projects/${id}/apply`, json({ change })),

  undo: (c: Connection, id: string) =>
    request<UndoResult>(c, `/v1/projects/${id}/undo`, json({})),

  applied: (c: Connection, id: string) =>
    request<AppliedChange[]>(c, `/v1/projects/${id}/applied`),

  async patch(c: Connection, id: string, change: string): Promise<PatchFile> {
    const response = await send(
      c,
      `/v1/projects/${id}/patch?change=${encodeURIComponent(change)}`,
    );
    return { name: attachmentName(response, "change.patch"), text: await response.text() };
  },
};
