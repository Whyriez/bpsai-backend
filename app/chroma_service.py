import chromadb
import logging
from flask import current_app

class ChromaService:
    def __init__(self):
        try:
            host = current_app.config.get('CHROMA_HOST', '127.0.0.1')
            port = current_app.config.get('CHROMA_PORT', 8000)
            
            # Gunakan HttpClient untuk terhubung ke instance ChromaDB yang berjalan
            self.client = chromadb.HttpClient(host=host, port=port)
            
            # Dapatkan atau buat dua collection terpisah
            self.berita_collection = self.client.get_or_create_collection(
                name="berita_bps",
                metadata={"hnsw:space": "cosine"} # Menggunakan cosine distance
            )
            self.chunk_collection = self.client.get_or_create_collection(
                name="document_chunks",
                metadata={"hnsw:space": "cosine"}
            )
            logging.info("ChromaDB service initialized and connected.")
        except Exception as e:
            logging.error(f"Failed to connect to ChromaDB: {e}")
            self.client = None

    def _get_collection(self, entity_type: str):
        """Helper untuk mendapatkan collection yang tepat."""
        if entity_type == 'berita_bps':
            return self.berita_collection
        elif entity_type == 'document_chunk':
            return self.chunk_collection
        raise ValueError("Unknown entity_type for ChromaDB")

    def add_or_update_document(self, entity_type: str, embedding: list, metadata: dict, doc_id: str):
        """Menambahkan atau memperbarui dokumen di collection yang sesuai."""
        if not self.client or embedding is None:
            return
        try:
            collection = self._get_collection(entity_type)
            collection.upsert(
                ids=[doc_id],
                embeddings=[embedding],
                metadatas=[metadata]
            )
        except Exception as e:
            logging.error(f"Error upserting to ChromaDB for doc_id {doc_id}: {e}")

    def delete_document(self, entity_type: str, doc_id: str):
        """Menghapus dokumen dari collection."""
        if not self.client:
            return
        try:
            collection = self._get_collection(entity_type)
            collection.delete(ids=[doc_id])
        except Exception as e:
            logging.error(f"Error deleting from ChromaDB for doc_id {doc_id}: {e}")

    def query(self, entity_type: str, query_embedding: list, n_results: int = 15):
        """Melakukan query ke collection."""
        if not self.client or query_embedding is None:
            return {'ids': [[]], 'distances': [[]]} # Kembalikan struktur data kosong
        try:
            collection = self._get_collection(entity_type)
            results = collection.query(
                query_embeddings=[query_embedding],
                n_results=n_results,
                # Anda bisa menambahkan filter 'where' di sini jika perlu
            )
            return results
        except Exception as e:
            logging.error(f"Error querying ChromaDB: {e}")
            return {'ids': [[]], 'distances': [[]]}