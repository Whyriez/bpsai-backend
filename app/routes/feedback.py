from flask import Blueprint, request, jsonify
from app.models import db, Feedback, PromptLog, DocumentFeedbackScore
from sqlalchemy.orm import joinedload
from sqlalchemy import func
import datetime
from flask_jwt_extended import jwt_required

feedback_bp = Blueprint('feedback', __name__, url_prefix='/api')

def format_time_ago(dt):
    """Mengubah objek datetime menjadi string 'time ago' yang mudah dibaca."""
    if not dt:
        return ""
    now = datetime.datetime.utcnow()
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
    data = request.json
    
    prompt_log_id = data.get('prompt_log_id')
    feedback_type = data.get('type')
    comment = data.get('comment')
    sessionId = data.get('session_id')

    if not prompt_log_id or not feedback_type:
        return jsonify({'error': 'prompt_log_id and type are required'}), 400

    if feedback_type not in ['positive', 'negative']:
        return jsonify({'error': 'Invalid feedback type'}), 400

    # Buat entri feedback baru
    new_feedback = Feedback(
        prompt_log_id=prompt_log_id,
        type=feedback_type,
        comment=comment,
        session_id=sessionId
    )
    
    prompt_log = PromptLog.query.get(prompt_log_id)
    if not prompt_log:
        return jsonify({'error': 'PromptLog not found'}), 404
    
    db.session.add(new_feedback)

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
            
            # --- PERBAIKAN DI SINI ---
            # Pastikan nilai count bukan None sebelum operasi penambahan
            if feedback_score.positive_feedback_count is None:
                feedback_score.positive_feedback_count = 0
            if feedback_score.negative_feedback_count is None:
                feedback_score.negative_feedback_count = 0
            # --- AKHIR PERBAIKAN ---

            # Update count berdasarkan tipe feedback
            if feedback_type == 'positive':
                feedback_score.positive_feedback_count += 1
            else:
                feedback_score.negative_feedback_count += 1
            
            # Hitung ulang skor
            feedback_score.update_score()
            
    db.session.commit()

    return jsonify({'message': 'Feedback received successfully'}), 201