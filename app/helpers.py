import re
from app.models import BeritaBps, DocumentChunk, PromptLog, DocumentFeedbackScore
import nltk
from datetime import datetime
from nltk.corpus import stopwords

BPS_ACRONYM_DICTIONARY = {
    'ntp': 'nilai tukar petani',
    'ipm': 'indeks pembangunan manusia',
    'ihk': 'indeks harga konsumen',
    'pdb': 'produk domestik bruto',
    'pph': 'perkembangan pariwisata dan hotel',
    'tpt': 'tingkat pengangguran terbuka',
    'ikg': 'indeks ketimpangan gender',
}

def expand_query_with_synonyms(prompt: str, dictionary: dict) -> str:
    """Memperluas query dengan sinonim/akronim dari kamus."""
    expanded_terms = []
    words = re.findall(r'\b\w+\b', prompt.lower())
    
    for word in words:
        if word in dictionary:
            expanded_terms.append(dictionary[word])
    
    if expanded_terms:
        return f"{prompt} {' '.join(expanded_terms)}"
    
    return prompt

def extract_years(prompt: str) -> list:
    """Mengekstrak tahun dari sebuah string prompt."""
    years = set()
    range_match = re.search(r'\b(20\d{2})\s*(?:hingga|sampai|ke|dan|-)\s*(20\d{2})\b', prompt, re.IGNORECASE)
    if range_match:
        start_year, end_year = map(int, range_match.groups())
        for year in range(start_year, end_year + 1):
            years.add(year)
    
    individual_matches = re.findall(r'\b(20\d{2})\b', prompt)
    for year in individual_matches:
        years.add(int(year))
        
    return sorted(list(years))

def extract_keywords(prompt: str) -> list:
    """Mengekstrak kata kunci dari prompt dengan menghapus stop words."""
    standard_stop_words = set(stopwords.words('indonesian'))
    custom_stop_words  = [
        'apa', 'siapa', 'kapan', 'dimana', 'mengapa', 'bagaimana', 'berapa',
        'jelaskan', 'tampilkan', 'berikan', 'sebutkan', 'cari', 'carikan',
        'analisis', 'buatkan', 'buat', 'analisa', 'di', 'ke', 'dari', 'pada',
        'untuk', 'dengan', 'dan', 'atau', 'tapi', 'hingga', 'sampai',
        'menurut', 'data', 'informasi', 'tahun', 'bulan', 'terbaru',
        'provinsi', 'gorontalo', 'lebih', 'detail', 'rinci', 'lengkap',
        'secara', 'dong', 'ya', 'tolong', 'tentang', 'mengenai', 'bentuk', 'butuh'
    ]
    all_stop_words = standard_stop_words.union(custom_stop_words)
    words = re.findall(r'\b\w+\b', prompt.lower())
    
    keywords = [word for word in words if word not in all_stop_words and not word.isdigit() and len(word) > 2]
    return list(set(keywords))

def detect_intent(prompt: str) -> str:
    """Mendeteksi niat sederhana dari prompt (sapaan atau permintaan data)."""
    cleaned_prompt = prompt.lower().strip()
    greetings = ['halo', 'hai', 'selamat pagi', 'selamat siang', 'selamat malam', 'kamu siapa', 'siapa kamu', 'terima kasih']
    for greeting in greetings:
        if cleaned_prompt.startswith(greeting):
            return 'sapaan'
    return 'data_request'

def build_context(relevant_items: list, requested_years: list = []) -> str:
    """
    Membangun string konteks dari daftar item gabungan (BeritaBps dan DocumentChunk).
    Secara cerdas dan iteratif mengambil semua halaman tabel lanjutan.
    """
    if not relevant_items:
        return "Tidak ditemukan data yang relevan. Mohon informasikan kepada pengguna."

    news_items = [item for item in relevant_items if isinstance(item, BeritaBps)]
    doc_chunks = [item for item in relevant_items if isinstance(item, DocumentChunk)]

    # --- LOGIKA BARU YANG DISEMPURNAKAN ---
    # Gunakan dictionary untuk menyimpan chunk yang sudah ditemukan agar tidak duplikat
    augmented_chunks_map = {chunk.id: chunk for chunk in doc_chunks}
    continuation_pattern = re.compile(r'(lanjutan tabel|tabel.*lanjutan|continued table)', re.IGNORECASE)

    # Buat salinan daftar awal untuk diiterasi
    chunks_to_check = list(doc_chunks)

    for chunk in chunks_to_check:
        # 1. Pencarian ke belakang (jika yang ditemukan adalah halaman lanjutan)
        if continuation_pattern.search(chunk.chunk_content):
            current_page_num = chunk.page_number
            while True:
                prev_page_num = current_page_num - 1
                if prev_page_num <= 0: break
                
                prev_chunk = DocumentChunk.query.filter_by(document_id=chunk.document_id, page_number=prev_page_num).first()
                if prev_chunk and prev_chunk.id not in augmented_chunks_map:
                    augmented_chunks_map[prev_chunk.id] = prev_chunk
                    print(f"INFO: Backward search found page {prev_page_num} for continued table.")
                    current_page_num -= 1 # Lanjutkan pencarian ke belakang
                    if not continuation_pattern.search(prev_chunk.chunk_content):
                        break # Berhenti jika sudah menemukan awal tabel
                else:
                    break # Berhenti jika halaman sebelumnya tidak ada atau sudah diproses

        # 2. Pencarian ke depan (iteratif untuk menemukan SEMUA halaman lanjutan)
        current_page_num = chunk.page_number
        while True:
            next_page_num = current_page_num + 1
            next_chunk = DocumentChunk.query.filter_by(document_id=chunk.document_id, page_number=next_page_num).first()

            # Berhenti jika tidak ada halaman berikutnya ATAU halaman berikutnya bukan lanjutan
            if not next_chunk or not continuation_pattern.search(next_chunk.chunk_content):
                break
            
            # Jika itu adalah halaman lanjutan dan belum kita proses, tambahkan
            if next_chunk.id not in augmented_chunks_map:
                augmented_chunks_map[next_chunk.id] = next_chunk
                print(f"INFO: Forward search found continued table on page {next_page_num}.")
            
            # PENTING: Lanjutkan loop dari halaman yang baru ditemukan
            current_page_num = next_page_num

    # Konversi kembali dari map ke list
    doc_chunks = list(augmented_chunks_map.values())
    # --- AKHIR DARI LOGIKA BARU ---

    context = ""
    
    if news_items:
        context += "--- KONTEKS DARI BERITA RESMI BPS ---\n\n"
        # ... (sisa kode untuk format berita tidak berubah)
        news_by_year = {}
        for news in news_items:
            year = news.tanggal_rilis.year
            if year not in news_by_year:
                news_by_year[year] = []
            news_by_year[year].append(news)
        for year in sorted(news_by_year.keys(), reverse=True):
            context += f"### Berita Tahun {year} ###\n"
            for news in news_by_year[year]:
                context += f"**Judul:** {news.judul_berita}\n"
                context += f"**Tanggal Rilis:** {news.tanggal_rilis.strftime('%Y-%m-%d')}\n"
                context += f"**Ringkasan:** {news.ringkasan}\n"
                context += f"**Link:** {news.link}\n\n"

    if doc_chunks:
        context += "--- KONTEKS DARI DOKUMEN PDF ---\n\n"
        chunks_by_doc = {}
        for chunk in doc_chunks:
            if chunk.document:
                filename = chunk.document.filename
                if filename not in chunks_by_doc:
                    chunks_by_doc[filename] = []
                chunks_by_doc[filename].append(chunk)

        for filename, chunks in chunks_by_doc.items():
            context += f"### Dokumen: {filename} ###\n"
            # Pastikan halaman diurutkan dengan benar sebelum ditampilkan
            for chunk in sorted(chunks, key=lambda c: c.page_number):
                context += f"**Halaman {chunk.page_number}:**\n"
                context += f"{chunk.chunk_content}\n\n"

    if requested_years:
        # ... (sisa kode untuk format tahun tidak berubah)
        found_years_news = {n.tanggal_rilis.year for n in news_items}
        missing_years = sorted(list(set(requested_years) - found_years_news))
        if missing_years:
            context += f"CATATAN UNTUK AI: Data berita untuk tahun {', '.join(map(str, missing_years))} tidak ditemukan. Anda wajib memberitahu pengguna.\n\n"

    context += "--- AKHIR DARI KONTEKS ---\n\n"
    return context


def format_conversation_history(history: list[PromptLog]) -> str:
    """Mengubah daftar objek PromptLog menjadi string riwayat percakapan."""
    if not history:
        return ""
    
    formatted_history = "### RIWAYAT PERCAKAPAN SEBELUMNYA ###\n\n"
    
    for i, log in enumerate(history):
        if (log.user_prompt and log.model_response and 
            not log.model_response.lower().strip().startswith('error') and
            not log.model_response.lower().strip().startswith('data:')):
            
            formatted_history += f"**Interaksi {i + 1}:**\n"
            formatted_history += f"User: {log.user_prompt}\n"
            formatted_history += f"Asisten: {log.model_response}\n\n"
    
    if formatted_history != "### RIWAYAT PERCAKAPAN SEBELUMNYA ###\n\n":
        formatted_history += "**CATATAN PENTING:** Jawablah pertanyaan user saat ini dengan mempertimbangkan konteks dari seluruh riwayat percakapan di atas.\n\n"
    else:
        formatted_history = ""
        
    return formatted_history

def build_final_prompt(context: str, user_prompt: str, history_context: str = "") -> str:
    """Membangun prompt final yang akan dikirim ke Gemini dengan instruksi yang lebih tegas dan spesifik."""
    full_context = history_context + context if history_context else context

    return f"""
Kamu adalah Asisten AI Data dari BPS Provinsi Gorontalo. Misi utama kamu adalah menyajikan data secara akurat dan dalam format yang paling mudah dibaca.

{history_context}

--- Konteks Data Relevan (Sumber Utama Jawaban) ---
{context}
--- Akhir Konteks Data ---

**Pertanyaan Pengguna:** {user_prompt}

---
## ATURAN & FORMAT JAWABAN (WAJIB DIIKUTI)
1.  **PENANGANAN HEADER TABEL HIERARKIS/BERLAPIS (SANGAT PENTING):** Tabel dalam konteks mungkin memiliki header dengan beberapa tingkat (induk, anak, cucu, dst.). Tugasmu adalah menggabungkan semua tingkat ini menjadi satu header kolom yang deskriptif.
    * **Prinsip:** Gabungkan header dari tingkat tertinggi ke tingkat terendah, dipisahkan oleh tanda hubung (` - `).
    * **Contoh 2 Tingkat:** Jika header induk adalah "Bukan Angkatan Kerja" dan di bawahnya ada "Sekolah" dan "Lainnya", maka header kolom gabungannya adalah "Bukan Angkatan Kerja - Sekolah" dan "Bukan Angkatan Kerja - Lainnya".
    * **Contoh 3 Tingkat:** Jika header induk adalah "Angkatan Kerja", di bawahnya ada sub-header "Pengangguran", dan di bawah "Pengangguran" ada "Pernah Bekerja", maka header kolom gabungan finalnya adalah **"Angkatan Kerja - Pengangguran - Pernah Bekerja"**.
    * **PENTING:** Jangan pernah memperlakukan header tingkat manapun sebagai baris data. Selalu gabungkan ke bawah hingga mencapai header tingkat terendah.

2.  **TAMPILKAN SEMUA DATA RELEVAN (SANGAT PENTING):** Jika "Konteks Data" berisi beberapa halaman dari dokumen yang sama (misal: Halaman 133, 134, 135), ini menandakan data tersebut saling berkaitan dan merupakan satu kesatuan. Kamu **WAJIB** menampilkan informasi dari **SEMUA** halaman tersebut secara berurutan.

3.  **FORMAT JAWABAN TERPISAH:**
    * Sajikan data dari **setiap halaman yang relevan secara terpisah** di bawah sub-judul yang jelas (contoh: **Data dari Halaman 133**, **Lanjutan Tabel dari Halaman 134**, dst.). **JANGAN MENGGABUNGKANNYA MENJADI SATU TABEL BESAR.**
    * Jika data pada sebuah halaman berbentuk tabel, **WAJIB** gunakan format **tabel Markdown** untuk halaman tersebut.

4.  **FOKUS PADA DATA MURNI UNTUK TABEL:** Saat **membangun tabel**, fokuslah hanya pada baris data dan abaikan header yang berulang di halaman lanjutan.

5.  **SERTAKAN CATATAN KAKI & SUMBER (SANGAT PENTING):** Setelah menampilkan semua tabel data, kamu **WAJIB** mencari dan menyertakan semua teks penjelasan tambahan seperti **"Catatan/Note"** dan **"Sumber/Source"** yang ada di dalam konteks. Letakkan informasi ini di bagian paling akhir dari jawabanmu di bawah sub-judul "Catatan Tambahan". Ini penting untuk memberikan konteks penuh pada data yang disajikan.

6.  **FOKUS PADA KONTEKS:** Jawabanmu **HARUS** didasarkan **HANYA** pada "Konteks Data Relevan". Jangan membuat kalkulasi atau estimasi.

7.  **SITASI SUMBER:** Sebutkan nama file dan **rentang halaman** yang digunakan (contoh: "Menurut dokumen provinsi-gorontalo-dalam-angka-2025.pdf, halaman 133-135,...").

8.  **SAPAAN:** Jika pertanyaan hanya sapaan, abaikan konteks dan jawab dengan singkat dan ramah.
"""


# SPK SAW
def normalize(value, min_val, max_val):
    """Normalisasi nilai ke rentang 0-1."""
    if max_val == min_val:
        return 0.5 # Hindari pembagian dengan nol
    return (value - min_val) / (max_val - min_val)

def rerank_with_dss(results_with_distance: list):
    """
    Menyusun ulang peringkat hasil pencarian menggunakan metode Weighted Scoring.
    Inputnya adalah list tuple [(item, distance), ...].
    """
    if not results_with_distance:
        return []

    # Definisikan bobot kriteria
    weights = {
        'relevance': 0.40,
        'feedback': 0.35,
        'recency': 0.15,
        'content_type': 0.10
    }

    scored_items = []
    
    # Ekstrak semua ID untuk query feedback score yang efisien
    berita_ids = [str(item.id) for item, dist in results_with_distance if isinstance(item, BeritaBps)]
    chunk_ids = [str(item.id) for item, dist in results_with_distance if isinstance(item, DocumentChunk)]

    feedback_scores_db = DocumentFeedbackScore.query.filter(
        ((DocumentFeedbackScore.entity_type == 'berita_bps') & (DocumentFeedbackScore.entity_id.in_(berita_ids))) |
        ((DocumentFeedbackScore.entity_type == 'document_chunk') & (DocumentFeedbackScore.entity_id.in_(chunk_ids)))
    ).all()
    
    # Ubah ke dictionary untuk akses cepat
    feedback_map = {f"{fs.entity_type}-{fs.entity_id}": fs.score for fs in feedback_scores_db}

    for item, distance in results_with_distance:
        scores = {}
        
        # 1. Skor Relevansi (semakin kecil distance, semakin bagus)
        scores['relevance'] = 1 - distance 

        # 2. Skor Feedback
        entity_type = 'berita_bps' if isinstance(item, BeritaBps) else 'document_chunk'
        entity_id = str(item.id)
        scores['feedback'] = feedback_map.get(f"{entity_type}-{entity_id}", 0.5) # Default 0.5 (netral)

        # 3. Skor Keterbaruan
        recency_date = None
        if isinstance(item, BeritaBps):
            recency_date = item.tanggal_rilis
        elif isinstance(item, DocumentChunk):
            recency_date = item.created_at.date() # Ambil tanggalnya saja
        
        if recency_date:
            days_ago = (datetime.utcnow().date() - recency_date).days
            # Skor menurun setelah 1 tahun (365 hari)
            scores['recency'] = max(0, 1 - (days_ago / 365)) 
        else:
            scores['recency'] = 0.5

        # 4. Skor Tipe Konten
        if isinstance(item, DocumentChunk) and item.chunk_metadata.get('type') == 'table':
            scores['content_type'] = 1.0 # Nilai tertinggi untuk tabel
        else:
            scores['content_type'] = 0.5 # Nilai standar
        
        # Kalkulasi skor akhir
        final_score = (scores['relevance'] * weights['relevance'] +
                       scores['feedback'] * weights['feedback'] +
                       scores['recency'] * weights['recency'] +
                       scores['content_type'] * weights['content_type'])
        
        scored_items.append({'item': item, 'final_score': final_score, 'details': scores})

    # Urutkan berdasarkan skor akhir tertinggi
    sorted_items = sorted(scored_items, key=lambda x: x['final_score'], reverse=True)
    
    # Kembalikan hanya objek item yang sudah terurut
    return [x['item'] for x in sorted_items]