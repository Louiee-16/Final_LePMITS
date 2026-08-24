"""
rag/generator.py
LePMITS — AI Legal Basis: national-law suggestions for a draft title.

National law only, by design. Local-ordinance precedent is a separate
concern already handled by the inline drafting check (see
documents.views.ai_inline_check, which calls
documents.rag.retriever.retrieve directly) — this module doesn't duplicate
that lookup.

Public entry point:
    generate_legal_basis(title, doc_type=None, content=None) -> list[dict]

Internally:
    1. Extracts a short search phrase from the title via the LLM (cached
       per title — see _extract_search_keywords), since the Open Congress
       API does exact-phrase matching and a full sentence rarely matches.
    2. Searches the Open Congress API for related national bills, and
       deduplicates near-identical ones (see _dedupe_bills).
    3. Builds a prompt grounded in those results plus a plain-text excerpt
       of the draft's own content, so the AI can reason about the measure's
       actual mechanism instead of only its title.
    4. Dispatches to the configured LLM backend, parses its JSON response,
       and resolves each citation's source URL server-side (see
       _parse_citations / _law_url) — the LLM never supplies a URL itself.

Backend selection:
    settings.LEGAL_BASIS_BACKEND, falling back to settings.LLM_BACKEND,
    falling back to "gemini" if neither is set. One of: "claude" | "gemini"
    | "ollama".

Privacy notes:
    The draft title is sent to the configured LLM backend twice (keyword
    extraction, then final generation), and a ≤3000-char plain-text excerpt
    of the draft *content* is sent once, as part of the final generation
    call — before the document is approved. The title, as extracted
    keywords, is also sent to the public Open Congress API
    (open-congress-api.bettergov.ph); the draft content is never sent
    there. The Open Congress lookup can be disabled with
    settings.RAG_EXTERNAL_LAW_SEARCH_ENABLED, in which case the AI falls
    back to its own training knowledge of Philippine law instead — this
    does not affect what's sent to the LLM backend itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache


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



def _resolve_backend() -> str:
    return getattr(
        settings, "LEGAL_BASIS_BACKEND",
        getattr(settings, "LLM_BACKEND", _DEFAULT_BACKEND)
    ).lower().strip()


def _dispatch_to_backend(prompt: str, backend: str) -> str:
    if backend == "claude":
        return _call_claude(prompt)
    elif backend == "gemini":
        return _call_gemini(prompt)
    elif backend == "ollama":
        return _call_ollama(prompt)
    else:
        raise RuntimeError(f"Unknown backend: {backend!r}")


_BOILERPLATE_RE = re.compile(
    r"\b(an ordinance|a resolution|providing for|establishing|regulating|"
    r"in san juan city|the city of san juan|city of san juan|metro manila)\b",
    re.IGNORECASE,
)


def _fallback_keywords(title: str) -> str:
    """Rule-based backup for when the LLM keyword extractor is unavailable."""
    cleaned = _BOILERPLATE_RE.sub("", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.\n")
    return " ".join(cleaned.split()[:6])


_KEYWORD_CACHE_TIMEOUT = 3600  # seconds — covers a typical drafting session


def _extract_search_keywords(title: str) -> str:
    """
    Turn a full draft title into a short search phrase for the Open
    Congress API.

    The API does exact-phrase matching, not keyword/relevance search — word
    order matters and match counts collapse past a handful of words
    (confirmed live: "special education" → 283 hits, "education special"
    → 2, any 3+ word natural-language phrase → 0). Sending a full 20-40
    word draft title returns nothing almost by construction, regardless of
    whether the topic has related national legislation.

    Falls back to a simple rule-based cleanup if the LLM call fails, so a
    flaky extraction never blocks the national-law search entirely.

    Cached per exact title (1 hour): re-clicking "Suggest Legal Basis" on
    the same unsaved draft is a common pattern, and the extracted phrase
    for a given title is stable, so repeat requests skip this LLM call.
    """
    cache_key = "legal_basis_keywords:" + hashlib.sha256(title.encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    prompt = (
        "Extract the single best 2-4 word search phrase from this "
        "Philippine local ordinance/resolution title, to search a "
        "national bill database for related legislation. Return ONLY "
        "the phrase — no punctuation, no quotes, no explanation.\n\n"
        f'Title: "{title}"'
    )

    try:
        keywords = _dispatch_to_backend(prompt, _resolve_backend()).strip().strip('"\'')
        keywords = keywords or _fallback_keywords(title)
    except Exception as exc:
        logger.warning("Keyword extraction failed, using rule-based fallback: %s", exc)
        keywords = _fallback_keywords(title)

    cache.set(cache_key, keywords, _KEYWORD_CACHE_TIMEOUT)
    return keywords


def search_philippine_laws(query: str, limit: int = 5) -> list:
    """
    Query the Open Congress API's dedicated search endpoint.

    `query` should already be a short phrase (see _extract_search_keywords)
    — this function sends it to the API as-is.
    """
    try:
        response = requests.get(
            "https://open-congress-api.bettergov.ph/api/search/documents",
            params={"q": query, "limit": limit},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("success"):
            return data.get("data", [])
        return []
    except Exception as e:
        logger.warning("Open Congress API unavailable: %s", e)
        return []


def _dedupe_bills(laws: list, keep: int = 5) -> list:
    """
    Collapse near-identical bills into one entry.

    It's routine in Philippine Congress for several legislators to file the
    same bill text separately (each claiming their own bill number) before
    it's consolidated in committee — a search can easily return 3+ copies
    of one proposal. Left as-is, that floods the prompt with redundant
    entries and crowds out genuinely different legal bases.

    Each kept entry gets an `_also_filed_as` list of the sibling bill
    numbers it absorbed, so that fact isn't silently lost — a councilor
    knowing 3 legislators filed the same bill is useful context, worth
    mentioning once rather than 3 separate near-identical citations.
    """
    groups: dict[str, list] = {}
    for law in laws:
        key = re.sub(
            r"\s+", " ",
            (law.get("title") or law.get("long_title") or law.get("congress_website_title") or "").strip().lower()
        )
        groups.setdefault(key or law.get("name", ""), []).append(law)

    deduped = []
    for group in groups.values():
        primary = dict(group[0])
        primary["_also_filed_as"] = [b["name"] for b in group[1:] if b.get("name")]
        deduped.append(primary)
    return deduped[:keep]


def _law_url(law: dict) -> str | None:
    """Real, verifiable source link for a retrieved bill — never fabricated."""
    if law.get("senate_website_permalink"):
        return law["senate_website_permalink"]
    sources = law.get("download_url_sources") or []
    if sources:
        return sources[0]
    return None  # e.g. House bills — the API doesn't expose a direct link for these


_LAWPHIL_RA_URL = "https://lawphil.net/statutes/repacts/ra{year}/ra_{number}_{year}.html"


def _verified_enacted_law_url(ra_number: int | None, year: int | None, timeout: int = 4) -> str | None:
    """
    LawPhil.net's Republic Act pages follow a predictable
    /ra{year}/ra_{number}_{year}.html pattern — but the LLM supplying an RA
    number and year is exactly the kind of thing it can get wrong, so the
    URL is never shown unless a live HEAD request confirms it actually
    resolves. A wrong guess 404s (verified: bad year/number combos return
    404, they don't silently land on an unrelated law), so this can only
    ever produce a real link or no link.
    """
    if not ra_number or not year:
        return None
    url = _LAWPHIL_RA_URL.format(year=year, number=ra_number)
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        return url if resp.status_code == 200 else None
    except requests.RequestException as exc:
        logger.warning("LawPhil verification failed for RA %s (%s): %s", ra_number, year, exc)
        return None


def _parse_citations(raw: str, national_laws: list) -> list[dict]:
    """
    Parse the LLM's JSON citation list and attach a real source URL to each
    item by matching its `bill_ref` back against the bills we actually
    retrieved from Open Congress — the LLM never supplies URLs itself, so
    there's no risk of a hallucinated link.

    Falls back to a single non-clickable citation holding the raw text if
    the response isn't valid JSON, so a formatting slip degrades the
    feature instead of breaking it.
    """
    by_ref = {law.get("name", "").strip().lower(): law for law in national_laws if law.get("name")}

    match = re.search(r"\[.*\]", raw, re.DOTALL)
    json_text = match.group(0) if match else raw

    try:
        items = json.loads(json_text)
        if not isinstance(items, list):
            raise ValueError("expected a JSON array")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Could not parse citation JSON, falling back to plain text: %s", exc)
        return [{"law_title": "AI-generated legal basis", "reason": raw.strip(), "bill_ref": None, "status": None, "url": None}]

    citations = []
    for item in items:
        if not isinstance(item, dict) or not item.get("law_title"):
            continue
        bill_ref = (item.get("bill_ref") or "").strip()
        source_law = by_ref.get(bill_ref.lower())
        status = item.get("status") if item.get("status") in ("enacted", "pending") else (
            "pending" if source_law else "enacted"
        )

        if source_law:
            url = _law_url(source_law)
        elif status == "enacted":
            # Not from a retrieved bill — try a verified LawPhil.net link
            # using the RA number/year the model gave, if any.
            url = _verified_enacted_law_url(item.get("ra_number"), item.get("year"))
        else:
            url = None

        citations.append({
            "law_title": item["law_title"],
            "reason":    item.get("reason", ""),
            "bill_ref":  bill_ref or None,
            "status":    status,
            "url":       url,
        })
    return citations


_CONTENT_EXCERPT_LIMIT = 3000  # chars — enough for the operative clauses without ballooning the prompt


def _clean_content_excerpt(content: str | None) -> str:
    """Plain-text excerpt of the draft body for prompt context — strips the
    Quill-generated HTML tags and caps length."""
    if not content:
        return ""
    from django.utils.html import strip_tags
    text = strip_tags(content).strip()
    return text[:_CONTENT_EXCERPT_LIMIT]


def generate_legal_basis(title: str, doc_type: str | None = None, content: str | None = None) -> list[dict]:
    """
    Searches Open Congress API for related Philippine national legislation,
    then asks AI to suggest legal bases grounded in that source.

    `content`, if given, is an excerpt of the draft's actual body (WHEREAS
    clauses, operative provisions) — without it, the AI only has the title
    to reason from, which produces generic, boilerplate-sounding citations
    ("the Local Government Code grants LGUs the power to regulate...") that
    don't engage with what the measure actually does.

    Returns a list of citations, each shaped:
        {"law_title": str, "reason": str, "bill_ref": str | None,
         "status": "enacted" | "pending", "url": str | None}
    `status` distinguishes real, in-force Republic Acts ("enacted") from
    bills that have merely been filed in Congress and carry no legal
    authority yet ("pending") — the Open Congress API is a bills tracker,
    not a database of enacted law, so this distinction matters for what a
    councilor can actually cite. `url` is only set when the underlying
    bill has a real, verifiable source link — never a guessed or
    LLM-supplied address.

    National law only, by design — local-ordinance precedent is already
    surfaced separately by the inline drafting check (ai_inline_check /
    documents.rag.retriever.retrieve), so duplicating that lookup here
    would just repeat the same result under a different feature.
    """
    if not title or not title.strip():
        raise ValueError("generate_legal_basis() requires a non-empty document title.")

    # Search national laws from Open Congress API — opt-out via settings,
    # since this sends the draft title (via keyword extraction) externally.
    if getattr(settings, "RAG_EXTERNAL_LAW_SEARCH_ENABLED", True):
        keywords = _extract_search_keywords(title)
        # Fetch a wider pool than we'll actually use — Congress often has
        # several near-identical bills on one topic, so deduping down to 5
        # from a 5-item fetch leaves too little to choose from.
        raw_laws = search_philippine_laws(keywords, limit=15)
        national_laws = _dedupe_bills(raw_laws, keep=5)
        logger.info(
            "Retrieved %d national law(s), %d after deduping, from Open Congress API "
            "for keywords %r (title: %r)",
            len(raw_laws), len(national_laws), keywords, title
        )
    else:
        national_laws = []
        logger.info("RAG_EXTERNAL_LAW_SEARCH_ENABLED is False — skipping Open Congress API lookup.")

    content_excerpt = _clean_content_excerpt(content)
    prompt = _build_prompt(title=title, doc_type=doc_type, national_laws=national_laws, content_excerpt=content_excerpt)
    raw = _dispatch_to_backend(prompt, _resolve_backend())

    return _parse_citations(raw, national_laws)


def _build_prompt(title: str, doc_type: str | None, national_laws: list, content_excerpt: str = "") -> str:
    """
    Build the RAG prompt from national laws retrieved via the Open Congress
    API. National law only — local-ordinance precedent is out of scope for
    this feature (see generate_legal_basis()'s docstring).
    """
    doc_label = doc_type or "ORDINANCE"

    if national_laws:
        national_section = (
            "PENDING BILLS FROM OPEN CONGRESS API (filed in Congress, NOT yet enacted "
            "law — these carry no legal authority on their own; treat them as context "
            "on legislative activity, not as citable legal basis):\n"
        )
        for law in national_laws:
            # Use congress_website_title as fallback since title is often None
            title_text = (
                law.get('title') or
                law.get('long_title') or
                law.get('congress_website_title') or
                'Unknown'
            )
            bill_no = law.get('name', '')           # e.g. "HBN-04131" — the bill_ref the model must echo back
            congress = law.get('congress', '')
            date_filed = law.get('date_filed', '')
            also_filed = law.get('_also_filed_as') or []
            also_note = f" — also filed separately as {', '.join(also_filed)}" if also_filed else ""
            national_section += (
                f"- bill_ref: {bill_no} (Congress {congress}, filed {date_filed}){also_note}:\n"
                f"  {title_text}\n"
            )
    else:
        national_section = (
            "NATIONAL LEGISLATION: No related pending bills found in Open Congress API.\n"
        )

    draft_section = (
        f"DRAFT EXCERPT (the actual measure — use this to reason about the specific "
        f"mechanism, not just the topic):\n{content_excerpt}\n"
        if content_excerpt else
        "DRAFT EXCERPT: not available — only the title is known, so reasoning must stay "
        "general and should say so rather than inventing specifics.\n"
    )

    return f"""/no_think
You are a legal basis assistant for the Sangguniang Panlungsod of San Juan City, Metro Manila.

{national_section}

{draft_section}

Based on the above, suggest national-law legal bases for the following:
{doc_label}: "{title}"

PRIORITIZE actual enacted Philippine national law (Republic Acts, Presidential Decrees,
the Local Government Code, etc.) that you are highly confident actually exists and is in
force — this is what belongs in an ordinance's legal-basis section. Only mention a pending
bill from the source above if it adds genuinely useful context (e.g. Congress is actively
legislating on this exact issue) — do not present a pending bill as if it were existing law.

If several bills above cover the same underlying topic, treat that as one point (e.g. note
that multiple legislators have filed on it), not as separate near-duplicate entries.

BE SPECIFIC, NOT GENERIC:
- When citing the Local Government Code (RA 7160), name the actual applicable clause —
  e.g. "Section 16 (General Welfare Clause)", "Section 458 (specific powers of the
  Sangguniang Panlungsod)", or the relevant taxing-power provision — not just "the Local
  Government Code grants LGUs the power to regulate/tax." If you cannot tell which section
  applies, say "[verify section]" rather than defaulting to a vague, generic description.
- Match the cited law's MECHANISM to the draft's actual mechanism. If the draft excerpt is
  a straight prohibition/ban with penalties, do not cite tax or revenue-raising provisions
  (e.g. NIRC excise tax sections) just because a retrieved bill above happens to be about
  taxing the same subject — that bill's approach is not necessarily this draft's approach.
  Only cite tax law if the draft excerpt itself imposes a tax, fee, or similar charge.
- "reason" must be a real explanation (2-4 sentences), not a one-liner: state what the cited
  provision actually says or authorizes, then explain concretely how it supports what THIS
  draft does — referencing its specific mechanism (ban, tax, permit, labeling, penalty,
  etc.) and, where relevant, quoting or paraphrasing the operative language from the draft
  excerpt. A sentence that would fit any ordinance on any topic is not acceptable.

Respond with ONLY a JSON array — no markdown code fences, no other text. Each item:
{{"bill_ref": "<exact bill_ref from the source above if citing a pending bill, else null>", "law_title": "<law name and number, e.g. Republic Act 7160>", "ra_number": <bare Republic Act number as an integer, e.g. 7160 — only if status is "enacted" and you are certain, else null>, "year": <year the law was enacted, as an integer — only if you are certain, else null>, "reason": "<2-4 sentences: what the provision says, then why it specifically applies to this draft>", "status": "enacted" or "pending"}}

STRICT RULES:
- "status": "enacted" for real, in-force national law; "pending" only for bills from the source above
- When citing a source above, "bill_ref" MUST exactly match its bill_ref value — do not paraphrase it
- When citing enacted law from general knowledge instead, set "bill_ref" to null
- Only cite an enacted law if you are 100% certain of its RA/PD number — never invent one, and leave "ra_number"/"year" null rather than guess
- Never invent section numbers — write "[verify section]" in the reason if unsure
- Do not cite local San Juan City ordinances — this feature covers national law only
- Prefer 2-4 distinct, genuinely relevant citations over a longer list of redundant ones
- If nothing relevant applies, return an empty JSON array: []
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


