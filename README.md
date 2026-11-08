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
| `engine/` | The `codesync` Python package: project analysis, safety checks, model client, web API |
| `engine/experiments/trapset/` | The trap set: a test project, 50 traps, 12 controls and a hidden test suite |
| `web/` | The web interface: a React app with a side-by-side before and after view |
| `render.yaml` | Blueprint that deploys the web API and the web interface to Render |

## Status

| Phase | Weeks | State |
| --- | --- | --- |
| 1. Understanding code | 1–4 | Done: repository map, dependency map, usages, covering tests, `impact` |
| 2. Bringing in the AI | 5–7 | Done: AI variable mapping, hosted web API, function rewriting as a diff |
| 3. The safety checks | 8–11 | Done: contract, call-site, name, type and test checks, retry loop, undo |
| 4. Interface and results | 12–14 | Interface and trap set done; the comparison is built but needs a Gemini key to run |

## How a change is checked

Nothing a check does touches the real project. The checks run cheapest first, and
a suggestion that fails a cheap one never reaches the slow ones.

| Check | The question it asks | Needs |
| --- | --- | --- |
| Contract | Does it still take the same inputs and give the same kind of output? | Syntax only |
| Call sites | Does every caller, and every override, still fit? | Syntax only |
| Names | Does everything the rewrite uses exist? | Syntax only |
| Types | Did `mypy` find any new error? Errors that were already there do not count | A temporary copy |
| Tests | Do the tests that reach this function still pass? | A temporary copy |

A rewrite ends up `safe`, `unsafe` or `unconfirmed`. Unconfirmed means nothing
failed but something that had to run could not, usually because no test reaches
the function. It is treated as a refusal, because a tool that cannot confirm a
change is safe should not keep it. `--allow-untested` accepts such a change.

When a check fails, the model is told exactly what broke, as JSON, and gets up to
three more tries. If it never gets there, the change is refused and the places
that would have stopped working are named.

Running tests means running the project's code. It happens in a temporary copy
with a time limit, and anything in the environment that looks like a secret is
left out, but that is not a security sandbox: only use it on code you trust.

### What the checks can and cannot catch

Measured on the trap set: fifty hand-written changes that each look reasonable and
hide one break, and twelve harmless controls. A hidden test suite that the checks
never see proves each trap is real and each control is harmless.

| Category | Traps | Caught | Refused as unconfirmed | Missed |
| --- | --- | --- | --- | --- |
| Signature | 10 | 10 | 0 | 0 |
| Return value | 10 | 9 | 0 | 1 |
| Async and generators | 4 | 4 | 0 | 0 |
| Exceptions | 6 | 4 | 0 | 2 |
| Names | 6 | 6 | 0 | 0 |
| Behaviour | 14 | 3 | 2 | 9 |
| **Total** | **50** | **36** | **2** | **12** |

Of the 12 controls, 11 were kept, none was wrongly refused, and 1 came back
unconfirmed because no project test reaches it.

The pattern is the honest result. Changes to the shape of a function are caught
almost every time. Changes to what it does are caught only when one of the
project's own tests exercises that behaviour: nine of the twelve misses are
behaviour changes in branches its tests do not reach. The checks can only be as
good as the tests behind them, and this project's tests were deliberately left at
an ordinary level. These numbers describe this one small test project, written by
the same people as the checks, and are not a claim about code in general.

```bash
codesync traps --validate   # prove the set itself with the hidden suite
codesync traps              # score the checks
```

The four-way comparison runs the same 24 tasks with a plain model, then adding the
project map, the checks and the retry loop, and counts how often each one keeps a
change that quietly breaks the project. It needs a Gemini key, so it has no
results yet. `--dry-run` shows the cost first.

```bash
codesync compare --dry-run
codesync compare --output results.json
```

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
aistudio.google.com/apikey (the free tier needs no card).

Copy `engine/.env.example` to `engine/.env` and paste the key after the equals
sign. Every command then finds it, from any folder and in any terminal. Git
ignores that file, so the key stays on your computer.

```
GEMINI_API_KEY=your-key
```

Only `engine/.env` is read: a `.env` in the current folder or in the project being
checked is ignored, so one project cannot redirect your key. A value set in the
terminal overrides the file for that window, and the key is never passed to the
tests the checks run, nor visible to `pytest`.

| Setting | Meaning |
| --- | --- |
| `GEMINI_API_KEY` | your Gemini API key; required |
| `GEMINI_MODEL` | the model to use; the built-in default is `gemini-3.8-flash` |
| `GEMINI_BASE_URL` | send the requests through a gateway instead |

To use a different key for one PowerShell window only:

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

### What breaks if this changes?

```bash
codesync impact path/to/project ledger.pricing.tax_for
codesync deps path/to/project
```

`impact` follows the callers of a function outwards and lists the files affected
and the tests that reach it, through helpers and pytest fixtures too. `deps` shows
which modules import which.

### Changing code safely

```bash
codesync verify path/to/project ledger.pricing.tax_for my_version.py
codesync improve path/to/project ledger.pricing.tax_for --goal "Make this clearer." --apply
codesync undo path/to/project
```

`verify` checks a rewrite you wrote yourself, with no model and no key. `improve`
has the model rewrite the function, with the checks and the retries. Neither
touches the project unless `--apply` is given, and then only if the change is
kept. `undo` puts the file back byte for byte, and refuses if the file was edited
after the change was applied.

A refusal looks like this:

```
app.services.parser.parse_row: unsafe

  FAIL  contract  0 ms
  FAIL  call-sites  0 ms
  pass  names  1 ms
  skip  types  (not run, because an earlier check failed)
  skip  tests  (not run, because an earlier check failed)

what is wrong (5)
  `sep` was renamed to `delimiter`
  `strict` was removed
  it no longer raises `RecordError`, which callers may be catching
  app/services/parser.py:20: the call has no parameter `sep`
  app/services/parser.py:30: the call has no parameter `sep`

refused: 2 places would have stopped working
  app/services/parser.py:20  parse_row(line, sep=sep)
  app/services/parser.py:30  parse_row(line, sep=self.sep)
```

### Refining a function

`refine` rewrites one function with the project's context and returns the result
as a diff, with no safety checks. Nothing is written to the project. `improve`,
above, is the same thing with the checks.

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

## The web interface

Pick a function, see what depends on it, then rewrite it in a side-by-side editor
and check the result. Checking your own version needs no Gemini key.

In one terminal, start the engine for local use:

```powershell
cd engine
$env:CODESYNC_ALLOW_ANONYMOUS = "1"
$env:CODESYNC_RUN_TESTS = "1"
codesync serve
```

In another, start the page and open http://localhost:5173:

```bash
cd web
npm install
npm run dev
```

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
| `CODESYNC_RUN_TESTS` | `1` to run uploaded code's tests; only allowed with `CODESYNC_ALLOW_ANONYMOUS` |
| `CODESYNC_CHECK_TIMEOUT` | seconds each slow check may take, default 60 |

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/projects/upload` | upload a zip of a project |
| `POST /v1/projects/github` | import a public GitHub repository |
| `GET /v1/projects/{id}/variables` | list variables; filter by `kind`, `module`, `container`, `name`, `q`, `cross_file`, `annotated`, `role` |
| `GET /v1/projects/{id}/variables/{name}` | one variable with every access and its code |
| `GET /v1/projects/{id}/map` | the whole variable map as one document |
| `POST /v1/projects/{id}/annotate` | start describing the variables; returns a job |
| `GET /v1/capabilities` | whether this server runs tests and has a model key |
| `GET /v1/projects/{id}/symbols` | list the functions that can be rewritten |
| `GET /v1/projects/{id}/symbols/{name}` | one function with its code and contract |
| `GET /v1/projects/{id}/impact/{name}` | what breaks if a function changes |
| `GET /v1/projects/{id}/dependencies` | which modules import which |
| `POST /v1/projects/{id}/verify` | check a rewrite written by hand |
| `POST /v1/projects/{id}/improve` | start a model rewriting a function; returns a job |
| `GET /v1/jobs/{id}` | job progress, token usage and the result |
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
4. Set `VITE_API_URL` on `codesync-web` to the address of `codesync-api`, and
   `CODESYNC_CORS_ORIGINS` on `codesync-api` to the address of `codesync-web`.
5. Deploy, then open `https://<your-service>.onrender.com/docs`.

On the free plan the service sleeps when idle, so the first request after a
pause is slow, and projects live in memory and on a disk that is wiped on every
restart. A project that has to survive should be uploaded again, and the map
downloaded once it is described.

### What the service does with uploaded code

It reads the code and never runs it, so on the hosted service a rewrite gets every
check except the tests. A sound rewrite therefore comes back `unconfirmed`, and the
report says the tests were not run. The server refuses to start with
`CODESYNC_RUN_TESTS` unless it is also set up for local use. An archive is unpacked only for its `.py`
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
| 12–14 | Interface and results | Web interface, four-way comparison and the trap set |
