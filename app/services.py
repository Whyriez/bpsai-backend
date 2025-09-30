import os
import json
import logging
import requests
import google.generativeai as genai
from google.generativeai.types import generation_types
import fitz
import re
import time
import hashlib
from typing import List, Dict, Any
from .models import db, PdfDocument, DocumentChunk
from flask import current_app

logging.basicConfig(level=logging.INFO)

class EmbeddingService:
    def __init__(self):
        # --- Bagian ini tidak perlu diubah ---
        api_keys_str = os.getenv('GEMINI_API_KEYS', "")
        api_keys_list = [key.strip() for key in api_keys_str.split(',') if key.strip()]

        if not api_keys_list:
            self.api_key = None
            logging.error("GEMINI_API_KEYS tidak diatur atau kosong di .env")
        else:
            self.api_key = api_keys_list[0]
        
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={self.api_key}"
        # --- Akhir bagian yang tidak berubah ---

    def generate(self, text: str) -> list | None:
        if not text or not self.api_key:
            if not self.api_key:
                logging.error("Embedding generation failed: API key is None.")
            return None
        
        # --- AWAL DARI LOGIKA RETRY ---
        retries = 3  # Jumlah maksimum percobaan ulang
        backoff_factor = 2  # Waktu tunggu akan menjadi 2s, 4s, 8s
        
        for i in range(retries):
            try:
                response = requests.post(
                    self.url,
                    json={'model': 'models/text-embedding-004', 'content': {'parts': [{'text': text}]}},
                    timeout=20 # Tambahkan timeout agar tidak menunggu selamanya
                )
                # Lemparkan error untuk status 5xx agar bisa ditangkap oleh blok except
                response.raise_for_status() 
                result = response.json()
                return result.get('embedding', {}).get('values')
                
            except requests.exceptions.RequestException as e:
                # Cek apakah errornya adalah server error (5xx) yang layak untuk dicoba lagi
                if isinstance(e, requests.exceptions.HTTPError) and 500 <= e.response.status_code < 600:
                    wait_time = backoff_factor ** (i + 1)
                    logging.warning(f"Gemini API error ({e.response.status_code}). Mencoba lagi dalam {wait_time} detik...")
                    time.sleep(wait_time)
                    # Lanjutkan ke iterasi retry berikutnya
                    continue 
                else:
                    # Untuk error lain (koneksi, error 4xx client, dll.), langsung hentikan percobaan
                    logging.error(f"Gemini Embedding API Error (tidak bisa di-retry): {e}")
                    return None 
        
        # Jika semua percobaan gagal, catat error terakhir dan kembalikan None
        logging.error(f"Gagal mengambil embedding dari Gemini API setelah {retries} kali percobaan.")
        return None

class GeminiService:
    """
    Kelas ini tidak berubah. Biarkan seperti yang sudah Anda miliki.
    Mengelola beberapa API key dan melakukan rotasi otomatis.
    """
    def __init__(self):
        self.api_keys = [key.strip() for key in os.getenv("GEMINI_API_KEYS", "").split(',') if key.strip()]
        self.current_key_index = 0
        self.model = None
        if not self.api_keys:
            logging.error("GEMINI_API_KEYS environment variable is not set or is empty.")
            return
        self._initialize_model()

    def _initialize_model(self):
        if self.current_key_index >= len(self.api_keys):
            self.model = None
            logging.warning("All Gemini API keys have exceeded their quota.")
            return
        try:
            current_key = self.api_keys[self.current_key_index]
            genai.configure(api_key=current_key)
            self.model = genai.GenerativeModel('gemini-2.5-flash')
            logging.info(f"Gemini Service initialized successfully with API key index: {self.current_key_index}")
        except Exception as e:
            self.model = None
            logging.error(f"Failed to initialize Gemini Service with key index {self.current_key_index}: {e}")

    def _rotate_key(self):
        logging.warning(f"API key at index {self.current_key_index} has exceeded its quota. Rotating to the next key.")
        self.current_key_index += 1
        self._initialize_model()

    def stream_generate_content(self, final_prompt: str):
        while self.current_key_index < len(self.api_keys):
            if not self.model:
                error_message = json.dumps({'error': {'message': 'Semua API key telah mencapai batas kuota.'}})
                yield f"data: {error_message}\n\n"
                return
            try:
                response = self.model.generate_content(final_prompt, stream=True)
                for chunk in response:
                    if chunk.text:
                        sse_formatted_chunk = json.dumps({"text": chunk.text})
                        yield f"data: {sse_formatted_chunk}\n\n"
                yield "data: [DONE]\n\n"
                return
            except generation_types.StopCandidateException as e:
                logging.error(f"Content generation stopped due to safety settings: {e}")
                error_message = json.dumps({'error': {'message': 'Konten diblokir karena kebijakan keamanan.'}})
                yield f"data: {error_message}\n\n"
                return
            except Exception as e:
                if "429" in str(e) and "quota" in str(e).lower():
                    self._rotate_key()
                    continue
                else:
                    logging.error(f"An unexpected error occurred in SDK streaming: {e}")
                    error_message = json.dumps({'error': {'message': 'Terjadi kesalahan tak terduga pada layanan AI.'}})
                    yield f"data: {error_message}\n\n"
                    return
        final_error_message = json.dumps({'error': {'message': 'Semua API key telah mencapai batas kuota.'}})
        yield f"data: {final_error_message}\n\n"


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


def process_and_save_pdf(pdf_path: str) -> Dict[str, Any]:
    try:
        file_hash = hashlib.sha256(open(pdf_path, "rb").read()).hexdigest()
        existing_doc = PdfDocument.query.filter_by(document_hash=file_hash).first()
        if existing_doc:
            return {"status": "skipped", "filename": os.path.basename(pdf_path), "reason": "Dokumen sudah ada di database."}

        # Gunakan kelas detektor yang baru
        detector = RobustTableDetector()
        labeled_chunks = detector.extract_and_label_pages(pdf_path)
        
        if not labeled_chunks:
            return {"status": "failed", "filename": os.path.basename(pdf_path), "reason": "Tidak ada konten yang bisa diekstrak."}

        new_document = PdfDocument(
            filename=os.path.basename(pdf_path),
            total_pages=len(labeled_chunks),
            document_hash=file_hash,
            doc_metadata={'source_path': pdf_path}
        )
        db.session.add(new_document)
        db.session.flush() # Dapatkan ID dokumen sebelum commit

        for chunk_data in labeled_chunks:
            # Hanya proses/simpan chunk yang bukan halaman exclude
            if not chunk_data['metadata']['is_excluded']:
                new_chunk = DocumentChunk(
                    document_id=new_document.id,
                    page_number=chunk_data['page_number'],
                    chunk_content=chunk_data['content'],
                    # Simpan semua metadata hasil deteksi ke database
                    chunk_metadata=chunk_data['metadata'] 
                )
                db.session.add(new_chunk)
            
        db.session.commit()
        return {"status": "success", "filename": os.path.basename(pdf_path), "pages_chunked": len(labeled_chunks)}

    except Exception as e:
        db.session.rollback()
        logging.error(f"Error processing {pdf_path}: {e}")
        return {"status": "error", "filename": os.path.basename(pdf_path), "reason": str(e)}