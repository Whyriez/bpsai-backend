import imaplib
import ssl
from flask import Blueprint, request, jsonify
from app.models import User
from app import db
from sqlalchemy import or_
from flask_jwt_extended import create_access_token, create_refresh_token, jwt_required, get_jwt_identity

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')


@auth_bp.route('/login', methods=['POST'])
def login():
    """Endpoint untuk login user (IMAP Only).
    ---
    tags:
      - Authentication
    summary: Login user menggunakan Akun BPS (IMAP).
    description: >
        Endpoint ini memverifikasi kredensial langsung ke server email BPS (IMAP).
        Tidak ada pengecekan password lokal.

        Alur:
        1. User input Username/Email & Password.
        2. Sistem mencoba login ke IMAP BPS.
        3. Jika sukses, sistem mengecek apakah Username/Email tersebut terdaftar di database aplikasi (Authorization).
    parameters:
      - in: body
        name: body
        description: Kredensial login.
        required: true
        schema:
          type: object
          properties:
            email:
              type: string
              description: Username (nip/nama) atau Email BPS.
              example: "nur.alim"
            password:
              type: string
              description: Password email BPS.
              example: "password_email_bps"
    responses:
      200:
        description: Login berhasil, mengembalikan token JWT.
      401:
        description: Password salah atau Login IMAP gagal.
      403:
        description: Login Email berhasil, tapi user tidak terdaftar di aplikasi ini (Hubungi Admin).
      500:
        description: Error koneksi ke server IMAP.
    """
    data = request.json
    # Input bisa berupa "nama" atau "nama@bps.go.id"
    input_identifier = data.get('email', None)
    password = data.get('password', None)

    if not input_identifier or not password:
        return jsonify({"msg": "Username/Email dan password diperlukan"}), 400

    # ---------------------------------------------------------
    # 1. PERSIAPAN USERNAME UNTUK IMAP
    # ---------------------------------------------------------
    # Server IMAP biasanya login menggunakan username saja (sebelum @)
    # Jadi kita ambil bagian depannya saja untuk login ke mail server
    if '@' in input_identifier:
        imap_username = input_identifier.split('@')[0]
    else:
        imap_username = input_identifier

    # ---------------------------------------------------------
    # 2. PROSES AUTENTIKASI KE IMAP (Satu-satunya cara verifikasi password)
    # ---------------------------------------------------------
    try:
        imap_host = 'mail.bps.go.id'
        imap_port = 993

        # Konfigurasi SSL Context (Legacy Support untuk Server Lama)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.set_ciphers('DEFAULT@SECLEVEL=1')  # Penting untuk server lama

        # Koneksi ke IMAP
        mail = imaplib.IMAP4_SSL(imap_host, imap_port, ssl_context=context)

        # Coba Login
        mail.login(imap_username, password)

        # Jika lolos baris ini, berarti Password BENAR
        mail.logout()
        print(f"✅ IMAP Auth Success: {imap_username}")

    except imaplib.IMAP4.error as e:
        print(f"❌ IMAP Auth Failed: {e}")
        return jsonify({"msg": "Username atau password salah"}), 401
    except Exception as e:
        print(f"⚠️ IMAP Connection Error: {e}")
        return jsonify({"msg": "Gagal terhubung ke server email BPS"}), 500

    # ---------------------------------------------------------
    # 3. PROSES OTORISASI (Cek Database Lokal)
    # ---------------------------------------------------------
    # Kita cari user di DB berdasarkan username atau email yang mungkin cocok
    # Karena DB tidak menyimpan password, kita hanya cek keberadaan user & role-nya

    possible_email = f"{imap_username}@bps.go.id"

    user = User.query.filter(
        or_(
            User.username == imap_username,  # Cek field username
            User.email == possible_email,  # Cek email lengkap
            User.email == input_identifier  # Cek input mentah user
        )
    ).first()

    if not user:
        # Kasus: Orang BPS asli (login IMAP sukses), tapi belum didaftarkan Admin di aplikasi
        return jsonify({
            "msg": "Login berhasil, namun akun Anda belum terdaftar di sistem ini. Silahkan hubungi Administrator."
        }), 403

    # 4. Generate Token jika user valid
    return generate_user_token(user)


def generate_user_token(user):
    """Helper function untuk generate token JWT."""
    additional_claims = {"role": user.role}
    access_token = create_access_token(identity=str(user.id), additional_claims=additional_claims)
    refresh_token = create_refresh_token(identity=str(user.id))

    # Fallback username handling
    username_display = getattr(user, 'username', None)
    if not username_display:
        username_display = user.email.split('@')[0]

    return jsonify({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": {
            "id": user.id,
            "username": username_display,
            "email": user.email,
            "role": user.role
        }
    }), 200


# Route Refresh & Profile tetap sama, karena menggunakan JWT Identity (ID User)
# Tidak perlu perubahan pada route di bawah ini.

@auth_bp.route('/refresh', methods=['POST'])
@jwt_required(refresh=True)
def refresh():
    current_user_id = get_jwt_identity()
    new_access_token = create_access_token(identity=current_user_id)
    return jsonify(access_token=new_access_token)


@auth_bp.route('/profile', methods=['GET'])
@jwt_required()
def profile():
    current_user_id_str = get_jwt_identity()
    user = User.query.get(int(current_user_id_str))

    if user:
        return jsonify(
            id=user.id,
            username=getattr(user, 'username', user.email.split('@')[0]),
            email=user.email,
            role=user.role
        )
    return jsonify({"msg": "User tidak ditemukan"}), 404