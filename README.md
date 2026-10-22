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
| `engine/` | The `codesync` Python package: project analysis, model client and refinement pipeline |

`gateway/`, `web/` and `bench/` arrive in later phases.

## Status

| Phase | Weeks | State |
| --- | --- | --- |
| 1. Understanding code | 1–4 | Week 1 done; import resolution and usage finding done early for Phase 2 |
| 2. Bringing in the AI | 5–7 | Done |
| 3. The safety checks | 8–11 | Not started |
| 4. Interface and results | 12–14 | Not started |

### Phase 1, Week 1 — reading a repository

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

### Phase 2 — bringing in the AI

A refinement runs in five steps, and nothing in it writes to the project:

1. **Find who depends on the function.** Names are resolved statically through
   imports, aliases (`import x as y`), re-exports from `__init__.py`, relative
   and star imports, `self`, `cls`, `super()` and base classes. A method call on
   a value whose type is unknown (`stock.value()`) is kept as a name-only
   match rather than dropped, because missing a caller is worse than a false
   alarm.
2. **Choose what the model sees.** Each piece of context is a ranked section:
   the definition and its contract (signature plus every usage) are always
   sent, then the enclosing class, the module's imports, the first callers,
   the code it calls and the remaining callers, for as long as they fit the
   token budget. Whatever is left out is recorded.
3. **Ask the model** over the OpenAI-compatible chat API, which DeepSeek,
   Ollama and Gemini all serve. Busy servers and timeouts are retried.
4. **Read the reply.** The rewritten definition is taken from the reply even
   when it comes with prose, a before-and-after pair, or the method still
   inside its class. A reply that adds other code, does not parse or has no
   definition is rejected with a reason.
5. **Splice it in.** The definition's lines are replaced in a copy of the file,
   re-indented to fit, with line endings kept and any new import added to the
   module's imports. The result is a unified diff that `git apply` accepts.

Two context modes make the comparison experiment possible: `baseline` sends the
function alone, `project` sends the contract, callers and callees. Both ask for
the same reply format, so they differ only in what the model knows.

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

### Connecting a model

| Provider | Setup |
| --- | --- |
| DeepSeek (default) | set `DEEPSEEK_API_KEY` |
| Ollama, local | install Ollama, run `ollama pull deepseek-coder:6.7b`, then pass `--provider ollama` |
| Gemini | set `GEMINI_API_KEY`, pass `--provider gemini` |
| Any OpenAI-compatible server | pass `--base-url`, `--model` and, if it needs a key, `--api-key-env` |

`--model` overrides any provider's default model.

## Using the engine

### Reading a project

```bash
codesync index path/to/project
codesync symbols path/to/project --module requests.sessions --kind method
codesync at path/to/project src/requests/sessions.py:589
```

`index` summarises what was found, `symbols` lists it, and `at` names the
definitions that enclose a line. `codesync index --json` writes the whole table
as JSON.

### Finding who uses a symbol

```bash
codesync usages engine/tests/fixtures/inventory inventory.models.Stock.value
```

```
inventory.models.Stock.value is used in 3 places

  call  inventory/models.py:28  inventory.models.PerishableStock.value  super().value()
  call  inventory/report.py:10  inventory.report.summary                stock.value()   [name only]
  call  scripts/restock.py:6    restock.restock                         stock.value()   [name only]
```

### Seeing the prompt before sending it

`context` builds exactly what `refine` would send, without calling a model:

```bash
codesync context engine/tests/fixtures/inventory inventory.pricing.apply_discount
```

```
target    inventory.pricing.apply_discount (inventory/pricing.py:10-13)
mode      project
budget    321 of 6000 tokens
included  target, contract, module, callers, callees
omitted   -
```

followed by the system and user messages. `--mode baseline` shows what the
model gets without project context, and `--budget` shows what a smaller budget
leaves out.

### Refining a function

```bash
codesync refine path/to/project inventory.pricing.apply_discount --goal "Make the validation clearer."
```

The target is a qualified name or a `PATH:LINE` inside the function. The output
reports the context used, the model's latency and token counts, and the outcome,
followed by the diff:

| Outcome | Meaning |
| --- | --- |
| `changed` | a rewrite was spliced in; the diff follows |
| `unchanged` | the model returned the function as it was |
| `invalid-reply` | the reply had no usable definition; the reason is given |
| `splice-failed` | the file no longer matches the index, or the result does not parse |
| `provider-failed` | the model could not be reached or returned an error |

`--output change.patch` saves the diff, which can then be applied with
`git apply change.patch`. `--show-reply` prints the model's raw reply.

### Running many refinements

A task file lists refinements; each `root` is relative to the file:

```json
{
  "tasks": [
    {"root": "projects/inventory", "target": "inventory.pricing.apply_discount"},
    {"root": "projects/inventory", "target": "inventory.models.Stock.add",
     "goal": "Reject a missing item."}
  ]
}
```

```bash
codesync batch tasks.json --mode baseline --patches out/baseline
codesync batch tasks.json --mode project --patches out/project
```

Each line reports one task, and the last line totals the outcomes.

### The run log

`refine` and `batch` append one JSON line per run to `.codesync/runs.jsonl`
(change it with `--log`, turn it off with `--no-log`). A record holds the
target, the context included and left out, the messages sent, the reply, token
counts, latency, the outcome and the diff, so every number in the evaluation
can be traced back to the run that produced it.

## Plan

| Weeks | Phase | Outcome |
| --- | --- | --- |
| 1–4 | Understanding code | Repository map and impact analysis: what breaks if this function changes |
| 5–7 | Bringing in the AI | Context selection, a rewritten function applied as a diff |
| 8–11 | The safety checks | Contract, call-site, type and test checks, plus the retry loop |
| 12–14 | Interface and results | Web interface, ablation study and the trap set |
