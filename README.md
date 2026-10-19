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

Without Docker, replace `./grade.sh` with `autograder`. Useful flags:
`--out DIR`, `--offline` (answer LLM metrics from the cache only, never call a
model), `--cache DIR`, `--keep` (keep the temporary build folder for debugging).

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

## 4. How it works

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

## 5. Adding a metric without editing the agent

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

## 6. Tests

```sh
.venv/bin/python -m pytest -q        # 41 tests on the host (1 is Linux-only and skips on macOS)
./scripts/test_in_docker.sh          # same suite inside the Linux image (valgrind, LSan, RLIMIT_AS)
```

## 7. Repository layout

```
autograder/
  cli.py            grade / batch / verify / check-rules / tools
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
  data/usefulness_limitations.json
problems/           two example problems with rules, tests, reference, submissions
sample_outputs/     the agent's reports on every sample submission (Docker toolchain)
scripts/            make_expected.py, generate_known_solutions.py, test_in_docker.sh
examples/plugins/   an analyzer added from the rules file only
tests/              pytest suite
docs/DESIGN.md      design, method defence, usefulness & limitations
```
