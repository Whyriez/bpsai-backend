import os
from flask import Blueprint, jsonify, current_app, request, send_from_directory
from flask_jwt_extended import jwt_required, get_jwt
from ..services import process_and_save_pdf, GeminiService
from ..models import db, PdfDocument, DocumentChunk, BatchJob, JobStatus 
from datetime import datetime, timedelta
from sqlalchemy import cast, String, func
from sqlalchemy.orm import aliased
import threading
import time
from pathlib import Path
import shutil
from urllib.parse import unquote
import traceback
from ..job_utils import check_job_should_stop, cleanup_job_state, update_job_heartbeat

document_bp = Blueprint('document', __name__, url_prefix='/api/documents')

JOB_TIMEOUT_MINUTES = 30
HEARTBEAT_INTERVAL = 10

@document_bp.route('/', methods=['GET'])
# @jwt_required()
def get_all_documents():
    """
    Mengembalikan daftar semua dokumen yang telah diproses (paginasi).
    ---
    tags:
      - Documents
    summary: Mendapatkan daftar semua dokumen (paginasi).
    security:
      - Bearer: []
    parameters:
      - name: page
        in: query
        type: integer
        description: Nomor halaman untuk paginasi.
        default: 1
      - name: per_page
        in: query
        type: integer
        description: Jumlah item per halaman.
        default: 10
      - name: search
        in: query
        type: string
        description: Kata kunci untuk mencari nama file.
    responses:
      200:
        description: Daftar dokumen berhasil diambil.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
      500:
        description: Gagal mengambil data dokumen.
    """
    try:
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
        search_term = request.args.get('search', None, type=str)
        
        # 1. BUAT SUBQUERY untuk menghitung chunk 'table' per dokumen
        # Ini akan membuat query (SELECT document_id, COUNT(*) as table_page_count FROM ... GROUP BY document_id)
        table_counts_sq = db.session.query(
            DocumentChunk.document_id,
            func.count(DocumentChunk.id).label('table_page_count')
        ).filter(
            DocumentChunk.chunk_metadata.op('->>')('type') == 'table'
        ).group_by(DocumentChunk.document_id).subquery()

        # 2. MODIFIKASI QUERY UTAMA untuk mengambil data dari subquery
        # Kita menggunakan outerjoin agar dokumen yang tidak punya tabel (count = 0) tetap muncul
        query = db.session.query(
            PdfDocument,
            # func.coalesce digunakan untuk mengubah hasil NULL (jika tidak ada tabel) menjadi 0
            func.coalesce(table_counts_sq.c.table_page_count, 0).label('calculated_table_count')
        ).outerjoin(
            table_counts_sq, PdfDocument.id == table_counts_sq.c.document_id
        )

        if search_term:
            query = query.filter(PdfDocument.filename.ilike(f"%{search_term}%"))
        
        # Paginate hasil query gabungan
        paginated_results = query.order_by(PdfDocument.created_at.desc()).paginate(
            page=page, 
            per_page=per_page, 
            error_out=False
        )
        
        # 3. UBAH CARA ITERASI karena hasilnya sekarang adalah tuple (PdfDocument, count)
        results = []
        for doc, table_page_count in paginated_results.items:
            results.append({
                "id": doc.id,
                "filename": doc.filename,
                "link": doc.link,
                "total_pages": doc.total_pages,
                "table_page_count": table_page_count, # <-- Gunakan hasil yang sudah dihitung SQL
                "processed_at": doc.created_at.isoformat()
            })
            
        return jsonify({
            "pagination": {
                "total_items": paginated_results.total,
                "total_pages": paginated_results.pages,
                "current_page": paginated_results.page,
                "per_page": paginated_results.per_page,
                "has_next": paginated_results.has_next,
                "has_prev": paginated_results.has_prev
            },
            "documents": results
        }), 200
    except Exception as e:
        # Tambahkan logging untuk debug yang lebih baik
        current_app.logger.error(f"Error fetching document list: {e}", exc_info=True)
        return jsonify({"error": "Gagal mengambil data dokumen", "details": str(e)}), 500

@document_bp.route('/<uuid:document_id>', methods=['PUT'])
# @jwt_required()
def update_document_details(document_id):
    """
    Memperbarui field filename dan/atau link untuk sebuah dokumen.
    ---
    tags:
      - Documents
    summary: Memperbarui detail (filename/link) dokumen.
    security:
      - Bearer: []
    parameters:
      - name: document_id
        in: path
        type: string
        format: uuid
        required: true
        description: ID unik dari dokumen.
      - in: body
        name: body
        description: Data yang akan diperbarui.
        required: true
        schema:
          type: object
          properties:
            filename:
              type: string
              example: "Laporan Inflasi Terbaru.pdf"
            link:
              type: string
              example: "http://bps.go.id/laporan/inflasi.pdf"
    responses:
      200:
        description: Detail dokumen berhasil diperbarui.
      400:
        description: Request body tidak boleh kosong.
      404:
        description: Dokumen tidak ditemukan.
      500:
        description: Gagal memperbarui detail dokumen.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body tidak boleh kosong"}), 400

        doc = db.get_or_404(PdfDocument, document_id)
        
        # Update field jika ada di dalam data request
        if 'filename' in data:
            doc.filename = data['filename']
        if 'link' in data:
            doc.link = data['link']
        
        db.session.commit()
        
        return jsonify({
            "message": f"Detail untuk dokumen '{doc.filename}' berhasil diperbarui.",
            "document": {
                "id": doc.id,
                "filename": doc.filename,
                "link": doc.link
            }
        }), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating details for document {document_id}: {e}")
        return jsonify({"error": "Gagal memperbarui detail dokumen", "details": str(e)}), 500

@document_bp.route('/images/<path:filepath>')
def serve_document_image(filepath):
    """
    Menyajikan file gambar (halaman tabel) dari direktori yang dikonfigurasi.
    ---
    tags:
      - Documents
    summary: Menyajikan gambar halaman dokumen.
    parameters:
      - name: filepath
        in: path
        type: string
        format: path
        required: true
        # PERBAIKAN DILAKUKAN DI SINI: Gunakan tanda kutip ganda
        description: "Path relatif ke file gambar (misal: 'nama_dokumen/page_5.png')."
    responses:
      200:
        description: Mengembalikan file gambar.
      404:
        description: File gambar tidak ditemukan.
      500:
        description: Konfigurasi server error atau gagal menyajikan.
    """
    image_directory = current_app.config.get('PDF_IMAGES_DIRECTORY')
    
    if not image_directory:
        current_app.logger.error("PDF_IMAGES_DIRECTORY tidak diatur dalam konfigurasi.")
        return jsonify({"error": "Konfigurasi server untuk gambar tidak lengkap."}), 500

    # DECODE URL ENCODING (sangat penting untuk spasi dan karakter khusus)
    filepath = unquote(filepath)
    current_app.logger.info(f"[1] Original filepath (after decode): '{filepath}'")

    # --- LOGIKA PEMBERSIHAN PATH ---
    safe_filepath = filepath
    
    # Daftar prefix yang perlu dihapus
    prefixes_to_strip = [
        "data/onlineData/png/",
        "data/pdf_images/",
        "pdf_images/",
        image_directory + "/",
    ]
    
    # Hapus prefix yang ditemukan
    for prefix in prefixes_to_strip:
        if safe_filepath.startswith(prefix):
            safe_filepath = safe_filepath[len(prefix):]
            current_app.logger.info(f"[2] Stripped prefix '{prefix}'. New path: '{safe_filepath}'")
            break
    
    # PENTING: Gunakan forward slash untuk Flask compatibility
    safe_filepath = safe_filepath.replace('\\', '/')
    current_app.logger.info(f"[3] After slash normalization: '{safe_filepath}'")
    
    # Konstruksi full path untuk validasi
    full_path = os.path.join(image_directory, safe_filepath)
    full_path = os.path.abspath(full_path)
    current_app.logger.info(f"[4] Full path to check: '{full_path}'")
    current_app.logger.info(f"[5] File exists: {os.path.isfile(full_path)}")
    
    # Debug: Cek parent directory
    parent_dir = os.path.dirname(full_path)
    current_app.logger.info(f"[6] Parent dir: '{parent_dir}' exists: {os.path.isdir(parent_dir)}")
    
    if os.path.isdir(parent_dir):
        files = os.listdir(parent_dir)
        current_app.logger.info(f"[7] Files in parent: {files}")
    
    # Keamanan: Pastikan path tidak keluar dari image_directory
    if not full_path.startswith(os.path.abspath(image_directory)):
        current_app.logger.warning(f"[SECURITY] Path traversal blocked.")
        return jsonify({"error": "Invalid file path."}), 403

    try:
        # Cek apakah file ada
        if not os.path.isfile(full_path):
            current_app.logger.error(f"[ERROR] File not found: {full_path}")
            return jsonify({
                "error": "File gambar tidak ditemukan.",
                "debug": {
                    "filepath_received": filepath,
                    "safe_filepath": safe_filepath,
                    "full_path": full_path,
                    "parent_exists": os.path.isdir(os.path.dirname(full_path))
                }
            }), 404
        
        current_app.logger.info(f"[SUCCESS] Attempting to serve: directory='{image_directory}', file='{safe_filepath}'")
        
        # Coba langsung dengan full path jika send_from_directory gagal
        return send_from_directory(image_directory, safe_filepath)
        
    except Exception as e:
        current_app.logger.error(f"[EXCEPTION] Error: {e}", exc_info=True)
        
        # Fallback: Coba kirim file secara langsung
        try:
            current_app.logger.info("[FALLBACK] Trying direct file send...")
            from flask import send_file
            return send_file(full_path, mimetype='image/png')
        except Exception as e2:
            current_app.logger.error(f"[FALLBACK FAILED] {e2}")
            return jsonify({"error": "Gagal menyajikan gambar.", "details": str(e)}), 500


    
@document_bp.route('/<uuid:document_id>', methods=['DELETE'])
# @jwt_required() # Sangat disarankan untuk mengaktifkan ini untuk keamanan
def delete_document(document_id):
    """
    Menghapus sebuah dokumen, semua chunk, dan folder gambar terkait.
    ---
    tags:
      - Documents
    summary: Menghapus dokumen dan semua data terkait.
    security:
      - Bearer: []
    parameters:
      - name: document_id
        in: path
        type: string
        format: uuid
        required: true
        description: ID unik dari dokumen yang akan dihapus.
    responses:
      200:
        description: Dokumen berhasil dihapus.
      404:
        description: Dokumen tidak ditemukan.
      500:
        description: Gagal menghapus dokumen.
    """
    try:
        doc = db.get_or_404(PdfDocument, document_id)
    
        filename = doc.filename
        
        # --- LOGIKA BARU UNTUK MENGHAPUS FOLDER GAMBAR ---
        base_filename = os.path.splitext(filename)[0]
        image_dir = current_app.config.get('PDF_IMAGES_DIRECTORY')

        if image_dir:
            doc_image_folder = os.path.join(image_dir, base_filename)
            if os.path.isdir(doc_image_folder):
                try:
                    shutil.rmtree(doc_image_folder)
                    current_app.logger.info(f"Successfully deleted image folder: {doc_image_folder}")
                except Exception as e:
                    # Log error jika gagal hapus folder, tapi proses tetap lanjut
                    current_app.logger.error(f"Failed to delete image folder {doc_image_folder}: {e}")
        # --- AKHIR LOGIKA BARU ---

        db.session.delete(doc)
        db.session.commit()
        
        return jsonify({"message": f"Dokumen '{filename}' dan semua data terkait berhasil dihapus."}), 200
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting document {document_id}: {e}")
        return jsonify({"error": "Gagal menghapus dokumen", "details": str(e)}), 500
    
@document_bp.route('/<uuid:document_id>/pages', methods=['GET'])
# @jwt_required()
def get_document_pages(document_id):
    """
    Mengembalikan daftar halaman/chunk dari sebuah dokumen (paginasi).
    ---
    tags:
      - Documents
    summary: Mendapatkan daftar halaman/chunk dokumen (paginasi).
    security:
      - Bearer: []
    parameters:
      - name: document_id
        in: path
        type: string
        format: uuid
        required: true
        description: ID unik dari dokumen.
      - name: page
        in: query
        type: integer
        description: Nomor halaman untuk paginasi.
        default: 1
      - name: per_page
        in: query
        type: integer
        description: Jumlah item per halaman.
        default: 10
      - name: filter
        in: query
        type: string
        description: Filter berdasarkan tipe chunk.
        enum: ["table", "text"]
        default: "table"
    responses:
      200:
        description: Daftar halaman/chunk berhasil diambil.
      404:
        description: Dokumen tidak ditemukan.
      500:
        description: Gagal mengambil detail halaman.
    """
    try:
        doc = db.get_or_404(PdfDocument, document_id)
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
        filter_type = request.args.get('filter', 'table', type=str) # Default ke 'table'

        query = DocumentChunk.query.filter(DocumentChunk.document_id == document_id)

        if filter_type in ['table', 'text']:
            query = query.filter(DocumentChunk.chunk_metadata.op('->>')('type') == filter_type)
        
        paginated_chunks = query.order_by(DocumentChunk.page_number).paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        pages_data = []
        for chunk in paginated_chunks.items:
            chunk_data = {
                "chunk_id": chunk.id,
                "page_number": chunk.page_number,
                "type": chunk.chunk_metadata.get('type'),
                "status": "Sudah Direkonstruksi" if chunk.reconstructed_content else "Belum Direkonstruksi",
            }
            # PERUBAHAN 1: Sertakan konten teks jika filter adalah 'text'
            if filter_type == 'text':
                chunk_data['chunk_content'] = chunk.chunk_content
                chunk_data['reconstructed_content'] = chunk.reconstructed_content
            else: # Untuk tabel, sertakan path gambar
                 chunk_data['image_path'] = f"/documents/images/{chunk.chunk_metadata.get('image_path')}" if chunk.chunk_metadata.get('image_path') else None

            pages_data.append(chunk_data)

        return jsonify({
            "id": doc.id,
            "filename": doc.filename,
            "pagination": {
                "total_items": paginated_chunks.total,
                "total_pages": paginated_chunks.pages,
                "current_page": paginated_chunks.page,
                "per_page": paginated_chunks.per_page,
                "has_next": paginated_chunks.has_next,
                "has_prev": paginated_chunks.has_prev
            },
            "pages": pages_data
        }), 200
    except Exception as e:
        current_app.logger.error(f"Error getting document pages for {document_id}: {e}")
        return jsonify({"error": "Gagal mengambil detail halaman dokumen", "details": str(e)}), 500
    

@document_bp.route('/chunk/<uuid:chunk_id>', methods=['GET'])
@jwt_required()
def get_chunk_details(chunk_id):
    """
    Mengembalikan data lengkap dari sebuah chunk (untuk modal edit).
    ---
    tags:
      - Documents
    summary: Mendapatkan detail lengkap satu chunk.
    security:
      - Bearer: []
    parameters:
      - name: chunk_id
        in: path
        type: string
        format: uuid
        required: true
        description: ID unik dari chunk.
    responses:
      200:
        description: Detail chunk berhasil diambil.
      404:
        description: Chunk tidak ditemukan.
      500:
        description: Gagal mengambil data chunk.
    """
    try:
        chunk = db.get_or_404(DocumentChunk, chunk_id)
        
        return jsonify({
            "chunk_id": chunk.id,
            "page_number": chunk.page_number,
            "type": chunk.chunk_metadata.get('type'),
            "image_path": f"/api/documents/images/{chunk.chunk_metadata.get('image_path')}" if chunk.chunk_metadata.get('image_path') else None,
            "chunk_content": chunk.chunk_content, # Teks mentah/sebelumnya
            "reconstructed_content": chunk.reconstructed_content # Teks yang sudah bersih (jika ada)
        }), 200
    except Exception as e:
        current_app.logger.error(f"Error fetching chunk details for {chunk_id}: {e}")
        return jsonify({"error": "Gagal mengambil data chunk", "details": str(e)}), 500
    

@document_bp.route('/chunk/<uuid:chunk_id>/reconstruct', methods=['POST'])
@jwt_required()
def reconstruct_chunk_content(chunk_id):
    """
    Memicu rekonstruksi AI untuk satu chunk (untuk testing di modal).
    ---
    tags:
      - Documents
    summary: (AI) Memicu rekonstruksi AI untuk satu chunk.
    security:
      - Bearer: []
    parameters:
      - name: chunk_id
        in: path
        type: string
        format: uuid
        required: true
        description: ID unik dari chunk yang akan direkonstruksi.
    responses:
      200:
        description: Teks berhasil direkonstruksi.
      404:
        description: Chunk tidak ditemukan.
      503:
        description: Layanan AI tidak terkonfigurasi atau kuota habis.
      500:
        description: Gagal memproses.
    """
    try:
        chunk = db.get_or_404(DocumentChunk, chunk_id)
        
        gemini_service = GeminiService()
        if not gemini_service.client:
            return jsonify({"error": "Layanan AI tidak terkonfigurasi atau kuota habis."}), 503

        # --- PROMPT FINAL DENGAN ATURAN SPANNING HEADER ---
        prompt = f"""
        Anda adalah seorang editor dan analis data profesional dengan spesialisasi pada data statistik dari BPS.
        Diberikan teks mentah dari satu halaman penuh sebuah dokumen. Teks ini berisi paragraf penjelasan dan juga bagian tabel yang mungkin tidak terstruktur.

        ## TUGAS UTAMA ANDA:
        Revisi seluruh teks halaman ini dengan tetap mempertahankan semua paragraf penjelasan dan HANYA merekonstruksi bagian tabel mentah menjadi format tabel Markdown yang bersih.

        ## ATURAN WAJIB UNTUK REKONSTRUKSI TABEL:

        **1. PENANGANAN HEADER HIERARKIS (VERTIKAL):**
           - **Prinsip:** Jika header induk mencakup sub-header di bawahnya, GABUNGKAN teks dari header induk ke setiap sub-headernya, dipisahkan oleh tanda hubung (` - `).
           - **Contoh:** Jika header "Angkatan Kerja" mencakup sub-header "Bekerja", dan "Bekerja" mencakup sub-header "Penuh Waktu", maka header kolom finalnya adalah **"Angkatan Kerja - Bekerja - Penuh Waktu"**.
           - **PENTING:** Jangan pernah memperlakukan header tingkat manapun sebagai baris data.

        **2. PENANGANAN HEADER YANG MERENTANG (HORIZONTAL) (SANGAT PENTING):**
           - **Prinsip:** Terkadang, satu header utama (contoh: 'Perubahan') bisa mencakup beberapa kolom di bawahnya (contoh: kolom untuk 'juta orang' dan kolom untuk 'persen').
           - **Instruksi:** Anda WAJIB membuat kolom terpisah untuk setiap sub-kategori tersebut. Gabungkan header utama dengan unit atau sub-kategorinya.
           - **Contoh:** Jika header "Perubahan Feb 2024–Feb 2025" mencakup kolom untuk "juta orang" dan "persen", maka buatlah dua header kolom final: **"Perubahan Feb 2024–Feb 2025 - juta orang"** dan **"Perubahan Feb 2024–Feb 2025 - persen"**.

        **3. PERTAHANKAN TEKS NARASI:**
           Semua teks narasi dan paragraf di luar tabel harus dipertahankan di posisi aslinya. JANGAN mengubah atau menghapusnya.

        **4. HASIL AKHIR:**
           Hasil akhir harus berupa teks halaman lengkap, dengan paragraf utuh dan tabel yang sudah diformat dengan baik sesuai SEMUA aturan di atas.

        --- TEKS MENTAH DARI HALAMAN PDF ---
        {chunk.chunk_content}
        --- AKHIR TEKS MENTAH ---
        """
        
        reconstructed_text = gemini_service.generate_content(prompt)

        if reconstructed_text is None:
            return jsonify({"error": "Gagal mendapatkan respons dari layanan AI."}), 500

        return jsonify({"reconstructed_text": reconstructed_text}), 200

    except Exception as e:
        current_app.logger.error(f"Error reconstructing chunk {chunk_id}: {e}")
        return jsonify({"error": "Terjadi kesalahan saat proses rekonstruksi AI", "details": str(e)}), 500
    
@document_bp.route('/chunk/<uuid:chunk_id>', methods=['PUT'])
@jwt_required()
def update_chunk_content(chunk_id):
    """
    Menyimpan konten chunk yang sudah diedit manual ke database.
    ---
    tags:
      - Documents
    summary: Menyimpan konten chunk yang sudah diedit.
    security:
      - Bearer: []
    parameters:
      - name: chunk_id
        in: path
        type: string
        format: uuid
        required: true
        description: ID unik dari chunk yang akan disimpan.
      - in: body
        name: body
        description: Konten baru yang akan disimpan.
        required: true
        schema:
          type: object
          properties:
            content:
              type: string
              example: "Ini adalah konten baru yang sudah diedit."
    responses:
      200:
        description: Chunk berhasil diperbarui.
      400:
        description: Request body salah (harus ada 'content').
      404:
        description: Chunk tidak ditemukan.
      500:
        description: Gagal menyimpan perubahan.
    """
    try:
        chunk = db.get_or_404(DocumentChunk, chunk_id)
        data = request.get_json()

        if 'content' not in data:
            return jsonify({"error": "Request body harus berisi key 'content'"}), 400

        new_content = data['content']
        
        # PENTING: Update kedua kolom
        # 'reconstructed_content' untuk melacak status
        # 'chunk_content' untuk memicu event listener agar embedding di-update otomatis
        chunk.reconstructed_content = new_content
        chunk.chunk_content = new_content
        
        db.session.commit()
        
        return jsonify({"message": f"Chunk untuk halaman {chunk.page_number} berhasil diperbarui."}), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating chunk {chunk_id}: {e}")
        return jsonify({"error": "Gagal menyimpan perubahan", "details": str(e)}), 500
    

# ===================================================================
# BACKGROUND JOB BARU UNTUK PDF CHUNKING
# ===================================================================
def run_pdf_chunking(app, job_name):
    """
    Worker yang berjalan di background untuk memproses semua PDF di folder.
    Dengan mekanisme interrupt dan laporan kegagalan yang detail.
    """
    job_id = None
    
    with app.app_context():
        try:
            # 1. VALIDASI INITIAL STATE
            job = BatchJob.query.filter_by(job_name=job_name).with_for_update().first()
            if not job or job.status != JobStatus.RUNNING:
                app.logger.warning(f"PDF Chunking worker for {job_name} started but job is not in RUNNING state.")
                return
            
            job_id = job.id
            app.logger.info(f"[CHUNKING] Starting job {job_name} (ID: {job_id})")

            # 2. VALIDASI DIREKTORI
            pdf_directory = app.config.get('PDF_CHUNK_DIRECTORY')
            if not pdf_directory or not os.path.isdir(pdf_directory):
                raise Exception(f"PDF directory not found or invalid: {pdf_directory}")

            pdf_files = [f for f in os.listdir(pdf_directory) if f.lower().endswith('.pdf')]
            total_files = len(pdf_files)

            if total_files == 0:
                app.logger.info(f"[CHUNKING] No PDF files to process.")
                cleanup_job_state(job_name, JobStatus.COMPLETED, "Tidak ada file PDF untuk diproses.")
                return

            app.logger.info(f"[CHUNKING] Found {total_files} PDF files to process.")

            # 3. PROSES SETIAP FILE DENGAN INTERRUPT CHECKING
            processed_count = 0
            error_count = 0
            skipped_count = 0
            
            # <--- PERUBAHAN 1: Tambahkan list untuk menyimpan detail kegagalan --->
            failed_files_details = []

            for i, filename in enumerate(pdf_files, 1):
                try:
                    # Cek stop signal
                    if check_job_should_stop(job_id):
                        app.logger.info(f"[CHUNKING] Stop signal detected before processing file {i}/{total_files}")
                        # Pesan saat berhenti juga bisa lebih informatif
                        stop_msg = f"Proses dihentikan oleh pengguna. Berhasil: {processed_count}, Gagal: {error_count}, Dilewati: {skipped_count}."
                        cleanup_job_state(job_name, JobStatus.IDLE, stop_msg)
                        return

                    status_msg = f"Mempersiapkan file {i}/{total_files}: {filename}"
                    update_job_heartbeat(job_id, message=status_msg)
                    app.logger.info(f"[CHUNKING] Processing {i}/{total_files}: {filename}")

                    pdf_path = os.path.join(pdf_directory, filename)
                    
                    def progress_callback(message: str):
                        update_job_heartbeat(job_id, message=message)
                        app.logger.info(f"[CHUNKING]   -> {message}")
                    
                    result = process_and_save_pdf(
                        pdf_path, 
                        job_id,
                        progress_callback=progress_callback
                    )

                    # <--- PERUBAHAN 2: Logika untuk mencatat status setiap file --->
                    status = result.get("status")
                    reason = result.get("reason", "Unknown error")

                    if status == "stopped":
                        app.logger.info(f"[CHUNKING] Processing of {filename} was stopped by user.")
                        stop_msg = f"Proses dihentikan pada file '{filename}'. Berhasil: {processed_count}, Gagal: {error_count}, Dilewati: {skipped_count}."
                        cleanup_job_state(job_name, JobStatus.IDLE, stop_msg)
                        return
                    elif status == "success":
                        processed_count += 1
                        app.logger.info(f"[CHUNKING] ✓ Success: {filename}")
                    elif status == "skipped":
                        skipped_count += 1
                        app.logger.info(f"[CHUNKING] ○ Skipped: {filename} - {reason}")
                    else: # "error" atau "failed"
                        error_count += 1
                        # Simpan nama file dan alasan kegagalan
                        failed_files_details.append(f"- {filename}: {reason}")
                        app.logger.error(f"[CHUNKING] ✗ Failed: {filename} - {reason}")
                    
                    update_job_heartbeat(job_id, processed_count=(processed_count + skipped_count + error_count))
                    time.sleep(0.5)

                except Exception as iter_error:
                    error_count += 1
                    error_msg = f"Error kritis pada iterasi '{filename}': {str(iter_error)}"
                    failed_files_details.append(f"- {filename}: {error_msg}")
                    app.logger.error(f"[CHUNKING] ✗ {error_msg}")
                    app.logger.error(traceback.format_exc())

            # 4. FINALISASI DENGAN PESAN YANG LEBIH DETAIL
            # <--- PERUBAHAN 3: Buat pesan akhir yang informatif --->
            final_status = JobStatus.COMPLETED
            final_msg = f"Selesai. Berhasil: {processed_count}, Gagal: {error_count}, Dilewati: {skipped_count} dari total {total_files} file."

            if error_count > 0:
                # Jika ada error, ubah status dan tambahkan detail kegagalan
                final_status = JobStatus.FAILED if processed_count == 0 else JobStatus.COMPLETED
                final_msg = f"Selesai dengan peringatan. Berhasil: {processed_count}, Gagal: {error_count}, Dilewati: {skipped_count}."
                
                # Gabungkan detail kegagalan menjadi satu string
                failures_string = "\n".join(failed_files_details)
                final_msg += f"\n\nDetail Kegagalan:\n{failures_string}"

            cleanup_job_state(job_name, final_status, final_msg)
            app.logger.info(f"[CHUNKING] Job completed: {final_msg}")

        except Exception as e:
            error_trace = traceback.format_exc()
            app.logger.error(f"[CHUNKING] FATAL ERROR in worker: {e}")
            app.logger.error(error_trace)
            cleanup_job_state(job_name, JobStatus.FAILED, f"Error fatal: {str(e)[:200]}")

        finally:
            try:
                db.session.remove()
            except:
                pass

# --- ENDPOINT API BARU UNTUK KONTROL CHUNKING JOB ---
@document_bp.route('/chunking/start', methods=['POST'])
# @jwt_required()
def start_chunking_job():
    """
    Memulai background job untuk memproses semua PDF di folder.
    ---
    tags:
      - Document Jobs (Chunking)
    summary: Memulai background job pemrosesan PDF.
    security:
      - Bearer: []
    responses:
      202:
        description: Proses chunking PDF dimulai.
      200:
        description: Tidak ada file PDF baru untuk diproses.
      409:
        description: Proses chunking sudah berjalan.
      500:
        description: "Gagal memulai proses (misal: folder tidak ada)."
    """
    job_name = 'pdf_chunking_process'
    
    try:
        job = BatchJob.query.filter_by(job_name=job_name).with_for_update().first()
        
        # BUAT JOB BARU JIKA BELUM ADA
        if not job:
            job = BatchJob(job_name=job_name)
            db.session.add(job)
            db.session.flush()
        
        # CEK APAKAH JOB STUCK (RUNNING TAPI SUDAH LAMA TIDAK UPDATE)
        if job.status == JobStatus.RUNNING:
            if job.last_updated:
                time_since_update = datetime.utcnow() - job.last_updated
                if time_since_update > timedelta(minutes=JOB_TIMEOUT_MINUTES):
                    app_logger = current_app.logger
                    app_logger.warning(f"[CHUNKING] Job stuck detected! Last update: {job.last_updated}. Resetting...")
                    
                    # RESET JOB YANG STUCK
                    job.status = JobStatus.IDLE
                    job.last_error = f"Job direset karena stuck (tidak ada update sejak {job.last_updated})"
                    db.session.commit()
                else:
                    return jsonify({
                        "error": "Proses chunking sudah berjalan.",
                        "last_update": job.last_updated.isoformat(),
                        "progress": f"{job.processed_items}/{job.total_items}"
                    }), 409
            else:
                return jsonify({"error": "Proses chunking sudah berjalan."}), 409

        # VALIDASI DIREKTORI
        pdf_directory = current_app.config.get('PDF_CHUNK_DIRECTORY')
        if not pdf_directory or not os.path.isdir(pdf_directory):
            return jsonify({"error": "Folder PDF tidak dikonfigurasi atau tidak ditemukan."}), 500

        # HITUNG FILE YANG AKAN DIPROSES
        files_to_process = [f for f in os.listdir(pdf_directory) if f.lower().endswith('.pdf')]
        if not files_to_process:
            return jsonify({"message": "Tidak ada file PDF baru untuk diproses."}), 200

        # INISIALISASI JOB
        job.status = JobStatus.RUNNING
        job.total_items = len(files_to_process)
        job.processed_items = 0
        job.started_at = datetime.utcnow()
        job.last_updated = datetime.utcnow()  # TAMBAHKAN HEARTBEAT TIMESTAMP
        job.completed_at = None
        job.last_error = "Memulai proses chunking..."
        db.session.commit()

        # JALANKAN WORKER THREAD
        thread = threading.Thread(
            target=run_pdf_chunking, 
            args=(current_app._get_current_object(), job_name),
            name=f"chunking-worker-{job.id}"
        )
        thread.daemon = True
        thread.start()

        return jsonify({
            "message": "Proses chunking PDF dimulai.",
            "job_id": job.id,
            "total_files": len(files_to_process)
        }), 202

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error starting chunking job: {e}")
        return jsonify({"error": "Gagal memulai proses chunking", "details": str(e)}), 500

@document_bp.route('/chunking/stop', methods=['POST'])
# @jwt_required()
def stop_chunking_job():
    """
    Mengirim sinyal berhenti ke background job chunking.
    ---
    tags:
      - Document Jobs (Chunking)
    summary: Menghentikan background job pemrosesan PDF.
    security:
      - Bearer: []
    responses:
      200:
        description: Sinyal berhenti telah dikirim.
      400:
        description: Tidak ada proses yang berjalan.
      404:
        description: Job tidak ditemukan.
      500:
        description: Gagal menghentikan proses.
    """
    job_name = 'pdf_chunking_process'
    
    try:
        job = BatchJob.query.filter_by(job_name=job_name).with_for_update().first()
        
        if not job:
            return jsonify({"error": "Job tidak ditemukan."}), 404
        
        if job.status not in [JobStatus.RUNNING, JobStatus.STOPPING]:
            return jsonify({
                "error": f"Tidak ada proses yang berjalan. Status saat ini: {job.status.value}"
            }), 400
        
        # SET STATUS STOPPING
        job.status = JobStatus.STOPPING
        job.last_error = "Mengirim sinyal berhenti..."
        job.last_updated = datetime.utcnow()
        db.session.commit()
        
        return jsonify({
            "message": "Sinyal berhenti telah dikirim. Proses akan berhenti setelah file saat ini selesai."
        }), 200
    
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error stopping chunking job: {e}")
        return jsonify({"error": "Gagal menghentikan proses", "details": str(e)}), 500

@document_bp.route('/chunking/status', methods=['GET'])
# @jwt_required()
def get_chunking_job_status():
    """
    Mendapatkan status terkini dari background job chunking.
    ---
    tags:
      - Document Jobs (Chunking)
    summary: Mendapatkan status job pemrosesan PDF.
    security:
      - Bearer: []
    responses:
      200:
        description: Status job saat ini.
      500:
        description: Gagal mengambil status.
    """
    job_name = 'pdf_chunking_process'
    
    try:
        job = BatchJob.query.filter_by(job_name=job_name).first()
        
        if not job:
            return jsonify({
                "status": JobStatus.IDLE.value,
                "progress": 0,
                "total_items": 0,
                "processed_items": 0,
                "message": "Belum ada proses yang berjalan.",
                "is_stuck": False
            }), 200
        
        # DETEKSI STUCK
        is_stuck = False
        stuck_duration = None
        
        if job.status == JobStatus.RUNNING and job.last_updated:
            time_since_update = datetime.utcnow() - job.last_updated
            if time_since_update > timedelta(minutes=JOB_TIMEOUT_MINUTES):
                is_stuck = True
                stuck_duration = int(time_since_update.total_seconds() / 60)
        
        return jsonify({
            "status": job.status.value,
            "progress": job.get_progress(),
            "total_items": job.total_items,
            "processed_items": job.processed_items,
            "message": job.last_error,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "last_updated": job.last_updated.isoformat() if job.last_updated else None,
            "is_stuck": is_stuck,
            "stuck_duration_minutes": stuck_duration
        }), 200
    
    except Exception as e:
        current_app.logger.error(f"Error getting chunking status: {e}")
        return jsonify({"error": "Gagal mengambil status", "details": str(e)}), 500
    
@document_bp.route('/chunking/reset', methods=['POST'])
# @jwt_required()
def reset_stuck_job():
    """
    Mereset job chunking yang macet (stuck) secara manual.
    ---
    tags:
      - Document Jobs (Chunking)
    summary: Mereset job pemrosesan PDF yang macet.
    security:
      - Bearer: []
    responses:
      200:
        description: Job berhasil direset.
      404:
        description: Job tidak ditemukan.
      500:
        description: Gagal mereset job.
    """
    job_name = 'pdf_chunking_process'
    
    try:
        job = BatchJob.query.filter_by(job_name=job_name).with_for_update().first()
        
        if not job:
            return jsonify({"error": "Job tidak ditemukan."}), 404
        
        # FORCE RESET KE IDLE
        old_status = job.status.value
        job.status = JobStatus.IDLE
        job.last_error = f"Job direset secara manual dari status {old_status} pada {datetime.utcnow()}"
        job.completed_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({
            "message": f"Job berhasil direset dari status {old_status} ke IDLE.",
            "previous_progress": f"{job.processed_items}/{job.total_items}"
        }), 200
    
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error resetting job: {e}")
        return jsonify({"error": "Gagal mereset job", "details": str(e)}), 500

@document_bp.route('/process-folder', methods=['POST'])
# @jwt_required()
def process_pdf_folder():
    """
    (DEPRECATED) Memproses semua PDF secara sinkron (blocking).
    Gunakan /chunking/start untuk proses background.
    ---
    tags:
      - Document Jobs (Chunking)
    summary: (DEPRECATED) Memproses folder PDF secara sinkron.
    security:
      - Bearer: []
    responses:
      200:
        description: Proses sinkron selesai.
      500:
        description: Folder tidak ditemukan atau error.
    """
    # Proteksi route: hanya admin yang boleh menjalankan proses ini
    # claims = get_jwt()
    # if claims.get('role') != 'admin':
    #     return jsonify({"msg": "Akses ditolak: Diperlukan hak admin"}), 403

    # Ambil path folder dari konfigurasi aplikasi
    pdf_directory = current_app.config.get('PDF_CHUNK_DIRECTORY')

    if not pdf_directory or not os.path.isdir(pdf_directory):
        return jsonify({"error": f"Folder PDF '{pdf_directory}' tidak ditemukan atau bukan direktori."}), 500

    processing_results = []
    
    # Iterasi semua file di dalam direktori
    for filename in os.listdir(pdf_directory):
        if filename.lower().endswith('.pdf'):
            pdf_path = os.path.join(pdf_directory, filename)
            try:
                # DIUBAH: Panggil fungsi 'process_and_save_pdf' secara langsung.
                result = process_and_save_pdf(pdf_path)
                processing_results.append(result)
            except Exception as e:
                processing_results.append({
                    "status": "error", 
                    "filename": filename, 
                    "reason": f"Terjadi kesalahan tak terduga: {str(e)}"
                })

    return jsonify({
        "message": "Proses chunking selesai.",
        "results": processing_results
    })


# ===================================================================
# FUNGSI UNTUK PROSES REKONSTRUKSI DI BACKGROUND
# ===================================================================
def run_batch_reconstruction(app, job_name, document_id):
    """
    Fungsi ini berjalan di thread terpisah untuk melakukan rekonstruksi
    tanpa memblokir response API.
    """
    with app.app_context():
        # Dapatkan pekerjaan dari DB
        job = BatchJob.query.filter_by(job_name=job_name).first()
        if not job or job.status != JobStatus.RUNNING:
            app.logger.warning(f"Batch reconstruction thread for {job_name} started but job is not in RUNNING state.")
            return

        try:
            # Ambil semua ID chunk yang perlu diproses
            chunks_to_process = db.session.query(DocumentChunk.id).filter(
                DocumentChunk.document_id == document_id, 
                DocumentChunk.chunk_metadata.op('->>')('type') == 'table',
                DocumentChunk.reconstructed_content == None
            ).order_by(DocumentChunk.page_number).all()
            
            # Ekstrak ID dari tuple
            chunk_ids = [c[0] for c in chunks_to_process]

            gemini_service = GeminiService()

            for i, chunk_id in enumerate(chunk_ids):
                # 1. Cek status di setiap iterasi, apakah ada perintah berhenti
                db.session.refresh(job) # Ambil status terbaru dari DB
                if job.status == JobStatus.STOPPING:
                    app.logger.info(f"Stop signal received for job {job_name}. Breaking loop.")
                    break

                # 2. Ambil chunk yang akan diproses
                chunk = db.session.get(DocumentChunk, chunk_id)
                if not chunk:
                    continue

                try:
                    # Ini adalah prompt yang sama dengan yang Anda miliki sebelumnya
                    prompt = f"""
                    Anda adalah seorang editor dan analis data profesional dengan spesialisasi pada data statistik dari BPS.
                    Diberikan teks mentah dari satu halaman penuh sebuah dokumen. Teks ini berisi paragraf penjelasan dan juga bagian tabel yang mungkin tidak terstruktur.

                    ## TUGAS UTAMA ANDA:
                    Revisi seluruh teks halaman ini dengan tetap mempertahankan semua paragraf penjelasan dan HANYA merekonstruksi bagian tabel mentah menjadi format tabel Markdown yang bersih.

                    ## ATURAN WAJIB UNTUK REKONSTRUKSI TABEL:

                    **1. PENANGANAN HEADER HIERARKIS (VERTIKAL):**
                       - **Prinsip:** Jika header induk mencakup sub-header di bawahnya, GABUNGKAN teks dari header induk ke setiap sub-headernya, dipisahkan oleh tanda hubung (` - `).
                       - **Contoh:** Jika header "Angkatan Kerja" mencakup sub-header "Bekerja", dan "Bekerja" mencakup sub-header "Penuh Waktu", maka header kolom finalnya adalah **"Angkatan Kerja - Bekerja - Penuh Waktu"**.
                       - **PENTING:** Jangan pernah memperlakukan header tingkat manapun sebagai baris data.

                    **2. PENANGANAN HEADER YANG MERENTANG (HORIZONTAL) (SANGAT PENTING):**
                       - **Prinsip:** Terkadang, satu header utama (contoh: 'Perubahan') bisa mencakup beberapa kolom di bawahnya (contoh: kolom untuk 'juta orang' dan kolom untuk 'persen').
                       - **Instruksi:** Anda WAJIB membuat kolom terpisah untuk setiap sub-kategori tersebut. Gabungkan header utama dengan unit atau sub-kategorinya.
                       - **Contoh:** Jika header "Perubahan Feb 2024–Feb 2025" mencakup kolom untuk "juta orang" dan "persen", maka buatlah dua header kolom final: **"Perubahan Feb 2024–Feb 2025 - juta orang"** dan **"Perubahan Feb 2024–Feb 2025 - persen"**.

                    **3. PERTAHANKAN TEKS NARASI:**
                       Semua teks narasi dan paragraf di luar tabel harus dipertahankan di posisi aslinya. JANGAN mengubah atau menghapusnya.

                    **4. HASIL AKHIR:**
                       Hasil akhir harus berupa teks halaman lengkap, dengan paragraf utuh dan tabel yang sudah diformat dengan baik sesuai SEMUA aturan di atas.

                    --- TEKS MENTAH DARI HALAMAN PDF ---
                    {chunk.chunk_content}
                    --- AKHIR TEKS MENTAH ---
                    """
                    
                    if not gemini_service.client:
                        raise Exception("Layanan AI tidak tersedia atau semua kuota API habis.")

                    reconstructed_text = gemini_service.generate_content(prompt)
                    
                    if reconstructed_text:
                        chunk.reconstructed_content = reconstructed_text
                        chunk.chunk_content = reconstructed_text # Update konten utama agar embedding diperbarui
                    db.session.commit()

                    current_processed_items = job.processed_items + 1
                    BatchJob.query.filter_by(id=job.id).update({'processed_items': current_processed_items})
                    db.session.commit() # Commit HANYA untuk update progress

                    app.logger.info(f"Successfully reconstructed chunk {chunk_id} ({current_processed_items}/{job.total_items})")

                except Exception as e:
                    # Jika gagal di satu chunk, hentikan seluruh pekerjaan
                    error_msg = f"Failed on chunk {chunk_id}: {str(e)}"
                    app.logger.error(f"Stopping batch reconstruction. {error_msg}")
                    job.status = JobStatus.FAILED
                    job.last_error = error_msg
                    db.session.commit()
                    return # Keluar dari fungsi worker

                time.sleep(1) # Beri jeda 1 detik untuk menghindari rate limit API

            # PERBAIKAN BAGIAN 3: Logika final setelah loop selesai
            # Refresh sekali lagi untuk mendapatkan state job paling akhir
            db.session.refresh(job)

            if job.status == JobStatus.STOPPING:
                job.status = JobStatus.IDLE
                app.logger.info(f"Batch reconstruction for {job_name} has been successfully stopped.")
            
            elif job.status == JobStatus.RUNNING:
                # Jika loop selesai secara alami (tidak di-break), maka pekerjaan selesai
                job.status = JobStatus.COMPLETED
                job.completed_at = datetime.utcnow()
                app.logger.info(f"Batch reconstruction for {job_name} completed successfully.")
            
            # Commit terakhir untuk menyimpan perubahan status final (IDLE atau COMPLETED)
            db.session.commit()

        except Exception as e:
            app.logger.error(f"A critical error occurred in the batch worker for job {job_name}: {str(e)}")
            # Pastikan job diambil lagi dari sesi baru jika ada error tak terduga
            job = BatchJob.query.filter_by(job_name=job_name).first()
            if job:
                job.status = JobStatus.FAILED
                job.last_error = str(e)
                db.session.commit()

# ===================================================================
# ENDPOINT API UNTUK KONTROL BATCH RECONSTRUCTION
# ===================================================================

@document_bp.route('/reconstruct/start/<uuid:document_id>', methods=['POST'])
@jwt_required()
def start_batch_reconstruction(document_id):
    """
    Memulai background job rekonstruksi AI untuk semua tabel di dokumen.
    ---
    tags:
      - Document Jobs (Reconstruction)
    summary: (AI) Memulai background job rekonstruksi untuk dokumen.
    security:
      - Bearer: []
    parameters:
      - name: document_id
        in: path
        type: string
        format: uuid
        required: true
        description: ID unik dari dokumen yang akan direkonstruksi.
    responses:
      202:
        description: Proses rekonstruksi dimulai.
      200:
        description: Tidak ada tabel yang perlu direkonstruksi.
      409:
        description: Proses rekonstruksi sudah berjalan.
    """
    job_name = f"reconstruction_doc_{document_id}"
    job = BatchJob.query.filter_by(job_name=job_name).first()
    if not job:
        job = BatchJob(job_name=job_name)
        db.session.add(job)

    if job.status == JobStatus.RUNNING:
        return jsonify({"error": "Pekerjaan rekonstruksi massal sudah berjalan."}), 409

    # Hitung total tabel yang perlu direkonstruksi
    chunks_to_process = DocumentChunk.query.filter(
        DocumentChunk.document_id == document_id,
        DocumentChunk.chunk_metadata.op('->>')('type') == 'table',
        DocumentChunk.reconstructed_content == None
    ).count()

    if chunks_to_process == 0:
        job.status = JobStatus.COMPLETED
        job.total_items = 0
        job.processed_items = 0
        db.session.commit()
        return jsonify({"message": "Tidak ada tabel yang perlu direkonstruksi."}), 200

    # Update status pekerjaan di DB
    job.status = JobStatus.RUNNING
    job.total_items = chunks_to_process
    job.processed_items = 0
    job.started_at = datetime.utcnow()
    job.completed_at = None
    job.last_error = None
    db.session.commit()

    # Jalankan proses di background thread
    thread = threading.Thread(target=run_batch_reconstruction, args=(current_app._get_current_object(), job_name, document_id))
    thread.daemon = True
    thread.start()

    return jsonify({
        "message": f"Proses rekonstruksi untuk dokumen {document_id} dimulai.",
        "total_items": chunks_to_process
    }), 202

@document_bp.route('/reconstruct/stop/<uuid:document_id>', methods=['POST'])
@jwt_required()
def stop_batch_reconstruction(document_id):
    """
    Mengirim sinyal berhenti ke background job rekonstruksi.
    ---
    tags:
      - Document Jobs (Reconstruction)
    summary: (AI) Menghentikan background job rekonstruksi.
    security:
      - Bearer: []
    parameters:
      - name: document_id
        in: path
        type: string
        format: uuid
        required: true
        description: ID unik dari dokumen yang prosesnya akan dihentikan.
    responses:
      200:
        description: Sinyal berhenti telah dikirim.
      404:
        description: Tidak ada pekerjaan yang sedang berjalan.
    """
    job_name = f"reconstruction_doc_{document_id}"
    job = BatchJob.query.filter_by(job_name=job_name).first()
    
    if not job or job.status != JobStatus.RUNNING:
        return jsonify({"error": "Tidak ada pekerjaan yang sedang berjalan untuk dihentikan."}), 404

    job.status = JobStatus.STOPPING
    db.session.commit()

    return jsonify({"message": "Sinyal berhenti telah dikirim."}), 200

@document_bp.route('/reconstruct/status/<uuid:document_id>', methods=['GET'])
@jwt_required()
def get_batch_reconstruction_status(document_id):
    """
    Mendapatkan status terkini dari job rekonstruksi.
    ---
    tags:
      - Document Jobs (Reconstruction)
    summary: (AI) Mendapatkan status job rekonstruksi.
    security:
      - Bearer: []
    parameters:
      - name: document_id
        in: path
        type: string
        format: uuid
        required: true
        description: ID unik dari dokumen yang statusnya dicek.
    responses:
      200:
        description: Status job saat ini.
    """
    job_name = f"reconstruction_doc_{document_id}"
    job = BatchJob.query.filter_by(job_name=job_name).first()

    if not job:
        # Jika belum ada job sama sekali, kirim status default
        return jsonify({
            "status": JobStatus.IDLE.value,
            "progress": 0,
            "total_items": 0,
            "processed_items": 0,
            "last_error": None
        }), 200
        
    return jsonify({
        "status": job.status.value,
        "progress": job.get_progress(),
        "total_items": job.total_items,
        "processed_items": job.processed_items,
        "last_error": job.last_error
    }), 200


