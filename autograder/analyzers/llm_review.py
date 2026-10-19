"""readability: an LLM scores the code against a fixed 10-point rubric.

Readability is the one metric with no crisp formula, so it is the one place an
LLM judges. To keep it reproducible and explainable:

* the rubric is fixed and split into four integer criteria (naming 0-3,
  comments 0-2, formatting 0-2, clarity 0-3) instead of one vague number;
* tool-measured facts (comment ratio, single-letter names, long lines) are put
  in the prompt so the model reasons from facts, not impressions;
* temperature 0, a fixed seed and JSON-schema output are used, and the answer
  is cached by a hash of the request (see llm.py), so a re-grade reuses it.
"""

from __future__ import annotations

import json

from ..ctokens import readability_facts
from ..llm import LLMUnavailable
from .base import Analyzer, AnalyzerResult, register

PROMPT_VERSION = "readability-v1"
CRITERIA = {"naming": 3, "comments": 2, "formatting": 2, "clarity": 3}

SYSTEM_PROMPT = """You grade the READABILITY of a student's C program in an introductory programming course.
Judge only how easy the code is for a human to read and maintain -- NOT whether it is correct or fast.

Score each criterion with an integer:
- naming (0-3): 3 = descriptive names throughout; 2 = mostly clear; 1 = several cryptic names; 0 = mostly single letters or meaningless names. Loop counters i/j/k are acceptable.
- comments (0-2): 2 = the program's purpose and every non-trivial function or block is explained; 1 = some useful comments; 0 = no useful comments. Comments that only restate the code do not count.
- formatting (0-2): 2 = consistent indentation and spacing; 1 = minor inconsistencies; 0 = cramped or inconsistent layout.
- clarity (0-3): 3 = straightforward logic, small focused functions, no magic numbers; 2 = mostly clear; 1 = hard to follow in places; 0 = convoluted.

Reply with JSON only: {"naming": int, "comments": int, "formatting": int, "clarity": int,
"reasons": [up to 4 short strings, cite line numbers], "suggestions": [up to 3 short, concrete improvements]}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "naming": {"type": "integer"}, "comments": {"type": "integer"},
        "formatting": {"type": "integer"}, "clarity": {"type": "integer"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["naming", "comments", "formatting", "clarity", "reasons", "suggestions"],
}


@register
class LLMReview(Analyzer):
    name = "llm_review"
    description = "LLM readability review against a fixed rubric (cached, T=0)"
    needs_execution = False

    def validate(self, params, rules):
        if params.get("provider", "ollama") not in ("ollama", "groq", "openai_compat"):
            return ["'provider' must be ollama, groq or openai_compat"]
        return []

    def run(self, ctx, params):
        provider = params.get("provider", "ollama")
        model = params.get("model", "qwen2.5:7b")
        temperature = float(params.get("temperature", 0))
        seed = int(params.get("seed", 42))
        facts = readability_facts(ctx.source)
        numbered = "\n".join(f"{i:3d}| {line}" for i, line in enumerate(ctx.source.splitlines(), 1))
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Measured facts (computed by a tool; trust them):\n"
                                        f"{json.dumps(facts)}\n\nCode:\n{numbered}"},
        ]
        try:
            ans = ctx.llm.chat_json(provider=provider, model=model, messages=messages,
                                    temperature=temperature, seed=seed, schema=SCHEMA)
        except LLMUnavailable as e:
            return AnalyzerResult.unavailable(f"LLM unavailable ({e})", model=model, provider=provider)

        criteria = {}
        for key, top in CRITERIA.items():
            try:
                criteria[key] = max(0, min(top, int(ans.data.get(key, 0))))
            except (TypeError, ValueError):
                criteria[key] = 0
        points = sum(criteria.values())
        reasons = [str(r) for r in ans.data.get("reasons", [])][:4]
        suggestions = [str(s) for s in ans.data.get("suggestions", [])][:3]
        return AnalyzerResult(
            score=points / sum(CRITERIA.values()),
            feedback=f"{points}/10 -- " + ("; ".join(reasons[:2]) if reasons else "no comments from reviewer"),
            evidence={
                "provider": provider, "model": model, "model_digest": ans.model_digest,
                "temperature": temperature, "seed": seed, "prompt_version": PROMPT_VERSION,
                "criteria": criteria, "facts": facts, "reasons": reasons,
                "raw_response": ans.raw,
            },
            suggestions=suggestions,
        )
