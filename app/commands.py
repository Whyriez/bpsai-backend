import click
import time
from .models import db, BeritaBps, User
from werkzeug.security import generate_password_hash
from .services import EmbeddingService

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
            