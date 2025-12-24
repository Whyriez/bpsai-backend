import os
import json
import hashlib
import logging
import requests
from google import genai
from google.genai import types
import fitz
import re
import time
import hashlib
from datetime import datetime  # ✅ TAMBAHKAN INI
import pytz  # ✅ TAMBAHKAN INI
from typing import List, Dict, Any
from .models import db, PdfDocument, DocumentChunk, GeminiApiKeyConfig
from flask import current_app
from cachetools import TTLCache
import shutil
from .job_utils import check_job_should_stop

logging.basicConfig(level=logging.INFO)

class EmbeddingService:
    def __init__(self):
        self.api_keys = self._load_keys_from_env()
        self.current_key_index = 0
        self.url = None
        self._update_url()
        
        self.cache = TTLCache(maxsize=1000, ttl=3600)

    def reload_keys(self):
        """Memuat ulang API keys dari environment dan mereset state."""
        logging.info("Reloading keys for EmbeddingService...")
        self.api_keys = self._load_keys_from_env()
        self.current_key_index = 0
        self._update_url()
        logging.info(f"Successfully reloaded {len(self.api_keys)} keys.")

    def _load_keys_from_env(self):
        """Load API keys dari environment variables"""
        keys = []
        i = 1
        while True:
            env_key = f"GEMINI_API_KEY_{i}"
            key_value = os.getenv(env_key)
            if not key_value:
                break
            keys.append(key_value)
            i += 1
        
        if not keys:
            old_keys_str = os.getenv('GEMINI_API_KEYS', '')
            old_keys_list = [key.strip() for key in old_keys_str.split(',') if key.strip()]
            keys.extend(old_keys_list)
        
        logging.info(f"Loaded {len(keys)} API keys for EmbeddingService")
        return keys

    def _update_url(self):
        """Update URL dengan API key saat ini"""
        if self.current_key_index < len(self.api_keys):
            current_key = self.api_keys[self.current_key_index]
            self.url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={current_key}"
        else:
            self.url = None
            logging.error("No valid API keys available for EmbeddingService")

    def _rotate_key(self):
        """Rotasi ke API key berikutnya"""
        if self.current_key_index < len(self.api_keys) - 1:
            self.current_key_index += 1
            self._update_url()
            logging.info(f"Rotated to API key index: {self.current_key_index}")
            return True  # ✅ Berhasil rotate
        else:
            logging.error("No more API keys to rotate to")
            self.url = None
            return False  # ✅ Tidak ada key lagi

    def generate(self, text: str) -> list | None:
        if not text:
            return None

        # Check cache
        if text in self.cache:
            logging.info(f"Embedding cache hit for text: '{text[:50]}...'")
            return self.cache[text]

        if not self.api_keys or not self.url:
            logging.error("Embedding generation failed: No API keys available")
            return None
        
        max_key_attempts = len(self.api_keys)  # ✅ Coba semua keys
        retries_per_key = 2  # ✅ Retry per key dikurangi
        backoff_factor = 2
        
        for key_attempt in range(max_key_attempts):  # ✅ Loop untuk setiap key
            for retry in range(retries_per_key):
                try:
                    response = requests.post(
                        self.url,
                        json={
                            'model': 'models/text-embedding-004', 
                            'content': {'parts': [{'text': text}]}
                        },
                        timeout=30
                    )
                    
                    if response.status_code == 429:
                        logging.warning(f"API key {self.current_key_index} quota exceeded (429). Rotating...")
                        if not self._rotate_key():
                            return None  # Semua keys habis
                        break  # Keluar dari retry loop, coba key berikutnya
                    
                    if response.status_code >= 500:
                        logging.warning(f"Server error {response.status_code}. Retrying...")
                        if retry < retries_per_key - 1:
                            wait_time = backoff_factor ** (retry + 1)
                            time.sleep(wait_time)
                            continue
                        else:
                            # Coba key berikutnya
                            if not self._rotate_key():
                                return None
                            break
                    
                    response.raise_for_status() 
                    result = response.json()
                    embedding_values = result.get('embedding', {}).get('values')

                    if embedding_values:
                        self.cache[text] = embedding_values
                        return embedding_values
                    else:
                        logging.error("No embedding values in response")
                        return None
                        
                except requests.exceptions.RequestException as e:
                    logging.error(f"Embedding API request failed: {e}")
                    
                    if retry < retries_per_key - 1:
                        wait_time = backoff_factor ** (retry + 1)
                        logging.warning(f"Retrying in {wait_time} seconds...")
                        time.sleep(wait_time)
                    else:
                        # Coba key berikutnya
                        if not self._rotate_key():
                            return None
                        break
        
        logging.error("All API keys exhausted for embedding generation")
        return None


class GeminiService:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(GeminiService, cls).__new__(cls, *args, **kwargs)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        
        self._initialized = True 
        
        logging.info("Initializing GeminiService Singleton...")
        self.api_keys = self._load_keys_from_env()
        self.current_key_index = 0
        self.client = None
        
        if not self.api_keys:
            logging.error("No Gemini API keys found in environment variables")
            return
        
        self._initialize_client()

    def reload_keys(self):
        """Memuat ulang API keys dari environment dan mereset state."""
        logging.info("Reloading keys for GeminiService...")
        self.api_keys = self._load_keys_from_env()
        self.current_key_index = 0
        self._initialize_client()
        logging.info(f"Successfully reloaded {len(self.api_keys)} keys.")

    def _load_keys_from_env(self):
        """Load API keys dari environment variables dengan lebih robust."""
        keys = []
        gemini_env_keys = {key: value for key, value in os.environ.items() if key.startswith('GEMINI_API_KEY_')}
        sorted_keys = sorted(gemini_env_keys.items(), key=lambda item: int(item[0].split('_')[-1]))
        keys = [value for key, value in sorted_keys]
        
        # Fallback ke format lama
        if not keys:
            old_keys_str = os.getenv('GEMINI_API_KEYS', '')
            old_keys_list = [key.strip() for key in old_keys_str.split(',') if key.strip()]
            keys.extend(old_keys_list)
        
        logging.info(f"Loaded {len(keys)} API keys for GeminiService")
        return keys

    def _get_current_key_config(self):
        """Dapatkan config untuk key yang sedang digunakan"""
        if self.current_key_index >= len(self.api_keys):
            return None
            
        try:
            alias = f"{self.current_key_index + 1}"
            config = GeminiApiKeyConfig.query.filter_by(key_alias=alias).first()
            return config
        except Exception as e:
            logging.error(f"Error getting key config: {e}")
            return None

    def _initialize_client(self):
        """Inisialisasi client dengan API key saat ini"""
        if self.current_key_index >= len(self.api_keys):
            self.client = None
            logging.warning("All Gemini API keys have been exhausted.")
            return False
        
        try:
            # Cek dan reset quota status untuk key yang akan digunakan
            key_config = self._get_current_key_config()
            if key_config:
                key_config.check_and_reset_quota()  # <- TAMBAHKAN INI
                
                # Skip key yang masih quota exceeded
                if key_config.quota_exceeded:
                    logging.warning(f"API key {self.current_key_index} still has quota exceeded. Skipping...")
                    return False
            
            current_key = self.api_keys[self.current_key_index]
            self.client = genai.Client(api_key=current_key)
            
            # Update last_used timestamp di database
            if key_config:
                key_config.last_used = datetime.now(pytz.utc)
                db.session.commit()
                
            logging.info(f"Gemini Client initialized with API key index: {self.current_key_index}")
            return True
            
        except Exception as e:
            self.client = None
            logging.error(f"Failed to initialize Gemini Client with key index {self.current_key_index}: {e}")
            return False

    def _rotate_key(self):
        """Rotasi ke API key berikutnya"""
        try:
            current_key_config = self._get_current_key_config()
            if current_key_config:
                current_key_config.mark_quota_exceeded()
        except Exception as e:
            logging.warning(f"Could not mark quota exceeded in database: {e}")
        
        logging.warning(f"API key at index {self.current_key_index} exceeded quota. Rotating to next key.")
        self.current_key_index += 1
        
        return self._initialize_client()  # ✅ Return hasil inisialisasi

    def stream_generate_content(self, prompt: str):
        """Stream generate content dari Gemini API."""
        max_attempts = len(self.api_keys)
        
        for attempt in range(max_attempts):
            if not self.client:
                if not self._initialize_client():
                    raise Exception("All API keys have exceeded their quota")
            
            try:
                response = self.client.models.generate_content_stream(
                    model='gemini-2.5-flash',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_budget=0)
                    )
                )
                
                # Tandai request sukses
                try:
                    key_config = self._get_current_key_config()
                    if key_config:
                        key_config.mark_successful_request()
                except Exception as db_error:
                    logging.warning(f"Could not mark successful request: {db_error}")
                
                for chunk in response:
                    if hasattr(chunk, 'text') and chunk.text:
                        yield chunk.text
                
                return  # Streaming selesai sukses
                
            except Exception as e:
                # Tandai request gagal
                try:
                    key_config = self._get_current_key_config()
                    if key_config:
                        key_config.mark_failed_request()
                except Exception as db_error:
                    logging.warning(f"Could not mark failed request: {db_error}")
                
                error_str = str(e).lower()
                
                # Deteksi quota/429 error
                if "429" in str(e) or "quota" in error_str or "resource_exhausted" in error_str:
                    logging.warning(f"Quota exceeded for key {self.current_key_index}: {e}")
                    if not self._rotate_key():
                        raise Exception("All API keys have exceeded their quota")
                    continue  # Retry dengan key baru
                
                # Deteksi safety filter
                elif "safety" in error_str or "blocked" in error_str:
                    raise Exception("Content blocked due to safety settings")
                
                # Error lain
                else:
                    logging.error(f"Gemini API error: {e}")
                    raise
        
        raise Exception("All API keys have exceeded their quota")

    def generate_content(self, prompt: str) -> str | None:
        """Generate content tanpa streaming (synchronous)."""
        max_attempts = len(self.api_keys)
        
        for attempt in range(max_attempts):
            if not self.client:
                if not self._initialize_client():
                    return None
            
            try:
                response = self.client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt
                )
                
                # Tandai request sukses
                try:
                    key_config = self._get_current_key_config()
                    if key_config:
                        key_config.mark_successful_request()
                except Exception as db_error:
                    logging.warning(f"Could not mark successful request: {db_error}")
                
                return response.text
                
            except Exception as e:
                # Tandai request gagal
                try:
                    key_config = self._get_current_key_config()
                    if key_config:
                        key_config.mark_failed_request()
                except Exception as db_error:
                    logging.warning(f"Could not mark failed request: {db_error}")
                
                error_str = str(e).lower()
                
                if "429" in str(e) or "quota" in error_str or "resource_exhausted" in error_str:
                    logging.warning(f"Quota exceeded for key {self.current_key_index}: {e}")
                    if not self._rotate_key():
                        return None
                    continue  # Retry dengan key baru
                else:
                    logging.error(f"Error generating content: {e}")
                    return None
        
        return None


class RobustTableDetector:
    def __init__(self):
        self.output_dir = current_app.config['PDF_IMAGES_DIRECTORY']
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
            
    def _save_page_screenshot(self, page: fitz.Page, base_filename: str, page_num: int) -> str | None:
        try:
            document_specific_dir = os.path.join(self.output_dir, base_filename)
            os.makedirs(document_specific_dir, exist_ok=True)

            image_filename = f"page_{page_num}.png"
            output_path = os.path.join(document_specific_dir, image_filename)
            web_accessible_path = f'pdf_images/{base_filename}/{image_filename}'
            
            if os.path.exists(output_path):
                return web_accessible_path

            matrix = fitz.Matrix(2, 2)
            pix = page.get_pixmap(matrix=matrix)
            pix.save(output_path)
            logging.info(f"Screenshot disimpan di: {output_path}")
            
            return web_accessible_path
        except Exception as e:
            logging.error(f"Gagal menyimpan gambar untuk halaman {page_num}: {e}")
            return None

    def _clean_text_for_rag(self, raw_text: str) -> str:
        lines = raw_text.strip().split('\n')
        cleaned_lines = [re.sub(r'\s{2,}', ' ', line.strip()) for line in lines if line.strip()]
        return '\n'.join(cleaned_lines)

    def _is_excluded_page(self, raw_text: str, page_num: int) -> bool:
        lines = raw_text.strip().split('\n')
        first_few_lines = '\n'.join(lines[:5]).lower()
        navigation_titles = [
            r'^\s*daftar\s+isi', r'table\s+of\s+contents', r'^\s*daftar\s+tabel', 
            r'list\s+of\s+tables', r'^\s*daftar\s+gambar', r'list\s+of\s+figures',
            r'^\s*daftar\s+grafik', r'list\s+of\s+graphs', r'^\s*daftar\s+lampiran', 
            r'list\s+of\s+appendices'
        ]
        for title_pattern in navigation_titles:
            if re.search(title_pattern, first_few_lines): return True
        dot_pattern_lines = [line for line in lines if re.search(r'\.{5,}\s*\d+\s*$', line)]
        if len(lines) > 5 and len(dot_pattern_lines) / len(lines) > 0.4: return True
        if re.search(r'^\s*daftar\s+pustaka|references|bibliography|referensi', first_few_lines): return True
        if page_num == 1 and len(lines) < 10: return True
        if len(raw_text.strip()) < 50: return True
        return False

    def _detect_table_keyword(self, text: str) -> tuple[bool, str]:
        text_lower = text.lower()
        table_patterns = [
            r'tabel\s+\d[\d\.]*', r'table\s+\d[\d\.]*', r'lanjutan\s+tabel',
            r'tabel\s+[\d\.]+\s*\(lanjutan\)', r'^tabel$', r'^table$'
        ]
        for pattern in table_patterns:
            if re.search(pattern, text_lower, re.MULTILINE):
                return True, "table_keyword_found"
        return False, "no_table_keyword"

    def _detect_lampiran_keyword(self, text: str) -> tuple[bool, str]:
        first_few_lines = '\n'.join(text.strip().split('\n')[:5]).lower()
        if re.search(r'^\s*lampiran|appendix', first_few_lines):
            return True, "lampiran_keyword_found"
        return False, "no_lampiran_keyword"

    def _detect_column_numbering(self, text: str) -> tuple[bool, str]:
        matches = re.findall(r'\(\s*\d+\s*\)', text)
        if len(set(matches)) >= 2:
            return True, "flexible_column_numbering_found"
        return False, "no_strong_column_numbering"

    def _detect_table_page(self, raw_text: str, page_num: int) -> tuple[bool, str]:
        if self._is_excluded_page(raw_text, page_num):
            return False, "excluded_page"
        has_table_kw, _ = self._detect_table_keyword(raw_text)
        has_lampiran_kw, _ = self._detect_lampiran_keyword(raw_text)
        has_any_keyword = has_table_kw or has_lampiran_kw
        has_structure, _ = self._detect_column_numbering(raw_text)
        if has_any_keyword and has_structure:
            return True, "table_with_structure" if has_table_kw else "lampiran_with_structure"
        if not has_any_keyword and has_structure:
            return True, "structure_only_table"
        if has_any_keyword and not has_structure:
            return False, "keyword_found_but_lacks_structure"
        return False, "no_keyword_and_no_structure"

    def extract_and_label_pages(self, pdf_path: str) -> List[Dict[str, Any]]:
        all_chunks = []
        doc = fitz.open(pdf_path)
        base_filename = os.path.splitext(os.path.basename(pdf_path))[0]
        logging.info(f"Memproses {len(doc)} halaman dari {os.path.basename(pdf_path)}...")
        for page_num, page in enumerate(doc, 1):
            raw_text = page.get_text("text")
            is_table, reason = self._detect_table_page(raw_text, page_num)
            
            content_type = "table" if is_table else "text"
            image_path = None
            if is_table:
                image_path = self._save_page_screenshot(page, base_filename, page_num)

            chunk = {
                "page_number": page_num,
                "content": self._clean_text_for_rag(raw_text),
                "metadata": {
                    "type": content_type,
                    "image_path": image_path,
                    "detection_reason": reason,
                    "is_excluded": reason == "excluded_page"
                }
            }
            all_chunks.append(chunk)
        doc.close()
        return all_chunks


def process_and_save_pdf(pdf_path: str, job_id: int = None, progress_callback=None) -> Dict[str, Any]:
    """
    Memproses PDF dengan strategi Hybrid Chunking:
    1. Halaman Tabel/Gambar -> Disimpan utuh per halaman (agar struktur tabel tidak rusak).
    2. Halaman Teks -> Di-buffer (digabung) lalu di-chunk pakai Sliding Window (agar kalimat utuh).
    """
    base_filename = os.path.splitext(os.path.basename(pdf_path))[0]
    original_filename = os.path.basename(pdf_path)

    document = None
    start_page = 1

    # --- 1. INISIALISASI & CEK DUPLIKASI/RESUME ---
    try:
        file_hash = hashlib.sha256(open(pdf_path, "rb").read()).hexdigest()
        document = PdfDocument.query.filter_by(document_hash=file_hash).first()

        doc_for_pages = fitz.open(pdf_path)
        total_pages = len(doc_for_pages)
        doc_for_pages.close()

        if document:
            # Cek chunk terakhir untuk resume
            last_chunk = DocumentChunk.query.filter_by(document_id=document.id) \
                .order_by(DocumentChunk.page_number.desc()).first()

            if last_chunk:
                if last_chunk.page_number >= total_pages:
                    logging.info(f"Skipping '{original_filename}': Sudah selesai diproses.")
                    return {"status": "skipped", "filename": original_filename,
                            "reason": "Dokumen sudah selesai diproses."}

                # Resume dari halaman berikutnya
                start_page = last_chunk.page_number + 1
                logging.info(f"Resuming '{original_filename}' from page {start_page}.")
            else:
                start_page = 1
        else:
            # Buat entry dokumen baru
            document = PdfDocument(
                filename=original_filename,
                total_pages=total_pages,
                document_hash=file_hash,
                doc_metadata={'source_path': pdf_path}
            )

    except Exception as e:
        logging.error(f"Gagal saat inisialisasi pra-proses untuk {pdf_path}: {e}")
        return {"status": "error", "filename": original_filename, "reason": f"Initialization error: {str(e)}"}

    # --- 2. MULAI PEMROSESAN UTAMA ---
    detector = RobustTableDetector()
    doc = fitz.open(pdf_path)

    # Buffer teks untuk context windowing (FIX FATAL #2)
    text_buffer = ""
    # Menandai dari halaman mana buffer ini dimulai (untuk metadata aproksimasi)
    buffer_start_page = start_page

    for page_num, page in enumerate(doc, 1):
        if page_num < start_page:
            continue

        try:
            # Cek interupsi job (tombol stop)
            if job_id and check_job_should_stop(job_id):
                doc.close()
                logging.info(f"Proses dihentikan oleh pengguna sebelum halaman {page_num}.")
                return {"status": "stopped", "filename": original_filename,
                        "reason": f"Dihentikan oleh pengguna pada halaman {page_num}"}

            if progress_callback:
                progress_callback(message=f"Menganalisis Halaman {page_num}/{total_pages} (File: {original_filename})")

            # Ekstrak Teks & Deteksi Tabel
            raw_text = page.get_text("text")
            is_table, reason = detector._detect_table_page(raw_text, page_num)

            if reason == "excluded_page":
                continue

            # Pastikan dokumen tersimpan di DB sebelum insert chunk
            if not document.id:
                db.session.add(document)
                db.session.flush()

            # --- LOGIKA HYBRID CHUNKING ---

            # KONDISI A: HALAMAN TABEL / GAMBAR PENUH
            # Jika halaman ini tabel, kita harus "flush" (simpan) buffer teks sebelumnya dulu.
            if is_table or reason == "image_only_page":

                # 1. Simpan sisa buffer teks (jika ada) sebelum masuk ke tabel
                if text_buffer:
                    text_chunks = semantic_sliding_window_chunker(text_buffer)
                    for txt_content in text_chunks:
                        chunk_obj = DocumentChunk(
                            document_id=document.id,
                            page_number=buffer_start_page,  # Aproksimasi halaman
                            chunk_content=txt_content,
                            chunk_metadata={"type": "text", "source": "buffered_text"}
                        )
                        db.session.add(chunk_obj)

                    text_buffer = ""  # Reset buffer

                # 2. Simpan Halaman Tabel ini secara UTUH (jangan dipotong sliding window)
                # Agar struktur tabel/gambar tetap terjaga dan bisa direkonstruksi nanti
                image_path = detector._save_page_screenshot(page, base_filename, page_num)

                table_chunk = DocumentChunk(
                    document_id=document.id,
                    page_number=page_num,
                    chunk_content=detector._clean_text_for_rag(raw_text),
                    chunk_metadata={
                        "type": "table" if is_table else "image",
                        "image_path": image_path,
                        "detection_reason": reason,
                        "is_excluded": False
                    }
                )
                db.session.add(table_chunk)
                db.session.commit()  # Commit tabel langsung

                # Reset penanda buffer untuk halaman teks berikutnya
                buffer_start_page = page_num + 1
                continue

                # KONDISI B: HALAMAN TEKS BIASA
            # Jangan simpan dulu! Masukkan ke buffer agar kalimat di akhir halaman bisa nyambung.
            cleaned_text = detector._clean_text_for_rag(raw_text)
            text_buffer += " " + cleaned_text

            # Optimasi: Jika buffer sudah terlalu besar (misal > 3 halaman / 5000 chars),
            # kita proses sebagian untuk menghemat memori, tapi sisakan ujungnya untuk overlap.
            if len(text_buffer) > 5000:
                text_chunks = semantic_sliding_window_chunker(text_buffer)

                # Ambil chunk terakhir untuk dimasukkan kembali ke buffer (agar overlap terjaga)
                if text_chunks:
                    last_chunk_to_keep = text_chunks.pop()

                    for txt_content in text_chunks:
                        chunk_obj = DocumentChunk(
                            document_id=document.id,
                            page_number=page_num,  # Aproksimasi
                            chunk_content=txt_content,
                            chunk_metadata={"type": "text"}
                        )
                        db.session.add(chunk_obj)

                    # Sisanya kembalikan ke buffer
                    text_buffer = last_chunk_to_keep
                    db.session.commit()  # Commit partial

        except Exception as e:
            db.session.rollback()
            doc.close()
            logging.error(f"Gagal memproses halaman {page_num} dari '{pdf_path}': {e}")
            return {"status": "error", "filename": original_filename, "reason": f"Error on page {page_num}: {str(e)}"}

    # --- 3. FLUSH BUFFER TERAKHIR (SANGAT PENTING) ---
    # Setelah loop selesai, kemungkinan masih ada teks tersisa di buffer
    if text_buffer:
        text_chunks = semantic_sliding_window_chunker(text_buffer)
        for txt_content in text_chunks:
            chunk_obj = DocumentChunk(
                document_id=document.id,
                page_number=total_pages,  # Tandai sebagai halaman akhir
                chunk_content=txt_content,
                chunk_metadata={"type": "text", "source": "final_buffer"}
            )
            db.session.add(chunk_obj)
        db.session.commit()

    doc.close()
    return {"status": "success", "filename": original_filename, "pages_chunked": total_pages - start_page + 1}


def semantic_sliding_window_chunker(text: str, chunk_size: int = 1000, overlap: int = 200) -> List[str]:
    """
    Memecah teks menjadi chunk dengan overlap.
    Berusaha memotong di akhir kalimat (titik, tanda tanya, seru) agar konteks utuh.
    """
    if not text:
        return []

    # Bersihkan multiple spasi/newline berlebih
    text = re.sub(r'\s+', ' ', text).strip()

    chunks = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = start + chunk_size

        # Jika sisa teks kurang dari chunk_size, ambil semuanya
        if end >= text_len:
            chunks.append(text[start:])
            break

        # Cari titik pemisah yang baik (titik, tanda tanya, seru) MUNDUR dari posisi 'end'
        last_period = text.rfind('.', start, end)
        last_question = text.rfind('?', start, end)
        last_exclamation = text.rfind('!', start, end)

        # Ambil posisi tanda baca terjauh (paling mendekati akhir chunk)
        split_point = max(last_period, last_question, last_exclamation)

        # Aturan pemotongan:
        # Split point harus valid (!=-1) DAN
        # Split point tidak boleh terlalu di awal (minimal 50% chunk terisi)
        if split_point != -1 and split_point > start + (chunk_size * 0.5):
            end = split_point + 1  # Sertakan tanda bacanya
        else:
            # Jika tidak ada tanda baca yang pas, cari spasi terdekat
            last_space = text.rfind(' ', start, end)
            if last_space != -1:
                end = last_space

        chunk_content = text[start:end].strip()
        if chunk_content:
            chunks.append(chunk_content)

        # Geser pointer start untuk chunk berikutnya
        # Kita mundur sebesar 'overlap' dari posisi end saat ini
        start = max(start + 1, end - overlap)

    return chunks