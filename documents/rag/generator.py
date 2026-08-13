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


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults (can be overridden in settings.py)
# ---------------------------------------------------------------------------
_DEFAULT_BACKEND = "gemini"
_DEFAULT_CLAUDE_MODEL = "claude-opus-4-5"
_OLLAMA_ENDPOINT = "http://100.118.208.125:11434/api/generate"
_OLLAMA_MODEL = "qwen3.5:27b"
_MAX_TOKENS = 1024


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _retrieve_national_law_chunks(title: str, top_k: int = 5) -> list[dict]:
    """Search NationalLawChunk by cosine similarity to the draft title."""
    try:
        from documents.models import NationalLawChunk
        from documents.rag.embedder import embed_text
        from pgvector.django import CosineDistance

        query_vec = embed_text(title)
        chunks = (
            NationalLawChunk.objects
            .exclude(embedding=None)
            .select_related('law')
            .annotate(distance=CosineDistance('embedding', query_vec))
            .order_by('distance')[:top_k]
        )
        return [
            {
                "law_number": c.law.law_number,
                "law_title":  c.law.title,
                "chunk_text": c.chunk_text,
                "score":      round(1.0 - float(c.distance) / 2.0, 4),
            }
            for c in chunks
        ]
    except Exception as e:
        logger.warning("National law chunk retrieval failed: %s", e)
        return []



import requests

def search_philippine_laws(query: str, limit: int = 5) -> list:
    try:
        # Extract keywords from title for better search
        # Remove common words that don't help search
        keywords = query.replace("An Ordinance", "").replace("A Resolution", "")
        keywords = keywords.replace("in San Juan City", "").replace("the City of San Juan", "")
        keywords = keywords.strip()
        
        response = requests.get(
            "https://open-congress-api.bettergov.ph/api/documents",
            params={
                "search": keywords,  # use cleaned keywords
                "limit": limit
            },
            timeout=10
        )
        data = response.json()
        if data.get("success"):
            return data.get("data", [])
        return []
    except Exception as e:
        logger.warning("Open Congress API unavailable: %s", e)
        return []

def generate_legal_basis(title: str, doc_type: str | None = None) -> str:
    """
    Searches Open Congress API for related Philippine national legislation,
    retrieves similar local ordinances via RAG,
    then asks AI to suggest legal bases grounded in both sources.
    Returns the AI-generated text.
    """
    if not title or not title.strip():
        raise ValueError("generate_legal_basis() requires a non-empty document title.")

    # 1. Search national laws from Open Congress API
    national_laws = search_philippine_laws(title, limit=5)
    logger.info(
        "Retrieved %d national law(s) from Open Congress API for: %r",
        len(national_laws), title
    )

    # 2. Get local ordinances via existing RAG retriever
    from documents.rag.retriever import retrieve
    local_docs = retrieve(query=title, top_k=5)
    logger.info(
        "Retrieved %d local document(s) from RAG for: %r",
        len(local_docs), title
    )

    # 3. Build prompt with both sources
    prompt = _build_prompt(
        title=title,
        doc_type=doc_type,
        national_laws=national_laws,
        local_docs=local_docs
    )

    # 4. Call LLM backend
    backend = getattr(
        settings, "LEGAL_BASIS_BACKEND",
        getattr(settings, "LLM_BACKEND", _DEFAULT_BACKEND)
    ).lower().strip()

    if backend == "claude":
        return _call_claude(prompt)
    elif backend == "gemini":
        return _call_gemini(prompt)
    elif backend == "ollama":
        return _call_ollama(prompt)
    else:
        raise RuntimeError(f"Unknown backend: {backend!r}")


def _build_prompt(
    title: str,
    doc_type: str | None,
    national_laws: list,
    local_docs: list
) -> str:
    """
    Build RAG prompt combining national laws from Open Congress API
    and local ordinances from pgvector.
    """
    doc_label = doc_type or "ORDINANCE"

    # Format national laws section
    if national_laws:
        national_section = "RELATED PHILIPPINE NATIONAL LEGISLATION (Open Congress API):\n"
        for law in national_laws:
            # Use congress_website_title as fallback since title is often None
            title_text = (
                law.get('title') or 
                law.get('long_title') or 
                law.get('congress_website_title') or 
                'Unknown'
            )
            bill_no = law.get('name', '')           # e.g. "HBN-04131"
            congress = law.get('congress', '')
            date_filed = law.get('date_filed', '')
            national_section += (
                f"- {bill_no} (Congress {congress}, filed {date_filed}):\n"
                f"  {title_text}\n"
            )
    else:
        national_section = (
            "NATIONAL LEGISLATION: No related bills found in Open Congress API. "
            "Use your training knowledge of Philippine laws carefully.\n"
        )

    # Format local ordinances section
    if local_docs:
        local_section = "RETRIEVED LOCAL ORDINANCES FROM SAN JUAN CITY DATABASE:\n"
        for doc in local_docs:
            local_section += (
                f"- [{doc.get('score', 0):.2f}] {doc.get('title', 'Unknown')}\n"
                f"  Snippet: {doc.get('snippet', '')[:200]}\n"
            )
    else:
        local_section = (
            "LOCAL ORDINANCES: No similar ordinances found in San Juan City database yet.\n"
        )

    return f"""/no_think
You are a legal basis assistant for the Sangguniang Panlungsod of San Juan City, Metro Manila.

{local_section}

{national_section}

Based on the sources above, suggest legal bases for the following:
{doc_label}: "{title}"

For each legal basis:
1. Cite the specific law or ordinance name and number
2. Explain why it is relevant to this draft
3. Mark each as [LOCAL PRECEDENT] or [NATIONAL LAW]

STRICT RULES:
- Only cite laws visible in the sources above OR well-known Philippine laws you are highly confident about
- For national laws not in the sources, only cite if you are 100% certain of the RA number
- Never invent section numbers — write [verify section] if unsure
- If no relevant laws found, say so honestly
- Return only the legal basis suggestions, nothing else
"""



def _build_national_law_prompt(title: str, doc_type: str | None, law_chunks: list[dict]) -> str:
    """Build a focused prompt grounded in uploaded national law chunks."""
    label = ""
    if doc_type and doc_type.strip().upper() in ("ORDINANCE", "RESOLUTION"):
        label = f" {doc_type.strip().upper()}"

    if law_chunks:
        context = "\n\n".join(
            f"[{i+1}] {c['law_number']} — {c['law_title']}\n"
            f"    Excerpt: {c['chunk_text'][:400]}"
            for i, c in enumerate(law_chunks)
        )
        context_block = (
            f"The following are relevant excerpts from uploaded national law documents:\n\n{context}"
        )
    else:
        context_block = (
            "No national law excerpts were found in the database. "
            "Use your knowledge of Philippine law — RA 7160 and relevant Republic Acts — carefully."
        )

    return f"""You are a legal research assistant for the Sangguniang Panlungsod ng San Juan City, Metro Manila, Philippines.

A councilor is drafting a new{label}:
"{title}"

--- NATIONAL LAW DATABASE EXCERPTS ---
{context_block}
--- END ---

Using ONLY the excerpts above (plus well-known Philippine laws you are certain about), list the relevant legal bases this measure can cite.

For each, provide:
- Law name and number
- Specific section (only if you are certain)
- One sentence on why it applies

RULES: Never invent section numbers. Max 5 items. Numbered list only."""


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


def _strip_thinking(text: str) -> str:
    """Remove thinking/reasoning content that leaks into the response."""
    import re
    # Strip <think>...</think> blocks
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # Strip leading "Thinking Process:" / "Thinking:" blocks — find first numbered answer
    match = re.search(r'(?m)^\s*1\.', text)
    if match:
        text = text[match.start():]
    return text.strip()


def _call_ollama(prompt: str) -> str:
    ollama_model = getattr(settings, "OLLAMA_MODEL", _OLLAMA_MODEL)
    # Change endpoint from /api/generate to /api/chat
    base_url = getattr(settings, "OLLAMA_ENDPOINT", _OLLAMA_ENDPOINT)
    chat_endpoint = base_url.replace("/api/generate", "/api/chat")

    payload = {
        "model": ollama_model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "stream": False,
        "options": {
            "temperature": 0.3,
            "num_predict": 4096,
            "num_ctx": 8192,
        },
    }

    logger.info("Calling Ollama chat (endpoint=%s, model=%s).", chat_endpoint, ollama_model)

    try:
        resp = requests.post(chat_endpoint, json=payload, timeout=120)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
            f"Could not connect to Ollama at {chat_endpoint}. "
            "Make sure Ollama is running: `ollama serve`"
        ) from exc
    except requests.exceptions.Timeout:
        raise RuntimeError(
            f"Ollama request timed out after 120 seconds (model={ollama_model}). "
            "Try a smaller model or increase the timeout."
        )
    except requests.exceptions.HTTPError as exc:
        raise RuntimeError(
            f"Ollama returned HTTP {resp.status_code}: {resp.text}"
        ) from exc

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Ollama returned non-JSON response: {resp.text[:200]}"
        ) from exc

    # Chat endpoint returns different structure
    response_text = (
        data.get("message", {}).get("content") or
        data.get("response") or
        ""
    ).strip()

    if not response_text:
        raise RuntimeError(
            "Ollama returned an empty response. "
            f"Full response: {json.dumps(data)[:400]}"
        )

    response_text = _strip_thinking(response_text)
    logger.info("Ollama call complete — %d chars returned.", len(response_text))
    return response_text

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def call_llm(prompt: str) -> str:
    """
    Dispatch *prompt* to whichever LLM backend is configured in settings.LLM_BACKEND.
    Returns the model's plain-text response.
    """
    backend = getattr(settings, "LLM_BACKEND", _DEFAULT_BACKEND).lower().strip()
    if backend == "claude":
        return _call_claude(prompt)
    elif backend == "gemini":
        return _call_gemini(prompt)
    elif backend == "ollama":
        print('will call ollama')
        return _call_ollama(prompt)
    else:
        raise RuntimeError(
            f"Unknown LLM_BACKEND value: {backend!r}. "
            "Set settings.LLM_BACKEND to 'claude', 'gemini', or 'ollama'."
        )


