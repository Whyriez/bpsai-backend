# app/vector_db.py
import chromadb
import os

from sqlalchemy import event
from .models import BeritaBps, DocumentChunk
import logging
import numpy as np

# Path untuk menyimpan data ChromaDB
CHROMA_DATA_PATH = "chroma_data/"

# Inisialisasi client. Ini akan membuat folder jika belum ada.
client = chromadb.PersistentClient(path=CHROMA_DATA_PATH)

# Anda bisa membuat collection di sini atau secara dinamis
# Collection mirip seperti tabel di SQL
berita_collection = client.get_or_create_collection(name="berita_bps")
document_collection = client.get_or_create_collection(name="document_chunks")

def get_collections():
    return berita_collection, document_collection


# LISTENER FROM POSTGRES TO CHROMADB
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
        berita_collection, _ = get_collections()

        embedding_list = target.embedding.tolist() if isinstance(target.embedding, np.ndarray) else target.embedding

        berita_collection.upsert(
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
        _, document_collection = get_collections()

        embedding_list = target.embedding.tolist() if isinstance(target.embedding, np.ndarray) else target.embedding

        document_collection.upsert(
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
        berita_collection, _ = get_collections()
        berita_collection.delete(ids=[str(target.id)])
        logging.info(f"Berhasil delete BeritaBps ID {target.id} dari ChromaDB.")
    except Exception as e:
        logging.error(f"Gagal delete BeritaBps ID {target.id} dari ChromaDB: {e}")

def delete_chunk_from_chroma(mapper, connection, target):
    """ Dijalankan setelah data DocumentChunk dihapus dari PostgreSQL. """
    logging.info(f"Listener 'delete_chunk_from_chroma' terpicu untuk ID: {target.id}")
    try:
        _, document_collection = get_collections()
        document_collection.delete(ids=[str(target.id)])
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
    
    print("Database listeners untuk sinkronisasi ChromaDB berhasil didaftarkan.")