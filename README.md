# AutoGrader: a rule-driven, reproducible grader for C programs

AutoGrader takes a student's `.c` file and an instructor's rules file. It runs
the program safely under time and memory limits and checks it with one analyzer
per metric: correctness, warnings, structure, modularity, corner cases,
complexity, memory safety, readability and originality. It returns **one
weighted grade** plus feedback. For every failure the feedback shows the input
that breaks the program, the expected output beside it, and why.

**Contract:** the same submission under the same rules always gets the same
grade. Every report carries a fingerprint so you can check this
(`autograder verify`).

```
$ ./grade.sh grade problems/max_subarray/submissions/s02_zero_init.c --rules problems/max_subarray/rules.json
===== Grading report for s02_zero_init.c =====
Overall grade: 84.2 / 100
...
test 03  (correctness, wrong answer)
  INPUT:    3
            -8 -3 -6
  EXPECTED: -3
  GOT:      0
  WHY:      You printed 0 but the answer is -3 (negative). A running maximum
            or best value initialised to 0 can never become negative.
  FIX:      Initialise the best value from the first element (or LLONG_MIN),
            not from 0.
```

Full reports for 15 sample submissions are in [`sample_outputs/`](sample_outputs).
The design, the reasoning behind each method, and the **Usefulness and
Limitations** section are in [`docs/DESIGN.md`](docs/DESIGN.md).

---

## 1. Installation

### Option A: Docker (recommended; this is how grades are made reproducible)

Requirements: Docker (Docker Desktop, or colima on macOS). Ollama is optional;
you only need it for the LLM readability metric.

```sh
git clone <this repo> autograder && cd autograder
docker build -t autograder:latest .          # pinned gcc 13, valgrind 3.22, cppcheck 2.13, lizard 1.17.10
ollama pull qwen2.5:7b                       # optional: readability metric (runs locally, free, offline)
./grade.sh tools                             # prints the toolchain the grades will be made with
```

`grade.sh` runs the image with the current folder mounted at `/work`. It also
reaches Ollama on the host through `host.docker.internal` and keeps the LLM
answer cache in `./.autograder-cache`.

### Option B: plain Python (for development)

Requirements: Python 3.9+, a C compiler (`gcc` or `clang`). `cppcheck` and
`valgrind` are optional but recommended.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
autograder tools
```

On macOS some checks are reduced: valgrind and LeakSanitizer do not exist, and
`RLIMIT_AS` is not enforced. As a result complexity falls back to CPU time, and
the report flags it as `nondeterministic_measurement`. Leak detection and the
memory limit are also off. Use Docker for grades you publish.

## 2. Usage

```sh
# grade one submission -> reports/<name>.json (machines) and reports/<name>.txt (student)
./grade.sh grade  problems/add_two_integers/submissions/add.c --rules problems/add_two_integers/rules.json

# grade a whole folder -> one report per file + reports/summary.csv
./grade.sh batch  problems/max_subarray/submissions --rules problems/max_subarray/rules.json --out reports/max_subarray

# prove the contract: grade N times, require identical report fingerprints
./grade.sh verify problems/max_subarray/submissions/s05_leaky.c --rules problems/max_subarray/rules.json --runs 3

# validate a rules file (weights, analyzer names, params, paths) without grading
./grade.sh check-rules problems/max_subarray/rules.json
```

### Web UI

```sh
./grade.sh serve                 # Docker: open http://localhost:8000 (AUTOGRADER_PORT=9000 to change)
autograder serve --port 8000     # without Docker
```

A local page with three tabs, over the same functions as the CLI:

* **Grade**: pick a `rules.json` from this folder (or upload one), paste or
  upload a `.c` file, optionally tick *Generate missing assets* / *Offline*,
  and get the full report: metrics, flags, failing inputs with expected vs.
  got, WHY/FIX, suggestions, generated-asset status, fingerprint, and the
  plain-text report the student receives.
* **Assets**: every generated asset bundle; per metric prepared / partial /
  not performed, generation failures, every asset (validated, rejected with
  the reason, skipped, unavailable) with its content, and every LLM attempt.
* **History**: every grade from the SQLite database (CLI or UI), with a
  reproducibility check: re-grades of the same submission under the same
  rules and assets must have identical fingerprints.

It binds to localhost only (it compiles and runs the code you give it).
Uploaded rules may not load plugins or use absolute or `..` paths.

Without Docker, replace `./grade.sh` with `autograder`. Useful flags:
`--out DIR`, `--offline` (answer LLM metrics from the cache only, never call a
model), `--cache DIR`, `--keep` (keep the temporary build folder for debugging).

Every grade is also recorded in a local SQLite database,
`.autograder-cache/autograder.sqlite3` (table `grades`: submission hash, rules
hash, grade, fingerprint, full JSON report). The `.json`/`.txt` files are still
written as before; pass `--no-db` to skip the database.

The LLM answers behind `sample_outputs/` are committed in `.autograder-cache/`.
So `./grade.sh batch ... --offline` reproduces the sample grades exactly on a
machine without Ollama or a GPU.

## 3. Writing rules

A problem is a folder. Everything the instructor controls lives in it, and none
of it is code:

```
problems/max_subarray/
  rules.json         which metrics, weights, thresholds, limits   <- the rules
  problem.md         statement (given to the LLM explainer)
  reference.c        correct solution: computes every expected output
  tests/NN.in(.out)  hidden tests (.out optional: made from reference.c)
  edge_cases.json    named corner-case families, e.g. "all_negative"
  gen.py             generate(size, rng) -> input, for stress tests and complexity
  submissions/       the class's submissions (also the plagiarism corpus)
  known/             web / LLM-generated solutions to compare against
```

`rules.json` (the PDF's format, plus a few optional keys):

```json
{
  "problem": "add_two_integers",
  "time_limit_s": 1,
  "memory_limit_mb": 64,
  "sample_input": "7 5\n",
  "reference": "reference.c",
  "metrics": [
    { "name": "correctness", "analyzer": "io_tests", "weight": 0.20,
      "params": { "testset_dir": "tests/" } },
    { "name": "complexity", "analyzer": "complexity_estimator", "weight": 0.10,
      "params": { "required": "O(1)", "sizes": [1, 10, 100] } }
  ]
}
```

Paths are relative to the rules file. Weights must sum to 1.00, and
`check-rules` reports any mistake with the metric's name.

| Top-level key | Meaning | Default |
|---|---|---|
| `problem` | Problem id | required |
| `time_limit_s`, `memory_limit_mb` | Per-run CPU-time and memory limits | 1, 256 |
| `sample_input` | Input for the first-pass smoke run | empty |
| `reference` | Correct C solution; expected outputs come from it | none |
| `statement` | Problem text, used by LLM explanations | none |
| `compile` | `{"cc", "flags", "ldflags"}` for the main build | gcc, `-O2 -std=c11`, `-lm` |
| `plugins` | Extra analyzer files to load (see section 5) | `[]` |
| `include_usefulness_limitations` | Append that section to every report | true |
| `sample_output` | Instructor's output for `sample_input`; a trusted test in the generated-assets workflow | none |
| `asset_generation` | Settings for `--generate-assets` (section 4) | see section 4 |

### Analyzers and their params

| Analyzer | Metric it measures | Key params |
|---|---|---|
| `io_tests` | Hidden tests pass | `testset_dir`, `generated` (seeded big tests), `compare` (`trim`/`exact`/`tokens`/`float:1e-6`), `explain` (`llm`) |
| `compiler_warnings` | Clean compile under strict flags | `flags`, `penalty_per_warning` (0.1) |
| `static_analysis` | Function size and complexity, cppcheck, globals, goto | `max_cyclomatic`, `max_function_length`, `cppcheck` |
| `function_split` | Decomposition into helpers | `min_functions`, `max_main_share` (0.8) |
| `edge_case_runner` | Corner cases and seeded random stress vs. the reference | `cases` (list or `"all"`), `random_tests`, `sizes`, `seed`, `explain` |
| `complexity_estimator` | Growth rate vs. the required bound | `required`, `sizes`, `penalty_per_class` (0.5), `timeout_s` |
| `sanitizer_run` | ASan, UBSan, LSan and valgrind over all inputs | `flags`, `valgrind`, `penalty_per_issue` (0.25), `max_input_bytes` |
| `llm_review` | Readability on a fixed 10-point rubric | `provider` (`ollama`/`groq`), `model`, `temperature`, `seed` |
| `similarity_flag` | Copying from classmates, the web or LLMs | `corpus_dir`, `known_dir`, `flag_threshold`, `flag_only` |
| `source_patterns` | Required or forbidden code (e.g. no `gets`) | `forbid`, `require` (regex + message) |

To catch LLM copies, run
`python scripts/generate_known_solutions.py problems/<p> --count 3`. It asks a
local model for solutions and stores them in `known/`. To write `.out` files
from the reference solution, run `python scripts/make_expected.py problems/<p>`.

## 4. Grading from only `submission.c` + `rules.json` (generated assets)

The workflow above needs a problem folder full of instructor assets: tests,
`reference.c`, `gen.py`, `edge_cases.json`, `known/`. With `--generate-assets`
the grader instead **detects which of those the configured analyzers need but
the folder lacks, generates candidates, validates them independently, stores
them in SQLite, and feeds the validated ones to the unchanged analyzers.**
Without the flag nothing changes, and with the flag on a complete folder
nothing is generated (same report fingerprint as without the flag).

### Both workflows with Docker

```sh
# A. explicit assets (unchanged): every file the rules name must exist
./grade.sh grade  problems/add_two_integers/submissions/add.c --rules problems/add_two_integers/rules.json
./grade.sh batch  problems/max_subarray/submissions --rules problems/max_subarray/rules.json --out reports/max_subarray

# B. only submission.c + rules.json: missing assets are generated + validated first
./grade.sh prepare --rules examples/minimal_workflow/rules.json        # optional: generate/inspect, no grading
./grade.sh grade  examples/minimal_workflow/submission.c --rules examples/minimal_workflow/rules.json --generate-assets
./grade.sh batch  path/to/submissions --rules path/to/rules.json --generate-assets --out reports/x
./grade.sh verify examples/minimal_workflow/submission.c --rules examples/minimal_workflow/rules.json --generate-assets --runs 3
./grade.sh grade  examples/minimal_workflow/submission.c --rules examples/minimal_workflow/rules.json --generate-assets --offline   # replay only
```

Generation uses the same LLM client as the readability metric (Ollama on the
host by default, reached through `host.docker.internal`). `--offline` never
calls a model: it reuses stored bundles and cached answers, and anything it
cannot reuse is reported as a generation failure.

### What gets generated, and what never is

| Asset | Needed by | How it is produced | How it is validated |
|---|---|---|---|
| input spec | everything below | instructor's `asset_generation.input_spec`, else the LLM reads the statement | strict schema; **must accept every instructor-written input** (`sample_input`, examples); otherwise retried with the validator's errors, at most `max_retries` times |
| `gen.py` | `complexity_estimator`, `io_tests.generated`, `edge_case_runner.random_tests` | fixed template that interprets the spec (no model-written code runs) | deterministic (same seed, same output) and every output parses under the spec at every requested size |
| corner-case inputs | `edge_case_runner`, `io_tests`, `sanitizer_run` | deterministic boundary families from the spec + LLM-proposed families | each input parsed against the spec; invalid ones are stored as *rejected* with the reason |
| expected outputs | `io_tests`, `edge_case_runner` | instructor outputs, instructor `reference`, or (opt-in) the LLM consensus oracle | see below |
| `known/` solutions | `similarity_flag` | LLM-written solutions | text only; never compiled or run |

**Expected outputs are never taken from the submission.** They come only from
(1) outputs the instructor wrote (`tests/*.out`, `"expected"`, `sample_output`,
`asset_generation.examples`), (2) the instructor's `reference`, or (3) if the
rules opt in with `"oracle": "llm_consensus"`, LLM-written programs that pass
a static screen, compile and run in the same sandbox as student code,
reproduce **every** instructor example (at least `min_examples_for_oracle`),
and agree with each other (a brute-force and an efficient version) on every
validation input. One disagreement rejects the oracle. If none of these exists,
correctness and corner cases are reported **not performed**: they get no
credit and are not hidden by rescaling (flag `tests_not_performed`), while
metrics that need no expected output (complexity, memory safety, warnings,
structure, readability, originality) still run.

Each metric's preparation status is in the report (`asset_preparation`) and in
the text report under "Test assets": `prepared`, `partial` (some probes could
not be checked and were skipped, flag `tests_incomplete`), or `not_performed`.
Generation failures (`asset_generation_failed`) are listed separately from the
student's failures.

### The `asset_generation` block (all keys optional)

| Key | Meaning | Default |
|---|---|---|
| `provider`, `model`, `seed` | LLM used for generation | `ollama`, `qwen2.5:7b`, 42 |
| `description` | Problem statement inline (used when there is no `statement` file) | none |
| `input_spec` | Instructor-written input spec (see `autograder/assets/inputspec.py`); makes generators and boundary cases work without any LLM | none |
| `examples` | `[{"input", "output"}]` trusted examples; they become tests | `[]` |
| `oracle` | `"none"` or `"llm_consensus"` | `"none"` |
| `oracle_candidates`, `min_examples_for_oracle` | Oracle programs to write (2-3) and instructor examples they must reproduce | 3, 2 |
| `max_retries` | Attempts per LLM step | 3 |
| `max_case_families`, `known_solutions` | LLM corner-case families; LLM solutions for `known/` | 6, 3 |
| `max_input_bytes`, `oracle_max_input_bytes` | Largest generated input kept; largest input cross-checked by the oracle | 50000, 4000 |

A complete example is [`examples/minimal_workflow/rules.json`](examples/minimal_workflow/rules.json).

### Storage, caching, reproducibility

Validated assets are stored in `.autograder-cache/autograder.sqlite3`
(`asset_bundles`, `assets` with every validated / rejected / skipped /
unavailable asset and its origin, `generation_events` with every LLM attempt)
and materialised read-only-by-convention into
`.autograder-cache/assets/<key>/`, never into the problem folder. The bundle
key hashes the rules file, the statement, the instructor assets that exist,
the generation settings and model, the generator and prompt versions and the
compiler. Change any of them and the assets are regenerated; a new Ollama
digest for the same model name also regenerates. A bundle built while the
model was unreachable is retried on the next online run. Assets do **not**
depend on the submission: every student in a batch is graded against the same
tests, and a batch prepares once. If the materialised folder is edited, it is
rebuilt from the database.

## 5. How it works

```
submission.c + rules.json
   │
   ▼  1. load rules ── validate weights, analyzer names, params, paths
   ▼  2. first pass ── compile · smoke run on sample input · static sanity
   │       does not compile / does not halt → skip execution metrics (score 0),
   │       still run source-only ones, mark the report PARTIAL
   ▼  3. dispatch ──── one analyzer per metric, in parallel threads
   │                   (shared builds and reference outputs, all cached)
   ▼  4. collate ──── grade = Σ score × weight · counterexamples grouped by cause
   │                   · suggestions ordered by points lost · flags
   ▼  reports/<name>.json  +  reports/<name>.txt   (+ fingerprint)
```

[`docs/DESIGN.md`](docs/DESIGN.md) explains each analyzer's method and why it
was chosen.

## 6. Adding a metric without editing the agent

Write an analyzer file next to your rules and list it under `"plugins"`. A
complete example is [`examples/plugins/line_budget.py`](examples/plugins/line_budget.py):

```python
from autograder.analyzers import Analyzer, AnalyzerResult, register

@register
class LineBudget(Analyzer):
    name = "line_budget"
    def run(self, ctx, params):
        ...
        return AnalyzerResult(score=..., feedback="...", evidence={...})
```

```json
"plugins": ["line_budget.py"],
"metrics": [ { "name": "conciseness", "analyzer": "line_budget", "weight": 0.05,
               "params": { "max_code_lines": 30 } } ]
```

## 7. Tests

```sh
.venv/bin/python -m pytest -q        # 77 tests on the host (1 is Linux-only and skips on macOS)
./scripts/test_in_docker.sh          # same suite inside the Linux image (valgrind, LSan, RLIMIT_AS)
```

## 8. Repository layout

```
autograder/
  cli.py            grade / batch / verify / prepare / serve / check-rules / tools
  coordinator.py    the main agent: first pass, dispatch, collation, fingerprint
  rules.py          load + validate the rules file, hash problem assets
  sandbox.py        run untrusted binaries under CPU/memory/output/wall limits
  context.py        per-submission state: builds, runs, reference outputs, generators
  compiler.py       compile + parse gcc/clang diagnostics
  diagnostics.py    parse ASan / UBSan / LSan / valgrind output
  explain.py        failing run -> WHY + FIX (rule-based, optional cached LLM)
  llm.py            Ollama / Groq client with content-addressed answer cache
  ctokens.py        small C lexer (normalisation, readability facts)
  report.py         JSON + student text report
  analyzers/        one file per analyzer (+ base.py: contract and registry)
  assets/           --generate-assets: inventory, LLM generation + retries, input specs,
                    oracle validation, SQLite store, materialisation (prepare.py is the pipeline)
  web/              `serve`: stdlib HTTP server + JSON API, static/index.html (no build step)
  data/usefulness_limitations.json
problems/           two example problems with rules, tests, reference, submissions
sample_outputs/     the agent's reports on every sample submission (Docker toolchain)
scripts/            make_expected.py, generate_known_solutions.py, test_in_docker.sh
examples/plugins/   an analyzer added from the rules file only
examples/minimal_workflow/  submission.c + rules.json only, for --generate-assets
tests/              pytest suite
docs/DESIGN.md      design, method defence, usefulness & limitations
```
