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
from typing import List, Dict, Any
from .models import db, PdfDocument, DocumentChunk
from flask import current_app
from cachetools import TTLCache
import shutil
from .job_utils import check_job_should_stop

logging.basicConfig(level=logging.INFO)

class EmbeddingService:
    def __init__(self):
        api_keys_str = os.getenv('GEMINI_API_KEYS', "")
        api_keys_list = [key.strip() for key in api_keys_str.split(',') if key.strip()]

        if not api_keys_list:
            self.api_key = None
            logging.error("GEMINI_API_KEYS tidak diatur atau kosong di .env")
        else:
            self.api_key = api_keys_list[0]
        
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={self.api_key}"
  
        self.cache = TTLCache(maxsize=1000, ttl=3600)
        # =========================================================

    def generate(self, text: str) -> list | None:
        if text in self.cache:
            logging.info(f"Embedding cache hit for text: '{text[:50]}...'")
            return self.cache[text]

        if not text or not self.api_key:
            if not self.api_key:
                logging.error("Embedding generation failed: API key is None.")
            return None
        
        # --- AWAL DARI LOGIKA RETRY ---
        retries = 3
        backoff_factor = 2
        
        for i in range(retries):
            try:
                response = requests.post(
                    self.url,
                    json={'model': 'models/text-embedding-004', 'content': {'parts': [{'text': text}]}},
                    timeout=20
                )
                response.raise_for_status() 
                result = response.json()
                embedding_values = result.get('embedding', {}).get('values')

                # Logika untuk menyimpan ke cache
                if embedding_values:
                    self.cache[text] = embedding_values
                
                return embedding_values
                
            except requests.exceptions.RequestException as e:
                if isinstance(e, requests.exceptions.HTTPError) and 500 <= e.response.status_code < 600:
                    wait_time = backoff_factor ** (i + 1)
                    logging.warning(f"Gemini API error ({e.response.status_code}). Mencoba lagi dalam {wait_time} detik...")
                    time.sleep(wait_time)
                    continue 
                else:
                    logging.error(f"Gemini Embedding API Error (tidak bisa di-retry): {e}")
                    return None 
        
        logging.error(f"Gagal mengambil embedding dari Gemini API setelah {retries} kali percobaan.")
        return None

class GeminiService:
    """
    Service sederhana untuk berkomunikasi dengan Gemini API.
    Mengelola API key rotation dan streaming response.
    """
    def __init__(self):
        self.api_keys = [key.strip() for key in os.getenv("GEMINI_API_KEYS", "").split(',') if key.strip()]
        self.current_key_index = 0
        self.client = None
        
        if not self.api_keys:
            logging.error("GEMINI_API_KEYS environment variable is not set or is empty.")
            return
        
        self._initialize_client()

    def _initialize_client(self):
        """Inisialisasi client dengan API key saat ini"""
        if self.current_key_index >= len(self.api_keys):
            self.client = None
            logging.warning("All Gemini API keys have exceeded their quota.")
            return
        
        try:
            current_key = self.api_keys[self.current_key_index]
            self.client = genai.Client(api_key=current_key)
            logging.info(f"Gemini Client initialized with API key index: {self.current_key_index}")
        except Exception as e:
            self.client = None
            logging.error(f"Failed to initialize Gemini Client with key index {self.current_key_index}: {e}")

    def _rotate_key(self):
        """Rotasi ke API key berikutnya"""
        logging.warning(f"API key at index {self.current_key_index} exceeded quota. Rotating to next key.")
        self.current_key_index += 1
        self._initialize_client()

    def stream_generate_content(self, prompt: str):
        """
        Stream generate content dari Gemini API.
        Yields raw chunk objects dari Gemini - biarkan caller yang handle formatting.
        
        Raises:
            StopIteration: Ketika streaming selesai
            Exception: Untuk error lainnya (quota, safety, dll)
        """
        while self.current_key_index < len(self.api_keys):
            if not self.client:
                raise Exception("All API keys have exceeded their quota")
            
            try:
                # Disable thinking untuk RAG - context sudah lengkap dari database
                from google.genai import types
                
                response = self.client.models.generate_content_stream(
                    model='gemini-2.5-flash',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_budget=0)
                    )
                )
                
                for chunk in response:
                    if hasattr(chunk, 'text') and chunk.text:
                        yield chunk.text
                
                return  # Streaming selesai sukses
                
            except Exception as e:
                error_str = str(e).lower()
                
                # Deteksi quota/429 error - rotate key dan retry
                if "429" in str(e) or "quota" in error_str or "resource_exhausted" in error_str:
                    logging.warning(f"Quota exceeded for key {self.current_key_index}: {e}")
                    self._rotate_key()
                    continue  # Retry dengan key baru
                
                # Deteksi safety filter
                elif "safety" in error_str or "blocked" in error_str:
                    raise Exception("Content blocked due to safety settings")
                
                # Error lain - propagate ke caller
                else:
                    logging.error(f"Gemini API error: {e}")
                    raise
        
        # Semua keys habis
        raise Exception("All API keys have exceeded their quota")

    def generate_content(self, prompt: str) -> str | None:
        """
        Generate content tanpa streaming (synchronous).
        Returns text atau None jika gagal.
        """
        while self.current_key_index < len(self.api_keys):
            if not self.client:
                return None
            
            try:
                response = self.client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt
                )
                return response.text
                
            except Exception as e:
                error_str = str(e).lower()
                
                if "429" in str(e) or "quota" in error_str or "resource_exhausted" in error_str:
                    self._rotate_key()
                    continue
                else:
                    logging.error(f"Error generating content: {e}")
                    return None
        
        return None


class RobustTableDetector:
    def __init__(self):
        # Menggunakan konfigurasi Flask untuk path output, bukan hardcode
        self.output_dir = current_app.config['PDF_IMAGES_DIRECTORY']
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
            
    def _save_page_screenshot(self, page: fitz.Page, base_filename: str, page_num: int) -> str | None:
        try:
            document_specific_dir = os.path.join(self.output_dir, base_filename)
            os.makedirs(document_specific_dir, exist_ok=True)

            # 2. Definisikan nama dan path file gambar di dalam subfolder
            image_filename = f"page_{page_num}.png"
            output_path = os.path.join(document_specific_dir, image_filename)

            # 3. Path yang akan disimpan ke database (TANPA 'data/')
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
    Memproses PDF dengan logika resume dan commit per halaman untuk memastikan
    integritas data saat proses dihentikan atau gagal.
    """
    base_filename = os.path.splitext(os.path.basename(pdf_path))[0]
    original_filename = os.path.basename(pdf_path)
    
    # <--- PERUBAHAN 1: LOGIKA GET-OR-CREATE DENGAN RESUME --->
    document = None
    start_page = 1
    
    try:
        # Selalu buka file untuk menghitung hash dan total halaman
        file_hash = hashlib.sha256(open(pdf_path, "rb").read()).hexdigest()
        
        # Cek apakah dokumen sudah ada berdasarkan hash
        document = PdfDocument.query.filter_by(document_hash=file_hash).first()

        doc_for_pages = fitz.open(pdf_path)
        total_pages = len(doc_for_pages)
        doc_for_pages.close()

        if document:
            # Dokumen sudah ada, cek halaman terakhir yang diproses
            last_chunk = DocumentChunk.query.filter_by(document_id=document.id)\
                .order_by(DocumentChunk.page_number.desc()).first()
            
            if last_chunk:
                # Jika semua halaman sudah diproses, lewati file ini
                if last_chunk.page_number >= total_pages:
                    logging.info(f"Skipping '{original_filename}': Sudah selesai diproses.")
                    return {"status": "skipped", "filename": original_filename, "reason": "Dokumen sudah selesai diproses."}
                
                # Tentukan halaman awal untuk melanjutkan
                start_page = last_chunk.page_number + 1
                logging.info(f"Resuming '{original_filename}' from page {start_page}.")
            # Jika dokumen ada tapi tidak ada chunk (kasus aneh), mulai dari awal
            else:
                 start_page = 1
        else:
            # Dokumen baru, buat instance baru
            document = PdfDocument(
                filename=original_filename,
                total_pages=total_pages,
                document_hash=file_hash,
                doc_metadata={'source_path': pdf_path}
            )
            # Jangan di-add ke session dulu, tunggu sampai chunk pertama siap
            
    except Exception as e:
        logging.error(f"Gagal saat inisialisasi pra-proses untuk {pdf_path}: {e}")
        return {"status": "error", "filename": original_filename, "reason": f"Initialization error: {str(e)}"}

    # --- AKHIR PERUBAHAN 1 ---

    detector = RobustTableDetector()
    doc = fitz.open(pdf_path)
    
    # Pastikan loop dimulai dari halaman yang benar
    for page_num, page in enumerate(doc, 1):
        if page_num < start_page:
            continue

        try:
            # CEK STOP SIGNAL SEBELUM MEMPROSES SETIAP HALAMAN
            if job_id and check_job_should_stop(job_id):
                doc.close()
                logging.info(f"Proses dihentikan oleh pengguna sebelum halaman {page_num} pada file '{original_filename}'. Progress tersimpan.")
                return {"status": "stopped", "filename": original_filename, "reason": f"Dihentikan oleh pengguna pada halaman {page_num}"}

            if progress_callback:
                message = f"Menganalisis Halaman {page_num}/{total_pages} (File: {original_filename})"
                progress_callback(message=message)

            raw_text = page.get_text("text")
            is_table, reason = detector._detect_table_page(raw_text, page_num)
            
            # Hanya proses/simpan chunk yang bukan halaman exclude
            if reason == "excluded_page":
                continue

            content_type = "table" if is_table else "text"
            image_path = None
            if is_table:
                image_path = detector._save_page_screenshot(page, base_filename, page_num)

            # <--- PERUBAHAN 2: COMMIT PER HALAMAN --->
            
            # Jika ini dokumen baru, sekarang saatnya menambahkannya ke DB
            if not document.id:
                db.session.add(document)
                db.session.flush() # flush untuk mendapatkan ID dokumen baru

            new_chunk = DocumentChunk(
                document_id=document.id,
                page_number=page_num,
                chunk_content=detector._clean_text_for_rag(raw_text),
                chunk_metadata={
                    "type": content_type,
                    "image_path": image_path,
                    "detection_reason": reason,
                    "is_excluded": False
                }
            )
            db.session.add(new_chunk)
            db.session.commit() # Simpan progress untuk halaman ini secara permanen
            
            # --- AKHIR PERUBAHAN 2 ---

        except Exception as e:
            db.session.rollback() # Batalkan hanya transaksi halaman ini yang gagal
            doc.close()
            logging.error(f"Gagal memproses halaman {page_num} dari '{pdf_path}': {e}")
            # Hentikan proses untuk file ini karena terjadi error, tapi progress sebelumnya aman
            return {"status": "error", "filename": original_filename, "reason": f"Error on page {page_num}: {str(e)}"}
            
    doc.close()
    return {"status": "success", "filename": original_filename, "pages_chunked": total_pages - start_page + 1}