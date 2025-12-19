from flask import Blueprint, jsonify
from app.models import db, PromptLog, Feedback
from sqlalchemy import func, cast, TEXT, distinct
from collections import Counter
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from flask_jwt_extended import jwt_required

# Membuat Blueprint untuk rute analytics
analytics_bp = Blueprint('analytics', __name__, url_prefix='/api/analytics')

@analytics_bp.route('/all', methods=['GET'])
@jwt_required()
def get_all_analytics():
    """
    Endpoint untuk mengambil semua data yang diperlukan untuk halaman Advanced Analytics.
    ---
    tags:
      - Analytics
    summary: Mendapatkan semua data agregat untuk halaman analitik.
    security:
      - Bearer: []
    responses:
      200:
        description: Berhasil mengambil semua data analitik.
        schema:
          type: object
          properties:
            usageTrends:
              type: object
              description: Data chart untuk tren penggunaan harian (sesi vs interaksi).
            responseTime:
              type: object
              description: Data waktu respons rata-rata dan distribusinya.
            topKeywords:
              type: array
              description: Daftar topik/kata kunci yang paling sering dicari.
            retrievalSuccessRate:
              type: number
              description: Persentase keberhasilan RAG dalam menemukan data.
            dataCoverage:
              type: array
              description: Persentase cakupan data untuk 3 tahun terakhir.
            servicePerformance:
              type: object
              description: KPI performa layanan (uptime, akurasi, kepuasan).
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
    """
     # --- 1. Tren Penggunaan (Usage Trends) ---
    usage_trends_data = {
        'labels': [],
        'datasets': []
    }
    daily_stats = {}
    # Inisialisasi 30 hari terakhir dengan 0
    for i in range(30):
        day = datetime.utcnow() - timedelta(days=i)
        daily_stats[day.strftime('%Y-%m-%d')] = {'sessions': 0, 'prompts': 0}

    thirty_days_ago = datetime.utcnow() - timedelta(days=30)

    # Query untuk menghitung sesi unik dan total prompt per hari
    usage_results = db.session.query(
        func.date_trunc('day', PromptLog.created_at).label('day'),
        func.count(distinct(PromptLog.session_id)).label('unique_sessions'),
        func.count(PromptLog.id).label('total_prompts')
    ).filter(PromptLog.created_at >= thirty_days_ago)\
     .group_by('day')\
     .all()

    # Isi data dari hasil query
    for row in usage_results:
        if row.day:
            day_str = row.day.strftime('%Y-%m-%d')
            if day_str in daily_stats:
                daily_stats[day_str]['sessions'] = row.unique_sessions
                daily_stats[day_str]['prompts'] = row.total_prompts
    
    # Urutkan dan format untuk chart
    sorted_days = sorted(daily_stats.keys())
    
    usage_trends_data['labels'] = [datetime.strptime(d, '%Y-%m-%d').strftime('%b %d') for d in sorted_days]
    
    sessions_data = [daily_stats[d]['sessions'] for d in sorted_days]
    prompts_data = [daily_stats[d]['prompts'] for d in sorted_days]
    
    usage_trends_data['datasets'] = [
        {
            'label': 'Sesi Harian',
            'data': sessions_data,
            'borderColor': '#3b82f6',
            'backgroundColor': 'rgba(59, 130, 246, 0.1)',
            'tension': 0.4,
            'fill': True
        },
        {
            'label': 'Total Interaksi',
            'data': prompts_data,
            'borderColor': '#10b981',
            'backgroundColor': 'rgba(16, 185, 129, 0.1)',
            'tension': 0.4,
            'fill': True
        }
    ]

    # --- 1. Analisis Waktu Respons ---
    response_time_query = db.session.query(PromptLog.processing_time_ms)\
        .filter(PromptLog.processing_time_ms.isnot(None))
    
    total_logs_with_time = response_time_query.count()
    avg_response_time = response_time_query.with_entities(func.avg(PromptLog.processing_time_ms)).scalar() or 0

    if total_logs_with_time > 0:
        fast_count = response_time_query.filter(PromptLog.processing_time_ms < 2000).count()
        medium_count = response_time_query.filter(PromptLog.processing_time_ms.between(2000, 5000)).count()
        slow_count = response_time_query.filter(PromptLog.processing_time_ms > 5000).count()
        response_time_distribution = {
            'fast': round((fast_count / total_logs_with_time) * 100),
            'medium': round((medium_count / total_logs_with_time) * 100),
            'slow': round((slow_count / total_logs_with_time) * 100)
        }
    else:
        response_time_distribution = {'fast': 0, 'medium': 0, 'slow': 0}

    # --- 2. Top Keywords & Topics (REFINED VERSION) ---
    
    all_keywords_logs = db.session.query(PromptLog.extracted_keywords)\
        .filter(PromptLog.extracted_keywords.isnot(None)).all()

    # Stop words
    analytics_stop_words = {
        'saya', 'aku', 'kamu', 'anda', 'dia', 'mereka', 'kita', 'kami',
        'ini', 'itu', 'tersebut', 'begini', 'begitu', 'hanya', 'saja', 'cuma',
        'apa', 'siapa', 'kapan', 'kenapa', 'mengapa', 'bagaimana', 'berapa', 'dimana', 'mana', 'yang',
        'hallo', 'hai', 'halo', 'selamat', 'pagi', 'siang', 'malam', 'sore',
        'jelaskan', 'tampilkan', 'berikan', 'sebutkan', 'cari', 'carikan', 'tolong',
        'analisis', 'buatkan', 'buat', 'analisa', 'lihat', 'lihatkan', 'tunjukkan',
        'minta', 'mohon', 'bantu', 'bantuan', 'butuh', 'dong', 'ya', 'aja',
        'di', 'ke', 'dari', 'pada', 'untuk', 'dengan', 'dan', 'atau', 'tapi', 
        'hingga', 'sampai', 'oleh', 'dalam', 'tentang', 'mengenai', 'per', 'se',
        'data', 'informasi', 'tahun', 'bulan', 'terbaru', 'lebih', 'detail', 'rinci', 
        'lengkap', 'secara', 'terima', 'kasih', 'bentuk', 'menurut',
        'kota', 'kabupaten', 'provinsi', 'daerah', 'wilayah', 'lokasi', 'tempat',
        'gorontalo', 'sulawesi', 'utara', 'selatan', 'barat', 'timur', 'tengah',
        'tabel', 'dokumen', 'file', 'laporan', 'list', 'daftar', 'jumlah', 'total',
        'ntp', 'dll', 'dsb', 'dst', 'yg', 'utk', 'dgn', 'sbg', 'pd', 'dr',
        'besar', 'kecil', 'tinggi', 'rendah', 'banyak', 'sedikit', 'baik', 'buruk',
        'khusus', 'umum', 'sama', 'beda', 'lain', 'semua', 'setiap',
        'sajikan', 'meningkat', 'tren', 'datanya', 'berdasarkan',
        'januari', 'februari', 'maret', 'april', 'mei', 'juni',
        'juli', 'agustus', 'september', 'oktober', 'november', 'desember',
        '2022', '2023', '2024', '2025'
    }

    # Kata yang HARUS dalam compound (tidak boleh standalone)
    must_be_compound = {
        'penduduk', 'kecamatan', 'kelurahan', 'desa',
        'tingkat', 'persentase', 'rasio', 'indeks',
        'statistik', 'analisis', 'laporan'
    }

    # Kata yang boleh standalone (sangat spesifik)
    standalone_allowed = {
        'inflasi', 'deflasi', 'kemiskinan', 'pengangguran',
        'ekspor', 'impor', 'investasi', 'produksi', 'konsumsi',
        'pdrb', 'apbd', 'apbn', 'pariwisata', 'pertanian',
        'perikanan', 'kehutanan', 'pertambangan', 'manufaktur',
        'konstruksi', 'transportasi'
    }

    # Ordered pairs untuk phrase normalization (urutan yang benar)
    phrase_order_rules = {
        ('ekonomi', 'pertumbuhan'): 'pertumbuhan ekonomi',
        ('pertumbuhan', 'ekonomi'): 'pertumbuhan ekonomi',
        ('produksi', 'timur'): None,  # Invalid combination
        ('timur', 'produksi'): None,  # Invalid combination
        ('miskin', 'penduduk'): 'penduduk miskin',
        ('penduduk', 'miskin'): 'penduduk miskin',
        ('tingkat', 'inflasi'): 'tingkat inflasi',
        ('inflasi', 'tingkat'): 'tingkat inflasi',
        ('tingkat', 'kemiskinan'): 'tingkat kemiskinan',
        ('kemiskinan', 'tingkat'): 'tingkat kemiskinan',
        ('tingkat', 'pengangguran'): 'tingkat pengangguran',
        ('pengangguran', 'tingkat'): 'tingkat pengangguran',
    }

    def normalize_phrase(words):
        """Normalisasi urutan kata dalam frasa."""
        if len(words) == 2:
            pair = (words[0], words[1])
            if pair in phrase_order_rules:
                normalized = phrase_order_rules[pair]
                return normalized.split() if normalized else None
        return words

    def is_valid_combination(words):
        """Cek apakah kombinasi kata valid (bukan random combination)."""
        # Cek di phrase_order_rules jika ada yang explicitly invalid
        if len(words) == 2:
            pair = (words[0], words[1])
            if pair in phrase_order_rules and phrase_order_rules[pair] is None:
                return False
        
        # Cek kombinasi yang tidak masuk akal (contoh: "produksi timur")
        # Arah mata angin tidak boleh jadi modifier produksi
        directions = {'utara', 'selatan', 'barat', 'timur', 'tengah'}
        has_direction = any(w in directions for w in words)
        has_production = any(w in ['produksi', 'konsumsi', 'distribusi'] for w in words)
        
        if has_direction and has_production:
            # Kecuali ada kata lain yang memvalidasi (misal: "produksi jawa timur")
            if len(words) < 3:
                return False
        
        return True

    def clean_and_combine_keywords(keywords_list):
        """Membersihkan dan menggabungkan keywords dengan rules yang lebih ketat."""
        if not keywords_list or not isinstance(keywords_list, list):
            return None
        
        keywords_lower = [kw.lower() for kw in keywords_list]
        
        # Filter stop words
        filtered = [kw for kw in keywords_lower if kw not in analytics_stop_words]
        
        if not filtered:
            return None
        
        # Cek apakah ada kata yang valuable
        has_valuable = any(
            kw in standalone_allowed or kw in must_be_compound 
            for kw in filtered
        )
        if not has_valuable:
            return None
        
        # Single keyword: hanya boleh jika dalam standalone_allowed
        if len(filtered) == 1:
            word = filtered[0]
            if word in standalone_allowed and len(word) >= 6:
                return word
            return None  # Single keyword dari must_be_compound tidak boleh
        
        # Multiple keywords: buat compound
        # Prioritaskan valuable keywords
        valuable_words = [w for w in filtered if w in standalone_allowed or w in must_be_compound]
        other_words = [w for w in filtered if w not in standalone_allowed and w not in must_be_compound]
        
        # Ambil max 2-3 kata
        phrase_words = valuable_words[:2]
        if len(phrase_words) < 2 and other_words:
            phrase_words.extend(other_words[:3 - len(phrase_words)])
        
        # Limit to max 3 words
        phrase_words = phrase_words[:3]
        
        if len(phrase_words) < 2:
            return None
        
        # Normalize urutan
        normalized = normalize_phrase(phrase_words)
        if normalized is None:
            return None
        
        # Validasi kombinasi
        if not is_valid_combination(normalized):
            return None
        
        return ' '.join(normalized)

    # Proses semua logs
    phrase_list = []
    for log in all_keywords_logs:
        keywords = log[0]
        phrase = clean_and_combine_keywords(keywords)
        if phrase:
            phrase_list.append(phrase)
    
    # Hitung frekuensi
    phrase_counter = Counter(phrase_list)
    
    # Filter dan deduplikasi
    def phrases_overlap(p1, p2):
        """Cek overlap antara 2 frasa."""
        words1 = set(p1.split())
        words2 = set(p2.split())
        
        if not words1 or not words2:
            return False
        
        overlap = len(words1 & words2)
        min_len = min(len(words1), len(words2))
        
        return overlap / min_len > 0.6  # 60% overlap
    
    # Sort by count desc, then by length desc
    phrase_items = list(phrase_counter.items())
    phrase_items.sort(key=lambda x: (-x[1], -len(x[0])))
    
    selected_phrases = []
    
    for phrase, count in phrase_items:
        if count < 3:  # Minimal 3 kemunculan
            continue
        
        # Cek overlap dengan yang sudah dipilih
        has_overlap = any(
            phrases_overlap(phrase, selected['keyword'].lower()) 
            for selected in selected_phrases
        )
        
        if not has_overlap:
            selected_phrases.append({
                'keyword': phrase.title(),
                'count': count
            })
        
        if len(selected_phrases) >= 12:
            break
    
    top_keywords = sorted(selected_phrases, key=lambda x: x['count'], reverse=True)[:12]

    # --- 3. Tingkat Keberhasilan Pengambilan Data ---
    data_requests_query = PromptLog.query.filter(PromptLog.detected_intent == 'data_request')
    total_data_requests = data_requests_query.count()
    successful_requests = data_requests_query.filter(PromptLog.found_results == True).count()
    retrieval_success_rate = (successful_requests / total_data_requests * 100) if total_data_requests > 0 else 0

    # --- 4. Cakupan Data (Data Coverage) ---
    data_coverage = []
    current_year = datetime.utcnow().year
    for year in range(current_year, current_year - 3, -1):
        year_query = PromptLog.query.filter(cast(PromptLog.extracted_years, TEXT).like(f'%{year}%'))
        total_year_queries = year_query.count()
        successful_year_queries = year_query.filter(PromptLog.found_results == True).count()
        coverage = (successful_year_queries / total_year_queries * 100) if total_year_queries > 0 else 0
        data_coverage.append({'year': year, 'coverage': round(coverage)})

    # --- 5. Performa Layanan ---
    total_feedback = db.session.query(func.count(Feedback.id)).scalar() or 0
    positive_feedback = db.session.query(func.count(Feedback.id)).filter(Feedback.type == 'positive').scalar() or 0
    negative_feedback = total_feedback - positive_feedback
    
    accuracy = (positive_feedback / total_feedback * 100) if total_feedback > 0 else 0
    satisfaction_score = ((positive_feedback * 5) + (negative_feedback * 1)) / total_feedback if total_feedback > 0 else 0

    return jsonify({
        'usageTrends': usage_trends_data,
        'responseTime': {
            'average': round(avg_response_time),
            'distribution': response_time_distribution
        },
        'topKeywords': top_keywords,
        'retrievalSuccessRate': round(retrieval_success_rate, 1),
        'dataCoverage': data_coverage,
        'servicePerformance': {
            'uptime': "99.9%",
            'accuracy': round(accuracy, 1),
            'userSatisfaction': round(satisfaction_score, 1)
        }
    })