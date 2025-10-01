import os
from flask import Flask
from dotenv import load_dotenv
from .models import db
from flask_cors import CORS
from .commands import register_commands
from flask_jwt_extended import JWTManager
from datetime import timedelta
import nltk
import pytz
from datetime import datetime
from .vector_db import register_db_listeners


def create_app():
    load_dotenv()

    app = Flask(__name__)

    CORS(app)
    
    # Konfigurasi aplikasi
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    # app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'a-super-secret-key')
    app.config["JWT_SECRET_KEY"] = os.getenv('JWT_SECRET_KEY')

    app.config['PDF_IMAGES_DIRECTORY'] = os.path.join(app.static_folder, 'pdf_images')
    app.config['PDF_CHUNK_DIRECTORY'] = os.getenv('PDF_CHUNK_DIRECTORY', 'data/chunkPdf') 

    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=1)
    app.config["JWT_REFRESH_TOKEN_EXPIRES"] = timedelta(days=30)

    # Inisialisasi ekstensi
    db.init_app(app)
    jwt = JWTManager(app)
    nltk.download('stopwords')
    # Daftarkan Blueprints
    from .routes.auth import auth_bp
    from .routes.chat import chat_bp
    from .routes.feedback import feedback_bp
    from .routes.berita import berita_bp
    from .routes.dashboard import dashboard_bp
    from .routes.analytics import analytics_bp
    from .routes.document import document_bp
    
    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(feedback_bp)
    app.register_blueprint(berita_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(document_bp)

    # Daftarkan perintah CLI
    # app.cli.add_command(cli)
    register_commands(app)

    register_db_listeners()

    with app.app_context():
        # Buat semua tabel database jika belum ada
        db.create_all()

    return app