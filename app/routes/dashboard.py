from flask import Blueprint, jsonify
from app.models import db, PromptLog, Feedback
from sqlalchemy import func, distinct, case
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from flask_jwt_extended import jwt_required

# Membuat Blueprint untuk rute dashboard
dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/api/dashboard')

def calculate_percentage_change(current, previous):
    """Menghitung perubahan persentase antara dua nilai."""
    if previous is None or previous == 0:
        return float('inf') if current > 0 else 0
    return ((current - previous) / previous) * 100

def format_subtitle(change, unit='%', period='dari bulan lalu'):
    """Memformat string subtitle berdasarkan nilai perubahan."""
    if change == float('inf'):
        return "↗ Aktivitas baru"
    if change > 0:
        return f"↗ +{change:.1f}{unit} {period}"
    elif change < 0:
        return f"↘ {change:.1f}{unit} {period}"
    else:
        return f"~ Tidak ada perubahan {period}"
    
@dashboard_bp.route('/kpis', methods=['GET'])
@jwt_required()
def get_kpis():
    """
    Endpoint untuk mengambil semua data Key Performance Indicators (KPIs).
    ---
    tags:
      - Dashboard
    summary: Mendapatkan 4 KPI utama untuk dashboard.
    security:
      - Bearer: []
    responses:
      200:
        description: Berhasil mengambil data KPI.
        schema:
          type: object
          properties:
            total_sessions:
              type: object
            active_users:
              type: object
            avg_conversation:
              type: object
            feedback_ratio:
              type: object
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
    """
    now = datetime.utcnow()
    
    # --- Definisi Periode Waktu ---
    # Bulan Ini
    start_of_current_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Bulan Lalu
    start_of_last_month = start_of_current_month - relativedelta(months=1)
    end_of_last_month = start_of_current_month - timedelta(microseconds=1)
    # 24 & 48 Jam Lalu
    twenty_four_hours_ago = now - timedelta(hours=24)
    forty_eight_hours_ago = now - timedelta(hours=48)

    # --- Kalkulasi KPI ---

    # 1. Total Sessions (Bulan Ini vs Bulan Lalu)
    current_month_sessions = db.session.query(func.count(distinct(PromptLog.session_id)))\
        .filter(PromptLog.created_at >= start_of_current_month).scalar() or 0
    last_month_sessions = db.session.query(func.count(distinct(PromptLog.session_id)))\
        .filter(PromptLog.created_at.between(start_of_last_month, end_of_last_month)).scalar() or 0
    sessions_change = calculate_percentage_change(current_month_sessions, last_month_sessions)

    # 2. Active Users (24 Jam Terakhir vs 24 Jam Sebelumnya)
    daily_active_users = db.session.query(func.count(distinct(PromptLog.session_id)))\
        .filter(PromptLog.created_at >= twenty_four_hours_ago).scalar() or 0
    previous_day_active_users = db.session.query(func.count(distinct(PromptLog.session_id)))\
        .filter(PromptLog.created_at.between(forty_eight_hours_ago, twenty_four_hours_ago)).scalar() or 0
    users_change = calculate_percentage_change(daily_active_users, previous_day_active_users)

    # 3. Avg Conversation (Keseluruhan)
    total_prompts = db.session.query(func.count(PromptLog.id)).scalar() or 0
    total_sessions_overall = db.session.query(func.count(distinct(PromptLog.session_id))).scalar() or 0
    avg_conversation = round(total_prompts / total_sessions_overall, 1) if total_sessions_overall > 0 else 0

    # 4. Feedback Ratio (Bulan Ini vs Bulan Lalu)
    current_month_feedback_total = db.session.query(func.count(Feedback.id))\
        .filter(Feedback.created_at >= start_of_current_month).scalar() or 0
    current_month_feedback_positive = db.session.query(func.count(Feedback.id))\
        .filter(Feedback.created_at >= start_of_current_month, Feedback.type == 'positive').scalar() or 0
    current_feedback_ratio = (current_month_feedback_positive / current_month_feedback_total * 100) if current_month_feedback_total > 0 else 0
    
    last_month_feedback_total = db.session.query(func.count(Feedback.id))\
        .filter(Feedback.created_at.between(start_of_last_month, end_of_last_month)).scalar() or 0
    last_month_feedback_positive = db.session.query(func.count(Feedback.id))\
        .filter(Feedback.created_at.between(start_of_last_month, end_of_last_month), Feedback.type == 'positive').scalar() or 0
    last_month_feedback_ratio = (last_month_feedback_positive / last_month_feedback_total * 100) if last_month_feedback_total > 0 else 0
    
    feedback_change = current_feedback_ratio - last_month_feedback_ratio

    return jsonify({
        'total_sessions': {
            'value': f'{current_month_sessions:,}',
            'subtitle': format_subtitle(sessions_change)
        },
        'active_users': {
            'value': f'{daily_active_users:,}',
            'subtitle': format_subtitle(users_change, period='dari kemarin')
        },
        'avg_conversation': {
            'value': avg_conversation,
            'subtitle': 'pesan per sesi'
        },
        'feedback_ratio': {
            'value': f'{int(current_feedback_ratio)}%',
            'subtitle': format_subtitle(feedback_change, unit=' poin')
        }
    })

@dashboard_bp.route('/charts/questions-frequency', methods=['GET'])
@jwt_required()
def get_questions_frequency():
    """
    Endpoint untuk data frekuensi pertanyaan pengguna per bulan (12 bulan terakhir).
    ---
    tags:
      - Dashboard
    summary: Data chart untuk frekuensi pertanyaan (12 bulan).
    security:
      - Bearer: []
    responses:
      200:
        description: Data chart line untuk frekuensi pertanyaan.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
    """
    twelve_months_ago = datetime.utcnow() - timedelta(days=365)
    
    # Query untuk menghitung prompt per bulan
    results = db.session.query(
        func.date_trunc('month', PromptLog.created_at).label('month'),
        func.count(PromptLog.id).label('count')
    ).filter(PromptLog.created_at >= twelve_months_ago)\
     .group_by('month')\
     .order_by('month')\
     .all()
     
    # Inisialisasi data untuk 12 bulan dengan nilai 0
    monthly_data = {}
    for i in range(12):
        month_date = (datetime.utcnow() - timedelta(days=i*30)).replace(day=1)
        month_key = month_date.strftime('%Y-%m')
        monthly_data[month_key] = 0

    # Isi data dari hasil query
    for row in results:
        month_key = row.month.strftime('%Y-%m')
        monthly_data[month_key] = row.count

    # Urutkan dan siapkan label dan data
    sorted_months = sorted(monthly_data.keys())
    labels = [datetime.strptime(m, '%Y-%m').strftime('%b') for m in sorted_months]
    data = [monthly_data[m] for m in sorted_months]

    return jsonify({
        'labels': labels,
        'datasets': [{
            'label': "Questions per Month",
            'data': data
        }]
    })

@dashboard_bp.route('/charts/intent-distribution', methods=['GET'])
@jwt_required()
def get_intent_distribution():
    """
    Endpoint untuk data distribusi intent.
    ---
    tags:
      - Dashboard
    summary: Data chart untuk distribusi intent (donut chart).
    security:
      - Bearer: []
    responses:
      200:
        description: Data chart donut untuk distribusi intent.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
    """
    results = db.session.query(
        PromptLog.detected_intent,
        func.count(PromptLog.id)
    ).group_by(PromptLog.detected_intent).all()
    
    # Filter hasil yang intent-nya None (tidak terdeteksi)
    filtered_results = [r for r in results if r[0] is not None]

    return jsonify({
        'labels': [row[0] for row in filtered_results],
        'datasets': [{
            'data': [row[1] for row in filtered_results]
        }]
    })

@dashboard_bp.route('/charts/question-types', methods=['GET'])
@jwt_required()
def get_question_types():
    """
    Endpoint untuk data tipe pertanyaan (unik vs berulang).
    ---
    tags:
      - Dashboard
    summary: Data chart untuk tipe pertanyaan unik vs berulang (pie chart).
    security:
      - Bearer: []
    responses:
      200:
        description: Data chart pie untuk tipe pertanyaan.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
    """
    total_prompts = db.session.query(func.count(PromptLog.id)).scalar() or 0
    
    if total_prompts == 0:
        return jsonify({
            'labels': ["Unique", "Repeated"],
            'datasets': [{'data': [0, 0]}]
        })

    unique_prompts = db.session.query(func.count(distinct(PromptLog.user_prompt))).scalar() or 0
    repeated_prompts = total_prompts - unique_prompts
    
    return jsonify({
        'labels': ["Unique", "Repeated"],
        'datasets': [{
            'data': [unique_prompts, repeated_prompts]
        }]
    })

@dashboard_bp.route('/recent-activity', methods=['GET'])
@jwt_required()
def get_recent_activity():
    """
    Endpoint untuk mengambil 5 aktivitas (prompt) terbaru.
    ---
    tags:
      - Dashboard
    summary: Mendapatkan 5 log prompt terbaru.
    security:
      - Bearer: []
    responses:
      200:
        description: Daftar 5 prompt terbaru.
        schema:
          type: array
          items:
            type: object
            properties:
              prompt:
                type: string
              timestamp:
                type: string
                format: date-time
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
    """
    recent_logs = PromptLog.query.order_by(PromptLog.created_at.desc()).limit(5).all()
    
    activities = []
    for log in recent_logs:
        activities.append({
            'prompt': log.user_prompt,
            'timestamp': log.created_at.isoformat()
        })
        
    return jsonify(activities)