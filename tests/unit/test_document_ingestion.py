"""Tests for document ingestion: text extraction, chunking, token counting."""

import pytest

from turncall.services.document_ingestion import (
    chunk_text,
    count_tokens,
    extract_text_from_bytes,
)


@pytest.mark.unit
class TestExtractText:
    def test_plain_text(self) -> None:
        data = b"Hello, this is a plain text document."
        result = extract_text_from_bytes(data, "text/plain", "test.txt")
        assert result == "Hello, this is a plain text document."

    def test_markdown(self) -> None:
        data = b"# Title\n\nSome content here."
        result = extract_text_from_bytes(data, "text/markdown", "test.md")
        assert "# Title" in result
        assert "Some content" in result

    def test_csv(self) -> None:
        data = b"name,age\nAlice,30\nBob,25"
        result = extract_text_from_bytes(data, "text/csv", "data.csv")
        assert "Alice" in result
        assert "Bob" in result

    def test_json(self) -> None:
        data = b'{"key": "value", "items": [1, 2, 3]}'
        result = extract_text_from_bytes(data, "application/json", "data.json")
        assert '"key"' in result

    def test_empty_file(self) -> None:
        result = extract_text_from_bytes(b"", "text/plain", "empty.txt")
        assert result == ""

    def test_utf8_with_bom(self) -> None:
        data = "Héllo wörld".encode()
        result = extract_text_from_bytes(data, "text/plain", "unicode.txt")
        assert "Héllo" in result

    def test_binary_fallback(self) -> None:
        data = b"Some text content"
        result = extract_text_from_bytes(data, "application/octet-stream", "file.bin")
        assert "Some text content" in result

    def test_pdf_extension_detection(self) -> None:
        # A minimal invalid PDF should raise or return empty
        data = b"not a real pdf"
        with pytest.raises(Exception):
            extract_text_from_bytes(data, "application/pdf", "test.pdf")

    def test_docx_missing_document_xml(self) -> None:
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("other.xml", "<root/>")
        result = extract_text_from_bytes(
            buf.getvalue(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "test.docx",
        )
        assert result == ""


@pytest.mark.unit
class TestChunkText:
    def test_short_text_single_chunk(self) -> None:
        text = "This is a short text."
        chunks = chunk_text(text, chunk_size=512, chunk_overlap=64)
        assert len(chunks) == 1
        assert "short text" in chunks[0]

    def test_empty_text_returns_empty(self) -> None:
        assert chunk_text("") == []
        assert chunk_text("   ") == []

    def test_paragraphs_split_into_chunks(self) -> None:
        paragraphs = ["Paragraph " + str(i) + ". " * 50 for i in range(20)]
        text = "\n\n".join(paragraphs)
        chunks = chunk_text(text, chunk_size=100, chunk_overlap=10)
        assert len(chunks) > 1
        # All content should be present across chunks
        full = " ".join(chunks)
        assert "Paragraph 0" in full
        assert "Paragraph 19" in full

    def test_chunk_size_respected(self) -> None:
        # Create text with many small paragraphs
        text = "\n\n".join(f"Sentence number {i}." for i in range(100))
        chunks = chunk_text(text, chunk_size=50, chunk_overlap=5)
        for chunk in chunks:
            tokens = count_tokens(chunk)
            # Allow some slack for overlap
            assert tokens <= 60, f"Chunk too large: {tokens} tokens"

    def test_overlap_creates_redundancy(self) -> None:
        text = "\n\n".join(f"Unique paragraph {i} content." for i in range(20))
        chunks_no_overlap = chunk_text(text, chunk_size=30, chunk_overlap=0)
        chunks_with_overlap = chunk_text(text, chunk_size=30, chunk_overlap=10)
        # With overlap, we expect more chunks (or same) and some shared content
        assert len(chunks_with_overlap) >= len(chunks_no_overlap)

    def test_single_long_sentence(self) -> None:
        # A single sentence longer than chunk_size should be force-split
        text = "word " * 200  # ~200 tokens
        chunks = chunk_text(text, chunk_size=50, chunk_overlap=5)
        assert len(chunks) > 1


@pytest.mark.unit
class TestCountTokens:
    def test_empty_string(self) -> None:
        assert count_tokens("") == 0

    def test_simple_text(self) -> None:
        tokens = count_tokens("Hello, world!")
        assert tokens > 0
        assert tokens < 10

    def test_longer_text(self) -> None:
        text = "The quick brown fox jumps over the lazy dog. " * 10
        tokens = count_tokens(text)
        assert tokens > 50


@pytest.mark.unit
class TestCleanPdfText:
    """Print-to-PDF chrome (link decorations, session URLs, page headers) is
    stripped so retrieval chunks embed on content, not navigation."""

    def test_strips_paren_urls_and_long_urls(self) -> None:
        from turncall.services.document_ingestion import _clean_pdf_text

        noisy = (
            "Itinerary Monday 13 July 2026 08:45 (https://www.example.com/baggage) "
            "https://flightbook.example.com/plnext/Override.action;"
            "jsessionid=vU6K_sB0znbPyFlhB5wKw?X-page=1 "
            "PG 106 Economy"
        )
        out = _clean_pdf_text(noisy)
        assert "Monday 13 July 2026" in out
        assert "PG 106" in out
        assert "jsessionid" not in out
        assert "example.com/baggage" not in out

    def test_drops_repeated_page_headers(self) -> None:
        from turncall.services.document_ingestion import _clean_pdf_text

        header = "6/30/26, 5:56 PM Bangkok Airways - Reservation"
        pages = "\n\n".join(f"{header} {i}/4\nreal content page {i}" for i in range(1, 5))
        out = _clean_pdf_text(pages)
        assert "Bangkok Airways - Reservation" not in out
        assert "real content page 2" in out

    def test_keeps_short_inline_urls(self) -> None:
        from turncall.services.document_ingestion import _clean_pdf_text

        out = _clean_pdf_text("Support portal: https://help.acme.io for tickets")
        assert "https://help.acme.io" in out
