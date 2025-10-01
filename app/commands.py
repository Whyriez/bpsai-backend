import click
import time
from flask.cli import with_appcontext
from .models import db, BeritaBps, User, DocumentChunk
from werkzeug.security import generate_password_hash
from .services import EmbeddingService
from .vector_db import get_collections

# @click.group()
# def cli():
#     """Command line interface for BPS App."""
#     pass

def register_commands(app):
    """Fungsi untuk mendaftarkan custom CLI commands ke aplikasi Flask."""

    @app.cli.command("embeddings:generate")
    @click.option('--chunk-size', default=50, help='Number of items to process in one chunk.')
    def generate_embeddings(chunk_size):
        """Generate and store embeddings for existing news."""
        click.echo('Starting to generate embeddings for Berita BPS...')

        embedding_service = EmbeddingService()
        
        # Query di dalam konteks aplikasi
        with app.app_context():
            items_to_process = BeritaBps.query.filter(BeritaBps.embedding == None).yield_per(chunk_size)

            count = 0
            for item in items_to_process:
                tags_string = ', '.join(item.tags) if isinstance(item.tags, list) else ''
                text_to_embed = f"Judul: {item.judul_berita}\nRingkasan: {item.ringkasan}\nTags: {tags_string}"

                embedding = embedding_service.generate(text_to_embed)

                if embedding:
                    item.embedding = embedding
                    db.session.add(item)
                    click.echo(f"Generated embedding for Berita ID: {item.id}")
                    count += 1
                else:
                    click.echo(f"Failed to generate embedding for Berita ID: {item.id}", err=True)

                # Commit per chunk untuk efisiensi
                if count % chunk_size == 0:
                    db.session.commit()
                    click.echo(f"--- Committed chunk of {chunk_size} items ---")
                
                time.sleep(1)

            db.session.commit()
            click.echo(f'Embedding generation complete. Processed {count} items.')

    @app.cli.command("db:seed")
    def db_seed():
        """Seeds the database with initial data (e.g., an admin user)."""
        click.echo("Seeding database...")

        with app.app_context():
            # Cek apakah admin sudah ada
            if User.query.filter_by(username='admin').first() is None:
                # Buat user admin baru
                admin_user = User(
                    username='admin',
                    email='admin@bps.go.id',
                    role='admin'
                )
                admin_user.set_password('admin123') # Ganti dengan password yang aman
                
                db.session.add(admin_user)
                click.echo("Admin user created.")
            else:
                click.echo("Admin user already exists.")

            db.session.commit()
            click.echo("Database seeded!")

    @app.cli.command("sync-vectordb")
    @with_appcontext
    def sync_vector_db():
        """
        Membaca data & embedding dari PostgreSQL dan menyimpannya ke ChromaDB.
        Jalankan perintah ini HANYA SEKALI setelah setup ChromaDB.
        """
        print("Memulai sinkronisasi dari PostgreSQL ke ChromaDB...")
        berita_collection, document_collection = get_collections()

        # --- Sinkronisasi BeritaBps ---
        print("Memproses tabel BeritaBps...")
        all_berita = BeritaBps.query.filter(BeritaBps.embedding.isnot(None)).all()
        if all_berita:
            berita_collection.upsert(
                ids=[str(item.id) for item in all_berita],
                embeddings=[item.embedding.tolist() for item in all_berita],
                metadatas=[
                    {"judul": item.judul_berita, "tanggal_rilis": str(item.tanggal_rilis)}
                    for item in all_berita
                ]
            )
            print(f"Berhasil sinkronisasi {len(all_berita)} item dari BeritaBps.")
        else:
            print("Tidak ada data BeritaBps untuk disinkronkan.")

        # --- Sinkronisasi DocumentChunk ---
        print("\nMemproses tabel DocumentChunk...")
        # Proses dalam batch untuk menghemat memori
        batch_size = 100
        offset = 0
        total_synced = 0
        while True:
            batch_chunks = DocumentChunk.query.filter(DocumentChunk.embedding.isnot(None)).limit(batch_size).offset(offset).all()
            if not batch_chunks:
                break

            document_collection.upsert(
                ids=[str(item.id) for item in batch_chunks],
                embeddings=[item.embedding.tolist() for item in batch_chunks],
                metadatas=[
                    {"document_id": str(item.document_id), "page_number": item.page_number}
                    for item in batch_chunks
                ]
            )
            total_synced += len(batch_chunks)
            print(f"  - Batch {offset // batch_size + 1}: {len(batch_chunks)} item disinkronkan...")
            offset += batch_size

        if total_synced > 0:
            print(f"Berhasil sinkronisasi {total_synced} item dari DocumentChunk.")
        else:
            print("Tidak ada data DocumentChunk untuk disinkronkan.")

        print("\nSinkronisasi selesai!")
            