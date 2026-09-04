"""Document ingestion: upload, extract text, chunk, embed, store."""

import asyncio
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

import tiktoken
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from turncall.adapters.storage.base import ObjectStorageAdapter
from turncall.storage.models import DocumentRow
from turncall.storage.repositories import knowledge_repo


@dataclass(frozen=True)
class IngestionResult:
    """Result of document ingestion."""

    document_id: UUID
    char_count: int
    chunk_count: int
    status: str
    error: str | None = None


# --- Text Extraction ---


def extract_text_from_bytes(data: bytes, content_type: str, filename: str) -> str:
    """Extract plain text from uploaded file bytes."""
    if content_type in ("text/plain", "text/markdown", "text/csv"):
        return data.decode("utf-8", errors="replace")

    if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
        return _extract_pdf(data)

    if content_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ) or filename.lower().endswith((".docx", ".doc")):
        return _extract_docx(data)

    if content_type in (
        "application/json",
        "text/yaml",
        "text/xml",
        "text/tab-separated-values",
    ):
        return data.decode("utf-8", errors="replace")

    # Fallback: try as text
    return data.decode("utf-8", errors="replace")


def _extract_pdf(data: bytes) -> str:
    """Extract text from PDF bytes using pypdf."""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return _clean_pdf_text("\n\n".join(pages))


# Link decorations like "(https://…)" and session-id URLs — print-to-PDF chrome.
_PAREN_URL_RE = re.compile(r"\(\s*https?://[^)]*\)")
_LONG_URL_RE = re.compile(r"https?://\S{50,}")


def _clean_pdf_text(text: str) -> str:
    """Strip print-to-PDF chrome so chunks embed on content, not navigation.

    Web pages saved as PDF drown the real content in parenthesized link
    decorations, session-id URLs, and a header/footer repeated on every page —
    retrieval then surfaces chunks of pure noise. Remove the noise classes;
    short inline URLs (which can be real content) are kept.
    """
    text = _PAREN_URL_RE.sub(" ", text)
    text = _LONG_URL_RE.sub(" ", text)

    # Drop lines repeated on 3+ pages (headers/footers), ignoring page numbers.
    lines = text.splitlines()
    normalized = [re.sub(r"\s*\d+/\d+\s*$", "", line).strip() for line in lines]
    counts = Counter(n for n in normalized if len(n) > 10)
    cleaned = [
        line
        for line, norm in zip(lines, normalized, strict=True)
        if not (len(norm) > 10 and counts[norm] >= 3)
    ]
    return re.sub(r"[ \t]{2,}", " ", "\n".join(cleaned))


def _extract_docx(data: bytes) -> str:
    """Extract text from DOCX bytes."""
    import io
    import zipfile

    from defusedxml.ElementTree import parse as safe_parse

    text_parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        if "word/document.xml" not in zf.namelist():
            return ""
        with zf.open("word/document.xml") as doc:
            tree = safe_parse(doc)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            for paragraph in tree.iter(f"{{{ns['w']}}}p"):
                runs = paragraph.findall(f".//{{{ns['w']}}}t")
                line = "".join(r.text or "" for r in runs)
                if line.strip():
                    text_parts.append(line)
    return "\n".join(text_parts)


# --- Chunking ---


@lru_cache(maxsize=1)
def _tiktoken_encoding():  # type: ignore[no-untyped-def]
    """Cached cl100k_base encoding — get_encoding reloads the BPE ranks each
    call, and ingestion tokenizes every chunk (chunk_text + count_tokens)."""
    return tiktoken.get_encoding("cl100k_base")


def chunk_text(
    text: str,
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> list[str]:
    """Split text into token-based chunks with overlap.

    Uses recursive splitting: paragraphs → sentences → words.
    """
    if not text.strip():
        return []

    enc = _tiktoken_encoding()

    # Split into paragraphs first
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks: list[str] = []
    current_tokens: list[int] = []

    for paragraph in paragraphs:
        paragraph_tokens = enc.encode(paragraph)

        # If single paragraph exceeds chunk_size, split further
        if len(paragraph_tokens) > chunk_size:
            sentences = _split_sentences(paragraph)
            for sentence in sentences:
                sentence_tokens = enc.encode(sentence)
                if len(current_tokens) + len(sentence_tokens) > chunk_size:
                    if current_tokens:
                        chunks.append(enc.decode(current_tokens))
                        # Keep overlap from end
                        current_tokens = (
                            current_tokens[-chunk_overlap:] if chunk_overlap > 0 else []
                        )
                    # If single sentence exceeds chunk_size, force-split by tokens
                    if len(sentence_tokens) > chunk_size:
                        for i in range(
                            0, len(sentence_tokens), chunk_size - chunk_overlap
                        ):
                            token_slice = sentence_tokens[i : i + chunk_size]
                            chunks.append(enc.decode(token_slice))
                        current_tokens = []
                    else:
                        current_tokens.extend(sentence_tokens)
                else:
                    current_tokens.extend(sentence_tokens)
        elif len(current_tokens) + len(paragraph_tokens) > chunk_size:
            if current_tokens:
                chunks.append(enc.decode(current_tokens))
                current_tokens = (
                    current_tokens[-chunk_overlap:] if chunk_overlap > 0 else []
                )
            current_tokens.extend(paragraph_tokens)
        else:
            current_tokens.extend(paragraph_tokens)

    # Flush remainder
    if current_tokens:
        chunks.append(enc.decode(current_tokens))

    return chunks


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences on '. ', '! ', '? '."""
    import re

    parts = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in parts if s.strip()]


def count_tokens(text: str) -> int:
    """Count tokens using cl100k_base encoding."""
    return len(_tiktoken_encoding().encode(text))


MAX_EXTRACTED_CHARS = 1_000_000  # 1M characters

SUPPORTED_EMBEDDING_MODELS = frozenset(
    {
        "text-embedding-3-small",
        "text-embedding-3-large",
        "text-embedding-ada-002",
    }
)


# --- Embedding ---


# Enrichment guards: prompt context is capped, and very large documents only
# enrich the first N chunks (the rest keep the filename prefix).
_ENRICH_DOC_CHARS = 24_000
_ENRICH_MAX_CHUNKS = 60
_ENRICH_MODEL = "gpt-4o-mini"

_ENRICH_PROMPT = (
    "Here is a document named {filename!r}:\n<document>\n{doc}\n</document>\n\n"
    "Here is one chunk of it:\n<chunk>\n{chunk}\n</chunk>\n\n"
    "Write 1-2 short sentences situating this chunk within the document "
    "(what document it is, what this part covers, key entities like names, "
    "dates, or identifiers). Answer with the sentences only."
)


async def enrich_chunks(
    chunks: list[str],
    *,
    full_text: str,
    filename: str,
    api_key: str = "",
) -> list[str]:
    """Prepend an LLM-written situating context to each chunk (ADR-0012).

    Returns chunks as "[<filename> — <context>]\\n<chunk>". Every failure path
    degrades to "[<filename>]\\n<chunk>" — enrichment never blocks ingestion.
    """
    import asyncio

    fallback = [f"[{filename}]\n{c}" for c in chunks]
    if not api_key:
        return fallback

    client = _openai_client(api_key)
    doc = full_text[:_ENRICH_DOC_CHARS]
    sem = asyncio.Semaphore(5)

    async def one(chunk: str) -> str | None:
        try:
            async with sem:
                resp = await client.chat.completions.create(
                    model=_ENRICH_MODEL,
                    max_tokens=120,
                    messages=[
                        {
                            "role": "user",
                            "content": _ENRICH_PROMPT.format(
                                filename=filename, doc=doc, chunk=chunk
                            ),
                        }
                    ],
                )
            return (resp.choices[0].message.content or "").strip() or None
        except Exception:
            # Per-chunk best-effort: fall back to the filename-only prefix, but
            # log so a systematic failure (bad key, quota) is visible, not silent.
            logger.warning("chunk_enrichment_chunk_failed — using filename prefix")
            return None

    to_enrich = chunks[:_ENRICH_MAX_CHUNKS]
    try:
        contexts = await asyncio.gather(*(one(c) for c in to_enrich))
    except Exception:
        logger.exception("chunk_enrichment_failed — using filename-only prefixes")
        return fallback

    enriched = [
        f"[{filename} — {ctx}]\n{chunk}" if ctx else f"[{filename}]\n{chunk}"
        for chunk, ctx in zip(to_enrich, contexts, strict=True)
    ]
    skipped = len(chunks) - len(to_enrich)
    if skipped > 0:
        logger.info("enrichment capped: {n} chunks kept filename-only prefix", n=skipped)
    return enriched + fallback[len(to_enrich) :]


# OpenAI's embedding endpoint caps input arrays (~2048 items / ~300k tokens);
# batch so a large document can't hard-fail the whole ingest with an opaque 400.
_EMBED_BATCH = 256


@lru_cache(maxsize=8)
def _openai_client(api_key: str):  # type: ignore[no-untyped-def]
    """One AsyncOpenAI per key for the process — building it per call pays a
    cold connection pool every embedding/enrichment request."""
    import openai

    return openai.AsyncOpenAI(api_key=api_key)


async def generate_embeddings(
    texts: list[str],
    *,
    model: str = "text-embedding-3-small",
    api_key: str = "",
) -> list[list[float]]:
    """Generate embeddings via OpenAI API, batched."""
    if model not in SUPPORTED_EMBEDDING_MODELS:
        logger.warning(
            "Unknown embedding model {m}, falling back to text-embedding-3-small",
            m=model,
        )
        model = "text-embedding-3-small"

    client = _openai_client(api_key)
    out: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH):
        batch = texts[start : start + _EMBED_BATCH]
        response = await client.embeddings.create(input=batch, model=model)
        out.extend(item.embedding for item in response.data)
    return out


# --- Orchestration ---


async def create_pending_document(
    session: AsyncSession,
    storage: ObjectStorageAdapter,
    *,
    knowledge_base_id: UUID,
    filename: str,
    content_type: str,
    data: bytes,
) -> DocumentRow:
    """Store the file and create the document row (status 'processing'). Fast —
    the heavy extract/chunk/embed runs separately (process_pending_document), so
    the upload endpoint returns 202 immediately and the client polls."""
    import os

    safe_filename = os.path.basename(filename)
    if not safe_filename or safe_filename in (".", ".."):
        safe_filename = "untitled"
    storage_key = f"kb/{knowledge_base_id}/docs/{safe_filename}"
    await storage.upload(storage_key, data, content_type=content_type)

    doc_row = await knowledge_repo.create_document(
        session,
        knowledge_base_id=knowledge_base_id,
        filename=filename,
        content_type=content_type,
        storage_key=storage_key,
    )
    await session.flush()
    return doc_row


async def process_pending_document(
    session: AsyncSession,
    document_id: UUID,
    *,
    knowledge_base_id: UUID,
    filename: str,
    content_type: str,
    data: bytes,
    openai_api_key: str = "",
    embedding_model: str = "text-embedding-3-small",
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> IngestionResult:
    """The heavy half: extract -> chunk -> enrich -> embed -> store, updating the
    document row's status to ready/failed. Never raises — a failure is recorded
    on the row and returned as a failed IngestionResult."""
    try:
        # 3. Extract text
        raw_text = extract_text_from_bytes(data, content_type, filename)
        if not raw_text.strip():
            await knowledge_repo.update_document_status(
                session,
                document_id,
                status="failed",
                error_message="No text could be extracted from the file",
            )
            return IngestionResult(
                document_id=document_id,
                char_count=0,
                chunk_count=0,
                status="failed",
                error="No text could be extracted from the file",
            )

        # Guard: cap extracted text to prevent unbounded memory usage
        if len(raw_text) > MAX_EXTRACTED_CHARS:
            await knowledge_repo.update_document_status(
                session,
                document_id,
                status="failed",
                error_message=(
                    f"Extracted text too large: {len(raw_text)} chars "
                    f"(max {MAX_EXTRACTED_CHARS})"
                ),
            )
            return IngestionResult(
                document_id=document_id,
                char_count=len(raw_text),
                chunk_count=0,
                status="failed",
                error=f"Extracted text too large: {len(raw_text)} chars",
            )

        # 4. Chunk
        chunks = chunk_text(
            raw_text, chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )
        if not chunks:
            await knowledge_repo.update_document_status(
                session,
                document_id,
                status="failed",
                error_message="Text was empty after chunking",
            )
            return IngestionResult(
                document_id=document_id,
                char_count=len(raw_text),
                chunk_count=0,
                status="failed",
                error="Text was empty after chunking",
            )

        # 5. Contextual enrichment (ADR-0012): situate each chunk in its document
        #    so embeddings + full-text search on it carry document identity.
        #    Best-effort — a failed LLM call leaves the raw chunk, never blocks.
        chunks = await enrich_chunks(
            chunks, full_text=raw_text, filename=filename, api_key=openai_api_key
        )

        # 6. Generate embeddings
        embeddings = await generate_embeddings(
            chunks,
            model=embedding_model,
            api_key=openai_api_key,
        )

        # 6. Store chunks + embeddings in one insert (savepoint prevents
        #    concurrent reads from seeing chunks without embeddings)
        async with session.begin_nested():
            chunk_dicts = [
                {
                    "document_id": document_id,
                    "knowledge_base_id": knowledge_base_id,
                    "chunk_index": i,
                    "content": chunk_text_content,
                    "token_count": count_tokens(chunk_text_content),
                    "embedding": embeddings[i],
                }
                for i, chunk_text_content in enumerate(chunks)
            ]
            await knowledge_repo.create_chunks_batch(session, chunk_dicts)

        # 7. Update document status
        await knowledge_repo.update_document_status(
            session,
            document_id,
            status="ready",
            char_count=len(raw_text),
            chunk_count=len(chunks),
            raw_text=raw_text,
        )

        logger.info(
            "Document ingested: {filename} → {chunks} chunks",
            filename=filename,
            chunks=len(chunks),
        )

        return IngestionResult(
            document_id=document_id,
            char_count=len(raw_text),
            chunk_count=len(chunks),
            status="ready",
        )

    except Exception as exc:
        logger.error(
            "Ingestion failed for {filename}: {exc}", filename=filename, exc=exc
        )
        await knowledge_repo.update_document_status(
            session,
            document_id,
            status="failed",
            error_message=str(exc)[:2000],
        )
        return IngestionResult(
            document_id=document_id,
            char_count=0,
            chunk_count=0,
            status="failed",
            error=str(exc)[:2000],
        )


async def ingest_document(
    session: AsyncSession,
    storage: ObjectStorageAdapter,
    *,
    knowledge_base_id: UUID,
    filename: str,
    content_type: str,
    data: bytes,
    openai_api_key: str = "",
    embedding_model: str = "text-embedding-3-small",
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> IngestionResult:
    """Full pipeline in one call (create row + process). Kept for the sync path
    and tests; the upload endpoint uses create_pending + background processing."""
    doc_row = await create_pending_document(
        session,
        storage,
        knowledge_base_id=knowledge_base_id,
        filename=filename,
        content_type=content_type,
        data=data,
    )
    return await process_pending_document(
        session,
        doc_row.id,
        knowledge_base_id=knowledge_base_id,
        filename=filename,
        content_type=content_type,
        data=data,
        openai_api_key=openai_api_key,
        embedding_model=embedding_model,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


_INGEST_TASKS: set[asyncio.Task] = set()


async def _process_document_background(
    session_factory: async_sessionmaker[AsyncSession],
    document_id: UUID,
    *,
    knowledge_base_id: UUID,
    filename: str,
    content_type: str,
    data: bytes,
    openai_api_key: str,
    embedding_model: str,
    chunk_size: int,
    chunk_overlap: int,
) -> None:
    """Run ingestion on its own session after the upload response returned.
    process_pending_document never raises, so the commit persists ready/failed."""
    async with session_factory() as session:
        await process_pending_document(
            session,
            document_id,
            knowledge_base_id=knowledge_base_id,
            filename=filename,
            content_type=content_type,
            data=data,
            openai_api_key=openai_api_key,
            embedding_model=embedding_model,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        await session.commit()


def ingest_in_background(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    document_id: UUID,
    knowledge_base_id: UUID,
    filename: str,
    content_type: str,
    data: bytes,
    openai_api_key: str,
    embedding_model: str,
    chunk_size: int,
    chunk_overlap: int,
) -> None:
    """Fire-and-forget the ingestion; hold a strong ref so it isn't GC'd."""
    task = asyncio.create_task(
        _process_document_background(
            session_factory,
            document_id,
            knowledge_base_id=knowledge_base_id,
            filename=filename,
            content_type=content_type,
            data=data,
            openai_api_key=openai_api_key,
            embedding_model=embedding_model,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    )
    _INGEST_TASKS.add(task)
    task.add_done_callback(_INGEST_TASKS.discard)
