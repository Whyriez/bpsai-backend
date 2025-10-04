import os
from flask import Blueprint, jsonify, current_app, request 
from flask_jwt_extended import jwt_required, get_jwt
from ..services import process_and_save_pdf, GeminiService
from ..models import db, PdfDocument, DocumentChunk, BatchJob, JobStatus 
from datetime import datetime
from sqlalchemy import cast, String
import threading
import time
import shutil

document_bp = Blueprint('document', __name__, url_prefix='/api/documents')

@document_bp.route('/', methods=['GET'])
# @jwt_required()
def get_all_documents():
    """
    Mengembalikan daftar semua dokumen yang telah diproses untuk ditampilkan
    di halaman utama dashboard.
    """
    try:
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
        search_term = request.args.get('search', None, type=str)
        query = PdfDocument.query

        if search_term:
            # Gunakan ilike untuk pencarian case-insensitive
            query = query.filter(PdfDocument.filename.ilike(f"%{search_term}%"))

        paginated_docs = query.order_by(PdfDocument.created_at.desc()).paginate(
            page=page, 
            per_page=per_page, 
            error_out=False
        )
        
        documents_on_page = paginated_docs.items
        
        results = []
        for doc in documents_on_page:
            table_page_count = sum(1 for chunk in doc.chunks if chunk.chunk_metadata and chunk.chunk_metadata.get('type') == 'table')
            results.append({
                "id": doc.id,
                "filename": doc.filename,
                "link": doc.link,
                "total_pages": doc.total_pages,
                "table_page_count": table_page_count,
                "processed_at": doc.created_at.isoformat()
            })
            
        return jsonify({
            "pagination": {
                "total_items": paginated_docs.total,
                "total_pages": paginated_docs.pages,
                "current_page": paginated_docs.page,
                "per_page": paginated_docs.per_page,
                "has_next": paginated_docs.has_next,
                "has_prev": paginated_docs.has_prev
            },
            "documents": results
        }), 200
    except Exception as e:
        return jsonify({"error": "Gagal mengambil data dokumen", "details": str(e)}), 500

@document_bp.route('/<uuid:document_id>', methods=['PUT'])
# @jwt_required()
def update_document_details(document_id):
    """
    Memperbarui field filename dan/atau link untuk sebuah dokumen.
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

@document_bp.route('/<uuid:document_id>', methods=['DELETE'])
# @jwt_required() # Sangat disarankan untuk mengaktifkan ini untuk keamanan
def delete_document(document_id):
    """
    Menghapus sebuah dokumen, semua chunk, dan folder gambar terkait.
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
    
@document_bp.route('/<uuid:document_id>/tables', methods=['GET'])
# @jwt_required()
def get_document_table_pages(document_id):
    """
    Mengembalikan detail sebuah dokumen beserta daftar halaman yang
    terdeteksi sebagai tabel.
    """
    try:
        # Mengambil dokumen atau mengembalikan error 404 jika tidak ditemukan
        doc = db.get_or_404(PdfDocument, document_id)

        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
        
        table_chunks_query = DocumentChunk.query.filter(
            DocumentChunk.document_id == document_id,
        ).order_by(DocumentChunk.page_number)


        all_table_chunks = [
            chunk for chunk in table_chunks_query.all()
            if chunk.chunk_metadata and chunk.chunk_metadata.get('type') == 'table'
        ]

        total_items = len(all_table_chunks)
        start = (page - 1) * per_page
        end = start + per_page
        paginated_chunks = all_table_chunks[start:end]
        total_pages = (total_items + per_page - 1) // per_page if total_items > 0 else total_pages

        results = {
            "id": doc.id,
            "filename": doc.filename,
            "pagination": {
                "total_items": total_items,
                "total_pages": total_pages,
                "current_page": page,
                "per_page": per_page,
                "has_next": end < total_items,
                "has_prev": start > 0
            },
            "table_pages": [
                {
                    "chunk_id": chunk.id,
                    "page_number": chunk.page_number,
                    "image_path": f"/static/{chunk.chunk_metadata.get('image_path')}" if chunk.chunk_metadata.get('image_path') else None,
                    "detection_reason": chunk.chunk_metadata.get('detection_reason'),
                    "status": "Sudah Direkonstruksi" if chunk.reconstructed_content else "Belum Direkonstruksi"
                } for chunk in paginated_chunks
            ]
        }
        return jsonify(results), 200
    except Exception as e:
        return jsonify({"error": "Gagal mengambil detail dokumen", "details": str(e)}), 500
    

@document_bp.route('/chunk/<uuid:chunk_id>', methods=['GET'])
@jwt_required()
def get_chunk_details(chunk_id):
    """
    Mengembalikan data lengkap dari sebuah chunk untuk ditampilkan di modal.
    """
    try:
        chunk = db.get_or_404(DocumentChunk, chunk_id)
        
        return jsonify({
            "chunk_id": chunk.id,
            "page_number": chunk.page_number,
            "image_path": f"/static/{chunk.chunk_metadata.get('image_path')}" if chunk.chunk_metadata.get('image_path') else None,
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
    Menggunakan prompt yang paling canggih untuk menangani hierarki vertikal dan 
    header yang merentang horizontal (spanning headers).
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
    Menyimpan konten yang sudah direkonstruksi (dan mungkin diedit manual) ke database.
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
    """
    with app.app_context():
        job = BatchJob.query.filter_by(job_name=job_name).first()
        if not job or job.status != JobStatus.RUNNING:
            app.logger.warning(f"PDF Chunking worker for {job_name} started but job is not in RUNNING state.")
            return

        stop_signal_received = False
        
        try:
            pdf_directory = app.config.get('PDF_CHUNK_DIRECTORY')
            pdf_files = [f for f in os.listdir(pdf_directory) if f.lower().endswith('.pdf')]
            total_files = len(pdf_files)

            # PERBAIKAN 1: Gunakan enumerate untuk penghitungan yang akurat
            for i, filename in enumerate(pdf_files, 1):
                current_status = db.session.query(BatchJob.status).filter_by(id=job.id).scalar()
                if current_status == JobStatus.STOPPING:
                    app.logger.info(f"Stop signal for {job_name} detected. Finalizing...")
                    stop_signal_received = True
                    break
                
                # FITUR BARU: Update pesan status sebelum memproses
                update_payload = {
                    'last_error': f"Memproses file {i}/{total_files}: {filename}"
                }
                BatchJob.query.filter_by(id=job.id).update(update_payload)
                db.session.commit()

                pdf_path = os.path.join(pdf_directory, filename)
                try:
                    process_and_save_pdf(pdf_path)
                    
                    # PERBAIKAN 2: Update progress dengan `i`, bukan akumulasi manual
                    BatchJob.query.filter_by(id=job.id).update({'processed_items': i})
                    db.session.commit()

                    app.logger.info(f"Successfully chunked '{filename}'. Progress: {i}/{total_files}")

                except Exception as e:
                    app.logger.error(f"Failed to process '{filename}': {e}")
                
                time.sleep(1) # Jeda singkat

            db.session.refresh(job)
            final_update = {}
            if stop_signal_received:
                final_update['status'] = JobStatus.IDLE
                final_update['last_error'] = "Proses dihentikan oleh pengguna."
            else:
                final_update['status'] = JobStatus.COMPLETED
                final_update['last_error'] = f"Selesai: {job.processed_items} file berhasil diproses."
            
            BatchJob.query.filter_by(id=job.id).update(final_update)
            db.session.commit()

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"A critical error occurred in the PDF chunking worker {job_name}: {e}", exc_info=True)
            job = BatchJob.query.filter_by(job_name=job_name).first()
            if job:
                job.status = JobStatus.FAILED
                job.last_error = f"Error kritis: {str(e)}"
                db.session.commit()

# --- ENDPOINT API BARU UNTUK KONTROL CHUNKING JOB ---
@document_bp.route('/chunking/start', methods=['POST'])
# @jwt_required()
def start_chunking_job():
    job_name = 'pdf_chunking_process'
    job = BatchJob.query.filter_by(job_name=job_name).first()
    if not job:
        job = BatchJob(job_name=job_name)
        db.session.add(job)
    
    if job.status == JobStatus.RUNNING:
        return jsonify({"error": "Proses chunking sudah berjalan."}), 409

    pdf_directory = current_app.config.get('PDF_CHUNK_DIRECTORY')
    if not pdf_directory or not os.path.isdir(pdf_directory):
        return jsonify({"error": "Folder PDF tidak dikonfigurasi atau tidak ditemukan."}), 500

    files_to_process = [f for f in os.listdir(pdf_directory) if f.lower().endswith('.pdf')]
    if not files_to_process:
        return jsonify({"message": "Tidak ada file PDF baru untuk diproses."}), 200

    job.status = JobStatus.RUNNING
    job.total_items = len(files_to_process)
    job.processed_items = 0
    job.started_at = datetime.utcnow()
    job.completed_at = None
    job.last_error = "Memulai proses..." # Pesan awal
    db.session.commit()

    thread = threading.Thread(target=run_pdf_chunking, args=(current_app._get_current_object(), job_name))
    thread.daemon = True
    thread.start()

    return jsonify({"message": "Proses chunking PDF dimulai.", "total_files": len(files_to_process)}), 202

@document_bp.route('/chunking/stop', methods=['POST'])
# @jwt_required()
def stop_chunking_job():
    job_name = 'pdf_chunking_process'
    job = BatchJob.query.filter_by(job_name=job_name).first()
    if not job or job.status not in [JobStatus.RUNNING, JobStatus.STOPPING]:
        return jsonify({"error": "Tidak ada proses chunking yang sedang berjalan untuk dihentikan."}), 404
    
    job.status = JobStatus.STOPPING
    job.last_error = "Mengirim sinyal berhenti..." # Pesan saat menghentikan
    db.session.commit()
    return jsonify({"message": "Sinyal berhenti telah dikirim ke proses chunking."}), 200

@document_bp.route('/chunking/status', methods=['GET'])
# @jwt_required()
def get_chunking_job_status():
    job_name = 'pdf_chunking_process'
    job = BatchJob.query.filter_by(job_name=job_name).first()
    if not job:
        return jsonify({
            "status": JobStatus.IDLE.value, "progress": 0, "total_items": 0,
            "processed_items": 0, "last_error": "Belum ada proses yang berjalan."
        }), 200
    
    # PERBAIKAN 3: Ubah nama field 'last_error' menjadi 'message' untuk kejelasan di frontend
    return jsonify({
        "status": job.status.value, 
        "progress": job.get_progress(),
        "total_items": job.total_items, 
        "processed_items": job.processed_items,
        "message": job.last_error # Mengirim pesan status ke frontend
    }), 200

@document_bp.route('/process-folder', methods=['POST'])
# @jwt_required()
def process_pdf_folder():
    """
    Endpoint untuk memproses semua file PDF dalam folder yang dikonfigurasi.
    Hanya bisa diakses oleh admin.
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
    """Memulai proses rekonstruksi untuk semua tabel yang belum diproses."""
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
    """Mengirim sinyal untuk menghentikan pekerjaan rekonstruksi."""
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
    """Mendapatkan status terkini dari pekerjaan rekonstruksi (untuk UI)."""
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


