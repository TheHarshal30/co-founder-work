# AutoGrader: design, method choices, usefulness and limitations

AutoGrader grades a C submission against an instructor's rules file. It is
unattended, and the result is reproducible: **the same submission under the
same rules gets the same grade**. This document explains how each part works,
why it was built that way, and where it fails.

---

## 1. The agent at a glance

```
              rules.json ──► load + validate ──────────────────────────┐
                                                                        │
submission.c ──► FIRST PASS (coordinator)                              ▼
                  compile (gcc -O2) ─ smoke run on sample ─ static sanity
                  │
                  ├─ does not compile   → execution metrics = 0 (skipped), flag compile_failed
                  ├─ does not halt      → execution metrics = 0 (skipped), flag suspected_infinite_loop
                  └─ ok                 → run everything
                  ▼
               DISPATCH: one analyzer per metric, in parallel threads
       ┌──────────────┬───────────────┬────────────────┬──────────────┐
   io_tests     edge_case_runner  complexity_est.   sanitizer_run   (execution)
   compiler_warnings  static_analysis  function_split  llm_review  similarity_flag (source only)
       └──────── each returns score ∈ [0,1] + evidence + feedback + counterexamples ─┘
                  ▼
               COLLATE: grade = Σ score·weight·100, counterexamples grouped by
               cause, suggestions ordered by points lost, flags, fingerprint
                  ▼
               reports/<name>.json (machines)   reports/<name>.txt (student)
```

**Why a deterministic coordinator, not an LLM deciding what to run.** The
contract forbids the grade from changing between runs. If an LLM chose which
analyzers to call, or in what order, two runs could take different paths. So
the policy that decides what runs is fixed code that reacts to observed facts
(it compiled; it halted; a test failed, so rerun that input under the
sanitizer). The LLM is used as a tool only for the subjective parts
(readability, optional failure explanations), and every LLM answer is cached.

**Why analyzers are plugins.** The coordinator only knows the contract in
`analyzers/base.py`: `validate(params)`, then `run(ctx, params)`, which returns
an `AnalyzerResult`. The set of metrics is open in two ways:

* A rules file can use an existing analyzer under a new metric name with
  different params, for example a second `io_tests` metric for performance
  tests.
* It can load a new analyzer file through `"plugins"`
  (`examples/plugins/line_budget.py`) without touching the agent.

---

## 2. Running untrusted code safely (`sandbox.py`)

| Threat | Defence |
|---|---|
| Infinite loop | `RLIMIT_CPU` as a hard backstop. The time-limit verdict uses the CPU time measured by `wait4`. |
| Sleeping or blocked forever | Wall-clock timer that `SIGKILL`s the whole process group |
| Memory bomb | `RLIMIT_AS` = `memory_limit_mb` (Linux) plus the container's `--memory` |
| Output flood | stdout goes to a file capped by `RLIMIT_FSIZE`, which raises `SIGXFSZ` (verdict `output_limit`) |
| Fork bomb | New session plus `killpg` after the run, and `--pids-limit` on the container |
| Core dumps, stray files | `RLIMIT_CORE = 0`; the program runs in a fresh empty directory |
| Toolchain differences | The whole grader runs in one Docker image with pinned versions, recorded in every report |

Two decisions here matter for reproducibility:

* **Limits are set by a `/bin/sh` prelude (`ulimit …; exec prog`), not a
  Python `preexec_fn`.** Analyzers run in threads, and `preexec_fn` can
  deadlock a forked child when another thread holds the malloc lock. A
  deadlocked child gets killed by the wall clock and reported as a timeout,
  which would be a random wrong verdict.
* **The TLE verdict uses CPU time, not wall time.** Nine analyzers run in
  parallel. With wall time, a correct program that merely waited for a busy
  CPU would sometimes be judged too slow.

---

## 3. The analyzers: method and why it was chosen

### correctness: `io_tests`
Runs every hidden test (`tests/NN.in`). Large tests can be *generated* from
the problem's generator with a fixed seed instead of being stored. The expected
output comes from `NN.out`, or from the reference solution if the file is
missing. `score = passed / total`. Each failing input is replayed under
ASan/UBSan, so the explanation can name the undefined behaviour (for example
"signed integer overflow at add.c:5"), not just "wrong answer".

### logical: `edge_case_runner` (counterexample generation)
This analyzer hunts for inputs that break the program. The **oracle is the
instructor's reference solution**: any input can become a test without anyone
hand-writing its expected output. It uses two sources of probes:

1. **Named corner-case families** from `edge_cases.json`, such as
   `all_negative`, `single_element`, `int_overflow`, `large_values` and
   `whitespace`. The rules pick which families count.
2. **Seeded random stress tests** from `gen.py`, run *smallest size first*.
   Because small inputs come first, the first failure found is already a
   short, readable counterexample. Because the seed is fixed, it is the same
   counterexample every run. This is property-based testing without the
   non-determinism.

`score = mean over families of (probes passed / probes)`, so a family counts
once however many probes it has.

### Explanations: WHY and FIX (`explain.py`)
A rule-based explainer looks at the verdict, the sanitizer diagnostic and the
shape of the wrong answer:

* expected − got is a multiple of 2³² → int overflow;
* printed 0 when the answer is negative → maximum initialised to 0;
* the answer is surrounded by extra text → print only the answer;
* a timeout on a 1 MB input → too slow; on a tiny input → a loop that never ends;
* a crash with SIGSEGV or SIGABRT → which memory error the sanitizer saw.

The FIX text is deliberately independent of the input, so the same mistake seen
on three inputs becomes one suggestion. Repeat failures are folded into
"ALSO: same problem on test 10, …". With `"explain": "llm"`, a local LLM adds a
problem-aware explanation that cites line numbers; it is cached like every
other LLM answer.

### complexity: `complexity_estimator`
1. Generate one input per size (fixed seed).
2. Run each under **valgrind/callgrind and count executed instructions**. A
   stopwatch varies by a few percent between runs; an instruction count is
   identical, so the estimate (and the grade) cannot flicker.
3. Fit `cost(n) = a + b·f(n)` for f ∈ {1, log n, n, n log n, n², n³, 2ⁿ} by
   least squares. Keep the best fit, preferring the simpler class on a
   near-tie. The intercept `a` absorbs libc start-up and the first read,
   which otherwise mask real growth at small n.
4. `score = 1` if the estimate is within the bound, otherwise
   `1 − 0.5 × (classes above the bound)`.

The same callgrind data gives a profile. The report names the hot line, for
example "line 10 in best_sum(), 41% of instructions at n = 8000", which turns a
complexity verdict into something the student can act on.

### memory_safety: `sanitizer_run`
Replays the sample, all hidden tests and all corner cases against an
`-fsanitize=address,undefined` build; LeakSanitizer is included on Linux.
valgrind memcheck then runs on a `-O0 -g` build. It finds what ASan cannot,
such as reads of uninitialised memory, and finds leaks where LSan is
unavailable. Findings are reduced to *(kind, file:line)* because raw sanitizer
output contains addresses and PIDs that change every run. They are
de-duplicated across tools, so one bug costs once.
`score = 1 − 0.25 × distinct issues`.

### syntax: `compiler_warnings`
Compiles with the rules' flags, at `-O2` because some warnings only appear when
the optimiser runs. Warnings that `-Werror` turned into errors still count as
warnings; only real errors score 0. `score = 1 − 0.1 × warnings`.

### structure: `static_analysis`
lizard measures cyclomatic complexity and length per function, and cppcheck
finds bugs (error, warning, performance and portability severities count; style
findings are only advisory). The analyzer also checks for mutable globals and
`goto`. The score starts at 1.0 and loses a fixed, documented amount per
finding. Each deduction appears in the evidence, so a student sees what cost
what.

### modularity: `function_split`
`0.6 × min(1, functions in use / min_functions) + 0.4 × (main() is not doing
most of the work)`. "In use" counts only helpers that are actually called, so
dead code earns nothing. With only `main()` and `min_functions = 2`, the score
is 0.3, which matches the PDF's worked example.

### readability: `llm_review`
This is the one metric without a crisp formula, so it is the one place an LLM
judges. The rubric is fixed and split into integer criteria: naming 0–3,
comments 0–2, formatting 0–2, clarity 0–3. Tool-measured facts go into the
prompt (comment ratio, single-letter identifiers, long lines). The model runs
at temperature 0 with a fixed seed and `top_k = 1`, under a JSON schema. The
default is a local Ollama model, `qwen2.5:7b`: free, offline and without rate
limits. Groq or any OpenAI-compatible endpoint can be configured instead.

### originality: `similarity_flag`
This uses winnowing (the MOSS algorithm):

1. Lex the code and erase names and literals.
2. Hash every 5-token run and keep the minimum hash in each window of 4.
3. Compare fingerprint sets with the Dice coefficient.

Renaming variables, reformatting or rewriting comments changes nothing.

* **Catching "copied from an LLM".** `scripts/generate_known_solutions.py` asks
  a model for a few solutions in advance and stores them in `known/`. A
  submission that is a near-copy of one gets the flag
  `suspected_external_copy`.
* **Avoiding false flags on tiny problems.** Fingerprints that also occur in
  the reference solution are ignored, because every correct answer shares
  them. Programs with fewer than 8 distinctive fingerprints are reported as
  "not judged" rather than flagged.

A flag is a request for human review, and the report lists the shared line
ranges. With `flag_only`, a flag does not cost points.

---

## 4. How reproducibility is guaranteed

Every known source of run-to-run variation, and what neutralises it:

| Source of variation | What would go wrong | How it is handled |
|---|---|---|
| CPU contention | A test near the limit flips between pass and TLE | TLE judged on CPU time from `wait4`; big tests sized far from the limit (n = 200 000 for an O(n²) solution) |
| Output printed before a kill | "GOT" text differs between runs | Timed-out runs never show partial output |
| Stopwatch timing for complexity | Fitted class flickers | Instruction counts from callgrind |
| Random tests | Different inputs each run | `random.Random("seed:…")`: string seeds hash with SHA-512 and do not depend on `PYTHONHASHSEED` |
| Python `hash()` | Salted per process; similarity would change | Fingerprints use `hashlib.md5` |
| LLM sampling | A different token gives a different score | T = 0, fixed seed, `top_k = 1`, JSON schema, **and** a cache keyed by a SHA-256 of the full request (model, settings, messages). A re-grade reuses the stored answer, even offline (`--offline`). |
| Sanitizer and valgrind output | Addresses and PIDs differ | Parsed into kind + file:line; raw text discarded |
| Temporary paths | `/tmp/autograder-x1y2/…` in messages | Stripped; reports use basenames and rules-relative paths |
| Toolchain | Another gcc gives other warnings and instruction counts | Pinned Docker image; versions in `tool_versions` |
| Environment and stack layout | Programs reading uninitialised memory vary | Fixed minimal environment for every run |
| Rules or test edits | Grade changes silently | `rules_hash` and `assets_hash` (tests, reference, generator, corpus) in every report |
| Generator `.pyc` files | Would change the assets hash | Generators are executed from source; caches are excluded from the hash |
| Timestamps and durations | Report bytes differ | Kept under `meta`, which the fingerprint excludes |

**Evidence:**

* `autograder verify` grades a submission N times and compares fingerprints.
  It printed `REPRODUCIBLE` for every sample tried (`add.c`, `s03_brute_force.c`
  and `s05_leaky.c`).
* All 15 sample reports were regenerated in fresh containers with identical
  fingerprints.
* Two completely fresh LLM caches produced byte-identical Ollama answers. So
  on one machine even the uncached path was deterministic, and the cache
  covers the cases where it would not be (GPU changes, server updates).

---

## 5. Results on the sample submissions

Produced in the Docker image (`sample_outputs/`, reports in `.txt` and `.json`).

**add_two_integers** (rules mirror the PDF example; weights 20/10/10/10/10/10/10/10/10)

| Submission | Grade | What the agent found |
|---|---|---|
| `add.c` (the PDF's) | 77.8 | Fails tests 07/10 (INT_MAX+1, 2·10⁹+2·10⁹). Shows expected 2147483648, got −2147483648, and why: UBSan signed overflow at add.c:5. Only `main()` (modularity 3/10). One `-Wunused-result` warning. |
| `add_clean.c` | 88.0 | All objective metrics full marks. Flagged together with `add_copied.c`. |
| `add_copied.c` | 83.9 | Renamed and reformatted copy of `add_clean.c`: similarity 1.00, flagged `suspected_plagiarism` |
| `add_llm.c` | 69.8 | 1.00 match to `known/llm_qwen2.5-7b_2.c`, flagged `suspected_external_copy`. Also inherits the LLM's int-overflow bug. |
| `add_leaky.c` | 79.0 | Output correct by luck. ASan finds a heap-buffer-overflow READ at line 10, and valgrind finds 8 bytes definitely lost (allocated at line 6). |
| `add_wrong_format.c` | 62.5 | Prints `Enter two numbers: Sum = 12`. Explanation: the right answer is surrounded by extra text. |
| `add_infinite_loop.c` | 39.0 | `while (scanf(...) != 0)` never stops. First pass flags `suspected_infinite_loop`; execution metrics are skipped and the report is marked partial. |
| `add_compile_error.c` | 27.0 | Missing `;`. Flagged `compile_failed`; only source-based metrics earn points. |

**max_subarray** (required O(n), n ≤ 200 000, |aᵢ| ≤ 10⁹)

| Submission | Grade | What the agent found |
|---|---|---|
| `s01_kadane.c` | 93.0 | Correct, O(n) confirmed, clean. Flagged together with its copy `s06`. |
| `s02_zero_init.c` | 84.2 | Counterexample `3 / -8 -3 -6`: expected −3, got 0, because the best value starts at 0. The LLM explainer adds the line. |
| `s03_brute_force.c` | 73.0 | Correct but O(n²): instruction counts 1.5M → 233M for n = 500 → 8000. Times out on the n = 200 000 tests. Hot spot: line 10 in `best_sum()`. |
| `s04_int_overflow.c` | 77.5 | `int` accumulator: expected 951144825913, got 1993529230 (wrapped). UBSan locates it at line 13. |
| `s05_leaky.c` | 62.5 | 1-based loop writes `arr[n]`: ASan heap-buffer-overflow WRITE at line 10. The −O2 build aborts with SIGABRT; valgrind shows the leak from line 8. |
| `s06_copy.c` | 89.0 | Renamed copy of `s01`: similarity 1.00, flagged. |
| `s07_llm.c` | 75.8 | Light edit of an LLM answer: flagged `suspected_external_copy`, and it also fails `large_values` (int overflow). |

---

## 6. Usefulness and limitations

### Usefulness: where this works in practice

1. **Grading a large programming lab.** A 300-student CS1 lab submits
   `max_subarray.c`, and the agent grades every submission unattended. Each
   report contains the exact failing input with the expected output, e.g.
   `3 / -5 -2 -9 → expected -2, got 0: best value initialised to 0`. Every
   student is judged by the same rules. A regrade request is settled by
   re-running and comparing fingerprints.
2. **Coding-interview and hiring screens.** 500 candidates submit "add two
   integers safely". The agent ranks them by correctness, overflow handling and
   memory safety, with a per-metric breakdown that justifies every rejection.
3. **Plagiarism and LLM-copy triage.** Of 300 submissions, the instructor reads
   only the handful flagged above 0.85. Each flag comes with the matched file
   and the shared line ranges, including matches against answers an LLM wrote
   in advance (`s07_llm.c`, `add_llm.c` above).
4. **Practice feedback before the deadline.** A student whose draft stores the
   sum in an `int` sees the exact input (`3 / 1000000000 1000000000 1000000000`),
   the expected 3000000000, the wrapped value printed, and the fix. That
   teaches corner-case thinking without waiting for a TA.

### Limitations: concrete cases where the verdict is wrong or unreliable

1. **Tests cannot prove correctness.** Suppose a submission handles every
   hidden test, every corner-case family and 40 random inputs, but crashes on
   an input that ends without a trailing newline, or on `n = 0` if the
   statement allowed it. No probe produces that input, so the agent reports
   100% correctness. *Mitigation:* add families term by term. The reference
   oracle makes a new probe a one-line edit.
2. **Complexity is measured, not proved.** A duplicate check with two nested
   loops that `return`s at the first duplicate looks O(n) on random inputs,
   because a duplicate appears early. The agent would confirm an O(n) bound
   for an O(n²) algorithm. Conversely, n log n vs n is a factor of about 1.4
   over n = 500–8000, so a sort-based solution may be reported as O(n).
   *Mitigation:* a worst-case generator per problem (all-distinct values),
   wider size ranges.
3. **Similarity is unreliable on tiny or canonical programs.** The PDF's
   `add.c` is token-for-token identical to what `qwen2.5:7b` writes
   (`known/llm_qwen2.5-7b_1.c`, similarity 1.00). Two honest students who both
   write scanf/printf in `main()` would also match. The agent therefore refuses
   to judge programs with fewer than 8 distinctive fingerprints, which means
   it **cannot** catch a copied 6-line answer (a false negative). In the other
   direction, an LLM answer whose loops the student rewrites as recursion falls
   below the threshold.
4. **Readability is subjective and can be gamed.** Comments that only restate
   the code (`// add a and b`) can earn a comments point from a 7B model, and
   a different model (`llama-3.3-70b`) scores the same code differently. The
   cache makes the score *reproducible*, not *correct*. *Mitigation:* low
   weight, measured facts in the prompt, and spot checks by a human.

Further known cases:

* **Undefined behaviour depends on the environment.** `add_leaky.c` reads
  `nums[2]` past a 2-element allocation and prints the right sum *by luck*:
  glibc's chunk padding happens to hold zero. Correctness says 10/10; only
  the memory-safety metric catches it. On another libc the same program could
  print garbage.
* **Running time near the limit.** A program using 0.98 s of CPU against a 1 s
  limit can pass on one machine and fail on a slower one. Reproducibility is
  guaranteed per toolchain and hardware class, not across all machines.
* **Sandbox gaps.** Student code runs in the same container as the grader. It
  has no network limits beyond the container's, and it could read the hidden
  test files from disk. A per-run container or a separate user would close
  this.
* **Single-file submissions only**, and only programs that use stdin/stdout.

---

## 7. Generated assets (`--generate-assets`, `autograder/assets/`)

**Problem.** Most analyzers need instructor assets (tests, a reference
solution, a generator, corner cases, a corpus). The new workflow has only
`submission.c` and `rules.json`. An LLM can write inputs, but it cannot be
trusted to know the right *outputs*, and the submission is the thing being
judged, so it cannot be the answer key either.

**Where it plugs in.** Before rules are loaded. `prepare.py` builds an
`AssetOverlay` (defined in `rules.py`) that `load_rules` applies in memory:
it points a metric's asset keys (`testset_dir`, `cases_file`, `generator`,
`known_dir`) at a separate generated-assets folder, may drop probes that
cannot be checked (`random_tests: 0`, `generated: []`, a missing `cases`
family), and lists metrics that are `not_performed`. Weights, thresholds and
limits are never touched, and the rules hash in the report is the hash of the
instructor's file. Analyzers, the sandbox, scoring and reporting are reused
unchanged; the coordinator only learned one new status.

```
rules.json ─► inventory (which analyzer needs what; what is missing?)
                │ nothing missing ─► overlay = None ─► explicit workflow, same fingerprint
                ▼
           cache key = H(rules, statement, instructor assets, settings, model,
                         generator + prompt version, compiler) ─► SQLite hit? ─┐
                ▼ miss                                                         │
  input spec ── LLM from the statement, or the instructor's; must parse every  │
                instructor input; bounded retries with validator feedback      │
  gen.py ────── fixed template interpreting the spec; determinism + validity   │
  inputs ────── boundary families (deterministic) + LLM families; each parsed  │
  outputs ───── instructor outputs │ instructor reference │ opt-in LLM oracle  │
  known/ ────── LLM solutions (text only)                                      │
                ▼                                                              │
  per metric: prepared │ partial │ not_performed  ─► SQLite (+ rejected, events)
                ▼                                                              │
  materialise <cache>/assets/<key>/ ◄──────────────────────────────────────────┘
                ▼
  load_rules(rules, overlay) ─► grade() as before
```

**Why the LLM writes data, not code, for inputs.** The input spec is a small
declarative format (lines, tokens, ranges, counts, sorted/distinct). It can be
checked mechanically, it is checked against the instructor's own sample input,
and the generator is our template that interprets it, so no model-written
Python runs and nothing it says can become a shell command. A constraint the
format cannot express (e.g. "the graph is connected") must be declared, and
then the spec is rejected rather than used wrongly.

**Why the oracle is opt-in and strict.** An LLM-written reference is only
evidence. It is accepted only if it passes a static screen (standard headers,
no process/file/network calls, no inline assembly), compiles and runs under
the student sandbox, reproduces every instructor example output, and agrees
with an independently written version (brute force vs. efficient) on every
validation input. Example mismatches are never fed back to the model, so the
examples stay an independent check. Any disagreement rejects the oracle; there
is no majority vote. The report flags `llm_oracle_used`.

**Why "not performed" is not rescaled.** A missing *tool* (no Ollama for
readability) is rescaled and flagged, as before. A missing *answer key* is
different: rescaling would award the correctness weight to whatever else the
student scored. So `not_performed` weight stays in the denominator and earns
nothing, and the flag says so. It is also distinct from `skipped` (the
student's program did not compile or halt) and from `error` (an analyzer or
reference crashed).

**Why assets ignore the submission.** If generated tests depended on the
submission, two students would be graded against different tests, and a batch
would regenerate per student. The submission hash is recorded with each grade
in the `grades` table instead.

**Reproducibility.** Every LLM call goes through the cached client
(temperature 0, fixed seed; the model digest salts the cache key when Ollama
reports it). The validated bundle is stored in SQLite and re-materialised
byte-for-byte; the report's `asset_preparation` section contains no paths,
timings or cache-hit flags, so `verify --generate-assets` gives identical
fingerprints across runs and machines.

### Limitations of generated assets

1. **No oracle, no correctness.** With only `rules.json` and a statement, and
   without `sample_output`/`examples`, correctness and corner cases are *not
   performed*. This is deliberate; it is the most common outcome for a bare
   rules file.
2. **The consensus oracle can still be wrong.** Two LLM programs can share the
   same misreading of an ambiguous statement and agree on every input. Passing
   two instructor examples does not rule that out. Treat `llm_oracle_used`
   grades as provisional.
3. **Inputs are only as good as the spec.** The spec format covers
   whitespace-separated numbers, words, arrays and repeated lines. Problems
   with structural constraints (trees, connected graphs, valid expressions) are
   rejected, so their complexity and corner-case metrics are not performed.
   Line structure is checked strictly, so `"7\n5"` is not accepted for a
   one-line format even though `scanf` would read it.
4. **Generated corner cases are not a proof of coverage.** Boundary families
   are generic (min/max values, minimum size); the LLM's families depend on the
   model. A missing family costs nothing to the student, so coverage can only
   be lower than an instructor's set, never higher.
5. **The cache key does not detect every model change.** Offline, or with
   providers that report no digest (Groq), a silently updated model behind the
   same name reuses the stored bundle.
6. **Similarity without a class corpus** compares only against generated
   LLM solutions; classmate copying cannot be detected from a single
   submission.

---

## 8. Possible extensions

* Worst-case generators (`gen.py` modes) to close limitation 2.
* AST-based similarity (tree-sitter) to resist loop-to-recursion rewrites.
* Per-run containers (gVisor or Firecracker) to close the sandbox gaps.
* Multi-file submissions with a Makefile.
