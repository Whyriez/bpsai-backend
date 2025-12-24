# 🤖 BPS AI Chatbot - Backend Service

Repository ini berisi kode sumber untuk Backend BPS AI Chatbot. Backend ini dibangun menggunakan **Flask**, **PostgreSQL**, **ChromaDB**, dan **Google Gemini API**.

---

## 📋 Prasyarat (Prerequisites)

Pastikan server atau environment Anda memiliki:
- **Python 3.10+**
- **PostgreSQL** (Running & Database Created)
- **Virtual Environment** (`venv`)
- **Git**

---

## 🚀 1. Instalasi & Setup (Server Side)

Lakukan langkah-langkah ini untuk menyiapkan environment backend dari nol.

### 1.1. Setup Python Environment

```bash
# 1. Clone repository (jika belum)
git clone [https://github.com/username/bpsai-backend.git](https://github.com/username/bpsai-backend.git)
cd bpsai-backend

# 2. Buat Virtual Environment
python3 -m venv venv

# 3. Aktifkan Virtual Environment
source venv/bin/activate

# 4. Update PIP
venv/bin/pip install --upgrade pip
```

### 1.2. Install Dependencies

Install paket-paket yang dibutuhkan, termasuk library tambahan untuk dokumentasi API, caching, dan server production.

```bash
# Install requirements utama
venv/bin/pip install -r requirements.txt

# Install library tambahan (Wajib untuk API Docs & Caching)
venv/bin/pip install flasgger
venv/bin/pip install Flask-Caching

# Install Gunicorn (Wajib untuk Production Server)
venv/bin/pip install gunicorn
```

### 1.3. Konfigurasi Environment (.env)

Buat file `.env` di root folder aplikasi dan sesuaikan konfigurasi berikut:

```env
# Database Configuration
DATABASE_URL="postgresql://postgres@localhost:5432/bpsai"

# ChromaDB Configuration (Vector Database)
CHROMA_HOST=127.0.0.1
CHROMA_PORT=8000

# Seeding Configuration (Default Admin)
SEED_ADMIN_EMAIL=
SEED_ADMIN_USERNAME=

# Security
JWT_SECRET_KEY=ganti-dengan-kunci-rahasia-anda-yang-panjang

# Data Directories
PDF_CHUNK_DIRECTORY=data/onlineData/pdf
PDF_IMAGES_DIRECTORY=data/onlineData/png

# App Settings
ENVIRONMENT=development
LANG=en_US.UTF-8
LC_ALL=en_US.UTF-8
PYTHONIOENCODING=utf-8

# Google Gemini API Keys (Multi-Key Support)
GEMINI_API_KEY_1=AIzaSy... (Isi dengan API Key Google Anda)
GEMINI_API_KEY_2=AIzaSy...
GEMINI_API_KEY_3=AIzaSy...
```

---

## 🗄️ 2. Setup Database

Pastikan service PostgreSQL sudah berjalan dan database kosong sudah dibuat sebelum menjalankan perintah ini.

```bash
# 1. Inisialisasi folder migrasi (Hanya jika folder 'migrations' belum ada)
venv/bin/flask db init

# 2. Membuat file migrasi
venv/bin/flask db migrate -m "Initial migration"

# 3. Menerapkan migrasi ke database (Create Tables)
venv/bin/flask db upgrade

# 4. Mengisi Data Awal (Seeding Role & Data Master)
venv/bin/flask db:seed
```

---

## 🛠️ 3. Command Line Interface (CLI)

Gunakan perintah berikut untuk manajemen data, user, dan pemeliharaan sistem.

### 👤 Manajemen User (Create Admin)

Membuat user baru dengan role Administrator untuk akses dashboard.

```bash
# Format: flask user:create-admin "EMAIL" "PASSWORD"

# Contoh 1 (Default sesuai .env)
venv/bin/flask user:create-admin "fitra@bps.go.id" "fitra"

# Contoh 2 (User Lain)
venv/bin/flask user:create-admin "siti.aminah@bps.go.id" "siti"
```

### 📥 Import Data Scraping

Memasukkan data hasil scraping berita/artikel dari CSV ke database.

```bash
# Pastikan path file CSV sesuai dengan lokasi di server
venv/bin/flask import:csv "data/scrap/hasil_scraping_bps_gorontalo.csv"
```

### 🏷️ Auto Tagging (Klasifikasi AI)

Menjalankan proses AI untuk memberikan tag/kategori otomatis pada berita yang belum terklasifikasi.

```bash
# Opsi 1: Default (Proses 100 data pertama yg belum ditag)
venv/bin/flask tags:auto

# Opsi 2: Limit Khusus (Misal: hanya 10 data)
venv/bin/flask tags:auto --limit 10

# Opsi 3: Semua Data (Hati-hati, butuh waktu lama tergantung jumlah data)
venv/bin/flask tags:auto --all
```

---

## 🏃 4. Menjalankan Server

### Mode Development (Testing/Local)

Gunakan mode ini saat pengembangan atau debugging (single thread).

```bash
venv/bin/python run.py
```
*Server berjalan di http://localhost:5000*

### Mode Production (Live Server)

Gunakan **Gunicorn** agar server stabil, aman, dan bisa menangani banyak request secara paralel.

```bash
# Menjalankan server dengan 4 Worker pada port 5000
venv/bin/gunicorn -w 4 -b 0.0.0.0:5000 run:app
```

---

## 🔧 5. Maintenance & Troubleshooting

### Re-Index Knowledge Base (RAG)

Jika Anda mengganti model embedding, memperbarui logika chunking, atau ingin mereset total pengetahuan chatbot karena data tidak sinkron:

```bash
# Perintah ini akan MENGHAPUS ChromaDB lama dan membuat ulang vector dari PDF yang ada
venv/bin/python reindex_documents.py --yes
```

### Cek Library Terinstall

Verifikasi apakah library penting sudah terinstall dengan benar di environment:

```bash
venv/bin/pip list | grep -E "Flask|flasgger|gunicorn"
```