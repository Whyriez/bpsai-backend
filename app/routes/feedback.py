from flask import Blueprint, request, jsonify
from app.models import db, Feedback, PromptLog, DocumentFeedbackScore
from sqlalchemy.orm import joinedload
from sqlalchemy import func
import datetime
from datetime import timezone
from flask_jwt_extended import jwt_required

feedback_bp = Blueprint('feedback', __name__, url_prefix='/api')

def format_time_ago(dt):
    """Mengubah objek datetime menjadi string 'time ago' yang mudah dibaca."""
    if not dt:
        return ""
    now = datetime.datetime.now(timezone.utc)
    diff = now - dt
    
    seconds = diff.total_seconds()
    if seconds < 60:
        return "beberapa detik yang lalu"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} menit yang lalu"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)} jam yang lalu"
    days = hours / 24
    if days < 30:
        return f"{int(days)} hari yang lalu"
    months = days / 30
    if months < 12:
        return f"{int(months)} bulan yang lalu"
    return f"{int(months / 12)} tahun yang lalu"

@feedback_bp.route('/feedback', methods=['GET'])
@jwt_required()
def get_all_feedback():
    """
    Mengambil data statistik dan daftar feedback terbaru dengan paginasi.
    ---
    tags:
      - Feedback
    summary: Mendapatkan data statistik dan daftar feedback (paginasi).
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
    responses:
      200:
        description: Data feedback berhasil diambil.
        schema:
          type: object
          properties:
            stats:
              type: object
            feedback:
              type: object
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
      500:
        description: Gagal mengambil data feedback.
    """
    try:
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
    
        # --- 1. Kalkulasi Statistik ---
        total_feedback = db.session.query(func.count(Feedback.id)).scalar() or 0
        positive_feedback = Feedback.query.filter_by(type='positive').count() or 0
        
        total_prompts = db.session.query(func.count(PromptLog.id)).scalar() or 0
        prompts_with_feedback = db.session.query(func.count(db.distinct(Feedback.prompt_log_id))).scalar() or 0

        stats = {
            'satisfactionRate': int((positive_feedback / total_feedback) * 100) if total_feedback > 0 else 0,
            'positivePercentage': int((positive_feedback / total_feedback) * 100) if total_feedback > 0 else 0,
            'negativePercentage': int(((total_feedback - positive_feedback) / total_feedback) * 100) if total_feedback > 0 else 0,
            'responseRate': int((prompts_with_feedback / total_prompts) * 100) if total_prompts > 0 else 0,
            'totalReviews': total_feedback
        }

        pagination = Feedback.query.options(
            joinedload(Feedback.prompt_log)
        ).order_by(Feedback.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )

        # --- 2. Ambil Daftar Feedback Terbaru ---
        # Menggunakan joinedload untuk efisiensi query (menghindari N+1 problem)
        # recent_feedback_query = Feedback.query.options(
        #     joinedload(Feedback.prompt_log)
        # ).order_by(Feedback.created_at.desc()).limit(20).all()

        feedback_items_on_page = pagination.items

        formatted_feedback = []
        for feedback in feedback_items_on_page:
            if feedback.prompt_log:
                formatted_feedback.append({
                    'id': feedback.id,
                    'type': feedback.type,
                    'time': format_time_ago(feedback.created_at),
                    'userPrompt': feedback.prompt_log.user_prompt,
                    'modelResponse': feedback.prompt_log.model_response,
                    'comment': feedback.comment
                })
        
        return jsonify({
            'stats': stats,
            'feedback': {
                'items': formatted_feedback,
                'totalPages': pagination.pages,
                'currentPage': pagination.page,
                'totalItems': pagination.total,
                'hasNext': pagination.has_next,
                'hasPrev': pagination.has_prev
            }
        })

    except Exception as e:
        # Sebaiknya log error ini di production
        print(f"Error fetching feedback data: {e}")
        return jsonify({'error': 'Gagal mengambil data feedback'}), 500

@feedback_bp.route('/feedback', methods=['POST'])
# @jwt_required()
def handle_feedback():
    """
    Menerima feedback (like/dislike) dari pengguna untuk sebuah respons.
    ---
    tags:
      - Feedback
    summary: Mengirimkan feedback untuk sebuah respons.
    parameters:
      - in: body
        name: body
        description: Data feedback yang dikirim oleh pengguna.
        required: true
        schema:
          type: object
          properties:
            prompt_log_id:
              type: string
              description: "(DEPRECATED - jangan diisi) ID unik pesan dari client."
            type:
              type: string
              enum: ["positive", "negative"]
              example: "positive"
            comment:
              type: string
              example: "Jawaban ini sangat membantu!"
            session_id:
              type: string
              example: "a1b2c3d4-..."
              description: "ID unik dari percakapan (conversation_id)."
    responses:
      201:
        description: Feedback berhasil diterima.
      400:
        # PERBAIKAN DILAKUKAN DI SINI: Hapus tanda kutip tunggal (' ')
        description: type atau session_id tidak diisi. 
      404:
        description: Log percakapan (PromptLog) tidak ditemukan.
    """
    data = request.json
    
    # ID ini adalah ID unik untuk PESAN (misal: "35ef4b97...")
    # Kita tidak menggunakannya untuk query, tapi mungkin berguna untuk 'comment'
    prompt_log_uuid_from_client = data.get('prompt_log_id') 
    
    feedback_type = data.get('type')
    comment = data.get('comment')
    
    # ID ini adalah ID untuk seluruh PERCAKAPAN (conversation_id)
    sessionId = data.get('session_id')

    if not feedback_type:
        return jsonify({'error': 'type is required'}), 400
    
    # --- INI SOLUSINYA ---
    # Kita harus mencari berdasarkan 'sessionId' (ID percakapan)
    # bukan 'prompt_log_id' (ID pesan)
    
    if not sessionId:
        return jsonify({'error': 'session_id is required for feedback'}), 400

    # 1. Cari log TERBARU yang cocok dengan ID PERCAKAPAN
    prompt_log = PromptLog.query.filter_by(session_id=sessionId)\
                                .order_by(PromptLog.id.desc())\
                                .first()

    if not prompt_log:
        # Jika ini terjadi, berarti 'sessionId' dari frontend tidak ada di DB
        return jsonify({'error': 'PromptLog not found for the given session_id'}), 404
    # --- AKHIR SOLUSI ---

    # 2. Buat feedback baru, tautkan ke 'prompt_log.id' (Integer) yang benar
    new_feedback = Feedback(
        prompt_log_id=prompt_log.id,  # <-- Tautkan ke PK integer yang benar
        type=feedback_type,
        comment=comment,
        session_id=sessionId 
    )
    
    db.session.add(new_feedback)

    # 3. (Sisa logika Anda sudah benar)
    if prompt_log.retrieved_news_ids:
        for item_ref in prompt_log.retrieved_news_ids:
            entity_type = item_ref.get('type')
            entity_id = str(item_ref.get('id'))

            if not entity_type or not entity_id:
                continue

            feedback_score = DocumentFeedbackScore.query.filter_by(
                entity_type=entity_type, 
                entity_id=entity_id
            ).first()

            if not feedback_score:
                feedback_score = DocumentFeedbackScore(entity_type=entity_type, entity_id=entity_id)
                db.session.add(feedback_score)
            
            if feedback_score.positive_feedback_count is None:
                feedback_score.positive_feedback_count = 0
            if feedback_score.negative_feedback_count is None:
                feedback_score.negative_feedback_count = 0

            if feedback_type == 'positive':
                feedback_score.positive_feedback_count += 1
            else:
                feedback_score.negative_feedback_count += 1
            
            feedback_score.update_score()
            
    db.session.commit()

    return jsonify({'message': 'Feedback received successfully'}), 201