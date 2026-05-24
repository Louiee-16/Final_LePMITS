"""
rag/generator.py
LePMITS — Step 5: RAG Prompt + LLM Call

Public entry point:
    generate_legal_basis(title: str, doc_type: str | None = None) -> str

Internally:
    1. Calls retrieve(title) → top-5 relevant snippets
    2. Builds a RAG prompt grounded in Philippine local legislation context
    3. Dispatches to the configured LLM backend (claude | ollama)
    4. Returns the LLM's plain-text response

Backend selection:
    settings.LLM_BACKEND = "claude"  →  Anthropic Claude API
    settings.LLM_BACKEND = "ollama"  →  local Ollama (http://localhost:11434)
    (defaults to "ollama" if the setting is absent)

Privacy guarantee:
    Only ≤300-char snippets of APPROVED/legacy documents are sent to any
    external API. The unpublished draft body is never transmitted.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests
from django.conf import settings

from documents.rag.retriever import retrieve

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults (can be overridden in settings.py)
# ---------------------------------------------------------------------------
_DEFAULT_BACKEND = "gemini"
_DEFAULT_CLAUDE_MODEL = "claude-opus-4-5"
_OLLAMA_ENDPOINT = "http://localhost:11434/api/generate"
_OLLAMA_MODEL = "llama3.2:3b"
_MAX_TOKENS = 1024


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(
    title: str,
    doc_type: str | None,
    results: list[dict[str, Any]],
) -> str:
    """
    Build the RAG prompt that will be sent to the LLM.

    Only snippet text (≤300 chars each) is included — never full document
    content and never the body of the unpublished draft.
    """
    doc_type_label = ""
    if doc_type:
        normalized = doc_type.strip().upper()
        if normalized in ("ORDINANCE", "RESOLUTION"):
            doc_type_label = f" ({normalized})"

    # --- context passages ---------------------------------------------------
    if results:
        context_lines: list[str] = []
        for i, r in enumerate(results, start=1):
            ref = f" [{r['reference_no']}]" if r.get("reference_no") else ""
            source_label = "Ordinance/Resolution" if r["source"] == "document" else "Legacy Document"
            context_lines.append(
                f"[{i}] {source_label}{ref} — \"{r['title']}\"\n"
                f"    Excerpt: {r['snippet']}\n"
                f"    Similarity score: {r['score']:.4f}"
            )
        context_block = "\n\n".join(context_lines)
        context_section = (
            "The following are the most relevant approved ordinances, resolutions, "
            "and archived documents retrieved from the San Juan City legislative database. "
            "Use ONLY these passages as your evidence base:\n\n"
            f"{context_block}"
        )
    else:
        context_section = (
            "No prior ordinances, resolutions, or legacy documents were found in the "
            "San Juan City legislative database that are closely related to this draft. "
            "You may reference widely-known Philippine national laws and the Local "
            "Government Code (RA 7160) as general legal bases, but clearly note that "
            "no local precedents were found in the system."
        )

    prompt = f"""You are a legal research assistant for the Sangguniang Panlungsod (City Council) of San Juan City, Metro Manila, Philippines.

A legislative staff member is drafting a new {doc_type_label or "legislative measure"} with the following working title:

    \"{title}\"

Your task is to suggest appropriate LEGAL BASES that this draft measure could cite, drawing from:
  - The retrieved context passages below (give these the highest priority)
  - The Local Government Code of 1991 (Republic Act No. 7160) — especially the general welfare clause and the specific powers of the Sangguniang Panlungsod
  - Other relevant Philippine national laws, if clearly applicable

--- RETRIEVED CONTEXT ---
{context_section}
--- END CONTEXT ---

INSTRUCTIONS:
1. List each suggested legal basis clearly (e.g., "Section X of RA XXXX" or "Resolution No. XX-XXXX of the Sangguniang Panlungsod of San Juan City").
2. For each legal basis, write one or two sentences explaining why it is relevant to the draft's subject matter.
3. Prioritize local San Juan City ordinances/resolutions from the context above over general national law where applicable.
4. If a retrieved passage is not actually relevant to the draft, do NOT force it in — only cite what is genuinely applicable.
5. DO NOT invent, hallucinate, or paraphrase laws that are not present in the context or not part of your verified knowledge. If uncertain, say so.
6. Keep your response concise and professional — this will be reviewed by legislative staff.


STRICT RULES:
- Only cite specific laws or ordinances you can see in the retrieved documents above
- Never invent section numbers — if you don't know the exact section, omit it
- If retrieved documents are not relevant enough, say so honestly
- Prioritize San Juan City ordinances over generic national law references
Respond with a numbered list of suggested legal bases followed by a brief rationale for each."""

    return prompt


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------

def _call_gemini(prompt: str) -> str:
    """Call the Google Gemini API and return the response text."""
    try:
        import google.generativeai as genai  # pip install google-generativeai
    except ImportError as exc:
        raise RuntimeError(
            "The 'google-generativeai' package is required for "
            "LLM_BACKEND='gemini'. "
            "Install it with: pip install google-generativeai"
        ) from exc

    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set in settings.py. "
            "Add it or switch LLM_BACKEND to another provider."
        )

    model_name = getattr(settings, "GEMINI_MODEL", "gemini-1.5-flash")

    logger.info(
        "Calling Gemini API (model=%s).",
        model_name,
    )

    genai.configure(api_key=api_key)

    model = genai.GenerativeModel(model_name)

    response = model.generate_content(prompt)

    response_text = response.text if hasattr(response, "text") else ""

    logger.info(
        "Gemini API call complete — %d chars returned.",
        len(response_text),
    )

    return response_text.strip()


def _call_gpt(prompt: str) -> str:
    """Call the OpenAI GPT API and return the response text."""
    try:
        from openai import OpenAI  # requires: pip install openai
    except ImportError as exc:
        raise RuntimeError(
            "The 'openai' Python package is required for LLM_BACKEND='gpt'. "
            "Install it with: pip install openai"
        ) from exc

    api_key = getattr(settings, "OPENAI_API_KEY", None)
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set in settings.py. "
            "Add it or switch LLM_BACKEND to another provider."
        )

    model = getattr(settings, "GPT_MODEL", "gpt-4.1-mini")

    logger.info("Calling OpenAI GPT API (model=%s, max_tokens=%d).", model, _MAX_TOKENS)

    client = OpenAI(api_key=api_key)

    response = client.chat.completions.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        messages=[
            {"role": "user", "content": prompt}
        ],
    )

    response_text = response.choices[0].message.content or ""

    logger.info("GPT API call complete — %d chars returned.", len(response_text))
    return response_text.strip()


def _call_claude(prompt: str) -> str:
    """Call the Anthropic Claude API and return the response text."""
    try:
        import anthropic  # optional dependency — only needed for claude backend
    except ImportError as exc:
        raise RuntimeError(
            "The 'anthropic' Python package is required for LLM_BACKEND='claude'. "
            "Install it with: pip install anthropic"
        ) from exc

    api_key = getattr(settings, "ANTHROPIC_API_KEY", None)
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set in settings.py. "
            "Add it or switch LLM_BACKEND to 'ollama'."
        )

    model = getattr(settings, "CLAUDE_MODEL", _DEFAULT_CLAUDE_MODEL)

    logger.info("Calling Claude API (model=%s, max_tokens=%d).", model, _MAX_TOKENS)

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    # Extract text from the first content block
    response_text: str = ""
    for block in message.content:
        if block.type == "text":
            response_text += block.text

    logger.info("Claude API call complete — %d chars returned.", len(response_text))
    return response_text.strip()


def _call_ollama(prompt: str) -> str:
    """Call the local Ollama HTTP API and return the response text."""
    ollama_model = getattr(settings, "OLLAMA_MODEL", _OLLAMA_MODEL)
    endpoint = getattr(settings, "OLLAMA_ENDPOINT", _OLLAMA_ENDPOINT)

    payload = {
        "model": ollama_model,
        "prompt": prompt,
        "stream": False,
    }

    logger.info("Calling Ollama (endpoint=%s, model=%s).", endpoint, ollama_model)

    try:
        resp = requests.post(endpoint, json=payload, timeout=120)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
            f"Could not connect to Ollama at {endpoint}. "
            "Make sure Ollama is running: `ollama serve`"
        ) from exc
    except requests.exceptions.Timeout:
        raise RuntimeError(
            f"Ollama request timed out after 120 seconds (model={ollama_model}). "
            "Try a smaller model or increase the timeout."
        )
    except requests.exceptions.HTTPError as exc:
        raise RuntimeError(f"Ollama returned HTTP {resp.status_code}: {resp.text}") from exc

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ollama returned non-JSON response: {resp.text[:200]}") from exc

    response_text = data.get("response", "")
    if not response_text:
        raise RuntimeError(
            "Ollama returned an empty 'response' field. "
            f"Full response: {json.dumps(data)[:400]}"
        )

    logger.info("Ollama call complete — %d chars returned.", len(response_text))
    return response_text.strip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_legal_basis(title: str, doc_type: str | None = None) -> str:
    """
    Generate suggested legal bases for a draft legislative measure.

    Parameters
    ----------
    title    : str            — working title of the draft document
    doc_type : str | None     — "ORDINANCE", "RESOLUTION", or None

    Returns
    -------
    str — LLM-generated plain text listing suggested legal bases with rationale.

    Raises
    ------
    ValueError   — if title is empty
    RuntimeError — if the LLM backend is misconfigured or unreachable
    """
    if not title or not title.strip():
        raise ValueError("generate_legal_basis() requires a non-empty document title.")

    # Step 1: retrieve relevant snippets
    try:
        results = retrieve(title)
        logger.info("Retrieved %d snippet(s) for title: %r", len(results), title)
    except Exception as exc:
        logger.error("Retrieval failed for title %r: %s", title, exc)
        # Degrade gracefully — continue with empty context rather than hard-failing
        results = []

    # Step 2: build prompt
    prompt = _build_prompt(title=title, doc_type=doc_type, results=results)

    # Step 3: dispatch to configured backend
    backend = getattr(settings, "LLM_BACKEND", _DEFAULT_BACKEND).lower().strip()

    if backend == "claude":
        return _call_claude(prompt)
    
    
    elif backend =="gemini":
        print('used gemini here')
        return _call_gemini(prompt)
    
    elif backend == "ollama":
        print('used ollama here')
        return _call_ollama(prompt)
    else:
        raise RuntimeError(
            f"Unknown LLM_BACKEND value: {backend!r}. "
            "Set settings.LLM_BACKEND to 'claude' or 'ollama'."
        )