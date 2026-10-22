# CodeSync — Project-Aware AI for Safe Code Refinement

An LLM can improve a single function and still break the project, because the
files that use that function are not in front of it. CodeSync gives the model
that context and then verifies the result: a change is kept only if the contract
of the rewritten code still holds, every call site still binds, and the tests
that cover it still pass. When it cannot confirm that, it refuses and names the
files that would have broken.

Minor project, B.Tech Semester 7 — 23BCP142 and 23BCP516D.

## Repository layout

| Path | Contents |
| --- | --- |
| `engine/` | The `codesync` Python package: project analysis, variable map, model client, web API |
| `render.yaml` | Blueprint that deploys the web API to Render |

## Status

| Phase | Weeks | State |
| --- | --- | --- |
| 1. Understanding code | 1–4 | Week 1 done; import resolution and usage finding done early |
| 2. Bringing in the AI | 5–7 | Done: AI variable mapping, hosted web API, function rewriting as a diff |
| 3. The safety checks | 8–11 | Not started |
| 4. Interface and results | 12–14 | Not started |

## Phase 2 — the AI variable map

The map lists **every** variable in a Python project, shows where each one is
read and written, and has a hosted model describe what it is.

Static analysis does the finding, so nothing is missed or invented by the model;
the model only explains what was found.

| Kind | What it is |
| --- | --- |
| `module` | a name assigned at module level, or created with `global` |
| `class` | a name assigned in a class body, or through `cls` |
| `instance` | an attribute set through `self`, or declared by a bare annotation in a class |
| `parameter` | a function or lambda parameter, with how it is passed and its default |
| `local` | any other name bound inside a function, lambda or comprehension |

For each variable the map records:

- **Where it is defined**, and every read and write across the project,
  including reads in other files through imports, aliases and re-exports.
- **A type guess from the code alone**: its annotation if it has one, otherwise
  what the assignments show (literals, constructors, project functions with a
  return annotation).
- **What it overrides**, when a subclass sets an attribute its base class has.
- **A description from the model**: its type, one sentence on its purpose, a
  role (constant, configuration, counter, flag, ...) and a confidence.

How the scan decides which variable a name means follows Python's own rules: a
name assigned in a function is local to it unless declared `global` or
`nonlocal`, a class body is not visible from its methods, comprehensions have
their own scope, and `self.x` is the attribute `x` of the method's class or of
its base classes. A mixin that reads an attribute set by the class it is mixed
into is linked to that attribute.

**What it cannot see.** Attribute access on a value whose type is unknown
(`obj.count`) is not attributed to any variable, and nothing that needs the code
to run (`setattr`, `globals()`, `exec`) is visible. The static type is a guess
from syntax, and the model's description is a description: both are evidence for
a reader, not proof.

**How it stays affordable.** Small scopes are bundled into one request, the
model answers by number so repeated names cannot be confused, every answer is
cached by the content of its request (code that has not changed is never sent
twice), and `--dry-run` prices a run before anything is sent. The whole of
`requests` (2,855 variables) takes 134 requests.

## Getting started

```bash
cd engine
python -m venv .venv
.venv/Scripts/activate
pip install -e ".[dev]"
pytest
```

On macOS or Linux, activate with `source .venv/bin/activate`. A virtual
environment cannot be moved or copied; delete `.venv` and create it again.

### Connecting Gemini

CodeSync calls Google's Gemini; nothing runs locally. Create a key at
aistudio.google.com/apikey (the free tier needs no card). The key is read from
the environment, never from a file.

| Setting | Meaning |
| --- | --- |
| `GEMINI_API_KEY` | your Gemini API key; required |
| `GEMINI_MODEL` | the model to use; the built-in default is `gemini-3.8-flash` |
| `GEMINI_BASE_URL` | send the requests through a gateway instead |

In PowerShell, for the current window:

```powershell
$env:GEMINI_API_KEY = "your-key"
```

`--model` overrides the model for a single run.

Test the key before spending anything on a real run. It sends one tiny request and
says what happened, without ever printing the key:

```bash
codesync check
```

```
model    gemini-3.8-flash
address  https://generativelanguage.googleapis.com/v1beta/openai
key      GEMINI_API_KEY, 39 characters
reply    "OK" in 1.1s

the key works
```

When it fails it names the cause and what to try: a rejected key (Gemini answers
400 or 401), access denied (403), a rate limit (429), a busy model (503), or no
connection. A key containing spaces is flagged, since that is a placeholder and
not a key. `annotate` prints the same explanation if a run fails. A busy or slow
Gemini is retried up to four times, waiting longer each time; `--retries` changes
that.

Google retires models, and a built-in default can go stale. If a model is refused
(a `404` saying it is not available), ask Gemini what it offers now and use one
of those:

```bash
codesync models --contains flash
```

```powershell
$env:GEMINI_MODEL = "a-model-from-that-list"
```

A model that thinks before it answers spends part of its token budget doing so.
If a reply comes back empty with a finish reason of `length`, raise
`--max-tokens`.

## Using the engine

### The variable map

List the variables of a project, narrowed by kind, module, scope or name:

```bash
codesync variables path/to/project --kind module --cross-file
```

```
kind    variable             type   r/w  location
module  shop.config.TAX      float  1/1  shop/config.py:1
module  shop.config.LIMITS   dict   1/1  shop/config.py:2
module  shop.config.counter  int    3/2  shop/config.py:3

3 variables
```

`--cross-file` keeps variables used in a file other than the one defining them,
which are the ones a change can break from a distance. `r/w` is reads over writes.

Show one variable with every place it is written and read:

```bash
codesync variable path/to/project shop.cart.Cart.items
```

```
variable   shop.cart.Cart.items
kind       instance variable
declared   shop/cart.py:19
scope      shop.cart.Cart
type       list (inferred)
overrides  shop.cart.Base.items
used       1 read and 1 write in 1 file

written (1)
  shop/cart.py:19  []

read (1)
  shop/cart.py:26  names = [name.upper() for name in self.items if name]
```

Have the model describe every variable and write the map. Look at the cost first:

```bash
codesync annotate path/to/project --dry-run
```

```
variables                30
described by the model   26
described statically     4 (self and cls)
skipped                  0
requests                 2
estimated prompt tokens  2,202
```

`self` and `cls` are described without the model. Then run it:

```bash
codesync annotate path/to/project
```

This writes `.codesync/variable-map.json` (`--output` to change it) and keeps its
answer cache in `.codesync/annotations.jsonl`, so running it again on unchanged
code sends nothing. `--kinds module,class` limits what is described,
`--max-requests` caps the spend, `--workers` sets how many requests run at once,
and `--accesses` puts every read and write into the map file. If Gemini keeps
failing, for instance with a wrong key, the run stops after five errors in a row
instead of burning through the rest.

### Reading a project

```bash
codesync index path/to/project
codesync symbols path/to/project --module requests.sessions --kind method
codesync at path/to/project src/requests/sessions.py:589
codesync usages path/to/project requests.utils.get_auth_from_url
```

`index` summarises what was found, `symbols` lists classes and functions, `at`
names the definitions enclosing a line, and `usages` lists every place a symbol
is used. `codesync index --json` writes the whole table as JSON.

### Refining a function

Phase 2 also rewrites one function with the project's context and returns the
result as a diff. Nothing is written to the project.

```bash
codesync context path/to/project inventory.pricing.apply_discount
codesync refine path/to/project inventory.pricing.apply_discount --goal "Make the validation clearer."
codesync batch tasks.json --mode project --patches out/project
```

`context` shows exactly what would be sent without sending it, `refine` returns
the diff (`--output change.patch`, applied with `git apply`), and `batch` runs a
task file. `--mode baseline` sends the function alone, which is what the
comparison experiment needs.

Each run is appended to `.codesync/runs.jsonl`: the context included and left
out, the prompt, the reply, token counts and the diff.

## Hosting it online

The same engine runs as a web API, so it can be used from a browser or another
program with nothing installed. The API is documented interactively at `/docs`.

```bash
pip install -e ".[server]"
codesync serve
```

| Setting | Meaning |
| --- | --- |
| `CODESYNC_API_KEYS` | comma-separated keys clients must send; required |
| `GEMINI_API_KEY` | the Gemini key the server spends |
| `CODESYNC_ALLOW_ANONYMOUS` | `1` to skip client keys, for local development only |
| `CODESYNC_CORS_ORIGINS` | origins allowed to call the API from a browser |
| `CODESYNC_MAX_UPLOAD_MB` | largest upload, default 25 |
| `CODESYNC_MAX_FILES` | most Python files in a project, default 5000 |
| `CODESYNC_PROJECT_TTL_HOURS` | how long a project is kept, default 24 |
| `CODESYNC_MAX_PROJECTS` | projects held per key, default 10 |
| `CODESYNC_MAX_REQUESTS_PER_JOB` | model requests one job may make, default 300 |
| `CODESYNC_DAILY_REQUESTS` | model requests per key per day, default 2000 |

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/projects/upload` | upload a zip of a project |
| `POST /v1/projects/github` | import a public GitHub repository |
| `GET /v1/projects/{id}/variables` | list variables; filter by `kind`, `module`, `container`, `name`, `q`, `cross_file`, `annotated`, `role` |
| `GET /v1/projects/{id}/variables/{name}` | one variable with every access and its code |
| `GET /v1/projects/{id}/map` | the whole variable map as one document |
| `POST /v1/projects/{id}/annotate` | start describing the variables; returns a job |
| `GET /v1/jobs/{id}` | job progress and token usage |
| `DELETE /v1/jobs/{id}` | cancel a job |
| `DELETE /v1/projects/{id}` | delete a project |

Requests carry the key in an `X-API-Key` header. The quickest way to try the
API is the `/docs` page: press **Authorize**, paste a key, and every endpoint can
be run from the browser. Use `curl.exe` in PowerShell, because plain `curl` there
is an alias for something else.

A zip can hold the project folder itself or the folder around it. A single
wrapper folder, like the one GitHub puts around an archive, is removed, but a
folder that is a Python package (it has an `__init__.py`) is kept, so
`Compress-Archive -Path path\to\package` and `-Path path\to\project` both work.

```bash
curl -H "X-API-Key: $KEY" -F "file=@project.zip" $URL/v1/projects/upload
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d "{}" $URL/v1/projects/$ID/annotate
curl -H "X-API-Key: $KEY" $URL/v1/jobs/$JOB
curl -H "X-API-Key: $KEY" "$URL/v1/projects/$ID/variables?kind=module&cross_file=true"
curl -H "X-API-Key: $KEY" $URL/v1/projects/$ID/map -o variable-map.json
```

### Deploying to Render

`render.yaml` describes the service and `engine/Dockerfile` builds it.

1. Push the repository to GitHub.
2. In the Render dashboard choose **New → Blueprint** and select the repository.
3. When asked for the secret values, set `CODESYNC_API_KEYS` to a long random
   string (`python -c "import secrets; print(secrets.token_urlsafe(32))"`), and
   `GEMINI_API_KEY` to your Gemini key.
4. Deploy, then open `https://<your-service>.onrender.com/docs`.

On the free plan the service sleeps when idle, so the first request after a
pause is slow, and projects live in memory and on a disk that is wiped on every
restart. A project that has to survive should be uploaded again, and the map
downloaded once it is described.

### What the service does with uploaded code

It reads the code and never runs it. An archive is unpacked only for its `.py`
files and root `.gitignore`; paths that climb out of the project, links, archives
that expand past a limit and downloads from anywhere but GitHub are refused. Every
project and job belongs to the key that made it and is invisible to others, a
project is deleted after its lifetime, and the model keys never leave the server.
Because the server's model keys are being spent, each job is capped, a project
runs one job at a time, and each key has a daily request budget.

## Plan

| Weeks | Phase | Outcome |
| --- | --- | --- |
| 1–4 | Understanding code | Repository map and impact analysis: what breaks if this function changes |
| 5–7 | Bringing in the AI | AI variable map, hosted API, context selection, a rewritten function as a diff |
| 8–11 | The safety checks | Contract, call-site, type and test checks, plus the retry loop |
| 12–14 | Interface and results | Web interface, ablation study and the trap set |
