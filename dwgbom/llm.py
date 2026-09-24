"""Stage 4 - local LLM for the notes the rules could not place.

Two providers, standard library only:
  * "ollama" - Ollama's native API (default). Lets us switch the model's thinking
    mode off, fix the context size and keep the model loaded between runs.
  * "openai" - any OpenAI-compatible server: LM Studio, mlx-lm server, llama.cpp server.

What the model is allowed to do: classify each leftover note and write a short,
readable description. What it is NOT allowed to do: invent quantities, sizes or
materials. Every answer is checked afterwards (see `guard`):
  * the note id must be one we sent
  * every number in its description/designation must appear in the original note
Anything that fails is kept but marked "unverified" for a human to check.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from .rules import CATEGORY_ORDER, flat

SYSTEM_PROMPT = f"""You classify short notes taken from construction drawings, to help build a bill of materials.

For EACH note, return one object:
- "id": the note id, unchanged
- "kind": "material" if the note names something to be procured or fabricated;
          "info" if it is a level, reference, instruction, label or dimension;
          "unclear" if you cannot tell (for example a lone code like "W12")
- "category": one of {json.dumps(CATEGORY_ORDER)}
- "description": short plain-English name, abbreviations expanded (ALUM. -> aluminum, S.S. -> stainless steel, TYP. dropped)
- "designation": the size or profile exactly as written in the note, or ""
- "question": what an estimator would need to ask to complete this line, or ""

Hard rules:
- Use only information in the note. Never add sizes, grades, thicknesses, quantities or brands.
- Do not merge notes; one object per id.
- Reply with JSON only: {{"notes": [ ... ]}}"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": ["material", "info", "unclear"]},
                    "category": {"type": "string", "enum": CATEGORY_ORDER},
                    "description": {"type": "string"},
                    "designation": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["id", "kind", "category", "description", "designation", "question"],
            },
        }
    },
    "required": ["notes"],
}


class LLMError(RuntimeError):
    pass


class LocalLLM:
    def __init__(self, base_url: str, model: str = "auto", api_key: str = "local",
                 temperature: float = 0.0, max_tokens: int = 4000, timeout_s: int = 900,
                 provider: str = "openai", context_length: int = 8192, keep_alive: str = "30m", **_):
        if provider not in ("ollama", "openai"):
            raise LLMError(f"unknown provider {provider!r} (use 'ollama' or 'openai')")
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        if provider == "ollama" and self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]          # native API lives at the root, not /v1
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.context_length = context_length
        self.keep_alive = keep_alive
        self._unsupported: set[str] = set()      # request fields this server refused
        self.model = self._first_loaded_model() if model == "auto" else model
        if provider == "ollama":
            self._check_ollama_model()

    # ---------------------------------------------------------------- public
    def classify(self, notes: list[dict], batch_size: int = 30, log=print) -> list[dict]:
        """notes: [{"id", "text"}] -> [{"id", "kind", "category", ...}] (validated)."""
        results: list[dict] = []
        for i in range(0, len(notes), batch_size):
            batch = notes[i:i + batch_size]
            log(f"  LLM: classifying notes {i + 1}-{i + len(batch)} of {len(notes)} with {self.model} ...")
            reply = self._chat(_user_prompt(batch))
            results.extend(guard(batch, parse_json(reply)))
        return results

    def benchmark(self, prompt: str, max_tokens: int = 200) -> dict:
        """One plain generation, timed. Returns tokens, generation seconds and wall seconds."""
        t0 = time.time()
        if self.provider == "ollama":
            body = {"model": self.model, "stream": False, "think": False, "keep_alive": self.keep_alive,
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0, "num_ctx": self.context_length, "num_predict": max_tokens}}
            data = self._ollama_chat(body)
            return {"tokens": data.get("eval_count"), "gen_s": (data.get("eval_duration") or 0) / 1e9,
                    "load_s": (data.get("load_duration") or 0) / 1e9, "wall_s": time.time() - t0}
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        data = self._request("POST", "/chat/completions", body)
        wall = time.time() - t0
        return {"tokens": (data.get("usage") or {}).get("completion_tokens"), "gen_s": wall,
                "load_s": 0.0, "wall_s": wall}

    def gpu_share(self) -> float | None:
        """Ollama only: fraction of the loaded model that sits in GPU memory (1.0 = all)."""
        if self.provider != "ollama":
            return None
        for m in self._request("GET", "/api/ps").get("models", []):
            if m.get("name") == self.model or m.get("model") == self.model:
                size, vram = m.get("size") or 0, m.get("size_vram") or 0
                return vram / size if size else None
        return None

    # --------------------------------------------------------------- plumbing
    def _first_loaded_model(self) -> str:
        if self.provider == "ollama":
            names = [m.get("name") for m in self._request("GET", "/api/ps").get("models", [])] or \
                    [m.get("name") for m in self._request("GET", "/api/tags").get("models", [])]
            names = [n for n in names if n and "embed" not in n.lower()]
            if not names:
                raise LLMError("Ollama has no models. Run: ./start_ollama.sh")
            return names[0]
        data = self._request("GET", "/models")
        models = [m.get("id") for m in data.get("data", []) if m.get("id")]
        # LM Studio lists embedding models too; prefer anything that is not one.
        chat = [m for m in models if "embed" not in m.lower()]
        if not (chat or models):
            raise LLMError("The LLM server reports no loaded model. Load one in LM Studio / Ollama first.")
        return (chat or models)[0]

    def _check_ollama_model(self) -> None:
        names = {m.get("name") for m in self._request("GET", "/api/tags").get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            raise LLMError(f"model {self.model!r} is not downloaded in Ollama. Run: ./start_ollama.sh {self.model}")

    def _ollama_chat(self, body: dict) -> dict:
        """POST /api/chat, stepping back when this Ollama engine/model lacks a feature.

        Ollama's MLX engine answers `format` with HTTP 501 "structured output is
        unavailable"; older versions reject `think` with HTTP 400. Whatever is refused
        is dropped (and remembered for later batches); the prompt still asks for JSON,
        and parse_json() + guard() check what comes back.
        """
        body = {k: v for k, v in body.items() if k not in self._unsupported}
        for _ in range(3):
            try:
                return self._request("POST", "/api/chat", body)
            except LLMError as exc:
                msg = str(exc).lower()
                if not any(f"http {code}" in msg for code in (400, 422, 500, 501)):
                    raise
                if "format" in body and any(w in msg for w in ("structured", "format", "schema")):
                    drop = "format"
                elif "think" in body:
                    drop = "think"
                elif "format" in body:
                    drop = "format"
                else:
                    raise
                self._unsupported.add(drop)
                body = {k: v for k, v in body.items() if k != drop}
        return self._request("POST", "/api/chat", body)

    def _chat(self, user: str) -> str:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
        if self.provider == "ollama":
            data = self._ollama_chat({
                "model": self.model, "messages": messages, "stream": False,
                "think": False,                   # Qwen3.8 thinks by default; this task needs no reasoning
                "format": _SCHEMA,                # constrain the reply to our JSON schema
                "keep_alive": self.keep_alive,
                "options": {"temperature": self.temperature, "num_ctx": self.context_length,
                            "num_predict": self.max_tokens},
            })
            content = (data.get("message") or {}).get("content")
            if content is None:
                raise LLMError(f"unexpected reply from Ollama: {str(data)[:300]}")
            return content
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "notes", "strict": True, "schema": _SCHEMA}},
        }
        try:
            data = self._request("POST", "/chat/completions", body)
        except LLMError as exc:
            if "400" not in str(exc):
                raise
            body.pop("response_format")          # server without structured output: plain JSON mode
            data = self._request("POST", "/chat/completions", body)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected reply from server: {str(data)[:300]}") from exc

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise LLMError(f"HTTP {exc.code} from {self.base_url}{path}: {detail}") from exc
        except urllib.error.URLError as exc:
            hint = "Is Ollama running? Try ./start_ollama.sh" if self.provider == "ollama" \
                else "Is the LM Studio server running?"
            raise LLMError(f"cannot reach {self.base_url} ({exc.reason}). {hint}") from exc


def _user_prompt(batch: list[dict]) -> str:
    lines = [f'{n["id"]}: {flat(n["text"])}' for n in batch]
    return "Notes:\n" + "\n".join(lines)


def parse_json(reply: str) -> list[dict]:
    """Accept plain JSON, fenced JSON, and reasoning models' <think> preambles."""
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S).strip()
    reply = re.sub(r"^```(?:json)?|```$", "", reply, flags=re.M).strip()
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end < 0:
        raise LLMError(f"model did not return JSON: {reply[:200]!r}")
    obj = json.loads(reply[start:end + 1])
    notes = obj.get("notes", obj if isinstance(obj, list) else [])
    return [n for n in notes if isinstance(n, dict)]


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def guard(batch: list[dict], answers: list[dict]) -> list[dict]:
    """Drop unknown ids; mark answers whose numbers are not in the source note."""
    by_id = {n["id"]: n for n in batch}
    out, seen = [], set()
    for a in answers:
        nid = str(a.get("id", ""))
        if nid not in by_id or nid in seen:
            continue
        seen.add(nid)
        src_numbers = set(_NUMBER.findall(flat(by_id[nid]["text"])))
        claimed = set(_NUMBER.findall(f'{a.get("description", "")} {a.get("designation", "")}'))
        a = {
            "id": nid,
            "kind": a.get("kind") if a.get("kind") in ("material", "info", "unclear") else "unclear",
            "category": a.get("category") if a.get("category") in CATEGORY_ORDER else "Other",
            "description": str(a.get("description", "")).strip()[:120],
            "designation": str(a.get("designation", "")).strip()[:60],
            "question": str(a.get("question", "")).strip()[:200],
            "verified": claimed <= src_numbers,
        }
        if not a["verified"]:
            a["question"] = (a["question"] + " " if a["question"] else "") + \
                f"LLM added numbers not in the note: {sorted(claimed - src_numbers)}"
        out.append(a)
    for nid in by_id.keys() - seen:          # model skipped a note
        out.append({"id": nid, "kind": "unclear", "category": "Other", "description": "",
                    "designation": "", "question": "LLM gave no answer for this note", "verified": False})
    return out
