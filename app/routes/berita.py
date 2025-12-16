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
    ---
    tags:
      - Berita BPS
    summary: Mendapatkan daftar berita BPS (untuk Datatables).
    security:
      - Bearer: []
    parameters:
      - name: draw
        in: query
        type: integer
        description: Nomor draw dari Datatables untuk sinkronisasi.
      - name: start
        in: query
        type: integer
        description: Indeks awal untuk paginasi.
      - name: length
        in: query
        type: integer
        description: Jumlah data yang akan diambil (ukuran halaman).
      - name: search[value]
        in: query
        type: string
        description: Kata kunci pencarian.
      - name: order[0][column]
        in: query
        type: integer
        description: Indeks kolom yang akan diurutkan.
      - name: order[0][dir]
        in: query
        type: string
        description: Arah pengurutan (asc atau desc).
    responses:
      200:
        description: Sukses, mengembalikan data format Datatables.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
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
    ---
    tags:
      - Berita BPS
    summary: Menambahkan data berita BPS baru.
    security:
      - Bearer: []
    parameters:
      - in: body
        name: body
        description: Data berita BPS yang akan ditambahkan.
        required: true
        schema:
          type: object
          properties:
            judul_berita:
              type: string
              example: "Inflasi Gorontalo Bulan Oktober 2025"
            tanggal_rilis:
              type: string
              format: date
              example: "2025-11-01"
            link_sumber:
              type: string
              example: "https://gorontalo.bps.go.id/..."
            tags:
              type: array
              items:
                type: string
              example: ["inflasi", "gorontalo", "2025"]
            ringkasan:
              type: string
              example: "Inflasi Gorontalo pada bulan Oktober 2025 tercatat..."
    responses:
      201:
        description: Data berita berhasil ditambahkan.
      400:
        description: Format data salah atau field wajib tidak diisi.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
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
    ---
    tags:
      - Berita BPS
    summary: Mendapatkan detail satu berita berdasarkan ID.
    security:
      - Bearer: []
    parameters:
      - name: berita_id
        in: path
        type: integer
        required: true
        description: ID unik dari data berita.
    responses:
      200:
        description: Sukses, mengembalikan detail berita.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
      404:
        description: Data berita tidak ditemukan.
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
    ---
    tags:
      - Berita BPS
    summary: Memperbarui data berita BPS yang ada.
    security:
      - Bearer: []
    parameters:
      - name: berita_id
        in: path
        type: integer
        required: true
        description: ID unik dari data berita yang akan diperbarui.
      - in: body
        name: body
        description: Data berita BPS yang akan diperbarui.
        required: true
        schema:
          type: object
          properties:
            judul_berita:
              type: string
            tanggal_rilis:
              type: string
              format: date
            link_sumber:
              type: string
            tags:
              type: array
              items:
                type: string
            ringkasan:
              type: string
    responses:
      200:
        description: Data berita berhasil diperbarui.
      400:
        description: Format data salah.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
      404:
        description: Data berita tidak ditemukan.
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
    ---
    tags:
      - Berita BPS
    summary: Menghapus data berita BPS berdasarkan ID.
    security:
      - Bearer: []
    parameters:
      - name: berita_id
        in: path
        type: integer
        required: true
        description: ID unik dari data berita yang akan dihapus.
    responses:
      200:
        description: Data berita berhasil dihapus.
      401:
        description: Token tidak valid atau tidak ada (Unauthorized).
      404:
        description: Data berita tidak ditemukan.
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