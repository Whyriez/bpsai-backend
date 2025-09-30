from flask import Blueprint, request, jsonify
from app.models import User
from flask_jwt_extended import create_access_token, create_refresh_token, jwt_required, get_jwt_identity

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')

@auth_bp.route('/login', methods=['POST'])
def login():
    """Endpoint untuk login user dan mendapatkan JWT."""
    data = request.json
    email = data.get('email', None)
    password = data.get('password', None)

    if not email or not password:
        return jsonify({"msg": "Email dan password diperlukan"}), 400

    # Cari user di database
    user = User.query.filter_by(email=email).first()

    # Periksa apakah user ada dan passwordnya cocok
    if user and user.check_password(password):
        additional_claims = {"role": user.role}
        access_token = create_access_token(identity=str(user.id), additional_claims=additional_claims)
        refresh_token = create_refresh_token(identity=str(user.id))
        return jsonify(access_token=access_token, refresh_token=refresh_token)
    else:
        return jsonify({"msg": "Username atau password salah"}), 401


@auth_bp.route('/refresh', methods=['POST'])
@jwt_required(refresh=True)
def refresh():
    """Endpoint untuk mendapatkan access token baru menggunakan refresh token."""
    current_user_id = get_jwt_identity()
    # Buat access token baru
    new_access_token = create_access_token(identity=current_user_id)
    return jsonify(access_token=new_access_token)

@auth_bp.route('/profile', methods=['GET'])
@jwt_required() # <-- Melindungi route ini
def profile():
    """Endpoint untuk mendapatkan data user yang sedang login."""
    current_user_id_str = get_jwt_identity()
    user = User.query.get(int(current_user_id_str))
    
    if user:
        return jsonify(
            id=user.id, 
            username=user.username, 
            email=user.email,
            role=user.role
        )
    return jsonify({"msg": "User tidak ditemukan"}), 404