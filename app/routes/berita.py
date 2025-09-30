from flask import Blueprint, request, jsonify
from app.models import db, BeritaBps
from sqlalchemy import or_
import datetime
from flask_jwt_extended import jwt_required

# Membuat Blueprint untuk rute berita
berita_bp = Blueprint('berita', __name__, url_prefix='/api')

@berita_bp.route('/berita/list', methods=['GET'])
@jwt_required()
def get_berita_list():
    """
    Endpoint untuk mengambil daftar data BeritaBps dengan paginasi,
    pencarian, dan pengurutan untuk diintegrasikan dengan Datatables.
    """
    # Parameter dari DataTables
    draw = request.args.get('draw', type=int)
    start = request.args.get('start', type=int)
    length = request.args.get('length', type=int)
    search_value = request.args.get('search[value]', type=str)
    
    # Pengurutan
    order_column_index = request.args.get('order[0][column]', type=int)
    order_dir = request.args.get('order[0][dir]', type=str)
    
    # Mendapatkan nama kolom untuk pengurutan
    columns = ['id', 'judul_berita', 'tanggal_rilis', 'tags']
    
    # Atur pengurutan default jika tidak ada parameter order dari DataTables
    order_column_name = 'id'
    if order_column_index is not None and order_column_index < len(columns):
        order_column_name = columns[order_column_index]

    # Membangun query dasar
    query = BeritaBps.query
    
    # Menghitung total record sebelum filter
    total_records = query.count()
    
    # Filter pencarian
    if search_value:
        query = query.filter(or_(
            BeritaBps.judul_berita.ilike(f'%{search_value}%'),
            BeritaBps.ringkasan.ilike(f'%{search_value}%')
        ))
    
    # Menghitung total record setelah filter
    filtered_records = query.count()
    
    # Pengurutan
    order_column = getattr(BeritaBps, order_column_name)
    if order_dir == 'desc':
        query = query.order_by(order_column.desc())
    else:
        # Default ke pengurutan ascending jika tidak ada atau nilainya salah
        query = query.order_by(order_column.asc())
        
    # Paginasi
    query = query.offset(start).limit(length)
    
    # Mengambil data
    berita_list = query.all()
    
    # Format data untuk response
    data = []
    for berita in berita_list:
        data.append({
            'id': berita.id,
            'judul_berita': berita.judul_berita,
            'tanggal_rilis': berita.tanggal_rilis.strftime('%Y-%m-%d'),
            'link': berita.link,
            'ringkasan': berita.ringkasan,
            'tags': berita.tags
        })
        
    # Membuat response JSON yang sesuai format DataTables
    response = {
        'draw': draw,
        'recordsTotal': total_records,
        'recordsFiltered': filtered_records,
        'data': data
    }
    
    return jsonify(response)

@berita_bp.route('/berita', methods=['POST'])
@jwt_required()
def add_berita():
    """
    Endpoint untuk menambahkan data BeritaBps baru.
    Embedding akan digenerate secara otomatis oleh model listener.
    """
    data = request.get_json()

    if not data:
        return jsonify({'error': 'Request body harus dalam format JSON'}), 400

    # Mengambil data dari body request
    judul_berita = data.get('judul_berita')
    tanggal_rilis_str = data.get('tanggal_rilis')
    link_sumber = data.get('link_sumber')
    tags = data.get('tags')  # Bisa berupa list atau string dipisah koma
    ringkasan = data.get('ringkasan')

    # Validasi input wajib
    if not all([judul_berita, tanggal_rilis_str, link_sumber, ringkasan]):
        return jsonify({'error': 'Field judul_berita, tanggal_rilis, link_sumber, dan ringkasan tidak boleh kosong'}), 400

    # Konversi tanggal_rilis dari string ke objek date
    try:
        tanggal_rilis = datetime.datetime.strptime(tanggal_rilis_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Format tanggal_rilis harus YYYY-MM-DD'}), 400
        
    # Memastikan tags adalah list
    if isinstance(tags, str):
        # Membersihkan spasi dan mengubah jadi list jika inputnya string
        processed_tags = [tag.strip() for tag in tags.split(',') if tag.strip()]
    elif isinstance(tags, list):
        processed_tags = tags
    else:
        # Jika tidak ada tags atau formatnya salah, default ke list kosong
        processed_tags = []

    # Membuat instance baru dari model BeritaBps
    new_berita = BeritaBps(
        judul_berita=judul_berita,
        tanggal_rilis=tanggal_rilis,
        link=link_sumber,
        tags=processed_tags,
        ringkasan=ringkasan
    )

    try:
        # Menambahkan ke sesi database dan commit
        db.session.add(new_berita)
        db.session.commit()

        # Respon sukses
        return jsonify({
            'message': 'Data Berita BPS berhasil ditambahkan.',
            'id': new_berita.id
        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Terjadi kesalahan saat menyimpan data: {str(e)}'}), 500
    
@berita_bp.route('/berita/<int:berita_id>', methods=['GET'])
@jwt_required()
def get_berita_by_id(berita_id):
    """
    Endpoint untuk mengambil satu data BeritaBps berdasarkan ID.
    """
    berita = BeritaBps.query.get(berita_id)
    if not berita:
        return jsonify({'error': 'Data tidak ditemukan'}), 404

    return jsonify({
        'id': berita.id,
        'judul_berita': berita.judul_berita,
        'tanggal_rilis': berita.tanggal_rilis.strftime('%Y-%m-%d'),
        'link_sumber': berita.link,
        'ringkasan': berita.ringkasan,
        'tags': berita.tags
    })

@berita_bp.route('/berita/<int:berita_id>', methods=['PUT'])
@jwt_required()
def update_berita(berita_id):
    """
    Endpoint untuk memperbarui data BeritaBps yang ada.
    """
    berita = BeritaBps.query.get(berita_id)
    if not berita:
        return jsonify({'error': 'Data tidak ditemukan'}), 404

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body harus dalam format JSON'}), 400

    # Mengambil data dari body request
    berita.judul_berita = data.get('judul_berita', berita.judul_berita)
    berita.link = data.get('link_sumber', berita.link)
    berita.ringkasan = data.get('ringkasan', berita.ringkasan)
    
    tanggal_rilis_str = data.get('tanggal_rilis')
    if tanggal_rilis_str:
        try:
            berita.tanggal_rilis = datetime.datetime.strptime(tanggal_rilis_str, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'error': 'Format tanggal_rilis harus YYYY-MM-DD'}), 400

    tags = data.get('tags')
    if tags is not None:
        if isinstance(tags, str):
            berita.tags = [tag.strip() for tag in tags.split(',') if tag.strip()]
        elif isinstance(tags, list):
            berita.tags = tags

    try:
        db.session.commit()
        return jsonify({'message': f'Data Berita BPS dengan ID {berita_id} berhasil diperbarui.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Terjadi kesalahan saat memperbarui data: {str(e)}'}), 500
    
@berita_bp.route('/berita/delete/<int:berita_id>', methods=['DELETE'])
@jwt_required()
def delete_berita(berita_id):
    """
    Endpoint untuk menghapus data BeritaBps berdasarkan ID.
    """
    berita = BeritaBps.query.get(berita_id)
    if not berita:
        return jsonify({'error': 'Data tidak ditemukan'}), 404
        
    try:
        db.session.delete(berita)
        db.session.commit()
        return jsonify({'message': f'Data Berita BPS dengan ID {berita_id} berhasil dihapus.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Terjadi kesalahan saat menghapus data: {str(e)}'}), 500