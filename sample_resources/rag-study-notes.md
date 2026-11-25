# RAG study notes (before the P3 demo)

Date: 2026-11-03

Retrieval-augmented generation, my summary:

1. Chunk documents, embed each chunk (we used Infinity as the embedding
   server), store vectors in ChromaDB.
2. At question time: embed the question, nearest-neighbor search, stuff the
   top-k chunks into the prompt.
3. The model answers *from the retrieved context* — cite the chunk, don't
   trust parametric memory.

Gotchas we hit: chunk size too small → answers lose context; similarity
threshold too loose → the bot answers questions it shouldn't. Confidence
gating matters more than I expected.
