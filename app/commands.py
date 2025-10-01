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
            