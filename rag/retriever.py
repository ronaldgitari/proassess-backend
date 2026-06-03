"""
RAG Retriever — Stage 2 of the pipeline.

Strategy:
  1. Query expansion: GPT rewrites the LM's context prompt into N diverse sub-queries.
  2. Dense retrieval: Chroma vector similarity search for each sub-query.
  3. BM25 keyword retrieval: in-memory BM25 over the same candidate pool.
  4. Reciprocal Rank Fusion (RRF): merges the two ranked lists.
  5. Cross-encoder re-ranking: selects final top-K candidates.
"""

from __future__ import annotations

import logging
from typing import Any

import chromadb
from langchain.schema import Document
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_chroma import Chroma
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder

from config import settings

logger = logging.getLogger(__name__)

# Lazy singletons ─────────────────────────────────────────────────
_chroma: Chroma | None = None
_reranker: CrossEncoder | None = None


def get_chroma() -> Chroma:
    global _chroma
    if _chroma is None:
        embeddings = OpenAIEmbeddings(
            model=settings.OPENAI_EMBEDDING_MODEL,
            dimensions=settings.OPENAI_EMBEDDING_DIMENSIONS,
            openai_api_key=settings.OPENAI_API_KEY,
        )
        client = chromadb.HttpClient(
            host=settings.CHROMA_HOST,
            port=settings.CHROMA_PORT,
        )
        _chroma = Chroma(
            client=client,
            collection_name=settings.CHROMA_COLLECTION,
            embedding_function=embeddings,
        )
    return _chroma


def get_reranker() -> CrossEncoder | None:
    global _reranker
    if _reranker is None:
        try:
            _reranker = CrossEncoder(settings.RERANKER_MODEL)
        except Exception as e:
            logger.warning("Cross-encoder failed to load (%s) — re-ranking disabled", e)
            return None
    return _reranker


# ─────────────────────────────────────────────────────────────────
# Step 1: Query expansion
# ─────────────────────────────────────────────────────────────────

async def expand_query(topic: str, context_prompt: str | None, domain: str) -> list[str]:
    """
    Use GPT to rewrite the assessment topic into 4 diverse sub-queries
    optimised for retrieval.
    """
    llm = ChatOpenAI(
        model=settings.OPENAI_CHAT_MODEL,
        temperature=0.3,
        openai_api_key=settings.OPENAI_API_KEY,
    )

    user_input = f"Topic: {topic}"
    if context_prompt:
        user_input += f"\nAdditional context: {context_prompt}"

    prompt = f"""You are a retrieval query expert. Given an assessment topic and optional context, generate 4 distinct search queries that would together retrieve the most relevant information from a knowledge base.

Domain: {domain}
{user_input}

Return exactly 4 queries, one per line, no numbering or bullets. Each query should approach the topic from a different angle (definition, application, policy/regulation, best practice).
"""
    from services import pipeline_tracker as pt
    async with pt.track_span("openai", f"chat.completion · {settings.OPENAI_CHAT_MODEL}", phase="retrieve", detail="query expansion"):
        response = await llm.ainvoke(prompt)
    queries = [q.strip() for q in response.content.strip().split("\n") if q.strip()]
    queries = queries[:4]
    if not queries:
        queries = [topic]
    logger.info("Expanded '%s' → %d queries", topic, len(queries))
    return queries


# ─────────────────────────────────────────────────────────────────
# Step 2: Dense retrieval from Chroma
# ─────────────────────────────────────────────────────────────────

def dense_search(
    queries: list[str],
    org_id: str,
    domain_tag: str | None = None,
    source_id: str | None = None,
    k: int = 20,
) -> list[tuple[Document, float]]:
    """
    Run each sub-query against Chroma and collect unique results.
    Returns (Document, score) pairs sorted by best score per doc.
    """
    chroma = get_chroma()

    # Build Chroma filter: source_id takes precedence over domain_tag
    if source_id:
        filter_dict: dict[str, Any] = {
            "$and": [
                {"org_id":    {"$eq": org_id}},
                {"source_id": {"$eq": source_id}},
            ]
        }
    elif domain_tag:
        filter_dict = {
            "$and": [
                {"org_id":     {"$eq": org_id}},
                {"domain_tag": {"$eq": domain_tag}},
            ]
        }
    else:
        filter_dict = {"org_id": {"$eq": org_id}}

    seen: dict[str, tuple[Document, float]] = {}

    def _run_search(f: dict) -> None:
        for query in queries:
            results = chroma.similarity_search_with_relevance_scores(
                query, k=k, filter=f
            )
            for doc, score in results:
                cid = doc.metadata.get("source_id", "") + doc.page_content[:50]
                if cid not in seen or score > seen[cid][1]:
                    seen[cid] = (doc, score)

    _run_search(filter_dict)

    # Fall back to org-only filter if source/domain filter returns nothing
    if not seen and (source_id or domain_tag):
        logger.info("Filtered search returned 0 results; retrying with org_id filter only")
        _run_search({"org_id": {"$eq": org_id}})

    ranked = sorted(seen.values(), key=lambda x: x[1], reverse=True)
    logger.info("Dense retrieval: %d unique candidates", len(ranked))
    return ranked


# ─────────────────────────────────────────────────────────────────
# Step 3: BM25 keyword search
# ─────────────────────────────────────────────────────────────────

def bm25_search(
    queries: list[str],
    candidate_docs: list[Document],
    k: int = 20,
) -> list[tuple[Document, float]]:
    """
    Run BM25 over the candidate pool retrieved by dense search.
    Returns (Document, bm25_score) pairs.
    """
    if not candidate_docs:
        return []

    tokenised = [doc.page_content.lower().split() for doc in candidate_docs]
    bm25 = BM25Okapi(tokenised)

    combined_query = " ".join(queries)
    scores = bm25.get_scores(combined_query.lower().split())

    ranked = sorted(
        zip(candidate_docs, scores),
        key=lambda x: x[1],
        reverse=True,
    )
    return ranked[:k]


# ─────────────────────────────────────────────────────────────────
# Step 4: Reciprocal Rank Fusion
# ─────────────────────────────────────────────────────────────────

def reciprocal_rank_fusion(
    dense_results: list[tuple[Document, float]],
    bm25_results: list[tuple[Document, float]],
    k_rrf: int = 60,
) -> list[Document]:
    """
    Merge two ranked lists using RRF.
    Score = Σ 1 / (k_rrf + rank_i)
    """
    scores: dict[str, float] = {}
    docs_by_key: dict[str, Document] = {}

    def doc_key(doc: Document) -> str:
        return doc.page_content[:100]

    for rank, (doc, _) in enumerate(dense_results, start=1):
        key = doc_key(doc)
        scores[key] = scores.get(key, 0.0) + 1.0 / (k_rrf + rank)
        docs_by_key[key] = doc

    for rank, (doc, _) in enumerate(bm25_results, start=1):
        key = doc_key(doc)
        scores[key] = scores.get(key, 0.0) + 1.0 / (k_rrf + rank)
        docs_by_key[key] = doc

    fused = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [docs_by_key[key] for key, _ in fused]


# ─────────────────────────────────────────────────────────────────
# Step 5: Cross-encoder re-ranking
# ─────────────────────────────────────────────────────────────────

def cross_encode_rerank(
    query: str,
    docs: list[Document],
    top_k: int = 10,
) -> list[Document]:
    """
    Re-rank the fused candidate set using a cross-encoder model.
    Falls back to truncation if the cross-encoder is unavailable.
    """
    if not docs:
        return []

    reranker = get_reranker()
    if reranker is None:
        logger.warning("Cross-encoder unavailable; returning top-%d by fusion score", top_k)
        return docs[:top_k]

    pairs = [(query, doc.page_content) for doc in docs]
    scores = reranker.predict(pairs)

    ranked = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
    logger.info("Cross-encoder: selected %d from %d candidates", top_k, len(docs))
    return [doc for doc, _ in ranked[:top_k]]


# ─────────────────────────────────────────────────────────────────
# Public interface
# ─────────────────────────────────────────────────────────────────

async def retrieve(
    *,
    topic: str,
    context_prompt: str | None,
    domain: str,
    org_id: str,
    source_id: str | None = None,
    top_k_retrieval: int | None = None,
    top_k_final: int | None = None,
) -> list[Document]:
    """
    Full retrieval pipeline: expand → dense → BM25 → RRF → cross-encoder.
    Returns the top-K most relevant Document chunks for question generation.
    """
    k_ret = top_k_retrieval or settings.TOP_K_RETRIEVAL
    k_fin = top_k_final or settings.TOP_K_FINAL

    # 1. Expand query
    queries = await expand_query(topic, context_prompt, domain)

    from services import pipeline_tracker as pt

    # 2. Dense retrieval (Chroma vector search — embeds each query via OpenAI internally)
    async with pt.track_span("chroma", "similarity_search", phase="retrieve",
                             detail=f"{len(queries)} sub-queries × k={k_ret}"):
        dense_results = dense_search(queries, org_id, domain_tag=domain, source_id=source_id, k=k_ret)
    candidate_docs = [doc for doc, _ in dense_results]

    # 3. BM25 over candidates
    bm25_results = bm25_search(queries, candidate_docs, k=k_ret)

    # 4. RRF merge
    fused_docs = reciprocal_rank_fusion(dense_results, bm25_results)

    # 5. Cross-encoder re-rank (local model, CPU)
    combined_query = f"{topic} {context_prompt or ''}"
    async with pt.track_span("app", "cross_encoder.rerank", phase="retrieve",
                             detail=f"{len(fused_docs)} candidates → top {k_fin}"):
        final_docs = cross_encode_rerank(combined_query, fused_docs, top_k=k_fin)

    logger.info(
        "Retrieval complete: %d final chunks for topic '%s'",
        len(final_docs), topic,
    )
    return final_docs
