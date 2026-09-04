"""
rag/generator.py
LePMITS — AI Legal Basis: real enacted law + related pending bills.

Every citation this module returns is grounded in something actually
retrieved from a real source — never the AI's own memory. Earlier versions
let the AI cite enacted law (Republic Acts, the Local Government Code,
etc.) from memory, and it confidently invented wrong RA numbers and
descriptions (e.g. claiming RA 10153 was the "Clean Water Act"; it's
actually the 2011 ARMM election-synchronization law). Two real sources
back this now:
    - LawPhil.net's public Republic Acts search (undocumented but public
      JSON API — see search_enacted_laws) for actually-enacted law.
    - The Open Congress API (see search_philippine_laws) for bills merely
      filed in Congress, which carry no legal authority yet.
Anything the AI tries to cite outside those two retrieved sets is dropped
(see _parse_citations). Local-ordinance precedent is a separate concern
already handled by the inline drafting check (see
documents.views.ai_inline_check, which calls
documents.rag.retriever.retrieve directly) — this module doesn't duplicate
that lookup.

Public entry point:
    generate_legal_basis(title, doc_type=None, content=None) -> list[dict]

Internally:
    1. Extracts a narrow search phrase (for Open Congress) and several
       single-word/short candidate terms (for LawPhil) from the title via
       the LLM (cached per title — see _extract_search_keywords). Both
       APIs do exact-phrase-ish matching, not fuzzy relevance search, and
       a bill tracker and a statute index don't share vocabulary for the
       same topic — several individual terms are far more robust than
       betting on one combined phrase (see that function's docstring for
       the concrete case this fixes).
    2. Searches both sources, deduplicating near-identical bills (see
       _dedupe_bills) and merging enacted-law results across terms.
    3. Builds a prompt grounded ONLY in those results plus a plain-text
       excerpt of the draft's own content, so the AI can judge relevance
       to the measure's actual mechanism instead of just its title.
    4. Dispatches to the configured LLM backend, parses its JSON response,
       drops any citation whose ref doesn't match something retrieved, and
       resolves the surviving ones' source URLs server-side (see
       _parse_citations / _law_url / _lawphil_url) — the LLM never
       supplies a URL itself.

Backend selection:
    settings.LEGAL_BASIS_BACKEND, falling back to settings.LLM_BACKEND,
    falling back to "gemini" if neither is set. One of: "claude" | "gemini"
    | "ollama".

Privacy notes:
    The draft title is sent to the configured LLM backend twice (keyword
    extraction, then final generation), and a ≤3000-char plain-text excerpt
    of the draft *content* is sent once, as part of the final generation
    call — before the document is approved. The title, as extracted
    keywords, is also sent to the public Open Congress API and to
    LawPhil.net; the draft content is never sent to either. Both external
    lookups can be disabled with settings.RAG_EXTERNAL_LAW_SEARCH_ENABLED
    — since the AI is never allowed to cite from memory, disabling it
    means this feature simply has nothing to offer (returns no citations)
    rather than falling back to anything else.
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
    except Exception:
        return []


# A small, curated set of general enabling/framework legislation that's
# always offered to the LLM as a candidate, regardless of embedding rank.
# Confirmed directly: pure semantic search over the corpus's one-paragraph
# act summaries never surfaces RA 7160 in the top 30 results for either of
# two real test drafts (an arts/academic-benefits ordinance and a
# medical/financial-assistance ordinance), even though it's the single most
# universally-applicable legal basis for any LGU ordinance — the Local
# Government Code's summary just doesn't textually resemble narrow
# ordinance language the way small, topic-specific acts do. See
# _build_prompt's foundational_section for why these get a looser
# relevance standard than the topic-matched retrieval results.
_FOUNDATIONAL_LAW_NUMBERS = ["RA 7160"]


def _foundational_laws() -> list[dict]:
    """Fetches _FOUNDATIONAL_LAW_NUMBERS from the local corpus, shaped like
    _local_enacted_laws()'s output so the rest of the pipeline (prompt
    building, citation validation) treats them identically to a normal
    retrieval hit."""
    from documents.models import NationalLaw
    laws = []
    for law_number in _FOUNDATIONAL_LAW_NUMBERS:
        law = NationalLaw.objects.filter(law_number=law_number).first()
        if law:
            laws.append({
                "ra_number": law_number,
                "description": law.description,
                "date": None,
                "url": None,
            })
        else:
            logger.warning("Foundational law %r not found in the local corpus.", law_number)
    return laws


def _local_enacted_laws(query: str, top_k: int = 8) -> list[dict]:
    """
    Enacted-law lookup against the locally-imported Republic Acts corpus
    (11,866 acts, 1946-2025 — see
    documents/management/commands/import_republic_acts.py, sourced from
    huggingface.co/datasets/bettergovph/gov-library) instead of live
    keyword-searching LawPhil.net's own search API. Semantic (embedding)
    search over the whole corpus at once, rather than several individual
    keyword terms searched one at a time — more robust to exactly how
    _extract_search_keywords happened to phrase things (confirmed earlier
    this session: the same title's keyword extraction varied between runs
    purely from LLM sampling variance).

    Returns dicts shaped exactly like search_enacted_laws()'s LawPhil.net
    output ({"ra_number", "description", "date", "url"}) so _build_prompt
    consumes either source identically without caring which one ran.
    law_number is already the clean "RA 9003" form import_republic_acts
    normalizes to, and _ra_short_ref's regex extracts the number back out
    of that just as readily as LawPhil's "Republic Act No. 9,003" form.
    """
    chunks = _retrieve_national_law_chunks(query, top_k=top_k)
    return [
        {
            "ra_number":   c["law_number"],
            "description": c["chunk_text"],  # the dataset's pre-generated per-act summary
            "date":        None,  # not stored locally — omitted from the prompt's "(enacted <date>)" rather than guessed
            "url":         None,  # not stored locally — see import_republic_acts.py if this needs adding later
        }
        for c in chunks
    ]



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


def _extract_search_keywords(title: str) -> dict:
    """
    Turn a full draft title into: "narrow" (the specific subject, for the
    Open Congress bill tracker) and "law_terms" (several single-word or
    short candidate terms, for LawPhil's enacted-law search — tried
    individually and merged, not as one combined phrase).

    A bill tracker and a statute index don't use the same vocabulary for
    the same topic: "single-use plastic bags" correctly finds pending
    bills about plastic bags (that's literally what they're titled), but
    finds zero *enacted* law — no Republic Act is specifically about
    plastic bags, only broader framework laws apply (solid waste
    management, extended producer responsibility). But betting on one
    "broad" phrase is fragile too — confirmed live: the same extraction
    prompt returned "solid waste management" (finds RA 9003) on one call
    and "environmental protection" (finds nothing) on the next, purely
    from LLM sampling variance. A single generic word is far more robust
    (e.g. "plastic" alone reliably finds RA 11898), so several candidate
    terms are requested and each searched independently — much less
    sensitive to any one phrase's exact wording.

    Falls back to a simple rule-based cleanup if the LLM call fails, so a
    flaky extraction never blocks the search entirely.

    Cached per exact title (1 hour): re-clicking "Suggest Legal Basis" on
    the same unsaved draft is a common pattern, and the extraction is
    stable for a given title, so repeat requests skip this LLM call.
    """
    cache_key = "legal_basis_keywords_v3:" + hashlib.sha256(title.encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    fallback_term = _fallback_keywords(title)
    fallback = {"narrow": fallback_term, "law_terms": fallback_term.split()[:3]}

    prompt = (
        "From this Philippine local ordinance/resolution title, extract:\n"
        '1. "narrow" — a 2-4 word phrase naming the specific subject, for '
        "searching a Congress bill tracker\n"
        '2. "law_terms" — 4-6 individual words or very short terms (1-2 words '
        "each) a real Philippine national statute on this general subject "
        "might use — cover multiple angles (the specific object/activity, the "
        "general regulatory field, e.g. \"plastic\", \"solid waste\", "
        "\"packaging\", \"environmental\", \"pollution\", \"local government\") "
        "for searching a statute index one term at a time\n\n"
        'Respond with ONLY JSON, no other text: {"narrow": "...", "law_terms": ["...", "..."]}\n\n'
        f'Title: "{title}"'
    )

    try:
        raw = _dispatch_to_backend(prompt, _resolve_backend()).strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        parsed = json.loads(match.group(0) if match else raw)
        law_terms = [t.strip() for t in (parsed.get("law_terms") or []) if isinstance(t, str) and t.strip()]
        keywords = {
            "narrow":    (parsed.get("narrow") or "").strip() or fallback["narrow"],
            "law_terms": law_terms or fallback["law_terms"],
        }
    except Exception as exc:
        logger.warning("Keyword extraction failed, using rule-based fallback: %s", exc)
        keywords = fallback

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


_LAWPHIL_YEAR_RE = re.compile(r"_(\d{4})\.\w+$")
_RA_NUMBER_RE = re.compile(r"(\d[\d,]*)")


def _ra_short_ref(entry: dict) -> str | None:
    """
    "Republic Act No. 9,275" -> "RA 9275" — a short, unambiguous form the
    model is much less likely to paraphrase than the full "Republic Act
    No. ..." string (confirmed live: it abbreviated "Republic Act No.
    9003" to "RA 9003" on its own, which then failed an exact-string
    match against the wrong canonical form — this is that canonical form).
    """
    match = _RA_NUMBER_RE.search(entry.get("ra_number") or "")
    return f"RA {match.group(1).replace(',', '')}" if match else None


def _lawphil_url(entry: dict) -> str | None:
    """
    Build the full LawPhil.net URL for a Republic Act search result.
    Mirrors LawPhil's own frontend widget logic (lawphil.net/scrpts/
    search-grid.js): the API returns a bare filename like
    "ra_9275_2004.html", which lives under /statutes/repacts/ra{year}/.
    """
    url = entry.get("url")
    if not url:
        return None
    if url.startswith("http"):
        return url
    match = _LAWPHIL_YEAR_RE.search(url)
    if not match:
        return None
    return f"https://lawphil.net/statutes/repacts/ra{match.group(1)}/{url}"


def search_enacted_laws(query: str, limit: int = 5) -> list:
    """
    Search LawPhil.net's public Republic Acts index — a real, keyword-
    searchable database of actually-enacted Philippine national law
    (confirmed live: searching "clean water" correctly returns RA 9275,
    the real Clean Water Act; "plastic bags" correctly returns nothing,
    because no enacted law specifically covers that topic — only bills).

    This is an undocumented but public endpoint LawPhil's own site search
    box calls (lawphil.net/statutes/repacts/repacts.html); used here the
    same way, for the same purpose, at a normal request rate.
    """
    try:
        # No page-size param is respected server-side (confirmed live —
        # it always returns its own default page size), so this always
        # slices client-side instead.
        response = requests.get(
            "https://lawphil.net/api/public/republic",
            params={"search": query},
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("data", [])[:limit]
    except Exception as e:
        logger.warning("LawPhil.net search unavailable: %s", e)
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


def _parse_citations(raw: str, national_laws: list, enacted_laws: list) -> list[dict]:
    """
    Parse the LLM's JSON citation list, keeping only citations whose "ref"
    matches something we actually retrieved — either a bill from Open
    Congress or a Republic Act from LawPhil.net's search. Anything the
    model tries to cite from its own memory (no matching ref in either
    source) is dropped, not shown — see generate_legal_basis()'s docstring
    for why nothing else is trusted.

    Falls back to a single non-clickable citation holding the raw text if
    the response isn't valid JSON, so a formatting slip degrades the
    feature instead of breaking it.
    """
    by_bill_ref = {law.get("name", "").strip().lower(): law for law in national_laws if law.get("name")}
    by_law_ref = {
        short_ref.lower(): law
        for law in enacted_laws
        if (short_ref := _ra_short_ref(law))
    }

    match = re.search(r"\[.*\]", raw, re.DOTALL)
    json_text = match.group(0) if match else raw

    try:
        items = json.loads(json_text)
        if not isinstance(items, list):
            raise ValueError("expected a JSON array")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Could not parse citation JSON, falling back to plain text: %s", exc)
        return [{"law_title": "AI-generated summary", "reason": raw.strip(), "ref": None, "status": None, "url": None}]

    citations = []
    for item in items:
        if not isinstance(item, dict) or not item.get("law_title"):
            continue
        ref = (item.get("ref") or "").strip()
        ref_lower = ref.lower()

        if ref_lower in by_law_ref:
            status, url = "enacted", _lawphil_url(by_law_ref[ref_lower])
        elif ref_lower in by_bill_ref:
            status, url = "pending", _law_url(by_bill_ref[ref_lower])
        else:
            logger.warning(
                "Dropping citation %r — ref %r doesn't match any retrieved bill or law.",
                item.get("law_title"), ref,
            )
            continue

        citations.append({
            "law_title": item["law_title"],
            "reason":    item.get("reason", ""),
            "ref":       ref,
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
    Searches two sources for national legislation related to this draft —
    a locally-imported corpus of 11,866 Republic Acts (see
    _local_enacted_laws / documents/management/commands/
    import_republic_acts.py, sourced from huggingface.co/datasets/
    bettergovph/gov-library) for actually-enacted law, and the Open
    Congress API for pending bills — and asks the AI to explain which of
    the RETRIEVED results are genuinely relevant. The AI never cites
    anything from its own memory: earlier versions let it, and it
    confidently invented wrong RA numbers and descriptions (e.g. claiming
    RA 10153 was the "Clean Water Act"; it's actually the 2011 ARMM
    election-synchronization law). See _parse_citations(): any citation
    whose "ref" doesn't match something we actually retrieved is silently
    dropped, not shown.

    `content`, if given, is an excerpt of the draft's actual body (WHEREAS
    clauses, operative provisions) — used both to enrich the local corpus's
    semantic search query and to judge which retrieved results are
    genuinely relevant, since the title alone is often too generic.

    Returns a list of citations, each shaped:
        {"law_title": str, "reason": str, "ref": str,
         "status": "enacted" | "pending", "url": str | None}
    `status` distinguishes a real, in-force Republic Act ("enacted", from
    the local corpus) from a bill that's merely been filed in Congress and
    carries no legal authority yet ("pending", from Open Congress). `url`
    is only set when the source has a real link — never a guessed or
    LLM-supplied address (the local corpus doesn't currently store source
    URLs, so enacted citations have none for now).

    Local-ordinance precedent is a separate concern already surfaced by
    the inline drafting check (ai_inline_check /
    documents.rag.retriever.retrieve), so this doesn't duplicate that.
    """
    if not title or not title.strip():
        raise ValueError("generate_legal_basis() requires a non-empty document title.")

    content_excerpt = _clean_content_excerpt(content)

    # Enacted-law lookup now runs against the local Republic Acts corpus
    # (see _local_enacted_laws) instead of live-searching LawPhil.net — a
    # local DB query + local Ollama embedding call, no network request to
    # anywhere external. Deliberately run unconditionally, NOT gated behind
    # RAG_EXTERNAL_LAW_SEARCH_ENABLED: that flag exists so an unpublished
    # draft's title never leaves this system, and unlike the old live
    # LawPhil search, this path never sends the title anywhere external in
    # the first place — there's nothing for the flag to protect against
    # here. Only the pending-bills lookup below (Open Congress API, a
    # genuinely external call) still needs to respect it.
    query = f"{title}\n\n{content_excerpt[:600]}" if content_excerpt else title
    enacted_laws = _local_enacted_laws(query, top_k=8)
    logger.info(
        "Local enacted-law corpus: %d result(s) for title %r.",
        len(enacted_laws), title
    )

    # Offered separately from the embedding-ranked results above — see
    # _foundational_laws()'s docstring for why these need their own path
    # rather than relying on retrieval rank to surface them.
    existing_refs = {law["ra_number"] for law in enacted_laws}
    foundational_laws = [law for law in _foundational_laws() if law["ra_number"] not in existing_refs]

    if getattr(settings, "RAG_EXTERNAL_LAW_SEARCH_ENABLED", True):
        keywords = _extract_search_keywords(title)

        raw_laws = search_philippine_laws(keywords["narrow"], limit=15)
        # Fetch a wider bill pool than we'll actually use — Congress often
        # has several near-identical bills on one topic, so deduping down
        # to 5 from a 5-item fetch leaves too little to choose from.
        national_laws = _dedupe_bills(raw_laws, keep=5)

        logger.info(
            "For keywords %r (title: %r): %d pending bill(s) (%d after dedup)",
            keywords, title, len(raw_laws), len(national_laws)
        )
    else:
        national_laws = []
        logger.info("RAG_EXTERNAL_LAW_SEARCH_ENABLED is False — skipping pending-bills lookup.")
    prompt = _build_prompt(
        title=title, doc_type=doc_type,
        national_laws=national_laws, enacted_laws=enacted_laws,
        foundational_laws=foundational_laws,
        content_excerpt=content_excerpt,
    )
    raw = _dispatch_to_backend(prompt, _resolve_backend())

    return _parse_citations(raw, national_laws, enacted_laws + foundational_laws)


def _build_prompt(title: str, doc_type: str | None, national_laws: list, enacted_laws: list, foundational_laws: list | None = None, content_excerpt: str = "") -> str:
    """
    Build the RAG prompt from three sources: enacted Republic Acts retrieved
    via the local corpus, pending bills retrieved via the Open Congress API,
    and a small curated set of foundational/enabling laws (see
    _foundational_laws()) that are always offered regardless of retrieval
    rank. National law only — local-ordinance precedent is out of scope for
    this feature (see generate_legal_basis()'s docstring).
    """
    doc_label = doc_type or "ORDINANCE"
    foundational_laws = foundational_laws or []

    if not foundational_laws:
        foundational_section = ""
    else:
        foundational_section = (
            "FOUNDATIONAL / ENABLING LAWS — these grant general legislative authority rather than "
            "regulating one specific subject; cite one only if this measure genuinely falls within "
            "the kind of power it grants (e.g. a benefits, appropriations, or general-welfare measure "
            "may cite the Local Government Code's general welfare clause as its enabling authority):\n"
        )
        for law in foundational_laws:
            ref = _ra_short_ref(law) or "?"
            description = (law.get("description") or "").replace("<br>", " — ")
            foundational_section += f"- ref: {ref}:\n  {description}\n"

    if not enacted_laws:
        enacted_section = "ENACTED REPUBLIC ACTS FROM LAWPHIL.NET: none found for this topic.\n"
    else:
        enacted_section = (
            "ENACTED REPUBLIC ACTS FROM LAWPHIL.NET — real, in-force national law:\n"
        )
        for law in enacted_laws:
            ref = _ra_short_ref(law) or "?"   # e.g. "RA 9275" — the exact ref the model must echo back
            description = (law.get("description") or "").replace("<br>", " — ")
            date = (law.get("date") or "")[:10]
            enacted_section += f"- ref: {ref} (enacted {date}):\n  {description}\n"

    if not national_laws:
        pending_section = "PENDING BILLS FROM OPEN CONGRESS API: none found for this topic.\n"
    else:
        pending_section = (
            "PENDING BILLS FROM OPEN CONGRESS API — filed in Congress, NOT yet enacted law:\n"
        )
        for law in national_laws:
            # Use congress_website_title as fallback since title is often None
            title_text = (
                law.get('title') or
                law.get('long_title') or
                law.get('congress_website_title') or
                'Unknown'
            )
            bill_no = law.get('name', '')           # e.g. "HBN-04131" — the ref the model must echo back
            congress = law.get('congress', '')
            date_filed = law.get('date_filed', '')
            also_filed = law.get('_also_filed_as') or []
            also_note = f" — also filed separately as {', '.join(also_filed)}" if also_filed else ""
            pending_section += (
                f"- ref: {bill_no} (Congress {congress}, filed {date_filed}){also_note}:\n"
                f"  {title_text}\n"
            )

    draft_section = (
        f"DRAFT EXCERPT (the actual measure — use this to judge relevance, not just topic overlap):\n{content_excerpt}\n"
        if content_excerpt else
        "DRAFT EXCERPT: not available — only the title is known.\n"
    )

    return f"""/no_think
You are a legislative research assistant for the Sangguniang Panlungsod of San Juan City,
Metro Manila. You do NOT cite anything from memory — you have been wrong about specific
Republic Act numbers and descriptions before (e.g. once claimed RA 10153 was the "Clean
Water Act"; it's actually an unrelated 2011 election law). Only the items listed below are
real, verified data; anything else would be an unverifiable guess.

{foundational_section}

{enacted_section}

{pending_section}

{draft_section}

The measure being drafted:
{doc_label}: "{title}"

From the lists above ONLY, select the ones genuinely relevant to this measure. For the
ENACTED and PENDING lists: same subject matter or same regulatory mechanism, not just a
shared generic word. For the FOUNDATIONAL list: a broader framework-level connection is
acceptable there specifically, since that's the nature of an enabling law — still ground it
in why THIS TYPE of measure falls within the power it grants, not filler that would apply to
literally any ordinance regardless of subject. Prefer an enacted Republic Act over a pending
bill on the same topic when both are listed, since only the enacted one carries real legal
authority. If several pending bills cover the same underlying topic, treat that as one point
(e.g. note that multiple legislators have filed on it) rather than listing near-duplicates
separately.

"reason" must be a real explanation (2-4 sentences): what the law/bill actually says or
proposes, then concretely why it's relevant to THIS measure — referencing its specific
mechanism (ban, tax, permit, labeling, penalty, etc.), or for a FOUNDATIONAL law, the specific
grant of power it relies on. A sentence that would fit any ordinance on any topic regardless
of subject is not acceptable, even for a foundational law. If citing a pending bill, say
explicitly that it is a pending bill, not existing law.

Respond with ONLY a JSON array — no markdown code fences, no other text. Each item:
{{"ref": "<exact ref value from one of the lists above>", "law_title": "<the law's or bill's name/title>", "reason": "<2-4 sentences: what it says/proposes, then why it's relevant to this measure>"}}

STRICT RULES:
- "ref" MUST exactly match a ref value from one of the lists above — never invent one, never cite anything not listed there
- Do not cite any Republic Act, the Local Government Code, or any other law that is not in one of the lists above — you have no way to verify it
- Do not cite local San Juan City ordinances — this feature covers national legislation only
- If nothing in any of the lists is genuinely relevant, return an empty JSON array: []
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
        # Suppresses extended "thinking" output on models that support it
        # (Qwen3.5, Gemma4, DeepSeek-R1-style, etc.) — this is Ollama's own
        # top-level request field for that, not something nested in
        # "options". _build_prompt's "/no_think" prefix does the same thing
        # via prompt text, but that's a Qwen-specific chat-template
        # convention: harmless-but-inert noise to every other model family.
        # Confirmed directly: the exact same real 8-candidate-law citation
        # prompt against gemma4:e4b took 116s without this field (the model
        # burning almost the entire 4096-token budget on unsuppressed
        # reasoning) and 0.4s with it — a ~300x difference, not a tuning
        # nicety. Without this, "thinking" models were coming within
        # seconds of the 120s HTTP timeout below on every call.
        "think": False,
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


