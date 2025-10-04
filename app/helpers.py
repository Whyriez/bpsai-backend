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
        'apa', 'siapa', 'kapan', 'Hallo', 'kenapa', 'dimana', 'kota', 'kabupaten', 'mengapa', 'bagaimana', 'berapa',
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
    doc_ids_to_check = {chunk.document_id for chunk in doc_chunks}

    if doc_ids_to_check:
        # Ambil SEMUA chunk dari dokumen yang relevan dalam SATU KALI query
        all_related_chunks = DocumentChunk.query.filter(
            DocumentChunk.document_id.in_(doc_ids_to_check)
        ).all()

        # Buat map untuk akses cepat: {document_id: {page_num: chunk_obj}}
        chunks_by_doc_page = {}
        for c in all_related_chunks:
            if c.document_id not in chunks_by_doc_page:
                chunks_by_doc_page[c.document_id] = {}
            chunks_by_doc_page[c.document_id][c.page_number] = c

        # Sekarang lakukan logika pencarian di memori, BUKAN di database
        chunks_to_check = list(doc_chunks)
        for chunk in chunks_to_check:
            # 1. Pencarian ke belakang (in-memory)
            if continuation_pattern.search(chunk.chunk_content):
                current_page_num = chunk.page_number
                while True:
                    prev_page_num = current_page_num - 1
                    if prev_page_num <= 0: break
                    
                    prev_chunk = chunks_by_doc_page.get(chunk.document_id, {}).get(prev_page_num)
                    if prev_chunk and prev_chunk.id not in augmented_chunks_map:
                        augmented_chunks_map[prev_chunk.id] = prev_chunk
                        current_page_num -= 1
                        if not continuation_pattern.search(prev_chunk.chunk_content):
                            break
                    else:
                        break

            # 2. Pencarian ke depan (in-memory)
            current_page_num = chunk.page_number
            while True:
                next_page_num = current_page_num + 1
                next_chunk = chunks_by_doc_page.get(chunk.document_id, {}).get(next_page_num)
                if not next_chunk or not continuation_pattern.search(next_chunk.chunk_content):
                    break
                
                if next_chunk.id not in augmented_chunks_map:
                    augmented_chunks_map[next_chunk.id] = next_chunk
                
                current_page_num = next_page_num

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
                doc_key = (chunk.document.filename, chunk.document.link)
                if doc_key not in chunks_by_doc:
                    chunks_by_doc[doc_key] = []
                chunks_by_doc[doc_key].append(chunk)

        for (filename, link), chunks in chunks_by_doc.items():
            context += f"### Dokumen: {filename} ###\n"
            if link:
                context += f"**Link:** {link}\n"
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
    """
    Format riwayat percakapan dengan struktur yang LEBIH JELAS.
    Sekarang fokus pada interaksi yang paling relevan.
    """
    if not history:
        return ""

    # Filter log yang valid
    valid_logs = [
        log for log in history
        if (log.user_prompt and log.model_response and
            not log.model_response.lower().strip().startswith('error') and
            not log.model_response.lower().strip().startswith('data:'))
    ]

    if not valid_logs:
        return ""

    formatted_history = ""

    # **FIX: Untuk riwayat dengan beberapa interaksi**
    if len(valid_logs) >= 2:
        # Ambil 2 interaksi terakhir dari riwayat yang ada
        recent_interactions = valid_logs[-2:]
        
        formatted_history += "### Dua Interaksi Terakhir ###\n\n"
        for i, log in enumerate(recent_interactions):
            indicator = "PERTANYAAN TERAKHIR" if i == len(recent_interactions)-1 else "SEBELUMNYA"
            formatted_history += f"**{indicator}:** {log.user_prompt}\n"
            formatted_history += f"**JAWABAN:** {log.model_response}\n\n"
    
    else:
        # Hanya ada 1 interaksi dalam riwayat
        single_log = valid_logs[-1]
        formatted_history += "### Interaksi Terakhir ###\n\n"
        formatted_history += f"**PERTANYAAN:** {single_log.user_prompt}\n"
        formatted_history += f"**JAWABAN:** {single_log.model_response}\n\n"

    return formatted_history

def build_final_prompt(context: str, user_prompt: str, history_context: str = "") -> str:
    """
    Membangun prompt final yang menggabungkan format penyajian data yang detail 
    dengan kemampuan untuk menangani pertanyaan riwayat dan menyertakan link sumber.
    """
    full_context = history_context + context if history_context else context

    return f"""
Kamu adalah Asisten AI Data dari BPS Provinsi Gorontalo. Misi utama kamu adalah menyajikan data secara akurat dan dalam format yang paling mudah dibaca.

{history_context}

--- Konteks Data Relevan (Sumber Utama Jawaban) ---
{context}
--- Akhir Konteks Data ---

**Pertanyaan Pengguna:** {user_prompt}

---
## ATURAN WAJIB DIIKUTI

### Bagian A: Logika Interaksi & Percakapan

#### A1. Penanganan Pertanyaan Tentang Riwayat:
1.  **PERTANYAAN "APA YANG SAYA TANYAKAN TADI?":**
    * Jika pengguna bertanya "apa yang saya tanyakan tadi?" atau variasi serupa, **WAJIB merujuk HANYA pada PERTANYAAN TERAKHIR** sebelum pertanyaan ini. Abaikan 'Konteks Data' dan fokus hanya pada riwayat percakapan.
    * **JAWABAN CONTOH YANG BENAR:** "Pertanyaan terakhir Anda adalah: '[teks pertanyaan terakhir]'"

2.  **PERTANYAAN "DATA APA YANG SAYA MINTA TADI?":**
    * Sama seperti di atas, **HANYA merujuk ke permintaan data TERAKHIR**.

#### A2. Penanganan Sapaan:
* Jika pertanyaan hanya sapaan (contoh: "halo", "selamat pagi"), abaikan 'Konteks Data' dan jawab dengan singkat dan ramah.

---
### Bagian B: Aturan Format & Penyajian Data

#### B1. PENANGANAN HEADER TABEL HIERARKIS/BERLAPIS (SANGAT PENTING):
* Tabel dalam konteks mungkin memiliki header dengan beberapa tingkat. Tugasmu adalah menggabungkan semua tingkat ini menjadi satu header kolom yang deskriptif, dipisahkan oleh tanda hubung (` - `).
* **PENTING:** Jangan pernah memperlakukan header tingkat manapun sebagai baris data.

#### B2. TAMPILKAN SEMUA DATA RELEVAN (SANGAT PENTING):
* Jika "Konteks Data" berisi beberapa halaman dari dokumen yang sama, ini menandakan data tersebut saling berkaitan. Kamu **WAJIB** menampilkan informasi dari **SEMUA** halaman tersebut secara berurutan.

#### B3. FORMAT JAWABAN TERPISAH:
* Sajikan data dari **setiap halaman yang relevan secara terpisah** di bawah sub-judul yang jelas (contoh: **Data dari Halaman 133**). **JANGAN MENGGABUNGKANNYA MENJADI SATU TABEL BESAR.**
* Jika data berbentuk tabel, **WAJIB** gunakan format **tabel Markdown**.

#### B4. SERTAKAN CATATAN KAKI & SUMBER (SANGAT PENTING):
* Setelah menampilkan semua data, kamu **WAJIB** mencari dan menyertakan teks penjelasan tambahan seperti **"Catatan/Note"** dan **"Sumber/Source"** yang ada di dalam konteks. Letakkan ini di bagian akhir jawabanmu di bawah sub-judul "Catatan Tambahan".

#### B5. SITASI SUMBER:
* Di awal jawaban, sebutkan nama file dan **rentang halaman** yang digunakan (contoh: "Menurut dokumen provinsi-gorontalo-dalam-angka-2025.pdf, halaman 133-135,...").

#### B6. FOKUS PADA KONTEKS:
* Jawabanmu **HARUS** didasarkan **HANYA** pada "Konteks Data Relevan".

#### B7. SERTAKAN LINK SUMBER (BARU & PENTING):
* Setelah bagian "Catatan Tambahan", jika 'Konteks Data' menyediakan **Link** untuk dokumen atau berita yang digunakan, kamu **WAJIB** menampilkannya di bawah judul **"Sumber Digital"**.
* **Contoh Format:** `Sumber Digital: [provinsi-gorontalo-dalam-angka-2025.pdf](http://path/to/document.pdf)`
"""

# def build_final_prompt(context: str, user_prompt: str, history_context: str = "") -> str:
#     """Membangun prompt final yang akan dikirim ke Gemini dengan instruksi yang lebih tegas dan spesifik."""
#     full_context = history_context + context if history_context else context

#     return f"""
# Kamu adalah Asisten AI Data dari BPS Provinsi Gorontalo. Misi utama kamu adalah menyajikan data secara akurat dan dalam format yang paling mudah dibaca.

# {history_context}

# --- Konteks Data Relevan (Sumber Utama Jawaban) ---
# {context}
# --- Akhir Konteks Data ---

# **Pertanyaan Pengguna Saat Ini:** {user_prompt}

# ## ATURAN & FORMAT JAWABAN (WAJIB DIIKUTI)

# ### **PENANGANAN PERTANYAAN TENTANG RIWAYAT PERCAKAPAN (SANGAT PENTING):**

# 1. **PERTANYAAN "APA YANG SAYA TANYAKAN TADI?":**
#    - Jika pengguna bertanya "apa yang saya tanyakan tadi?" atau variasi serupa, 
#      **WAJIB merujuk HANYA pada PERTANYAAN TERAKHIR** sebelum pertanyaan ini.
#    - **JAWABAN CONTOH YANG BENAR:** "Pertanyaan terakhir Anda adalah: '[teks pertanyaan terakhir]'"
#    - **JANGAN PERNAH** membuat daftar semua pertanyaan yang pernah ditanyakan.

# 2. **PERTANYAAN "DATA APA YANG SAYA MINTA TADI?":**
#    - Sama seperti di atas, **HANYA merujuk ke permintaan data TERAKHIR**.

# 3. **PERTANYAAN UMUM TENTANG RIWAYAT:**
#    - Jika pengguna bertanya secara umum "apa saja yang pernah saya tanyakan?", 
#      baru boleh memberikan ringkasan singkat 2-3 pertanyaan terakhir.

# ### **ATURAN UMUM:**
# 4. Fokus pada pertanyaan SAAT INI ({user_prompt})
# 5. Gunakan konteks data HANYA jika relevan dengan pertanyaan saat ini
# 6. Untuk pertanyaan tentang riwayat, abaikan konteks data dan fokus pada riwayat percakapan

# ### **CONTOH INTERAKSI YANG BENAR:**
# - User: "Berapa jumlah penduduk Gorontalo?"
# - AI: [menjawab data penduduk]
# - User: "Apa yang saya tanyakan tadi?"
# - AI: "Pertanyaan terakhir Anda adalah: 'Berapa jumlah penduduk Gorontalo?'"

# ---
# **Sekarang jawab pertanyaan ini: "{user_prompt}"**
# Dengan mengikuti semua aturan di atas.
# """

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