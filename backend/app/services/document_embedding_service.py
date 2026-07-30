from functools import lru_cache

from fastembed import TextEmbedding

from ..core.config import (
    DOCUMENT_EMBEDDING_CACHE_DIRECTORY,
    DOCUMENT_EMBEDDING_DIMENSIONS,
    DOCUMENT_EMBEDDING_MODEL,
    DOCUMENT_EMBEDDING_THREADS,
)


@lru_cache(maxsize=1)
def get_document_embedding_model() -> TextEmbedding:
    return TextEmbedding(
        model_name=DOCUMENT_EMBEDDING_MODEL,
        cache_dir=DOCUMENT_EMBEDDING_CACHE_DIRECTORY,
        threads=DOCUMENT_EMBEDDING_THREADS,
    )


def create_document_embeddings(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []

    embedding_model = get_document_embedding_model()
    raw_embeddings = embedding_model.embed(texts)
    embeddings: list[list[float]] = []

    for raw_embedding in raw_embeddings:
        embedding = raw_embedding.tolist()

        if len(embedding) != DOCUMENT_EMBEDDING_DIMENSIONS:
            raise RuntimeError(
                f"Document embedding dimension must be {DOCUMENT_EMBEDDING_DIMENSIONS}"
            )

        embeddings.append(embedding)

    return embeddings


def create_document_query_embedding(question: str) -> list[float]:
    query_text = "Represent this sentence for searching relevant passages: " + question
    embeddings = create_document_embeddings([query_text])
    return embeddings[0]
