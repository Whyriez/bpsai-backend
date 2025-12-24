import logging
import time
from app import create_app, db
from app.models import BeritaBps
from app.vector_db import sync_berita_to_chroma, get_collections

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = create_app()


def reindex_all_berita():
    """
    Memaksa update semua data BeritaBps ke ChromaDB agar metadata 'year' masuk.
    """
    with app.app_context():
        logger.info("=== MEMULAI RE-SYNC BERITA BPS ===")

        # 1. Pastikan koneksi DB siap
        try:
            get_collections()
        except Exception as e:
            logger.error(f"Gagal konek ChromaDB: {e}")
            return

        # 2. Ambil semua berita
        all_news = BeritaBps.query.all()
        total = len(all_news)
        logger.info(f"Ditemukan {total} berita untuk disinkronisasi ulang.")

        for i, news in enumerate(all_news, 1):
            try:
                # Panggil fungsi sync yang sudah kita update tadi
                # Fungsi ini akan menimpa data lama dengan data baru yang ada 'year'-nya
                sync_berita_to_chroma(None, None, news)

                if i % 10 == 0:
                    logger.info(f"Progress: {i}/{total} berita diproses...")

            except Exception as e:
                logger.error(f"Gagal sync berita ID {news.id}: {e}")

        logger.info("=== RE-SYNC BERITA SELESAI ===")


if __name__ == "__main__":
    reindex_all_berita()