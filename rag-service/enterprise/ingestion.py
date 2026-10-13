"""LangChain documents and splitters; old pending jobs retain their split policy."""
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter, TextSplitter

from .errors import InvalidInput


class LegacyCharacterSplitter(TextSplitter):
    """Reproduce the original fixed slices for already-created v1 jobs."""
    def split_text(self, text):
        step = self._chunk_size - self._chunk_overlap
        return [text[i:i + self._chunk_size] for i in range(0, len(text), step)]


def split_documents(text, settings, *, signature=None, metadata=None):
    if settings.chunk_chars <= 0 or not 0 <= settings.chunk_overlap < settings.chunk_chars:
        raise ValueError("Invalid chunk size or overlap")
    signature = signature or settings.index_signature
    if signature == settings.legacy_index_signature:
        splitter = LegacyCharacterSplitter(chunk_size=settings.chunk_chars, chunk_overlap=settings.chunk_overlap)
    elif signature == settings.index_signature:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.chunk_chars, chunk_overlap=settings.chunk_overlap,
            separators=["\n\n", "\n", "。", "！", "？", "；", " ", ""],
            keep_separator="end", strip_whitespace=False,
        )
    else:
        raise InvalidInput("Index job uses an incompatible signature")
    documents = splitter.split_documents([Document(page_content=text, metadata=metadata or {})])
    if not documents or len(documents) > settings.max_chunks:
        raise InvalidInput("Document exceeds chunk limit")
    for ordinal, document in enumerate(documents):
        document.metadata["chunk_index"] = ordinal
    return documents
