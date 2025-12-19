# app/vector_db.py
import chromadb
from chromadb.config import Settings
import os
from sqlalchemy import event
from .models import BeritaBps, DocumentChunk
import logging
import numpy as np

# ==========================================
# UBAH DARI PersistentClient KE HttpClient
# ==========================================

IS_PRODUCTION = os.getenv('ENVIRONMENT') == 'production'

if IS_PRODUCTION:
    # Production: Gunakan HttpClient
    CHROMA_HOST = os.getenv('CHROMA_HOST', 'localhost')
    CHROMA_PORT = int(os.getenv('CHROMA_PORT', 8000))
    
    client = chromadb.HttpClient(
        host=CHROMA_HOST,
        port=CHROMA_PORT,
        settings=Settings(anonymized_telemetry=False)
    )
    print(f"🌐 ChromaDB: Connected to service at {CHROMA_HOST}:{CHROMA_PORT}")
else:
    # Local Development: Gunakan PersistentClient
    client = chromadb.PersistentClient(path="chroma_data/")
    print("💻 ChromaDB: Running in embedded mode (local)")

# Buat atau ambil collections
berita_collection = None
document_collection = None

def get_collections():
    """
    Lazy initialization untuk collections.
    Ini mencegah error jika ChromaDB belum ready saat import.
    """
    global berita_collection, document_collection
    
    if berita_collection is None or document_collection is None:
        try:
            berita_collection = client.get_or_create_collection(
                name="berita_bps",
                metadata={"hnsw:space": "cosine"}
            )
            document_collection = client.get_or_create_collection(
                name="document_chunks",
                metadata={"hnsw:space": "cosine"}
            )
            logging.info("ChromaDB collections initialized successfully")
        except Exception as e:
            logging.error(f"Failed to initialize ChromaDB collections: {e}")
            raise
    
    return berita_collection, document_collection


# ==========================================
# LISTENER FROM POSTGRES TO CHROMADB
# ==========================================

def sync_berita_to_chroma(mapper, connection, target):
    """
    Fungsi ini akan dijalankan setelah insert atau update pada BeritaBps.
    'target' adalah instance dari BeritaBps yang baru saja disimpan.
    """
    logging.info(f"Listener 'sync_berita_to_chroma' terpicu untuk ID: {target.id}")
    if target.embedding is None:
        logging.warning(f"Embedding untuk BeritaBps ID {target.id} kosong, skip sinkronisasi ke Chroma.")
        return

    try:
        berita_col, _ = get_collections()

        embedding_list = target.embedding.tolist() if isinstance(target.embedding, np.ndarray) else target.embedding

        berita_col.upsert(
            ids=[str(target.id)],
            embeddings=[embedding_list],
            metadatas=[
                {"judul": target.judul_berita, "tanggal_rilis": str(target.tanggal_rilis)}
            ]
        )
        logging.info(f"Berhasil upsert BeritaBps ID {target.id} ke ChromaDB.")
    except Exception as e:
        logging.error(f"Gagal sinkronisasi BeritaBps ID {target.id} ke ChromaDB: {e}")

def sync_chunk_to_chroma(mapper, connection, target):
    """
    Fungsi ini akan dijalankan setelah insert atau update pada DocumentChunk.
    'target' adalah instance dari DocumentChunk yang baru saja disimpan.
    """
    logging.info(f"Listener 'sync_chunk_to_chroma' terpicu untuk ID: {target.id}")
    if target.embedding is None:
        logging.warning(f"Embedding untuk DocumentChunk ID {target.id} kosong, skip sinkronisasi ke Chroma.")
        return

    try:
        _, document_col = get_collections()

        embedding_list = target.embedding.tolist() if isinstance(target.embedding, np.ndarray) else target.embedding

        document_col.upsert(
            ids=[str(target.id)],
            embeddings=[embedding_list],
            metadatas=[
                {"document_id": str(target.document_id), "page_number": target.page_number}
            ]
        )
        logging.info(f"Berhasil upsert DocumentChunk ID {target.id} ke ChromaDB.")
    except Exception as e:
        logging.error(f"Gagal sinkronisasi DocumentChunk ID {target.id} ke ChromaDB: {e}")

def delete_berita_from_chroma(mapper, connection, target):
    """ Dijalankan setelah data BeritaBps dihapus dari PostgreSQL. """
    logging.info(f"Listener 'delete_berita_from_chroma' terpicu untuk ID: {target.id}")
    try:
        berita_col, _ = get_collections()
        berita_col.delete(ids=[str(target.id)])
        logging.info(f"Berhasil delete BeritaBps ID {target.id} dari ChromaDB.")
    except Exception as e:
        logging.error(f"Gagal delete BeritaBps ID {target.id} dari ChromaDB: {e}")

def delete_chunk_from_chroma(mapper, connection, target):
    """ Dijalankan setelah data DocumentChunk dihapus dari PostgreSQL. """
    logging.info(f"Listener 'delete_chunk_from_chroma' terpicu untuk ID: {target.id}")
    try:
        _, document_col = get_collections()
        document_col.delete(ids=[str(target.id)])
        logging.info(f"Berhasil delete DocumentChunk ID {target.id} dari ChromaDB.")
    except Exception as e:
        logging.error(f"Gagal delete DocumentChunk ID {target.id} dari ChromaDB: {e}")


def register_db_listeners():
    """ Mendaftarkan semua listener ke model yang relevan. """
    # Listener untuk BeritaBps
    event.listen(BeritaBps, 'after_insert', sync_berita_to_chroma)
    event.listen(BeritaBps, 'after_update', sync_berita_to_chroma)
    event.listen(BeritaBps, 'after_delete', delete_berita_from_chroma)
    
    # Listener untuk DocumentChunk
    event.listen(DocumentChunk, 'after_insert', sync_chunk_to_chroma)
    event.listen(DocumentChunk, 'after_update', sync_chunk_to_chroma)
    event.listen(DocumentChunk, 'after_delete', delete_chunk_from_chroma)
    
    logging.info("Database listeners untuk sinkronisasi ChromaDB berhasil didaftarkan.")