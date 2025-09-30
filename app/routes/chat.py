import time
import json
import uuid
from flask import Blueprint, request, Response, session, current_app, jsonify
# DIUBAH: Tambahkan DocumentChunk untuk query
from app.models import db, BeritaBps, DocumentChunk, PromptLog, Feedback 
from app.services import EmbeddingService, GeminiService
# DIUBAH: build_context sekarang akan menangani data gabungan
from app.helpers import (
    extract_years, detect_intent, extract_keywords, build_context,
    build_final_prompt, expand_query_with_synonyms, BPS_ACRONYM_DICTIONARY,
    format_conversation_history,
    rerank_with_dss 
)
from sqlalchemy.orm import aliased

# Buat Blueprint untuk rute chat
chat_bp = Blueprint('chat', __name__)

embedding_service = EmbeddingService()
gemini_service = GeminiService()

def get_combined_relevant_results(user_prompt: str, limit: int = 15):
    """
    Melakukan vector search pada BeritaBps dan DocumentChunk, 
    menggabungkan hasilnya, dan mengurutkan berdasarkan relevansi.
    """
    expanded_prompt = expand_query_with_synonyms(user_prompt, BPS_ACRONYM_DICTIONARY)
    current_app.logger.info(f"Original prompt: '{user_prompt}', Expanded to: '{expanded_prompt}'")

    prompt_embedding = embedding_service.generate(expanded_prompt)
    if not prompt_embedding:
        return []

    # 1. Query ke tabel BeritaBps
    berita_results = db.session.query(
        BeritaBps,
        BeritaBps.embedding.cosine_distance(prompt_embedding).label('distance')
    ).order_by('distance').limit(limit).all()

    # 2. Query ke tabel DocumentChunk
    chunk_results = db.session.query(
        DocumentChunk,
        DocumentChunk.embedding.cosine_distance(prompt_embedding).label('distance')
    ).order_by('distance').limit(limit).all()

    # 3. Gabungkan hasil dari kedua query
    combined_results = berita_results + chunk_results

    # 4. Urutkan hasil gabungan berdasarkan 'distance' (semakin kecil semakin relevan)
    combined_results.sort(key=lambda x: x.distance)

    # 5. Ambil objeknya saja dan batasi sesuai limit
    # final_results = [item for item, distance in combined_results[:limit]]
    
    return combined_results[:limit]

@chat_bp.route('/stream', methods=['POST'])
def stream():
    start_time = time.time()
    data = request.json
    user_prompt = data.get('prompt')
    session_id = data.get('conversation_id')

    if not user_prompt or not session_id:
        return Response(json.dumps({'error': 'Prompt and conversation_id are required'}), status=400, mimetype='application/json')

    recent_history_logs = PromptLog.query.filter(
        PromptLog.session_id == session_id,
        PromptLog.model_response.isnot(None),
        ~PromptLog.model_response.ilike('data:%'),
        ~PromptLog.model_response.ilike('error%')
    ).order_by(PromptLog.id.desc()).limit(10).all()

    history_context = format_conversation_history(recent_history_logs)

    log = PromptLog(
        user_prompt=user_prompt,
        session_id=session_id,
        extracted_years=extract_years(user_prompt),
        extracted_keywords=extract_keywords(user_prompt),
        detected_intent=detect_intent(user_prompt)
    )
    db.session.add(log)
    db.session.commit()
    log_id = log.id

    try:
        # DIUBAH: Panggil fungsi pencarian gabungan yang baru
        relevant_items = get_combined_relevant_results(user_prompt, limit=15)

        items_to_rerank = [(item, dist) for item, dist in relevant_items]
        relevant_items = rerank_with_dss(items_to_rerank)
        
        # Fungsi build_context (di helpers.py) akan menangani item-item ini
        context = build_context(relevant_items, log.extracted_years)
        final_prompt = build_final_prompt(context, user_prompt, history_context)

        # DIUBAH: Log ID dari sumber yang berbeda
        retrieved_ids = []
        for item in relevant_items:
            if isinstance(item, BeritaBps):
                retrieved_ids.append({'type': 'berita', 'id': item.id})
            elif isinstance(item, DocumentChunk):
                retrieved_ids.append({'type': 'document_chunk', 'id': str(item.id)})

        log.found_results = bool(relevant_items)
        log.retrieved_news_count = len(relevant_items)
        log.retrieved_news_ids = retrieved_ids # Simpan ID yang sudah terstruktur
        log.final_prompt = final_prompt
        db.session.commit()

        app = current_app._get_current_object()
        model_response_buffer = ""
        def generate():
            nonlocal model_response_buffer
            try:
                for chunk in gemini_service.stream_generate_content(final_prompt):
                    try:
                        yield chunk
                    except GeneratorExit:
                        current_app.logger.info(f"Client disconnected for session {session_id}. Stopping stream.")
                        break 
                
                    if chunk.strip().startswith('data: '):
                        json_str = chunk.strip()[6:]
                        if json_str and json_str != '[DONE]':
                            try:
                                data_chunk = json.loads(json_str)
                                if 'text' in data_chunk:
                                    model_response_buffer += data_chunk['text']
                            except (json.JSONDecodeError, KeyError):
                                continue
            finally:
                update_log_after_streaming(app, log_id, model_response_buffer, start_time)

        return Response(generate(), mimetype='text/event-stream')

    except Exception as e:
        current_app.logger.error(f'Error processing chat stream: {e}')
        log_to_update = db.session.get(PromptLog, log.id)
        if log_to_update:
            log_to_update.model_response = f"Error: {str(e)}"
            log_to_update.processing_time_ms = int((time.time() - start_time) * 1000)
            db.session.commit()
        return Response(json.dumps({'error': 'Terjadi kesalahan internal.'}), status=500, mimetype='application/json')

def update_log_after_streaming(app, log_id, model_response, start_time):
    with app.app_context():
        try:
            processing_time = int((time.time() - start_time) * 1000)
            log_to_update = db.session.get(PromptLog, log_id)
            if log_to_update:
                log_to_update.model_response = model_response if model_response else "[No Content]"
                log_to_update.processing_time_ms = processing_time
                db.session.commit()
                current_app.logger.info(f'Log updated successfully for log_id: {log_id}')
            else:
                current_app.logger.warning(f'Log with id {log_id} not found for updating.')
        except Exception as e:
            current_app.logger.error(f'Error updating log after streaming for log_id {log_id}: {e}')

@chat_bp.route('/history/<conversation_id>', methods=['GET'])
def get_history(conversation_id):
    if not conversation_id:
        return jsonify({'error': 'Conversation ID is required'}), 400

    try:
        history_logs = PromptLog.query.filter_by(session_id=conversation_id).order_by(PromptLog.id.asc()).all()
        formatted_history = []
        for log in history_logs:
            if log.user_prompt and log.model_response and not log.model_response.lower().startswith('error'):
                feedback_record = Feedback.query.filter_by(prompt_log_id=log.id).first()
                feedback_type = feedback_record.type if feedback_record else None

                formatted_history.append({
                    'prompt_log_id': log.id, 
                    'user_prompt': log.user_prompt,
                    'model_response': log.model_response,
                    'has_feedback': feedback_type 
                })
        return jsonify(formatted_history)
    except Exception as e:
        current_app.logger.error(f'Error fetching history for {conversation_id}: {e}')
        return jsonify({'error': 'Gagal mengambil riwayat percakapan.'}), 500