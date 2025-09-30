from flask import Blueprint, jsonify
from app.models import db, PromptLog, Feedback
from sqlalchemy import func, cast, TEXT, distinct
from collections import Counter
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from flask_jwt_extended import jwt_required

# Membuat Blueprint untuk rute analytics
analytics_bp = Blueprint('analytics', __name__, url_prefix='/api/analytics')

@analytics_bp.route('/all', methods=['GET'])
@jwt_required()
def get_all_analytics():
    """
    Endpoint untuk mengambil semua data yang diperlukan untuk halaman Advanced Analytics.
    """
     # --- 1. Tren Penggunaan (Usage Trends) ---
    usage_trends_data = {
        'labels': [],
        'datasets': []
    }
    daily_stats = {}
    # Inisialisasi 30 hari terakhir dengan 0
    for i in range(30):
        day = datetime.utcnow() - timedelta(days=i)
        daily_stats[day.strftime('%Y-%m-%d')] = {'sessions': 0, 'prompts': 0}

    thirty_days_ago = datetime.utcnow() - timedelta(days=30)

    # Query untuk menghitung sesi unik dan total prompt per hari
    usage_results = db.session.query(
        func.date_trunc('day', PromptLog.created_at).label('day'),
        func.count(distinct(PromptLog.session_id)).label('unique_sessions'),
        func.count(PromptLog.id).label('total_prompts')
    ).filter(PromptLog.created_at >= thirty_days_ago)\
     .group_by('day')\
     .all()

    # Isi data dari hasil query
    for row in usage_results:
        if row.day:
            day_str = row.day.strftime('%Y-%m-%d')
            if day_str in daily_stats:
                daily_stats[day_str]['sessions'] = row.unique_sessions
                daily_stats[day_str]['prompts'] = row.total_prompts
    
    # Urutkan dan format untuk chart
    sorted_days = sorted(daily_stats.keys())
    
    usage_trends_data['labels'] = [datetime.strptime(d, '%Y-%m-%d').strftime('%b %d') for d in sorted_days]
    
    sessions_data = [daily_stats[d]['sessions'] for d in sorted_days]
    prompts_data = [daily_stats[d]['prompts'] for d in sorted_days]
    
    usage_trends_data['datasets'] = [
        {
            'label': 'Sesi Harian',
            'data': sessions_data,
            'borderColor': '#3b82f6',
            'backgroundColor': 'rgba(59, 130, 246, 0.1)',
            'tension': 0.4,
            'fill': True
        },
        {
            'label': 'Total Interaksi',
            'data': prompts_data,
            'borderColor': '#10b981',
            'backgroundColor': 'rgba(16, 185, 129, 0.1)',
            'tension': 0.4,
            'fill': True
        }
    ]

    # --- 1. Analisis Waktu Respons ---
    response_time_query = db.session.query(PromptLog.processing_time_ms)\
        .filter(PromptLog.processing_time_ms.isnot(None))
    
    total_logs_with_time = response_time_query.count()
    avg_response_time = response_time_query.with_entities(func.avg(PromptLog.processing_time_ms)).scalar() or 0

    if total_logs_with_time > 0:
        fast_count = response_time_query.filter(PromptLog.processing_time_ms < 100).count()
        medium_count = response_time_query.filter(PromptLog.processing_time_ms.between(100, 500)).count()
        slow_count = response_time_query.filter(PromptLog.processing_time_ms > 500).count()
        response_time_distribution = {
            'fast': round((fast_count / total_logs_with_time) * 100),
            'medium': round((medium_count / total_logs_with_time) * 100),
            'slow': round((slow_count / total_logs_with_time) * 100)
        }
    else:
        response_time_distribution = {'fast': 0, 'medium': 0, 'slow': 0}

    # --- 2. Top Keywords & Topics ---
    all_keywords_logs = db.session.query(PromptLog.extracted_keywords)\
        .filter(PromptLog.extracted_keywords.isnot(None)).all()
    
    keyword_list = [keyword for log in all_keywords_logs for keyword in log[0]]
    top_keywords = [{'keyword': item, 'count': count} for item, count in Counter(keyword_list).most_common(6)]

    # --- 3. Tingkat Keberhasilan Pengambilan Data ---
    data_requests_query = PromptLog.query.filter(PromptLog.detected_intent == 'data_request')
    total_data_requests = data_requests_query.count()
    successful_requests = data_requests_query.filter(PromptLog.found_results == True).count()
    retrieval_success_rate = (successful_requests / total_data_requests * 100) if total_data_requests > 0 else 0

    # --- 4. Cakupan Data (Data Coverage) ---
    data_coverage = []
    current_year = datetime.utcnow().year
    for year in range(current_year, current_year - 3, -1):
        # Menggunakan cast(..., TEXT) untuk mengubah JSON array ke text
        year_query = PromptLog.query.filter(cast(PromptLog.extracted_years, TEXT).like(f'%{year}%'))
        total_year_queries = year_query.count()
        successful_year_queries = year_query.filter(PromptLog.found_results == True).count()
        coverage = (successful_year_queries / total_year_queries * 100) if total_year_queries > 0 else 0
        data_coverage.append({'year': year, 'coverage': round(coverage)})

    # --- 5. Performa Layanan ---
    total_feedback = db.session.query(func.count(Feedback.id)).scalar() or 0
    positive_feedback = db.session.query(func.count(Feedback.id)).filter(Feedback.type == 'positive').scalar() or 0
    negative_feedback = total_feedback - positive_feedback
    
    accuracy = (positive_feedback / total_feedback * 100) if total_feedback > 0 else 0
    satisfaction_score = ((positive_feedback * 5) + (negative_feedback * 1)) / total_feedback if total_feedback > 0 else 0

    return jsonify({
        'usageTrends': usage_trends_data,
        'responseTime': {
            'average': round(avg_response_time),
            'distribution': response_time_distribution
        },
        'topKeywords': top_keywords,
        'retrievalSuccessRate': round(retrieval_success_rate, 1),
        'dataCoverage': data_coverage,
        'servicePerformance': {
            'uptime': "99.9%", # Nilai statis karena uptime biasanya dimonitor eksternal
            'accuracy': round(accuracy, 1),
            'userSatisfaction': round(satisfaction_score, 1)
        }
    })
