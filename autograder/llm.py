"""LLM client with a content-addressed answer cache.

Temperature 0 and a fixed seed make an LLM *mostly* repeatable, but not
guaranteed: a different GPU, a server update or batching can change a token and
therefore a score. So every answer is stored under a hash of everything that
went into the request (provider, model, sampling settings, messages). The second
time the same submission is graded under the same rules, the stored answer is
reused and the grade is identical by construction -- even offline.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class LLMAnswer:
    data: Dict[str, Any]  # parsed JSON object
    raw: str
    cache_key: str
    cache_hit: bool
    model_digest: Optional[str]


def default_cache_dir() -> Path:
    return Path(os.environ.get("AUTOGRADER_CACHE", ".autograder-cache")) / "llm"


def in_container() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("AUTOGRADER_IN_DOCKER") == "1"


def _ollama_base() -> str:
    """OLLAMA_HOST if set; otherwise the host machine's Ollama -- reached as
    host.docker.internal from inside the Docker image, 127.0.0.1 outside it."""
    default = "host.docker.internal:11434" if in_container() else "127.0.0.1:11434"
    host = (os.environ.get("OLLAMA_HOST") or default).strip()
    if host.startswith("0.0.0.0"):
        host = "127.0.0.1" + host[len("0.0.0.0"):]
    if not host.startswith("http"):
        host = "http://" + host
    return host.rstrip("/")


def _connection_help(base: str) -> str:
    if "host.docker.internal" in base:
        return ("Start Ollama on the host (`ollama serve`). From Docker the host is host.docker.internal "
                "(grade.sh adds --add-host=host.docker.internal:host-gateway); on Linux, Ollama must also "
                "listen beyond localhost: `OLLAMA_HOST=0.0.0.0 ollama serve`. Set AUTOGRADER_OLLAMA_HOST "
                "to use another address.")
    return "Start Ollama (`ollama serve`) or set OLLAMA_HOST to where it listens."


def _post(url: str, body: dict, headers: Optional[dict] = None, timeout: float = 600) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Ollama answers 404 {"error": "model 'x' not found"} for a model that is not pulled
        try:
            detail = json.loads(e.read().decode()).get("error") or str(e)
        except (ValueError, OSError, AttributeError):
            detail = str(e)
        raise LLMUnavailable(f"{url} answered HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        raise LLMUnavailable(f"cannot reach {url} ({reason})") from e


class LLMClient:
    def __init__(self, cache_dir: Optional[Path] = None, offline: bool = False):
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.offline = offline  # only answer from cache
        self._lock = threading.Lock()
        self._digests: Dict[str, Optional[str]] = {}
        self.stats = {"hits": 0, "misses": 0}

    # -- public -----------------------------------------------------------
    def chat_json(
        self,
        *,
        provider: str,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        seed: int = 42,
        schema: Optional[dict] = None,
        cache_salt: Optional[str] = None,
    ) -> LLMAnswer:
        request = {
            "provider": provider, "model": model, "temperature": temperature,
            "seed": seed, "messages": messages, "schema": schema,
        }
        if cache_salt:  # e.g. the model digest: new weights must not replay old answers
            request["cache_salt"] = cache_salt
        key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / key[:2] / f"{key}.json"
        if path.exists():
            entry = json.loads(path.read_text())
            with self._lock:
                self.stats["hits"] += 1
            return LLMAnswer(entry["data"], entry["raw"], key, True, entry.get("model_digest"))
        if self.offline:
            raise LLMUnavailable("offline mode and no cached answer for this request")

        if provider == "ollama":
            raw = self._ollama(model, messages, temperature, seed, schema)
            digest = self._ollama_digest(model)
        elif provider in ("groq", "openai_compat"):
            raw = self._openai_compat(provider, model, messages, temperature, seed)
            digest = None
        else:
            raise LLMUnavailable(f"unknown LLM provider '{provider}'")
        data = _parse_json_object(raw)
        with self._lock:
            self.stats["misses"] += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps({"request": request, "raw": raw, "data": data,
                                   "model_digest": digest}, indent=2))
        os.replace(tmp, path)  # atomic: concurrent graders never see half a file
        return LLMAnswer(data, raw, key, False, digest)

    def model_identity(self, provider: str, model: str) -> Optional[str]:
        """Digest of the model weights when the provider reports one (Ollama),
        else None. Offline mode never touches the network."""
        if self.offline or provider != "ollama":
            return None
        return self._ollama_digest(model)

    # -- providers --------------------------------------------------------
    def _ollama(self, model, messages, temperature, seed, schema) -> str:
        body = {
            "model": model, "messages": messages, "stream": False,
            "format": schema or "json",
            "options": {"temperature": temperature, "seed": seed, "top_k": 1, "top_p": 1.0,
                        "num_ctx": 8192},
        }
        base = _ollama_base()
        try:
            resp = _post(base + "/api/chat", body)
        except LLMUnavailable as e:
            if "not found" in str(e) and "model" in str(e):
                raise LLMUnavailable(f"Ollama at {base} does not have model '{model}': run "
                                     f"`ollama pull {model}` on the Ollama host") from e
            if "cannot reach" in str(e):
                raise LLMUnavailable(f"Ollama is not reachable at {base}: {e}. {_connection_help(base)}") from e
            raise
        if "error" in resp:
            raise LLMUnavailable(f"ollama: {resp['error']}")
        return resp.get("message", {}).get("content", "")

    def _ollama_digest(self, model: str) -> Optional[str]:
        with self._lock:
            if model in self._digests:
                return self._digests[model]
        digest = None
        try:
            with urllib.request.urlopen(_ollama_base() + "/api/tags", timeout=10) as r:
                for m in json.loads(r.read().decode()).get("models", []):
                    if m.get("name") == model or m.get("model") == model:
                        digest = m.get("digest")
        except (urllib.error.URLError, OSError, ValueError):
            pass
        with self._lock:
            self._digests[model] = digest
        return digest

    def check(self, provider: str, model: str) -> Dict[str, Any]:
        """Is the provider reachable and the model available? Never raises."""
        out: Dict[str, Any] = {"provider": provider, "model": model, "ok": False, "offline": self.offline}
        if self.offline:
            out["error"] = "offline mode: no LLM is called; only cached answers and stored assets are used"
            return out
        if provider != "ollama":
            key_env = "GROQ_API_KEY" if provider == "groq" else "OPENAI_COMPAT_API_KEY"
            out["base_url"] = ("https://api.groq.com/openai/v1" if provider == "groq"
                               else os.environ.get("OPENAI_COMPAT_BASE_URL", ""))
            out["ok"] = bool(os.environ.get(key_env)) and bool(out["base_url"])
            if not out["ok"]:
                out["error"] = f"{provider}: set {key_env}" + ("" if out["base_url"] else " and OPENAI_COMPAT_BASE_URL")
            return out
        base = _ollama_base()
        out["base_url"] = base
        try:
            with urllib.request.urlopen(base + "/api/tags", timeout=5) as r:
                models = [m.get("name") for m in json.loads(r.read().decode()).get("models", [])]
        except (urllib.error.URLError, OSError, ValueError) as e:
            out["error"] = f"Ollama is not reachable at {base} ({getattr(e, 'reason', e)}). {_connection_help(base)}"
            return out
        out["models"] = models
        names = set(models) | {m.split(":")[0] for m in models if m and m.endswith(":latest")}
        if model not in names:
            out["error"] = (f"Ollama at {base} is up but has no model '{model}' (has: {', '.join(models) or 'none'}). "
                            f"Run `ollama pull {model}` on the Ollama host, or choose one of those.")
            return out
        out["ok"] = True
        return out

    def _openai_compat(self, provider, model, messages, temperature, seed) -> str:
        if provider == "groq":
            base, key_env = "https://api.groq.com/openai/v1", "GROQ_API_KEY"
        else:
            base = os.environ.get("OPENAI_COMPAT_BASE_URL", "")
            key_env = "OPENAI_COMPAT_API_KEY"
        api_key = os.environ.get(key_env)
        if not base or not api_key:
            raise LLMUnavailable(f"{provider}: set {key_env}" + ("" if base else " and OPENAI_COMPAT_BASE_URL"))
        body = {"model": model, "messages": messages, "temperature": temperature, "seed": seed,
                "response_format": {"type": "json_object"}}
        resp = _post(base.rstrip("/") + "/chat/completions", body,
                     headers={"Authorization": f"Bearer {api_key}"}, timeout=120)
        try:
            return resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMUnavailable(f"{provider}: unexpected response {str(resp)[:200]}") from e


def _parse_json_object(raw: str) -> Dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise LLMUnavailable(f"model did not return JSON: {raw[:200]!r}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMUnavailable(f"model returned invalid JSON: {e}") from e
