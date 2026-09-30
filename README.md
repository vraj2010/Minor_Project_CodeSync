# CodeSync — Project-Aware AI for Safe Code Refinement

An LLM can improve a single function and still break the project, because the
files that call that function are not in front of it. CodeSync gives the model
that context and then verifies the result: a refinement is applied only if the
contract of the rewritten code still holds, every call site still binds, and the
tests that cover it still pass. When it cannot confirm that, it refuses and names
the files that would have broken.

Minor project, B.Tech Semester 7 — 23BCP142 and 23BCP516D.

## Repository layout

| Directory | Contents |
| --- | --- |
| `engine/` | Static analysis engine: discovery, parsing, symbol table (Python package `codesync`) |

`gateway/`, `web/` and `bench/` arrive in later phases.

## Current status — Phase 1, Week 1

The engine reads a repository and builds a symbol table.

- **Discovery** walks a project, skips virtual environments, caches and build
  output, and honours the project's `.gitignore`.
- **Module naming** follows packages upwards to the first directory without an
  `__init__.py`, so a `src/` layout yields `mypkg.mod`, not `src.mypkg.mod`.
- **Parsing** reads each file with its declared encoding. A file that cannot be
  read or parsed is recorded as a failure and the rest of the project is still
  indexed.
- **Symbols** are classes, functions, methods and module- or class-level
  variables, each with a qualified name, its file, its line span and the
  decorators it was written with. Names defined more than once are all kept.

## Getting started

```bash
cd engine
python -m venv .venv
.venv/Scripts/activate
pip install -e ".[dev]"
pytest
```

On macOS or Linux, activate with `source .venv/bin/activate`.

## Using the engine

Index a project and report what was found:

```bash
codesync index path/to/project
```

```
root       C:\projects\requests
files      37
modules    37
classes    96
functions  187
methods    524
variables  256
```

List symbols, optionally filtered by module, kind or short name:

```bash
codesync symbols path/to/project --module requests.sessions --kind method
```

Name the definitions that enclose a line — the entry point for "refine this
function", where the user points at a location rather than a qualified name:

```bash
codesync at path/to/project src/requests/sessions.py:589
```

```
src/requests/sessions.py:589  in module requests.sessions

  class   requests.sessions.Session          src/requests/sessions.py:395-905
  method  requests.sessions.Session.request  src/requests/sessions.py:557-653
```

`codesync index --json` writes the whole table as JSON, which is the interface
the rest of the system reads.

## Plan

| Weeks | Phase | Outcome |
| --- | --- | --- |
| 1–4 | Understanding code | Repository map and impact analysis: what breaks if this function changes |
| 5–7 | Bringing in the AI | Context selection, a rewritten function applied as a diff |
| 8–11 | The safety checks | Contract, call-site, type and test checks, plus the retry loop |
| 12–14 | Interface and results | Web interface, ablation study and the trap set |
