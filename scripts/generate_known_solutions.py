"""Fill a problem's known/ folder with LLM-written solutions.

The originality analyzer can only recognise "pasted from an LLM" if it has seen
what an LLM writes for this problem. Run this once per problem (and again when
students start using a new model):

    python scripts/generate_known_solutions.py problems/max_subarray --model qwen2.5:7b --count 3

Each answer is saved as known/llm_<model>_<i>.c and labelled in known/sources.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

PROMPTS = [
    "Write a complete C program that solves this problem. Reply with only the code.\n\n{statement}",
    "Solve the following programming problem in C (C11). Give the full program.\n\n{statement}",
    "I need a C solution for this exercise from my programming course:\n\n{statement}",
]


def ask_ollama(model: str, prompt: str, seed: int) -> str:
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
    host = host if host.startswith("http") else "http://" + host
    body = {"model": model, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.7, "seed": seed}}
    req = urllib.request.Request(host + "/api/generate", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())["response"]


def extract_code(text: str) -> str:
    m = re.search(r"```(?:c|C)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("problem_dir")
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--count", type=int, default=3)
    args = ap.parse_args()

    pdir = Path(args.problem_dir)
    statement = (pdir / "problem.md").read_text()
    known = pdir / "known"
    known.mkdir(exist_ok=True)
    sources_path = known / "sources.json"
    sources = json.loads(sources_path.read_text()) if sources_path.exists() else {}
    safe_model = re.sub(r"[^A-Za-z0-9.]+", "-", args.model)
    for i in range(args.count):
        prompt = PROMPTS[i % len(PROMPTS)].format(statement=statement)
        code = extract_code(ask_ollama(args.model, prompt, seed=1000 + i))
        name = f"llm_{safe_model}_{i + 1}.c"
        (known / name).write_text(code)
        sources[name] = f"LLM-generated ({args.model})"
        print(f"wrote {known / name}", file=sys.stderr)
    sources_path.write_text(json.dumps(sources, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
