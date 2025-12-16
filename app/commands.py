import os
import click
import time
import re
import pandas as pd
from flask.cli import with_appcontext
from sqlalchemy import or_, cast, JSON, Text
from .models import db, BeritaBps, User, DocumentChunk
from werkzeug.security import generate_password_hash
from .services import EmbeddingService
from .vector_db import get_collections

STOP_WORDS = {
    'di', 'ke', 'dari', 'pada', 'dan', 'atau', 'yang', 'untuk', 'dengan', 'tanpa',
    'dalam', 'atas', 'bawah', 'depan', 'belakang', 'samping', 'antara', 'oleh',
    'karena', 'jika', 'maka', 'tetapi', 'namun', 'sehingga', 'agar', 'supaya',
    'walau', 'meski', 'walaupun', 'meskipun', 'adalah', 'ialah', 'itu', 'ini',
    'situ', 'sini', 'nya', 'lah', 'kah', 'pun', 'per', 'provinsi', 'kota',
    'kabupaten', 'kecamatan', 'desa', 'kelurahan', 'bulan', 'tahun', 'data',
    'statistik', 'bps', 'badan', 'pusat', 'gorontalo', 'periode', 'hasil',
    'publikasi', 'rilis', 'terbaru', 'update', 'edisi', 'versi', 'sebesar'
}

SPECIAL_PHRASES = [
    'tingkat penghunian kamar', 'nilai tukar petani', 'indeks ketimpangan gender',
    'indeks pembangunan manusia', 'produk domestik bruto', 'tingkat pengangguran terbuka',
    'indeks harga konsumen', 'indeks harga produsen', 'indeks pembangunan gender',
    'indeks pemberdayaan gender', 'garis kemiskinan', 'tenaga kerja', 'triwulan iv',
    'triwulan iii', 'triwulan ii', 'triwulan i', 'semester 1', 'semester 2',
    'tpk', 'ntp', 'ikg', 'ipm', 'pdb', 'ihk', 'ihp', 'ipg', 'idg'
]

# Urutkan frasa dari yang terpanjang agar tidak tumpang tindih saat replace
SPECIAL_PHRASES.sort(key=len, reverse=True)

MONTHS = [
    'januari', 'februari', 'maret', 'april', 'mei', 'juni', 'juli', 'agustus',
    'september', 'oktober', 'november', 'desember'
]

def _extract_keywords_from_title(title):
    """Fungsi helper untuk mengekstrak keyword dari judul berita."""
    title_lower = title.lower()
    tags = []

    # STEP 1: Ekstrak frasa khusus terlebih dahulu
    for phrase in SPECIAL_PHRASES:
        if phrase in title_lower:
            tags.append(phrase)
            title_lower = title_lower.replace(phrase, '')

    # STEP 2: Ekstrak tahun (contoh: 2025)
    years = re.findall(r'\b(20\d{2})\b', title_lower)
    if years:
        tags.extend(years)
        title_lower = re.sub(r'\b(20\d{2})\b', '', title_lower)

    # STEP 3: Ekstrak bulan
    found_months = []
    for month in MONTHS:
        if month in title_lower:
            found_months.append(month)
            title_lower = title_lower.replace(month, '')
    if found_months:
        tags.extend(found_months)

    # STEP 4: Ekstrak kata-kata individual dari sisa judul
    # Hapus semua karakter selain huruf, angka, dan spasi
    clean_title = re.sub(r'[^\w\s]', '', title_lower)
    words = clean_title.split()

    word_tags = []
    for word in words:
        if len(word) >= 3 and word.isnumeric() is False and word not in STOP_WORDS:
            word_tags.append(word)

    tags.extend(word_tags)

    # Hapus duplikat sambil mempertahankan urutan dan batasi jumlahnya
    final_tags = list(dict.fromkeys(tags))
    return final_tags[:10]


def register_commands(app):
    """Fungsi untuk mendaftarkan custom CLI commands ke aplikasi Flask."""

    @app.cli.command("import:csv")
    @click.argument('filepath', type=click.Path(exists=True))
    @with_appcontext
    def import_csv(filepath):
        """
        Mengimpor data berita BPS dari file CSV ke dalam database.
        FILEPATH adalah path lengkap ke file .csv Anda.
        """
        click.echo(f"Memulai proses impor dari file: {filepath}")

        # --- Helper function untuk konversi tanggal ---
        # (Diambil langsung dari skrip lama Anda)
        def konversi_tanggal(tanggal_str):
            bulan_map = {
                'Januari': '01', 'Februari': '02', 'Maret': '03', 'April': '04',
                'Mei': '05', 'Juni': '06', 'Juli': '07', 'Agustus': '08',
                'September': '09', 'Oktober': '10', 'November': '11', 'Desember': '12'
            }
            try:
                parts = str(tanggal_str).strip().split()
                if len(parts) == 3:
                    # Format: YYYY-MM-DD
                    return f"{parts[2]}-{bulan_map.get(parts[1], '01')}-{parts[0].zfill(2)}"
            except Exception as e:
                click.echo(f"Gagal mengonversi tanggal '{tanggal_str}': {e}", err=True)
                return None
            return None

        # --- Logika Utama Impor Data ---
        try:
            # 1. Baca file CSV menggunakan Pandas
            df = pd.read_csv(filepath, engine='python', on_bad_lines='skip', header=0)
            
            # Logika pembersihan kolom dari skrip lama
            if len(df.columns) > 4:
                df = df.iloc[:, :4]
            df.columns = ['Tanggal Rilis', 'Judul Berita', 'Ringkasan', 'Link']
            
            click.echo(f"Ditemukan {len(df)} baris data di file CSV.")

            data_baru, data_dilewati = 0, 0
            
            # 2. Iterasi setiap baris di DataFrame
            for index, row in df.iterrows():
                link = str(row['Link']).strip()

                # Lewati jika link kosong
                if not link:
                    click.secho(f"⚠️ Baris {index+2} dilewati: Link kosong", fg='yellow')
                    continue

                # 3. Cek duplikasi menggunakan SQLAlchemy (menggantikan INSERT IGNORE)
                # Ini lebih aman dan menggunakan ORM Anda
                existing_news = BeritaBps.query.filter_by(link=link).first()
                if existing_news:
                    data_dilewati += 1
                    continue

                # 4. Jika tidak ada duplikat, siapkan data baru
                tanggal_rilis_fmt = konversi_tanggal(row['Tanggal Rilis'])
                if not tanggal_rilis_fmt:
                    click.secho(f"⚠️ Baris {index+2} dilewati: Format tanggal tidak valid ({row['Tanggal Rilis']})", fg='yellow')
                    continue

                new_entry = BeritaBps(
                    tanggal_rilis=tanggal_rilis_fmt,
                    judul_berita=str(row['Judul Berita']).strip(),
                    ringkasan=str(row['Ringkasan']).strip(),
                    link=link,
                    tags=[] # Anda bisa menambahkan logika untuk tags jika ada
                )
                db.session.add(new_entry)
                data_baru += 1

                # Commit per 100 data baru untuk efisiensi memori
                if data_baru % 100 == 0:
                    db.session.commit()
                    click.echo(f"--- Melakukan commit batch, {data_baru} data baru telah diproses ---")

            # 5. Commit sisa data yang belum di-commit
            db.session.commit()
            
            # 6. Tampilkan hasil akhir
            click.secho(f"\n✅ Proses Impor Selesai!", fg='green', bold=True)
            click.echo(f"   - Data baru ditambahkan: {data_baru}")
            click.echo(f"   - Duplikat dilewati: {data_dilewati}")

        except FileNotFoundError:
            click.secho(f"Error: File tidak ditemukan di '{filepath}'", fg='red')
        except Exception as e:
            db.session.rollback()
            click.secho(f"Terjadi error: {e}", fg='red')

    @app.cli.command("tags:auto")
    @click.option('--limit', default=100, type=int, help='Jumlah berita yang akan diproses.')
    @click.option('--all', is_flag=True, help='Proses semua berita yang belum memiliki tag.')
    @with_appcontext
    def auto_tag_berita(limit, all):
        """Secara otomatis memberikan tag pada berita BPS berdasarkan judulnya."""
        click.secho('Memulai proses auto-tagging dari judul berita...', fg='cyan')

        query = BeritaBps.query.filter(
            or_(
                BeritaBps.tags == None,
                cast(BeritaBps.tags, Text) == '[]'
            )
        )

        if not all:
            query = query.limit(limit)

        berita_list = query.all()
        total = len(berita_list)

        if total == 0:
            click.secho('Tidak ada berita baru yang perlu di-tag.', fg='green')
            return

        click.echo(f"Ditemukan {total} berita untuk diproses...")

        tagged_count = 0
        with click.progressbar(berita_list, label="Menproses berita") as bar:
            for berita in bar:
                try:
                    new_tags = _extract_keywords_from_title(berita.judul_berita)

                    if new_tags:
                        berita.tags = new_tags
                        db.session.add(berita)
                        tagged_count += 1

                except Exception as e:
                    click.secho(f"\nError saat memproses ID {berita.id}: {e}", fg='red')

        db.session.commit()

        click.secho(f"\n\nProses selesai!", fg='green', bold=True)
        click.echo(f"Berhasil memberikan tag pada {tagged_count} dari {total} berita.")


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
        """Seeds the database with initial data (admin user from ENV)."""
        click.echo("Seeding database...")

        # Ambil konfigurasi dari Environment Variable
        # Format di .env:
        # SEED_ADMIN_EMAIL=nama@bps.go.id
        # SEED_ADMIN_USERNAME=admin_utama (opsional, default 'admin')

        email_admin = os.getenv('SEED_ADMIN_EMAIL')
        username_admin = os.getenv('SEED_ADMIN_USERNAME', 'admin')

        # Validasi keamanan: Pastikan email diset di .env
        if not email_admin:
            click.secho("❌ Error: Variabel 'SEED_ADMIN_EMAIL' tidak ditemukan di .env", fg='red', bold=True)
            click.echo("Silahkan tambahkan baris berikut di file .env anda:")
            click.echo("SEED_ADMIN_EMAIL=email_anda@bps.go.id")
            return

        with app.app_context():
            existing_user = User.query.filter_by(email=email_admin).first()

            existing_username = User.query.filter_by(username=username_admin).first()

            if existing_user is None and existing_username is None:
                admin_user = User(
                    username=username_admin,
                    email=email_admin,
                    role='admin'
                )

                db.session.add(admin_user)
                db.session.commit()

                click.secho(f"✅ Admin user registered successfully!", fg='green')
                click.echo(f"   Email: {email_admin}")
                click.echo(f"   Username: {username_admin}")
                click.echo(f"   Role: admin")

            elif existing_user:
                click.secho(f"⚠️ User dengan email {email_admin} sudah ada.", fg='yellow')
            elif existing_username:
                click.secho(
                    f"⚠️ User dengan username {username_admin} sudah ada. Silahkan ganti SEED_ADMIN_USERNAME di .env",
                    fg='yellow')

            click.echo("Database seeding process finished.")

    @app.cli.command("user:create-admin")
    @click.argument("email")
    @click.argument("username")
    @with_appcontext
    def create_admin_manual(email, username):
        """
        Buat user admin baru langsung dari terminal.
        Contoh: flask user:create-admin nur.alim@bps.go.id nuralim
        """
        # 1. Cek duplikasi
        if User.query.filter((User.email == email) | (User.username == username)).first():
            click.secho(f"❌ User dengan email {email} atau username {username} sudah ada!", fg='red')
            return

        # 2. Buat user
        try:
            new_admin = User(
                username=username,
                email=email,
                role='admin'  # Langsung set jadi admin
            )

            db.session.add(new_admin)
            db.session.commit()

            click.secho(f"✅ Berhasil membuat Admin baru!", fg='green', bold=True)
            click.echo(f"   Email: {email}")
            click.echo(f"   Username: {username}")

        except Exception as e:
            db.session.rollback()
            click.secho(f"❌ Gagal membuat user: {e}", fg='red')

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

    @app.cli.command('migrate-gemini-keys')
    def migrate_gemini_keys():
        """Migrate dari format lama ke format baru"""
        from app.env_manager import EnvManager
        from app.models import GeminiApiKeyConfig, db
        import os
        
        env_manager = EnvManager()
        
        # Cek format lama
        old_keys_str = os.getenv('GEMINI_API_KEYS', '')
        if not old_keys_str:
            print("Tidak ada keys dalam format lama")
            return
        
        old_keys_list = [key.strip() for key in old_keys_str.split(',') if key.strip()]
        print(f"Found {len(old_keys_list)} keys in old format")
        
        # Convert ke format baru
        keys_config = {}
        for i, key_value in enumerate(old_keys_list, 1):
            alias = f"{i}"
            keys_config[alias] = {'value': key_value}
        
        # Update .env file
        success = env_manager.update_gemini_keys(keys_config)
        if success:
            print("Successfully migrated to new format")
            
            # Buat config di database
            for alias in keys_config.keys():
                if not GeminiApiKeyConfig.query.filter_by(key_alias=alias).first():
                    config = GeminiApiKeyConfig(
                        key_alias=alias,
                        key_name=f"Migrated Key {alias.replace('KEY_', '')}",
                        is_active=True
                    )
                    db.session.add(config)
            
            db.session.commit()
            print("Database config created")
        else:
            print("Migration failed")
            