import time
import json
import uuid
from flask import Blueprint, request, Response, session, current_app, jsonify
from app.models import db, BeritaBps, DocumentChunk, PromptLog, Feedback 
from app.services import EmbeddingService, GeminiService
from app.vector_db import get_collections
from app.helpers import (
    extract_years, detect_intent, extract_keywords, build_context,
    build_final_prompt, expand_query_with_synonyms, BPS_ACRONYM_DICTIONARY,
    format_conversation_history,
    rerank_with_dss
)
from sqlalchemy.orm import aliased

chat_bp = Blueprint('chat', __name__)

embedding_service = EmbeddingService()
gemini_service = GeminiService()

def send_thinking_status(status, detail=""):
    """Helper untuk mengirim status thinking ke client"""
    return f"data: {json.dumps({'thinking': True, 'status': status, 'detail': detail})}\n\n"

def get_combined_relevant_results(user_prompt: str, limit: int = 15):
    """
    Melakukan vector search menggunakan ChromaDB, menggabungkan hasilnya.
    """
    expanded_prompt = expand_query_with_synonyms(user_prompt, BPS_ACRONYM_DICTIONARY)
    current_app.logger.info(f"Original prompt: '{user_prompt}', Expanded to: '{expanded_prompt}'")

    prompt_embedding = embedding_service.generate(expanded_prompt)
    if not prompt_embedding:
        return []

    berita_collection, document_collection = get_collections()

    # Query ke collection BeritaBps
    berita_results = berita_collection.query(
        query_embeddings=[prompt_embedding],
        n_results=limit
    )

    # Query ke collection DocumentChunk
    chunk_results = document_collection.query(
        query_embeddings=[prompt_embedding],
        n_results=limit
    )

    # Gabungkan dan proses hasil dari ChromaDB
    combined_results = []

    if berita_results['ids'][0]:
        for i, item_id in enumerate(berita_results['ids'][0]):
            distance = berita_results['distances'][0][i]
            berita_obj = db.session.get(BeritaBps, int(item_id)) 
            if berita_obj:
                combined_results.append((berita_obj, distance))

    if chunk_results['ids'][0]:
        for i, item_id in enumerate(chunk_results['ids'][0]):
            distance = chunk_results['distances'][0][i]
            chunk_obj = db.session.get(DocumentChunk, item_id)
            if chunk_obj:
                combined_results.append((chunk_obj, distance))

    combined_results.sort(key=lambda x: x[1])

    return combined_results[:limit]

@chat_bp.route('/stream', methods=['POST'])
def stream():
    start_time = time.time()
    data = request.json
    user_prompt = data.get('prompt')
    session_id = data.get('conversation_id')

    if not user_prompt or not session_id:
        return Response(json.dumps({'error': 'Prompt and conversation_id are required'}), status=400, mimetype='application/json')

    db.session.expire_all()

    # Simpan pertanyaan baru
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

    # Dapatkan app context SEBELUM generator dimulai
    app = current_app._get_current_object()
    
    try:
        def generate():
            nonlocal log_id
            model_response_buffer = ""
            
            try:
                # Semua operasi database harus dalam app context
                with app.app_context():
                    # Step 1: Analisis pertanyaan
                    yield send_thinking_status("analyzing", "Menganalisis pertanyaan...")
                    time.sleep(0.3)
                    
                    # Step 2: Ambil riwayat percakapan
                    yield send_thinking_status("history", "Memuat riwayat percakapan...")
                    recent_history_logs = PromptLog.query.filter(
                        PromptLog.session_id == session_id,
                        PromptLog.model_response.isnot(None),
                        ~PromptLog.model_response.ilike('data:%'),
                        ~PromptLog.model_response.ilike('error%')
                    ).order_by(PromptLog.id.desc()).limit(4).all()
                    recent_history_logs.reverse()
                    history_context = format_conversation_history(recent_history_logs)
                    
                    # Step 3: Mencari data relevan
                    yield send_thinking_status("searching", "Mencari data statistik relevan...")
                    relevant_items = get_combined_relevant_results(user_prompt, limit=15)
                    
                    # Step 4: Ranking ulang hasil
                    if relevant_items:
                        yield send_thinking_status("ranking", f"Mengurutkan {len(relevant_items)} hasil pencarian...")
                        items_to_rerank = [(item, dist) for item, dist in relevant_items]
                        relevant_items = rerank_with_dss(items_to_rerank)
                    
                    # Step 5: Membangun konteks
                    yield send_thinking_status("building", "Menyusun konteks jawaban...")
                    context = build_context(relevant_items, log.extracted_years)
                    final_prompt = build_final_prompt(context, user_prompt, history_context)

                    # Simpan retrieved_ids
                    retrieved_ids = []
                    for item in relevant_items:
                        if isinstance(item, BeritaBps):
                            retrieved_ids.append({'type': 'berita', 'id': item.id})
                        elif isinstance(item, DocumentChunk):
                            retrieved_ids.append({'type': 'document_chunk', 'id': str(item.id)})

                    log.found_results = bool(relevant_items)
                    log.retrieved_news_count = len(relevant_items)
                    log.retrieved_news_ids = retrieved_ids
                    log.final_prompt = final_prompt
                    db.session.commit()

                # Step 6: Generate respons
                yield send_thinking_status("generating", "Menyusun jawaban...")
                time.sleep(0.3)
                
                # Setelah thinking selesai, kirim marker
                yield f"data: {json.dumps({'thinking': False})}\n\n"
                
                # Mulai streaming respons AI
                for text_chunk in gemini_service.stream_generate_content(final_prompt):
                    model_response_buffer += text_chunk
                    sse_chunk = json.dumps({"text": text_chunk})
                    try:
                        yield f"data: {sse_chunk}\n\n"
                    except GeneratorExit:
                        app.logger.info(f"Client disconnected for session {session_id}")
                        break
                
                yield "data: [DONE]\n\n"
                
            except Exception as e:
                app.logger.error(f'Error in stream generation: {e}')
                error_msg = json.dumps({'error': {'message': str(e)}})
                yield f"data: {error_msg}\n\n"
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