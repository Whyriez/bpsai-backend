import os
import logging
import time
import sys  # Tambahkan sys untuk membaca argumen terminal
from app import create_app, db
from app.models import PdfDocument, DocumentChunk
from app.vector_db import get_collections

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = create_app()


def reindex_all_documents():
    """
    Fungsi ini akan:
    1. Mengambil list semua dokumen PDF yang ada di DB.
    2. Menghapus embedding lama di ChromaDB.
    3. Menghapus chunk lama di Postgres.
    4. Menjalankan ulang process_and_save_pdf.
    """
    with app.app_context():
        # --- PERBAIKAN 1: Pindahkan Import ke SINI ---
        # Import services di dalam app_context agar GeminiService bisa baca Config
        # dan tidak error "Working outside of application context"
        from app.services import process_and_save_pdf

        logger.info("=== MEMULAI PROSES RE-INDEXING DOKUMEN ===")

        # 1. Ambil semua dokumen
        all_docs = PdfDocument.query.all()

        if not all_docs:
            logger.info("Tidak ada dokumen yang perlu diproses.")
            return

        total_docs = len(all_docs)
        logger.info(f"Ditemukan {total_docs} dokumen untuk diproses ulang.")

        # Ambil collection
        try:
            _, doc_collection = get_collections()
            logger.info("Berhasil terhubung ke ChromaDB Collection.")
        except Exception as e:
            logger.error(f"Gagal koneksi ke ChromaDB: {e}")
            return

        for index, doc in enumerate(all_docs, 1):
            file_path = doc.doc_metadata.get('source_path')
            # Fallback path
            if not file_path:
                file_path = os.path.join(app.config.get('UPLOAD_FOLDER', 'uploads'), doc.filename)

            original_filename = doc.filename
            doc_id = doc.id

            logger.info(f"[{index}/{total_docs}] Memproses: {original_filename} (ID: {doc_id})")

            # Cek fisik file
            if not os.path.exists(file_path):
                logger.error(f"   [SKIP] File fisik tidak ditemukan di: {file_path}")
                continue

            try:
                # 2. HAPUS DATA LAMA
                logger.info("   -> Menghapus vector lama di ChromaDB...")
                doc_collection.delete(where={"document_id": str(doc_id)})

                logger.info("   -> Menghapus chunk lama di Database SQL...")
                num_deleted = DocumentChunk.query.filter_by(document_id=doc_id).delete()

                db.session.delete(doc)
                db.session.commit()

                logger.info(f"   -> Terhapus {num_deleted} chunks lama.")

                # 3. PROSES ULANG
                logger.info("   -> Menjalankan ulang pemrosesan PDF (Sliding Window)...")
                time.sleep(0.5)

                result = process_and_save_pdf(file_path)

                if result.get("status") == "success":
                    logger.info(
                        f"   [SUKSES] Dokumen berhasil di-reindex. Total chunk baru: {result.get('pages_chunked', '?')}")
                else:
                    logger.warning(f"   [WARNING] Status: {result.get('status')} - {result.get('reason')}")

            except Exception as e:
                db.session.rollback()
                logger.error(f"   [ERROR] Gagal memproses {original_filename}: {str(e)}")

        logger.info("=== RE-INDEXING SELESAI ===")


if __name__ == "__main__":
    # --- PERBAIKAN 2: Tambahkan Support Flag '--yes' ---
    # Cek apakah user menjalankan dengan flag bypass: python reindex_documents.py --yes
    auto_confirm = len(sys.argv) > 1 and sys.argv[1] in ['--yes', '-y']

    if auto_confirm:
        print("Mode Auto-Confirm aktif. Memulai re-indexing...")
        confirm = 'y'
    else:
        print("\n!!! PERHATIAN !!!")
        print("Script ini akan MENGHAPUS embedding lama dan membuat ulang (Re-Index).")
        print("Pastikan file 'app/vector_db.py' sudah benar config Local/Prod-nya.")
        print("Pastikan file PDF asli masih ada di folder uploads.")

        try:
            confirm = input("\nKetik 'y' untuk lanjut: ")
        except EOFError:
            # Handle error jika dijalankan di server non-interaktif
            print("\n[ERROR] Tidak dapat membaca input keyboard.")
            print("Gunakan perintah berikut untuk menjalankan di server:")
            print("   venv/bin/python reindex_documents.py --yes")
            confirm = 'n'

    if confirm.lower() == 'y':
        reindex_all_documents()
    else:
        print("Dibatalkan.")