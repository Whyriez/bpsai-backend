from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.models import db, GeminiApiKeyConfig, User
from app.env_manager import EnvManager
from datetime import datetime
from dotenv import load_dotenv
import pytz
import re

from app.routes.chat import gemini_service, embedding_service

api_keys_bp = Blueprint('api_keys', __name__)
env_manager = EnvManager()

@api_keys_bp.route('/api/gemini-keys', methods=['GET'])
# @jwt_required()
def get_api_keys():
    """
    Mendapatkan semua info API keys (tanpa value sebenarnya).
    ---
    tags:
      - API Key Management
    summary: Mendapatkan daftar dan status semua API key Gemini.
    security:
      - Bearer: []
    responses:
      200:
        description: Daftar API key berhasil diambil.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
      500:
        description: Gagal mengambil data API keys.
    """
    try:
        # Dapatkan config dari database
        key_configs = GeminiApiKeyConfig.query.order_by(GeminiApiKeyConfig.key_alias).all()
        
        # Dapatkan keys dari .env
        env_keys = env_manager.get_gemini_keys()
        
        keys_data = []
        for config in key_configs:
            key_value = env_keys.get(config.key_alias)
            time_until_reset = config.get_time_until_reset()
            
            key_data = {
                'id': config.id,
                'key_alias': config.key_alias,
                'key_name': config.key_name,
                'has_value': bool(key_value),
                'value_preview': f"{key_value[:8]}...{key_value[-4:]}" if key_value else None,
                'quota_exceeded': config.quota_exceeded,
                'quota_exceeded_at': config.quota_exceeded_at.isoformat() if config.quota_exceeded_at else None,
                'time_until_reset': time_until_reset,
                'last_used': config.last_used.isoformat() if config.last_used else None,
                'total_requests': config.total_requests,
                'failed_requests': config.failed_requests,
                'success_rate': round(((config.total_requests - config.failed_requests) / config.total_requests * 100) if config.total_requests > 0 else 100, 2),
                'is_active': config.is_active,
                'created_at': config.created_at.isoformat()
            }
            keys_data.append(key_data)
        
        return jsonify({
            'success': True,
            'keys': keys_data
        })
        
    except Exception as e:
        current_app.logger.error(f'Error fetching API keys: {e}')
        return jsonify({'success': False, 'error': 'Gagal mengambil data API keys'}), 500

@api_keys_bp.route('/api/gemini-keys', methods=['POST'])
# @jwt_required()
def add_api_key():
    """
    Menambah API key baru ke .env dan me-reload service secara dinamis.
    ---
    tags:
      - API Key Management
    summary: Menambahkan API key baru.
    security:
      - Bearer: []
    parameters:
      - in: body
        name: body
        description: API key dan nama untuk key baru.
        required: true
        schema:
          type: object
          properties:
            api_key:
              type: string
              example: "AIzaSy...your...key...here"
            key_name:
              type: string
              example: "Kunci Cadangan 1"
    responses:
      201:
        description: API key berhasil ditambahkan.
      400:
        description: Format data salah atau key tidak valid.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
      500:
        description: "Gagal menambahkan API key (misal: .env tidak bisa ditulis)."
    """
    try:
        data = request.json
        api_key = data.get('api_key')
        key_name = data.get('key_name')
        
        if not api_key or not key_name:
            return jsonify({'success': False, 'error': 'API key dan nama key diperlukan'}), 400
        
        if not api_key.startswith('AI'):
            return jsonify({'success': False, 'error': 'Format API key tidak valid'}), 400
        
        # Cari alias berikutnya (KEY_1, KEY_2, dst)
        existing_keys = env_manager.get_gemini_keys()
        next_number = 1
        while f'{next_number}' in existing_keys:
            next_number += 1
        
        alias = f'{next_number}'
        
        # Tambah ke .env
        success = env_manager.add_single_key(alias, api_key, key_name)
        if not success:
            return jsonify({'success': False, 'error': 'Gagal menulis ke file .env'}), 500
        
        # Reload environment variables agar bisa dibaca oleh os.getenv()
        load_dotenv()
        
        # Panggil metode reload pada service agar daftar key internal mereka terupdate
        gemini_service.reload_keys()
        embedding_service.reload_keys()
        
        # Buat/update config di database
        config = GeminiApiKeyConfig.query.filter_by(key_alias=alias).first()
        if not config:
            config = GeminiApiKeyConfig(
                key_alias=alias,
                key_name=key_name,
                is_active=True
            )
            db.session.add(config)
        else:
            config.key_name = key_name
            config.is_active = True
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'API key berhasil ditambahkan dan langsung aktif',
            'key_alias': alias
        }), 201
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error adding API key: {e}')
        return jsonify({'success': False, 'error': 'Gagal menambahkan API key'}), 500

@api_keys_bp.route('/api/gemini-keys/<string:alias>', methods=['PUT'])
@jwt_required()
def update_api_key(alias):
    """
    Update metadata API key (nama atau status aktif).
    ---
    tags:
      - API Key Management
    summary: Memperbarui nama atau status aktif API key.
    security:
      - Bearer: []
    parameters:
      - name: alias
        in: path
        type: string
        required: true
        description: "Alias dari key (misal: '1', '2')."
      - in: body
        name: body
        description: Data yang akan diperbarui.
        required: true
        schema:
          type: object
          properties:
            key_name:
              type: string
              example: "Kunci Utama (Sudah Diperbarui)"
            is_active:
              type: boolean
              example: false
    responses:
      200:
        description: API key berhasil diupdate.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
      404:
        description: API key tidak ditemukan.
      500:
        description: Gagal mengupdate API key.
    """
    try:
        data = request.json
        config = GeminiApiKeyConfig.query.filter_by(key_alias=alias).first_or_404()
        
        if 'key_name' in data:
            config.key_name = data['key_name']
        if 'is_active' in data:
            config.is_active = data['is_active']
            # Jika diaktifkan ulang, reset quota status jika sudah waktunya
            if data['is_active'] and config.quota_exceeded:
                reset_time = config.get_quota_reset_time()
                if reset_time and reset_time <= datetime.now(pytz.utc):
                    config.quota_exceeded = False
                    config.quota_exceeded_at = None
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'API key berhasil diupdate'
        })
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error updating API key: {e}')
        return jsonify({'success': False, 'error': 'Gagal mengupdate API key'}), 500

@api_keys_bp.route('/api/gemini-keys/<string:alias>', methods=['DELETE'])
# @jwt_required()
def delete_api_key(alias):
    """
    Hapus API key dari .env, database, dan reload service secara dinamis.
    ---
    tags:
      - API Key Management
    summary: Menghapus API key secara permanen.
    security:
      - Bearer: []
    parameters:
      - name: alias
        in: path
        type: string
        required: true
        description: "Alias dari key yang akan dihapus (misal: '1', '2')."
    responses:
      200:
        description: API key berhasil dihapus.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
      500:
        description: Gagal menghapus API key.
    """
    try:
        # Hapus dari .env file
        success = env_manager.remove_key(alias)
        if not success:
            return jsonify({'success': False, 'error': 'Gagal menghapus dari file .env'}), 500
        
        # Hapus dari database
        config = GeminiApiKeyConfig.query.filter_by(key_alias=alias).first()
        if config:
            db.session.delete(config)
            db.session.commit()
        
        # Reload environment variables
        load_dotenv()
        
        # Panggil metode reload pada service
        gemini_service.reload_keys()
        embedding_service.reload_keys()
        
        return jsonify({
            'success': True,
            'message': 'API key berhasil dihapus permanen'
        })
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error deleting API key: {e}')
        return jsonify({'success': False, 'error': 'Gagal menghapus API key'}), 500

@api_keys_bp.route('/api/gemini-keys/sync', methods=['POST'])
@jwt_required()
def sync_keys():
    """
    Sinkronisasi key dari file .env ke database (jika ada key di .env tapi belum ada di DB).
    ---
    tags:
      - API Key Management
    summary: Sinkronisasi key dari .env ke database.
    security:
      - Bearer: []
    responses:
      200:
        description: Sinkronisasi berhasil.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
      500:
        description: Gagal sync API keys.
    """
    try:
        env_keys = env_manager.get_gemini_keys()
        
        synced_count = 0
        for alias, key_value in env_keys.items():
            config = GeminiApiKeyConfig.query.filter_by(key_alias=alias).first()
            if not config:
                config = GeminiApiKeyConfig(
                    key_alias=alias,
                    key_name=f"API Key {alias.replace('KEY_', '')}",
                    is_active=True
                )
                db.session.add(config)
                synced_count += 1
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Berhasil sync {synced_count} API keys',
            'synced_count': synced_count
        })
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error syncing API keys: {e}')
        return jsonify({'success': False, 'error': 'Gagal sync API keys'}), 500

@api_keys_bp.route('/api/gemini-keys/stats', methods=['GET'])
@jwt_required()
def get_api_keys_stats():
    """
    Mendapatkan statistik penggunaan agregat semua API keys.
    ---
    tags:
      - API Key Management
    summary: Mendapatkan statistik agregat semua API key.
    security:
      - Bearer: []
    responses:
      200:
        description: Statistik berhasil diambil.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
      500:
        description: Gagal mengambil statistik API keys.
    """
    try:
        total_keys = GeminiApiKeyConfig.query.count()
        active_keys = GeminiApiKeyConfig.query.filter_by(is_active=True).count()
        quota_exceeded_keys = GeminiApiKeyConfig.query.filter_by(quota_exceeded=True, is_active=True).count()
        available_keys = active_keys - quota_exceeded_keys
        
        total_requests = db.session.query(db.func.sum(GeminiApiKeyConfig.total_requests)).scalar() or 0
        failed_requests = db.session.query(db.func.sum(GeminiApiKeyConfig.failed_requests)).scalar() or 0
        success_rate = round(((total_requests - failed_requests) / total_requests * 100) if total_requests > 0 else 100, 2)
        
        return jsonify({
            'success': True,
            'stats': {
                'total_keys': total_keys,
                'active_keys': active_keys,
                'quota_exceeded_keys': quota_exceeded_keys,
                'available_keys': available_keys,
                'total_requests': total_requests,
                'failed_requests': failed_requests,
                'success_rate': success_rate
            }
        })
        
    except Exception as e:
        current_app.logger.error(f'Error fetching API keys stats: {e}')
        return jsonify({'success': False, 'error': 'Gagal mengambil statistik API keys'}), 500