# AutoGrader: a rule-driven, reproducible grader for C programs

AutoGrader grades a student's C program against an instructor's `rules.json`.
It compiles and runs the program in a sandbox, then checks it with one analyzer
per metric: correctness, warnings, structure, modularity, corner cases,
complexity, memory safety, readability and originality. It returns **one
weighted grade**, plus feedback that shows each failing input with the expected
and actual output, why it failed, and how to fix it. It also works when the
instructor provides **only `submission.c` + `rules.json`**: missing test assets
are generated, validated independently, and stored in a local SQLite database.
It never treats the student's own output as the answer key. It ships with a
command line, a local web UI, and a Docker image that pins the toolchain.

**Contract: the same submission under the same rules always gets the same
grade.** Every report carries a fingerprint, and `autograder verify` checks it.

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
The method behind each analyzer and the **Usefulness and Limitations**
analysis are in [`docs/DESIGN.md`](docs/DESIGN.md).

---

## Contents

1. [What happens when you grade](#1-what-happens-when-you-grade)
2. [Setup guide](#2-setup-guide)
3. [Your first grades (10-minute walkthrough)](#3-your-first-grades-10-minute-walkthrough)
4. [Command reference](#4-command-reference)
5. [The web UI](#5-the-web-ui)
6. [Writing a problem: rules.json and assets](#6-writing-a-problem-rulesjson-and-assets)
7. [Grading from only submission.c + rules.json](#7-grading-from-only-submissionc--rulesjson)
8. [How it works, and why it is built this way](#8-how-it-works-and-why-it-is-built-this-way)
9. [Configuration and environment variables](#9-configuration-and-environment-variables)
10. [Where data is stored](#10-where-data-is-stored)
11. [Adding a metric without editing the agent](#11-adding-a-metric-without-editing-the-agent)
12. [Tests](#12-tests)
13. [Troubleshooting](#13-troubleshooting)
14. [Repository layout and further reading](#14-repository-layout-and-further-reading)

---

## 1. What happens when you grade

1. **Load the rules.** `rules.json` says which metrics to measure, how much
   each one weighs (the weights must sum to 1.00), and the limits and test
   files to use. A typo fails here with the metric's name, not halfway through
   grading.
2. **(Optional) Prepare missing assets.** With `--generate-assets`, the grader
   finds the tests, generators and corner cases the rules need but the folder
   lacks. It generates and validates them, and points the analyzers at them.
   The `rules.json` file is never edited.
3. **First pass.** The program is compiled and run once on the sample input.
   If it does not compile or does not halt, metrics that need to run it score
   0 and the report is marked PARTIAL. Source-only metrics still run.
4. **Dispatch.** Each metric's analyzer runs in parallel against shared,
   cached builds. Untrusted code always runs in the sandbox, under CPU-time,
   memory, output-size and wall-clock limits.
5. **Collate.** grade = Σ score × weight. Failing inputs are grouped by cause,
   and suggestions are ordered by points lost. Flags explain anything unusual,
   and a fingerprint hashes everything that affects the grade.
6. **Write.** The machine report goes to `reports/<name>.json` and the student
   report to `reports/<name>.txt`. Both are also stored in the SQLite database
   that the web UI's History tab reads.

---

## 2. Setup guide

### 2.1 Choose how to run it

| Option | Use it for | What you get |
|---|---|---|
| **A. Docker** (recommended) | Grades you publish; any OS | The pinned Linux toolchain: gcc 13.3, valgrind 3.22, cppcheck 2.13, lizard 1.17.10, LeakSanitizer and memory limits. Grades are reproducible. |
| **B. Plain Python** | Developing AutoGrader itself; quick local tries | Your machine's compiler. On macOS: no valgrind, no LeakSanitizer, no memory limit, and complexity is measured with noisy CPU time (flagged in the report). |

Both options use the same code and the same commands: `./grade.sh <cmd>`
for Docker, and `autograder <cmd>` for plain Python.

### 2.2 Prerequisites

| Need | For | Install |
|---|---|---|
| git | cloning | preinstalled on most systems |
| Docker | option A | macOS: [Docker Desktop](https://www.docker.com/products/docker-desktop/) or `brew install colima docker && colima start --cpu 4 --memory 6`. Linux: Docker Engine (`curl -fsSL https://get.docker.com \| sh`, then add your user to the `docker` group). Windows: Docker Desktop (WSL 2 backend); run `./grade.sh` from WSL or Git Bash, or `.\grade.ps1` from PowerShell. The project folder is mounted, so `.autograder-cache\` (database, generated assets) appears in the project folder on Windows. |
| Python 3.9+ and a C compiler | option B | macOS: `xcode-select --install`. Debian/Ubuntu: `sudo apt install python3 python3-venv gcc` |
| cppcheck, valgrind | option B, optional | `brew install cppcheck` / `sudo apt install cppcheck valgrind` (valgrind: Linux only) |
| Ollama + a model | optional: readability, LLM explanations, generated assets | [ollama.com/download](https://ollama.com/download), then `ollama pull qwen2.5:7b` (about 4.7 GB) |

The LLM is **optional**. Without it, readability is reported as "not measured"
(other metrics are unaffected), and `--generate-assets` can still use
instructor-written input specs and examples (section 7). The answers behind
the sample reports are committed, so `--offline` reproduces them without any
model.

### 2.3 Option A: Docker (step by step)

```sh
# 1. get the code
git clone https://github.com/TheHarshal30/co-founder-work.git autograder
cd autograder

# 2. build the grading image (one-time, a few minutes; grade.sh also builds it on first use)
docker build -t autograder:latest .

# 3. (optional) a local model for readability / explanations / generated assets
ollama pull qwen2.5:7b
ollama serve            # only if Ollama is not already running as an app/service

# 4. check the toolchain the grades will be made with
./grade.sh tools
```

`./grade.sh tools` should print something like this (the platform depends on your CPU):

```
platform   Linux-aarch64
python     3.12.3
cc         gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0
valgrind   3.22.0
cppcheck   2.13.0
lizard     1.17.10
analyzers  compiler_warnings, complexity_estimator, edge_case_runner, function_split, io_tests, llm_review, sanitizer_run, similarity_flag, source_patterns, static_analysis
```

What `grade.sh` does on every call, so nothing about it is surprising:

| It does | Why |
|---|---|
| Mounts the current folder at `/work` and runs as your user id | Reports and caches land in your folder, owned by you |
| Sets `AUTOGRADER_CACHE=/work/.autograder-cache` | LLM answers, the SQLite database and generated assets survive between runs |
| Points `OLLAMA_HOST` at `host.docker.internal:11434` and adds `--add-host=host.docker.internal:host-gateway` | The container reaches Ollama on your machine, on Linux too (override with `AUTOGRADER_OLLAMA_HOST`); check with `./grade.sh llm-check` |
| Forwards `AUTOGRADER_LLM_PROVIDER`/`_MODEL`, `GROQ_API_KEY`, `OPENAI_COMPAT_*` | Choose the generation model, or use Groq / an OpenAI-compatible server instead of Ollama |
| Passes the host folder as `AUTOGRADER_HOST_WORKDIR` | The web UI shows generated-file paths you can open on your machine |
| On Git Bash (Windows): sets `MSYS_NO_PATHCONV=1`, mounts `pwd -W` | Git Bash would otherwise rewrite `/work` into `C:/Program Files/Git/work` |
| `--pids-limit 512 --memory 4g` | A fork bomb or memory hog in a submission cannot take down the host |
| For `serve`: publishes `127.0.0.1:${AUTOGRADER_PORT:-8000}` | The UI is reachable from your machine only |

**Run `grade.sh` from the repository root.** Only the current folder is
mounted, so paths must be inside it (for example `problems/...`). A path
like `~/Downloads/x.c` does not exist inside the container: copy the file into
the repository first.

### 2.4 Option B: plain Python (step by step)

```sh
git clone https://github.com/TheHarshal30/co-founder-work.git autograder
cd autograder
python3 -m venv .venv
. .venv/bin/activate            # Windows (no WSL): .venv\Scripts\activate
pip install -e '.[dev]'         # installs autograder + lizard (structure metrics) + pytest
autograder tools                # check what was found
```

If `autograder tools` shows `valgrind NOT FOUND` (always on macOS), complexity
falls back to CPU time and reports carry the flag
`nondeterministic_measurement`. That is fine for trying things out, but use
Docker for grades you publish.

### 2.5 Verify the installation

```sh
./grade.sh verify problems/max_subarray/submissions/s05_leaky.c --rules problems/max_subarray/rules.json --runs 3 --offline
# REPRODUCIBLE: 3 runs produced identical reports
python -m pytest -q             # option B; Docker: ./scripts/test_in_docker.sh
```

---

## 3. Your first grades (10-minute walkthrough)

All commands use Docker. Without Docker, replace `./grade.sh` with `autograder`.

**Grade one submission with a complete problem folder:**

```sh
./grade.sh grade problems/add_two_integers/submissions/add.c --rules problems/add_two_integers/rules.json
```

The student report is printed and written to `reports/add.txt`, and the full
machine report to `reports/add.json`. `add.c` stores the sum in an `int`, so
the report shows `2147483647 1 -> expected 2147483648, got -2147483648` and
explains the overflow.

**Grade a whole class:**

```sh
./grade.sh batch problems/max_subarray/submissions --rules problems/max_subarray/rules.json --out reports/max_subarray
# one report per student + reports/max_subarray/summary.csv
```

**Grade with only `submission.c` + `rules.json`:**

```sh
./grade.sh grade examples/bare_workflow/submission.c --rules examples/bare_workflow/rules.json --generate-assets
```

The output starts with an asset-preparation summary: what was missing, what
was generated, validated or rejected, where the expected outputs came from,
and which metrics could not be performed. Section 7 explains it.

**Open the web UI:**

```sh
./grade.sh serve      # then open http://localhost:8000
```

---

## 4. Command reference

| Command | What it does | Exit code |
|---|---|---|
| `grade SUB.c --rules R.json` | Grade one submission and write `<out>/<name>.json` + `.txt` | 0; 2 on invalid rules |
| `batch FOLDER --rules R.json` | Grade every `*.c` in a folder and write `summary.csv` | 0; 1 if no `.c` files |
| `verify SUB.c --rules R.json [--runs 3]` | Grade N times and require identical fingerprints | 0 reproducible; 2 not |
| `prepare --rules R.json` | Generate and validate missing assets only; no grading | 0; 2 on invalid rules |
| `serve [--port 8000] [--host 127.0.0.1] [--root .]` | Start the local web UI | runs until Ctrl-C |
| `llm-check [--rules R.json] [--llm-model M]` | Is the asset-generation LLM reachable, and is the model pulled? Prints the fix if not | 0 ok; 1 not available |
| `check-rules R.json` | Validate a rules file (weights, analyzers, params, paths) | 0 valid; 2 invalid |
| `tools [--cc gcc]` | Print the toolchain versions and analyzers | 0 |

| Flag | On | Meaning |
|---|---|---|
| `--out DIR` | grade, batch | Report folder (default `reports/`) |
| `--generate-assets` | grade, batch, verify | Prepare assets the rules need but the folder lacks (section 7) |
| `--offline` | all grading commands, prepare, serve | Never call an LLM; answer from the cache and stored bundles only |
| `--cache DIR` | all grading commands, prepare, serve | Cache root (default `$AUTOGRADER_CACHE` or `.autograder-cache`) |
| `--no-db` | grade, batch, verify, prepare | Do not record grades in SQLite (`prepare` still needs the database) |
| `--llm-provider`, `--llm-model` | grading commands, prepare, serve, llm-check | LLM for asset generation; overrides the rules' `asset_generation` (default: `$AUTOGRADER_LLM_PROVIDER`/`$AUTOGRADER_LLM_MODEL`, then the rules, then `ollama`/`qwen2.5:7b`) |
| `--keep` | grade | Keep the temporary build folder for debugging |
| `--quiet` | grade | Do not print the text report |

---

## 5. The web UI

```sh
./grade.sh serve                          # Docker -> http://localhost:8000
AUTOGRADER_PORT=9000 ./grade.sh serve     # another port
autograder serve --port 8000 --offline    # without Docker; "offline" ticked by default
```

| Tab | What you do there |
|---|---|
| **Grade** | Pick a `rules.json` under the repository (or upload one), paste or upload a `.c` file, and tick *Generate missing assets* / *Offline* if needed. With generation on, choose the LLM provider and model and press *Check* to see whether it is reachable. You get the full report: score and status per metric, flags, failing inputs with expected vs. actual output plus WHY/FIX, suggestions, the generated-asset status with every skip and its reason, where the assets are stored, and the fingerprint. **Download TXT Report** saves the plain-text report (scores, verdicts, analyzer findings, asset-generation results, not-performed/skipped reasons); **Download JSON** saves the full machine report. |
| **Assets** | Every generated asset bundle, with its folder under `.autograder-cache/generated/`. For one bundle: the per-metric decisions and skips, the oracle verdict per candidate, generation failures, and three sections: **Instructor-provided** material, **Generated** assets (validated, rejected with the reason, skipped, unavailable), and **Validated and used for grading** files. Each file shows its path, origin, validation result, where it is stored on your machine, and its content on click. Every LLM attempt is listed with the validator's verdict. |
| **History** | Every grade from the CLI or the UI, read from SQLite. Re-grades of the same submission under the same rules and assets are checked for identical fingerprints ("3 runs identical" or "2 different results"). |

The UI is one static HTML file served by a Python standard-library server
(`autograder/web/`). It needs no Node, no build step and no internet access,
so it works offline and inside the Docker image. It calls the same functions
as the CLI, so a grade from the UI equals a grade from the CLI.

**Security model:** the server listens on localhost only, because it compiles
and runs whatever code you give it. Uploaded rules files may not load
`plugins` (they are Python) and may not use absolute or `..` paths (they could
point the grader at any file on the machine). Rules files picked from the
repository are trusted like on the CLI. A submission uploaded unchanged from a
class `corpus_dir` (same name, same bytes) is graded in place, so the
originality check does not compare it with itself. A renamed copy is still
flagged.

---

## 6. Writing a problem: rules.json and assets

A problem is a folder. Everything the instructor controls lives in it, and none
of it is code (except an optional generator):

```
problems/max_subarray/
  rules.json         which metrics, weights, thresholds, limits   <- the rules
  problem.md         statement (given to the LLM explainer and to asset generation)
  reference.c        correct solution: computes every expected output
  tests/NN.in(.out)  hidden tests (.out optional: made from reference.c)
  edge_cases.json    named corner-case families, e.g. "all_negative"
  gen.py             generate(size, rng) -> input, for stress tests and complexity
  submissions/       the class's submissions (also the plagiarism corpus)
  known/             web / LLM-generated solutions to compare against
```

A minimal `rules.json`:

```json
{
  "problem": "add_two_integers",
  "time_limit_s": 1,
  "memory_limit_mb": 64,
  "sample_input": "7 5\n",
  "reference": "reference.c",
  "metrics": [
    { "name": "correctness", "analyzer": "io_tests", "weight": 0.8,
      "params": { "testset_dir": "tests/" } },
    { "name": "complexity", "analyzer": "complexity_estimator", "weight": 0.2,
      "params": { "required": "O(1)", "sizes": [1, 10, 100] } }
  ]
}
```

Paths are relative to the rules file, so a problem folder can be moved
around. Weights must sum to 1.00. Run `check-rules` after every edit: it lists
every mistake with the metric's name.

| Top-level key | Meaning | Default |
|---|---|---|
| `problem` | Problem id | required |
| `time_limit_s`, `memory_limit_mb` | Per-run CPU-time and memory limits | 1, 256 |
| `sample_input` | Input for the first-pass smoke run | empty |
| `sample_output` | Instructor's output for `sample_input`; a trusted test when assets are generated | none |
| `reference` | Correct C solution; expected outputs come from it | none |
| `statement` | Problem text file, used by LLM explanations and asset generation | none |
| `compile` | `{"cc", "flags", "ldflags"}` for the main build | gcc, `-O2 -std=c11`, `-lm` |
| `plugins` | Extra analyzer files to load (section 11) | `[]` |
| `include_usefulness_limitations` | Append the usefulness/limitations section to every report | true |
| `asset_generation` | Settings for `--generate-assets` (section 7) | see section 7 |

### Analyzers and their params

| Analyzer | Metric it measures | Needs these assets | Key params |
|---|---|---|---|
| `io_tests` | Hidden tests pass | `tests/*.in` + `.out` or `reference`; `gen.py` for `generated` | `testset_dir`, `generated` (seeded big tests), `compare` (`trim`/`exact`/`tokens`/`float:1e-6`), `explain` (`llm`) |
| `compiler_warnings` | Clean compile under strict flags | none | `flags`, `penalty_per_warning` (0.1) |
| `static_analysis` | Function size and complexity, cppcheck, globals, goto | none | `max_cyclomatic`, `max_function_length`, `cppcheck` |
| `function_split` | Decomposition into helpers | none | `min_functions`, `max_main_share` (0.8) |
| `edge_case_runner` | Corner cases and seeded random stress vs. the reference | `edge_cases.json`; `reference` or `"expected"`; `gen.py` if `random_tests > 0` | `cases` (list or `"all"`), `random_tests`, `sizes`, `seed`, `explain` |
| `complexity_estimator` | Growth rate vs. the required bound | `gen.py` | `required`, `sizes`, `penalty_per_class` (0.5), `timeout_s` |
| `sanitizer_run` | ASan, UBSan, LSan and valgrind over all inputs | none required; replays sample + tests + corner cases | `flags`, `valgrind`, `penalty_per_issue` (0.25), `max_input_bytes` |
| `llm_review` | Readability on a fixed 10-point rubric | an LLM | `provider` (`ollama`/`groq`/`openai_compat`), `model`, `temperature`, `seed` |
| `similarity_flag` | Copying from classmates, the web or LLMs | `corpus_dir` and/or `known_dir` | `corpus_dir`, `known_dir`, `flag_threshold`, `flag_only` |
| `source_patterns` | Required or forbidden code (e.g. no `gets`) | none | `forbid`, `require` (regex + message) |

Helper scripts for instructors:

```sh
python scripts/make_expected.py problems/<p>                          # write tests/*.out from reference.c
python scripts/generate_known_solutions.py problems/<p> --count 3     # fill known/ with LLM solutions
```

---

## 7. Grading from only `submission.c` + `rules.json`

The workflow in section 6 needs a folder full of instructor assets. With
`--generate-assets` (or the UI's **Generate missing assets** box) a bare
`rules.json` is enough: **no `asset_generation` block, statement, tests,
reference, generator or known solutions are required.** The grader:

1. **detects** which assets the configured analyzers need but the folder
   lacks (`autograder/assets/prepare.py: find_missing`);
2. **infers** the problem from what the instructor published: the problem
   title (`"problem": "add_two_integers"` -> "add two integers"), the statement
   or `asset_generation.description` if any, limits, the required complexity,
   the corner-case family names in the rules, `sample_input`/`sample_output`
   and any examples;
3. **generates** an input spec, a generator, boundary and LLM corner cases,
   three independent oracle programs (brute force, efficient, careful), and
   LLM solutions for the originality corpus;
4. **validates** each one independently and rejects failures with the reason;
5. **stores** everything in SQLite and in
   `.autograder-cache/generated/<problem>/<generation_id>/`;
6. **feeds** the validated files to the unchanged analyzers, and reuses them on
   every later run until the problem or the generation settings change.

Without the flag nothing changes. **With the flag on a complete folder nothing
is generated**, and the fingerprint is the same as without the flag.

```sh
./grade.sh llm-check                                                                   # Ollama reachable, model pulled?
./grade.sh grade  examples/bare_workflow/submission.c --rules examples/bare_workflow/rules.json --generate-assets
./grade.sh prepare --rules examples/bare_workflow/rules.json                           # inspect, no grading
./grade.sh batch  path/to/submissions --rules path/to/rules.json --generate-assets --out reports/x
./grade.sh verify examples/bare_workflow/submission.c --rules examples/bare_workflow/rules.json --generate-assets --runs 3
```

[`examples/bare_workflow/rules.json`](examples/bare_workflow/rules.json) is the
whole problem: a title, limits, one sample with its output, and the metrics.
[`examples/minimal_workflow/rules.json`](examples/minimal_workflow/rules.json)
shows the optional `asset_generation` overrides.

### What is generated, and how it is validated

| Asset | Needed by | Produced by | Validated by |
|---|---|---|---|
| input spec | everything below | `asset_generation.input_spec` if given; else the LLM reading the problem brief | a strict schema check, and it **must accept every instructor-written input** (sample, examples, test inputs). On failure the LLM is retried with the validator's errors, at most `max_retries` times. With no instructor input at all, the submission's `scanf`/`fgets` lines are offered as a *format hint* only; the report flags it (`input_format_from_submission`). |
| `gen.py` | `complexity_estimator`, `io_tests.generated`, `edge_case_runner.random_tests` | a fixed template that interprets the spec. **No model-written code runs.** | determinism (same seed, same output), and every output parses under the spec at every requested size. Requested sizes outside the spec's range are dropped and reported; fewer than 3 left -> complexity not performed, with the reason. |
| corner-case inputs | `edge_case_runner`, `io_tests`, `sanitizer_run` | deterministic boundary families (min/max values, minimum size, ...) plus LLM-proposed families (the rules' `cases` names are requested by name) | each input is parsed against the spec; invalid ones are stored as *rejected* with the reason |
| oracle programs + reference | `io_tests`, `edge_case_runner` | three LLM programs in different styles | see below; kept under `oracles/`, the accepted one becomes `reference/reference.c` |
| `known/` solutions | `similarity_flag` | LLM-written solutions | text only; never compiled or run |

### Where expected outputs may come from

**The submission is never the answer key, and it is never shown to the model**
(except the input-format hint above, which never reaches the oracle).
Trusted sources, in order:

1. outputs the instructor wrote (`tests/*.out`, `"expected"` in
   `edge_cases.json`, `sample_output`, `asset_generation.examples`);
2. the instructor's `reference` solution;
3. by default (`"oracle": "auto"`; `"none"` turns it off): three LLM-written
   programs (brute force, efficient, careful), each of which must
   * pass a static screen: standard headers only, and no process, file or
     network calls;
   * compile and run in the same sandbox as student code;
   * reproduce **every** instructor example output (the published sample is in
     the prompt; `asset_generation.examples` and instructor `tests/*.out` are
     **held out** and never shown, not even on failure);
   * not crash on any valid input (a crashing candidate is dropped, not the
     whole oracle).

   An input gets an expected output only if **every** remaining candidate prints
   the same thing (no majority vote). At least 2 must remain when instructor
   examples anchor them; **all 3** when there is no example output at all
   (flag `llm_oracle_unanchored`). If they agree on every validation input,
   the efficient one becomes the generated reference and random stress tests
   run against it (`llm_consensus_oracle`). If they disagree anywhere, only the
   agreed inputs are used and nothing is trusted for unseen inputs
   (`llm_consensus_inputs`); each skipped input lists what each candidate
   printed.

If none of these exists, correctness and corner cases are reported **not
performed**, with the precise reason (LLM unreachable at which address, which
candidate failed which example, which inputs the candidates disagree on, ...).
They earn **no credit** and are not hidden by rescaling the grade (flag
`tests_not_performed`). Metrics that need no expected output (complexity,
memory safety, warnings, structure, readability, originality) still run.

### Reading the result

| Status / flag | Meaning |
|---|---|
| `prepared` | The metric runs entirely on validated generated assets |
| `partial` + `tests_incomplete` | Some probes could not be checked and were skipped; the score covers only what ran. Every skip and its reason is in the report and the TXT download |
| `not_performed` + `tests_not_performed` | No trustworthy assets; no credit, and the weight stays in the denominator |
| `asset_generation_failed` | A generation step failed (LLM unreachable, every answer rejected, oracle rejected, ...). This is **not the student's fault**, and is listed separately from student failures |
| `llm_oracle_used` | Expected outputs came from the consensus oracle: evidence, not proof. Review before publishing |
| `llm_oracle_unanchored` | No instructor example output exists; the oracle is pure consensus of 3 programs. Add `sample_output` |
| `input_format_from_submission` | No instructor input exists; the input format leaned on the submission's reading code. Add `sample_input` |

### Where the generated files are

```text
.autograder-cache/generated/<problem>/<generation_id>/
  manifest.json              what was decided per metric, the oracle verdict, the LLM used
  validation/report.json     every asset: status, origin, reason (rejected ones with content)
  spec/input_spec.json       the validated input format
  generators/gen.py          deterministic generator (fixed template + the spec)
  tests/<metric>/*.in|.out   correctness tests (instructor examples + generated)
  edge_cases/<metric>.json   corner cases with their expected outputs
  inputs/replay_inputs.json  inputs replayed under the sanitizers
  oracles/<style>.c          every oracle candidate, accepted or not
  reference/reference.c      the generated reference (full consensus only)
  known/*.c                  LLM solutions for the originality check
```

`<generation_id>` is the first 16 hex digits of the bundle key (a hash of the
rules, statement, instructor files, generation settings including the model,
generator and prompt versions, and the compiler), so any change to the problem
or the generation configuration gives a new folder and an unchanged one is
reused. The instructor's folder is never written to. SQLite is the record; the
folder is rebuilt from it if a file is edited or deleted. In Docker the
project folder is mounted at `/work`, so these files land in the project's
`.autograder-cache/` on the host (Windows included); the Assets tab shows the
host path of every file.

### The `asset_generation` block (optional; every key overrides a default)

| Key | Meaning | Default |
|---|---|---|
| `provider`, `model`, `seed` | LLM used for generation (`--llm-provider`/`--llm-model`, the UI fields or `AUTOGRADER_LLM_PROVIDER`/`AUTOGRADER_LLM_MODEL` override these) | `ollama`, `qwen2.5:7b`, 42 |
| `description` | Problem statement inline, used when there is no `statement` file | none |
| `input_spec` | Instructor-written input spec (format: `autograder/assets/inputspec.py`). Generators and boundary cases then work without any LLM | none |
| `examples` | `[{"input", "output"}]` trusted examples, held out from the model; they become tests and anchor the oracle | `[]` |
| `oracle` | `"auto"`, `"llm_consensus"` (same) or `"none"` | `"auto"` |
| `oracle_candidates` | Oracle programs to write (2-3) | 3 |
| `min_examples_for_oracle` | Refuse the oracle unless at least this many instructor example outputs exist | 0 |
| `max_retries` | Attempts per LLM step | 3 |
| `max_case_families`, `known_solutions` | LLM corner-case families; LLM solutions for `known/` | 6, 3 |
| `max_input_bytes`, `oracle_max_input_bytes` | Largest generated input kept; largest input cross-checked by the oracle | 50000, 4000 |

---

## 8. How it works, and why it is built this way

### Grading path

```text
rules.json -> load_rules (rules.py) -> [AssetOverlay from assets/prepare.py, only with --generate-assets]
submission.c -> Context (context.py: copies source, caches builds + reference outputs)
  -> first_pass (coordinator.py): compile -> smoke run on sample_input -> static sanity
       compile fails / does not halt -> execution metrics "skipped" (score 0), report PARTIAL
  -> dispatch: one analyzer per metric, ThreadPoolExecutor
       analyzer.run(ctx, params) -> AnalyzerResult(score 0..1 | None, feedback, evidence, counterexamples)
  -> collate: grade, counterexamples grouped by (verdict, fix), suggestions by points lost, flags
  -> fingerprint = sha256(report minus timing metadata)
  -> reports/<name>.json + .txt  ->  SQLite grades table
```

### Asset-preparation path (`--generate-assets`)

```text
rules.json -> find_missing (per analyzer) -> nothing missing? -> overlay = None (explicit workflow)
  -> bundle key = H(rules, statement, instructor assets, settings, model, generator+prompt version, compiler)
  -> SQLite has a validated bundle with that key? -> reuse
  -> else: problem brief (title, statement, limits, sample, examples)
           -> input spec -> gen.py -> input pool (boundary + LLM) + random-stress inputs
           -> 3 oracle programs -> screen, compile, held-out examples, per-input consensus
           -> expected outputs -> known/
           each step: generate -> validate -> accept | retry with errors | reject (recorded)
  -> per metric: ready | prepared | partial | not_performed (with the reason)
  -> SQLite (bundle, every asset incl. rejected, every LLM attempt)
  -> materialise .autograder-cache/generated/<problem>/<generation_id>/ -> AssetOverlay -> load_rules
```

| Component | File | Responsibility |
|---|---|---|
| CLI | `autograder/cli.py` | Commands, flags, the database hook |
| Coordinator | `autograder/coordinator.py` | First pass, dispatch, collation, fingerprint |
| Rules | `autograder/rules.py` | Parse and validate rules; `AssetOverlay`; asset hashing |
| Sandbox | `autograder/sandbox.py` | Run untrusted binaries under CPU, memory, output and wall-clock limits |
| Context | `autograder/context.py` | Per-submission builds, runs, reference outputs, generators |
| Analyzers | `autograder/analyzers/*.py` | One metric each; contract in `base.py` |
| Explanations | `autograder/explain.py` | Failing run -> WHY + FIX (rule-based; optional cached LLM) |
| LLM client | `autograder/llm.py` | Ollama / Groq / OpenAI-compatible; content-addressed answer cache |
| Asset layer | `autograder/assets/` | Inventory, generation, validation, oracle, SQLite store |
| Web UI | `autograder/web/` | Stdlib HTTP server + JSON API + one static page |
| Reports | `autograder/report.py` | JSON and student text report |

### The decisions that carry the design

| Decision | Why | What breaks without it |
|---|---|---|
| Fixed code decides what runs; an LLM never chooses the analyzers | The grade must not depend on a model's choices | Two runs take different paths and get different grades |
| Every LLM answer is cached by a hash of the full request (temperature 0, fixed seed) | Temperature 0 is only *mostly* repeatable | A GPU or server update changes a token and the readability score drifts |
| Complexity counts instructions with callgrind, not seconds | Instruction counts are identical on every run | CPU-time noise flips O(n) and O(n log n) between runs (hence the macOS flag) |
| Limits are set by a `/bin/sh ulimit` prelude, not Python `preexec_fn` | Analyzers run in threads; `preexec_fn` can deadlock a forked child | A deadlocked child looks like a timeout: a random TLE |
| Time-limit verdicts use CPU time from `wait4` | Parallel analyzers compete for CPU | A correct program waiting for a core is marked too slow |
| The generated-asset LLM writes **data** (an input spec), never generator code | Data can be checked mechanically; the generator is our template | Model-written Python would run with the grader's permissions |
| The oracle needs agreement between independent programs on **each** input, plus every instructor example; held-out examples are never shown or fed back | The examples must stay an independent check; a disagreement marks an ambiguous input | The model "fixes" its oracle to the examples and the check proves nothing; a majority vote picks the common bug |
| `not_performed` weight stays in the denominator | No credit for tests that never ran | Rescaling hands the correctness weight to whatever else the student scored |
| Generated assets ignore the submission (only exception: the format hint when the instructor gave no input, which is then part of the key) | Every student is graded against the same tests; a batch prepares once | Tests differ per student, and a batch regenerates per student |
| Originality ignores the reference's and common I/O skeletons' fingerprints, and needs 8 distinctive fingerprints on both sides and 8 shared before FLAGGED | Normalisation erases names, so every small program is the same token stream | Two honest `a+b` programs score 1.00 and get flagged; below the bar it is SUSPECT, never an accusation |
| The report's `asset_preparation` has no paths, timings or cache-hit flag | The fingerprint must match across machines and re-runs | `verify` fails on a second run only because the cache was warm |

[`docs/DESIGN.md`](docs/DESIGN.md) defends each analyzer's method in depth and
lists concrete cases where the verdict is wrong.

---

## 9. Configuration and environment variables

| Variable | Read by | Default | Purpose |
|---|---|---|---|
| `AUTOGRADER_CACHE` | CLI, UI | `.autograder-cache` | Cache root: LLM answers, SQLite database, generated assets |
| `OLLAMA_HOST` | LLM client | `127.0.0.1:11434`; inside the Docker image `host.docker.internal:11434` | Where Ollama listens |
| `AUTOGRADER_OLLAMA_HOST` | `grade.sh`, `grade.ps1` | `http://host.docker.internal:11434` | Ollama address as seen from the container |
| `AUTOGRADER_LLM_PROVIDER`, `AUTOGRADER_LLM_MODEL` | asset generation (CLI, UI, Docker) | rules, then `ollama` / `qwen2.5:7b` | Which LLM generates assets; overrides the rules' `asset_generation`. Part of the bundle key and recorded in the report |
| `GROQ_API_KEY` | LLM client (`provider: groq`) | none | Groq instead of Ollama; forwarded into Docker |
| `OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPAT_API_KEY` | LLM client (`provider: openai_compat`) | none | Any OpenAI-compatible server; forwarded into Docker |
| `AUTOGRADER_HOST_WORKDIR` | web UI | set by `grade.sh`/`grade.ps1` | Host path of `/work`, so the Assets tab shows paths you can open |
| `AUTOGRADER_IMAGE` | `grade.sh` | `autograder:latest` | Image to run |
| `AUTOGRADER_PORT` | `grade.sh serve` | 8000 | Host port for the web UI |

Everything that affects *what is graded* lives in `rules.json`, never in
environment variables, with one exception: the model that *generates* assets
can be chosen at run time. It is part of the bundle key and is recorded in the
report's `asset_preparation.llm`, so the rules hash plus the bundle key
identify the grading criteria.

---

## 10. Where data is stored

| Path | Contents | Commit it? |
|---|---|---|
| `reports/` | `<name>.json`, `<name>.txt`, `summary.csv` | no (gitignored) |
| `.autograder-cache/llm/` | LLM answer cache, one JSON file per request hash | yes for sample problems: it makes `--offline` reproduce `sample_outputs/` exactly |
| `.autograder-cache/autograder.sqlite3` | tables `grades`, `asset_bundles`, `assets`, `generation_events` | no (gitignored) |
| `.autograder-cache/generated/<problem>/<generation_id>/` | Generated assets by kind (spec, generators, tests, edge cases, oracles, reference, known, validation report) | no (gitignored); rebuilt from SQLite if edited or deleted |

Inspect the database with any SQLite client:

```sh
sqlite3 .autograder-cache/autograder.sqlite3 \
  "SELECT id, submission, problem, grade, grade_complete, substr(fingerprint,8,12) FROM grades ORDER BY id DESC LIMIT 10;"
sqlite3 .autograder-cache/autograder.sqlite3 \
  "SELECT status, kind, name, origin, detail FROM assets WHERE status = 'rejected';"
```

Generated assets never go into the problem folder: instructor files are never
overwritten. Bundles are reused until a dependency changes (rules, statement,
instructor assets, settings, model or its Ollama digest, generator or prompt
version, compiler). A bundle built while the model was unreachable is retried
on the next online run.

---

## 11. Adding a metric without editing the agent

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

Return `score=None` with `AnalyzerResult.unavailable(reason)` when the metric
cannot be measured: the report then flags it instead of scoring 0. Plugins are
Python and run with the grader's permissions, so only load files you trust.
For the same reason, the web UI refuses `plugins` in uploaded rules.

---

## 12. Tests

```sh
python -m pytest -q                  # 101 tests (1 is Linux-only and skips on macOS)
./scripts/test_in_docker.sh          # the same suite inside the Linux image (valgrind, LSan, RLIMIT_AS)
```

| File | Covers |
|---|---|
| `tests/test_end_to_end.py` | Real submissions graded end to end (overflow counterexample, infinite loop, compile error, plagiarism) |
| `tests/test_rules.py` | Rules validation messages, relative paths, plugins |
| `tests/test_sandbox.py` | Limits, timeouts, output floods, crashes |
| `tests/test_units.py` | Comparison, explanations, complexity fitting, sanitizer parsing, similarity |
| `tests/test_assets.py` | Generated assets with a mocked LLM: backward compatibility, validation, retries, oracle acceptance and rejection, caching and invalidation, tamper recovery, failure reporting |
| `tests/test_autogen.py` | A bare rules.json (no `asset_generation`): inference, oracle validation, corner cases executed with trusted outputs, complexity on a generated generator, originality thresholds, persistence and reuse, the TXT report, Ollama connectivity against a fake server |
| `tests/test_web.py` | Web API over a real local server, upload restrictions, history, bundles |

Tests need a C compiler but no LLM and no network.

---

## 13. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `error: invalid rules file ... reference solution not found` | Explicit workflow, and a file the rules name is missing | Add the file, or grade with `--generate-assets` |
| readability `n/a`, "LLM unavailable" | Ollama not running, the model not pulled, or `--offline` with no cached answer | `ollama pull qwen2.5:7b` and start Ollama; or accept it (it is flagged `incomplete_grade`) |
| Docker container cannot reach Ollama (Linux / colima) | Ollama listens on 127.0.0.1 only, which the container cannot reach | Start it with `OLLAMA_HOST=0.0.0.0 ollama serve`, or set `AUTOGRADER_OLLAMA_HOST`. *Docker Desktop on macOS usually works as is; this fix is from Docker networking behaviour, not tested on every setup.* |
| `nondeterministic_measurement` flag; `verify` says NOT REPRODUCIBLE | No valgrind (macOS / option B), so complexity uses CPU time | Grade in Docker |
| structure / modularity show "Analyzer error" (option B) | `lizard` not installed (plain `python3` instead of the venv) | `pip install -e '.[dev]'` and use the venv's `python` |
| correctness / logical "Not performed" with `--generate-assets` | No trusted expected outputs; the feedback says why (LLM unreachable, oracle rejected, ...) | Run `./grade.sh llm-check`; add `sample_output` so the oracle is anchored; or add a `reference` |
| logical "partial", "oracle candidates disagree" | The problem is ambiguous on those inputs (e.g. overflow semantics) | Say it in a statement or `description`, or add an example output for that case |
| complexity "Not performed: requested size(s) ... outside the input's allowed range" | The rules' `sizes` exceed the bounds in the statement / input spec | Use sizes within the problem's limits |
| `asset_generation_failed: input_spec` | LLM unreachable, or every LLM answer was rejected (e.g. it did not accept the sample input) | Check the reason in the report; add `sample_input` and a `description`, or write `input_spec` yourself |
| `LLM unavailable: ... does not have model 'x'` | The model is not pulled in Ollama | `ollama pull x` on the Ollama host, or pick another with `--llm-model` / the UI |
| Generated assets look stale | A dependency outside the key changed (e.g. a silently updated Groq model) | Delete the bundle: `sqlite3 .autograder-cache/autograder.sqlite3 "DELETE FROM asset_bundles WHERE bundle_key LIKE '<prefix>%';"` and re-run |
| `./grade.sh: file not found` for a file outside the repository | Only the current folder is mounted into the container | Copy the file under the repository and use a relative path |
| Permission denied writing `.autograder-cache` or `reports` | The folder was created by root (e.g. an earlier `sudo docker run`) | `sudo chown -R "$USER" .autograder-cache reports` |
| Web UI: "address already in use" | Port 8000 is taken | `AUTOGRADER_PORT=9000 ./grade.sh serve` or `autograder serve --port 9000` |
| Web UI upload: "plugins are not allowed" / "absolute or '..' paths" | Uploaded rules are restricted on purpose | Put the problem folder in the repository and pick its rules from the list |
| Same submission, different fingerprints on two machines | Different toolchain or CPU architecture: instruction counts differ between arm64 and x86_64 | Compare `tool_versions` in both reports, and grade on one platform. *Inferred from how callgrind counts; not measured across architectures here.* |

---

## 14. Repository layout and further reading

```
autograder/
  cli.py            grade / batch / verify / prepare / serve / check-rules / tools
  coordinator.py    the main agent: first pass, dispatch, collation, fingerprint
  rules.py          load + validate the rules file, AssetOverlay, hash problem assets
  sandbox.py        run untrusted binaries under CPU/memory/output/wall limits
  context.py        per-submission state: builds, runs, reference outputs, generators
  compiler.py       compile + parse gcc/clang diagnostics
  diagnostics.py    parse ASan / UBSan / LSan / valgrind output
  explain.py        failing run -> WHY + FIX (rule-based, optional cached LLM)
  llm.py            Ollama / Groq / OpenAI-compatible client with content-addressed answer cache
  ctokens.py        small C lexer (normalisation, readability facts)
  report.py         JSON + student text report
  analyzers/        one file per analyzer (+ base.py: contract and registry)
  assets/           --generate-assets
    prepare.py        the pipeline: inventory, cache key, per-metric decisions, materialisation
    inputspec.py      input-spec format: validate, generate, boundary cases
    llmgen.py         LLM prompts, schema checks, bounded retries
    oracle.py         trusted-output policy, static screen, consensus oracle
    store.py          SQLite: bundles, assets, generation events, grades
  web/              serve: stdlib HTTP server + JSON API, static/index.html (no build step)
  data/usefulness_limitations.json
problems/           two example problems with rules, tests, reference, submissions
examples/plugins/   an analyzer added from the rules file only
examples/bare_workflow/     submission.c + a bare rules.json, for --generate-assets
examples/minimal_workflow/  the same with optional asset_generation overrides
sample_outputs/     the agent's reports on every sample submission (Docker toolchain)
scripts/            make_expected.py, generate_known_solutions.py, test_in_docker.sh
tests/              pytest suite
docs/DESIGN.md      design, method defence, usefulness & limitations
```

| Read | For |
|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | Each analyzer's method and why; reproducibility; usefulness and limitations; the generated-assets design (section 7) |
| `autograder/analyzers/base.py` | The analyzer contract |
| `autograder/assets/prepare.py` | The generated-assets pipeline, top to bottom |
| `autograder/assets/oracle.py` | Exactly what counts as a trusted expected output |
| `autograder/sandbox.py` | The limits untrusted code runs under |
