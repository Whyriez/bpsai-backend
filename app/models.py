from flask_sqlalchemy import SQLAlchemy
from pgvector.sqlalchemy import Vector
from sqlalchemy.orm import relationship
from sqlalchemy import JSON, Enum, event, inspect, ForeignKey, Uuid, DateTime, Integer, Text, String
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta  
import uuid
import enum
import pytz
# from app.services import EmbeddingService

db = SQLAlchemy()

# embedding_service = EmbeddingService()

class JobStatus(enum.Enum):
    IDLE = "IDLE"           # Tidak ada pekerjaan yang berjalan
    RUNNING = "RUNNING"     # Pekerjaan sedang aktif
    STOPPING = "STOPPING"   # Perintah berhenti diterima, sedang menyelesaikan item terakhir
    COMPLETED = "COMPLETED" # Semua item berhasil diproses
    FAILED = "FAILED"       # Terjadi error yang menghentikan pekerjaan


class GeminiApiKeyConfig(db.Model):
    __tablename__ = 'gemini_api_key_configs'
    
    id = db.Column(db.Integer, primary_key=True)
    key_name = db.Column(db.String(50), nullable=False)
    key_alias = db.Column(db.String(20), unique=True, nullable=False)  # KEY_1, KEY_2, dll
    quota_exceeded = db.Column(db.Boolean, default=False)
    quota_exceeded_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_used = db.Column(db.DateTime, nullable=True)
    total_requests = db.Column(db.Integer, default=0)
    failed_requests = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(pytz.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(pytz.utc), onupdate=lambda: datetime.now(pytz.utc))

    def get_quota_reset_time(self):
        """Menghitung waktu reset quota (asumsi reset setiap 24 jam)"""
        if not self.quota_exceeded_at:
            return None
        
        reset_time = self.quota_exceeded_at + timedelta(hours=24)
        return reset_time

    def get_time_until_reset(self):
        """Menghitung berapa lama lagi sampai reset"""
        # Cek dan reset dulu jika sudah waktunya
        self.check_and_reset_quota()
        
        reset_time = self.get_quota_reset_time()
        if not reset_time:
            return None
        
        now = datetime.now(pytz.utc)
        if reset_time > now:
            time_left = reset_time - now
            return {
                'hours': time_left.seconds // 3600,
                'minutes': (time_left.seconds % 3600) // 60,
                'seconds': time_left.seconds % 60,
                'total_seconds': time_left.total_seconds(),
                'reset_time': reset_time.isoformat()
            }
        return None
    
    def check_and_reset_quota(self):
        """Cek dan reset quota status jika sudah waktunya"""
        if self.quota_exceeded and self.quota_exceeded_at:
            reset_time = self.get_quota_reset_time()
            if reset_time and reset_time <= datetime.now(pytz.utc):
                self.quota_exceeded = False
                self.quota_exceeded_at = None
                db.session.commit()
                return True  # Berhasil di-reset
        return False  # Tidak perlu reset

    def mark_quota_exceeded(self):
        """Menandai bahwa quota key ini telah exceeded"""
        self.quota_exceeded = True
        self.quota_exceeded_at = datetime.now(pytz.utc)
        db.session.commit()

    def mark_successful_request(self):
        """Update stats untuk request sukses"""
        self.total_requests += 1
        self.last_used = datetime.now(pytz.utc)
        db.session.commit()

    def mark_failed_request(self):
        """Update stats untuk request gagal"""
        self.total_requests += 1
        self.failed_requests += 1
        self.last_used = datetime.now(pytz.utc)
        db.session.commit()

    def __repr__(self):
        return f'<GeminiApiKeyConfig {self.key_name} ({self.key_alias})>'
    
class BatchJob(db.Model):
    __tablename__ = 'batch_jobs'

    id = db.Column(db.Integer, primary_key=True)
    # Nama unik untuk pekerjaan, misal: 'full_reconstruction'
    job_name = db.Column(db.String(100), unique=True, nullable=False, default='full_reconstruction')
    
    status = db.Column(Enum(JobStatus), nullable=False, default=JobStatus.IDLE)
    total_items = db.Column(db.Integer, default=0)
    processed_items = db.Column(db.Integer, default=0)
    
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    last_updated = db.Column(db.DateTime, nullable=True, default=datetime.utcnow)
    
    last_error = db.Column(db.Text, nullable=True) # Untuk menyimpan pesan error jika gagal
    
    def get_progress(self):
        if self.total_items == 0:
            return 100.0
        return round((self.processed_items / self.total_items) * 100, 2)
    
    def is_stuck(self, timeout_minutes=30):
        """
        Cek apakah job stuck berdasarkan last_updated timestamp.
        Return True jika job RUNNING tapi tidak ada update dalam {timeout_minutes} menit.
        """
        if self.status != JobStatus.RUNNING:
            return False
        
        if not self.last_updated:
            return False
        
        from datetime import datetime, timedelta
        time_since_update = datetime.utcnow() - self.last_updated
        return time_since_update > timedelta(minutes=timeout_minutes)
    
    def reset_to_idle(self, reason=None):
        """Helper method untuk reset job ke IDLE dengan aman."""
        self.status = JobStatus.IDLE
        self.completed_at = datetime.utcnow()
        if reason:
            self.last_error = reason
    
    def __repr__(self):
        return f"<BatchJob {self.job_name} status={self.status.value} progress={self.get_progress()}%>"

class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    role = db.Column(db.String(80), nullable=False, default='user')
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(pytz.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(pytz.utc), onupdate=lambda: datetime.now(pytz.utc))

    def __repr__(self):
        return f'<User {self.username}>'
    
class BeritaBps(db.Model):
    __tablename__ = 'berita_bps'
    id = db.Column(db.Integer, primary_key=True)
    tanggal_rilis = db.Column(db.Date, nullable=False, index=True)
    judul_berita = db.Column(db.String(255), nullable=False)
    ringkasan = db.Column(db.Text, nullable=False)
    link = db.Column(db.Text, nullable=False)
    tags = db.Column(JSON, nullable=True)
    embedding = db.Column(Vector(768), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(pytz.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(pytz.utc), onupdate=lambda: datetime.now(pytz.utc))

def generate_embedding_listener(mapper, connection, target):
    """
    Fungsi ini akan dijalankan sebelum insert atau update pada model BeritaBps.
    'target' adalah instance dari BeritaBps yang akan disimpan.
    """
    from app.services import EmbeddingService
    embedding_service = EmbeddingService()


    # Cek apakah ada perubahan pada judul atau ringkasan (hanya untuk event 'update')
    # Ini penting agar kita tidak membuat embedding baru jika hanya kolom lain yang diubah.
    state = inspect(target)
    if state.modified and not (state.attrs.judul_berita.history.has_changes() or state.attrs.ringkasan.history.has_changes()):
        return # Tidak ada perubahan pada kolom relevan, jadi lewati

    # Gabungkan teks dari judul, ringkasan, dan tags untuk membuat embedding yang kaya
    tags_string = ', '.join(target.tags) if isinstance(target.tags, list) else ''
    text_to_embed = f"Judul: {target.judul_berita}\nRingkasan: {target.ringkasan}\nTags: {tags_string}"

    # Generate embedding baru
    new_embedding = embedding_service.generate(text_to_embed)

    # Tetapkan embedding baru ke instance model
    if new_embedding:
        target.embedding = new_embedding
        print(f"Embedding generated/updated for BeritaBps ID: {target.id or '(new)'}")

# --- MENEMPELKAN LISTENER KE MODEL BERITABPS ---
# Menjalankan fungsi 'generate_embedding_listener' setiap kali ada data BARU
event.listen(BeritaBps, 'before_insert', generate_embedding_listener)

# Menjalankan fungsi 'generate_embedding_listener' setiap kali ada data LAMA yang DIUPDATE
event.listen(BeritaBps, 'before_update', generate_embedding_listener)

class PromptLog(db.Model):
    __tablename__ = 'prompt_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_prompt = db.Column(db.Text, nullable=False)
    final_prompt = db.Column(db.Text, nullable=True)
    model_response = db.Column(db.Text, nullable=True)
    detected_intent = db.Column(db.String(50), nullable=True, index=True)
    extracted_keywords = db.Column(JSON, nullable=True)
    extracted_years = db.Column(JSON, nullable=True)
    found_results = db.Column(db.Boolean, default=False)
    retrieved_news_count = db.Column(db.Integer, default=0)
    retrieved_news_ids = db.Column(JSON, nullable=True)
    session_id = db.Column(db.String(255), nullable=True, index=True)
    processing_time_ms = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(pytz.utc), index=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(pytz.utc), onupdate=lambda: datetime.now(pytz.utc))

    # Relasi ke feedback
    feedbacks = db.relationship('Feedback', backref='prompt_log', lazy=True, cascade="all, delete-orphan")

class Feedback(db.Model):
    __tablename__ = 'feedback'

    id = db.Column(db.Integer, primary_key=True)
    prompt_log_id = db.Column(db.Integer, db.ForeignKey('prompt_logs.id'), nullable=False)
    type = db.Column(Enum('positive', 'negative', name='feedback_type_enum'), nullable=False)
    comment = db.Column(db.Text, nullable=True)
    session_id = db.Column(db.String(255), nullable=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(pytz.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(pytz.utc), onupdate=lambda: datetime.now(pytz.utc))

class PdfDocument(db.Model):
    """
    Menyimpan metadata untuk setiap file PDF yang diunggah atau diproses.
    Setiap dokumen akan memiliki banyak chunk/halaman.
    """
    __tablename__ = 'pdf_documents'

    id = db.Column(Uuid, primary_key=True, default=uuid.uuid4)
    filename = db.Column(String(255), nullable=False, index=True)
    link = db.Column(Text, nullable=True)
    total_pages = db.Column(Integer, nullable=True)
    document_hash = db.Column(String(64), nullable=True, unique=True)
    doc_metadata = db.Column(JSON, nullable=True) # Metadata tambahan (misal: penulis, tanggal publikasi)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(pytz.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(pytz.utc), onupdate=lambda: datetime.now(pytz.utc))

    chunks = relationship('DocumentChunk', back_populates='document', lazy='dynamic', cascade="all, delete-orphan")

    def __repr__(self):
        return f'<PdfDocument {self.filename}>'
    
class DocumentChunk(db.Model):
    """
    Menyimpan setiap chunk (misalnya, per halaman) dari sebuah dokumen PDF.
    Setiap chunk memiliki konten teks dan vektor embedding-nya sendiri.
    """
    __tablename__ = 'document_chunks'

    id = db.Column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id = db.Column(Uuid, ForeignKey('pdf_documents.id'), nullable=False, index=True)
    
    page_number = db.Column(Integer, nullable=False)
    chunk_content = db.Column(Text, nullable=False)
    reconstructed_content = db.Column(Text, nullable=True) 
    embedding = db.Column(Vector(768), nullable=True)
    chunk_metadata = db.Column(JSON, nullable=True) # Metadata spesifik chunk (misal: ada tabel di halaman ini)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(pytz.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(pytz.utc), onupdate=lambda: datetime.now(pytz.utc))

    document = relationship('PdfDocument', back_populates='chunks')

    def __repr__(self):
        return f'<DocumentChunk Page {self.page_number} of Doc ID {self.document_id}>'

# --- EVENT LISTENER UNTUK MEMBUAT EMBEDDING OTOMATIS ---

def generate_chunk_embedding_listener(mapper, connection, target):
    """
    Dijalankan sebelum insert atau update pada DocumentChunk.
    Membuat embedding dari chunk_content.
    """
    from app.services import EmbeddingService
    embedding_service = EmbeddingService()

    state = inspect(target)
    
    # Hanya generate embedding jika 'chunk_content' berubah atau saat data baru dibuat
    if state.modified and not state.attrs.chunk_content.history.has_changes():
        return

    # Teks yang akan di-embed
    text_to_embed = target.chunk_content

    if text_to_embed:
        new_embedding = embedding_service.generate(text_to_embed)
        if new_embedding is not None:
            target.embedding = new_embedding
            print(f"Embedding generated for chunk (Page: {target.page_number}, Doc ID: {target.document_id})")

# Menempelkan listener ke model DocumentChunk
event.listen(DocumentChunk, 'before_insert', generate_chunk_embedding_listener)
event.listen(DocumentChunk, 'before_update', generate_chunk_embedding_listener)


class DocumentFeedbackScore(db.Model):
    __tablename__ = 'document_feedback_scores'

    id = db.Column(db.Integer, primary_key=True)
    # Gunakan polymorphic identity untuk menyimpan tipe dan ID dari sumber yang berbeda
    entity_type = db.Column(db.String(50), nullable=False) # 'berita_bps' atau 'document_chunk'
    entity_id = db.Column(db.String, nullable=False) # Bisa Integer atau UUID
    
    positive_feedback_count = db.Column(db.Integer, default=0)
    negative_feedback_count = db.Column(db.Integer, default=0)
    # Skor akhir yang dinormalisasi, bisa diupdate secara periodik
    score = db.Column(db.Float, default=0.5) 

    __table_args__ = (db.UniqueConstraint('entity_type', 'entity_id', name='_entity_uc'),)

    def update_score(self):
        """Menghitung skor sederhana berdasarkan feedback."""
        total = self.positive_feedback_count + self.negative_feedback_count
        if total == 0:
            self.score = 0.5 # Skor netral awal
        else:
            # Formula sederhana: (positif + 1) / (total + 2) -> Bayesian smoothing
            self.score = (self.positive_feedback_count + 1) / (total + 2)