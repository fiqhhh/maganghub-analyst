# app.py
from flask import Flask, request, jsonify, render_template
import requests
import pandas as pd
import json
import math
import time
import os
from google import genai
from google.genai.errors import APIError
from werkzeug.utils import secure_filename

# --- KONFIGURASI AWAL ---
# Vercel akan menyediakan Environment Variables secara langsung

app = Flask(__name__)

# Konfigurasi MagangHub API
MAGANGHUB_BASE_URL = "https://maganghub.kemnaker.go.id/be/v1/api/list/vacancies-aktif"
MAGANGHUB_PARAMS = {
    'order_by': 'jumlah_kuota',
    'order_direction': 'DESC',
    'limit': 20,
    'kode_provinsi': 31 # Filter DKI Jakarta
}

# Ambil GEMINI_API_KEY dari Environment Variable Vercel
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY') 

# Konfigurasi Upload File (Menggunakan /tmp yang bisa ditulis di Serverless)
UPLOAD_FOLDER = '/tmp/uploads' 
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Klien Gemini
gemini_client = None
try:
    if GEMINI_API_KEY:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        print("Gemini Client berhasil diinisialisasi.")
    else:
        print("WARNING: GEMINI_API_KEY kosong. Fitur AI tidak akan berfungsi.")
except Exception as e:
    print(f"ERROR: Gagal inisialisasi Gemini Client. {e}")

# Cache data lowongan
LOWONGAN_CACHE = []
LAST_SCRAPED = 0

# --- FUNGSI UTILITY ---

def hitung_peluang(kuota, pelamar):
    """Menghitung persentase peluang, menangani pembagian nol dan peluang > 100%."""
    if pelamar == 0:
        return 100.0 if kuota > 0 else 0.0
    peluang = (kuota / pelamar) * 100
    return min(peluang, 100.0)

# --- FUNGSI GEMINI KLASIFIKASI ---

def classify_lowongan_gemini(lowongan_list):
    """Mengklasifikasikan lowongan menjadi IT-RELATED atau NON-IT."""
    if not gemini_client or not lowongan_list:
        return {}
    
    sample_lowongan = lowongan_list[:500] 
    
    prompt = (
        "Tugasmu adalah mengklasifikasikan lowongan pekerjaan. Tentukan apakah setiap lowongan 'IT-RELATED' atau 'NON-IT'. "
        "Jawab hanya dalam format JSON Array: [{\"id\": \"id_posisi\", \"kategori\": \"IT-RELATED\"/\"NON-IT\"}]."
    )
    
    data_to_send = [{
        "id": item['id'],
        "posisi": item['posisi'],
        "deskripsi": item['deskripsi'][:150] 
    } for item in sample_lowongan]

    prompt += "\n\nData Lowongan:\n" + json.dumps(data_to_send, indent=2)

    try:
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        classification_result = json.loads(response.text)
        return {item['id']: item['kategori'].upper() for item in classification_result}
    except Exception as e:
        print(f"ERROR KLASIFIKASI GEMINI: {e}")
        return {}

# --- FUNGSI SCRAPING ---

def proses_data_api():
    """Mengambil semua halaman API MagangHub (dengan cache 1 jam)."""
    global LOWONGAN_CACHE, LAST_SCRAPED
    
    # Gunakan cache jika data belum kedaluwarsa
    if time.time() - LAST_SCRAPED < 3600 and LOWONGAN_CACHE:
        print("Menggunakan data dari cache.")
        return LOWONGAN_CACHE
    
    # --- PENTING: BATASAN VERCEL FREE TIER ---
    # Vercel Free Tier memiliki batas eksekusi 10 detik. Scraping semua halaman akan menyebabkan TIMEOUT.
    # Kita batasi hanya mengambil halaman 1.
    MAX_PAGES_LIMIT = 1 
    
    all_data = []
    current_page = 1
    total_pages = 1
    
    print("Memulai pengambilan data dari API MagangHub (mode uji coba Vercel)...")

    while current_page <= total_pages and current_page <= MAX_PAGES_LIMIT:
        params = MAGANGHUB_PARAMS.copy()
        params['page'] = current_page
        
        try:
            response = requests.get(
                MAGANGHUB_BASE_URL, 
                params=params, 
                timeout=30, # Timeout 30 detik
                headers={'User-Agent': 'MagangHub-Scraper/1.0 (Python Flask)'}
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            print(f"ERROR MagangHub API: Gagal mengambil halaman {current_page}. Berhenti. ({e})")
            time.sleep(10) # Jeda 10 detik setelah error 502, lalu keluar
            break

        if current_page == 1:
            total_pages_full = data.get('meta', {}).get('pagination', {}).get('last_page', 1)
            total_items = data.get('meta', {}).get('pagination', {}).get('total', 0)
            
            if total_pages_full > MAX_PAGES_LIMIT:
                 print(f"Total Lowongan MagangHub: {total_items}. Uji coba dibatasi hingga {MAX_PAGES_LIMIT} halaman (Total harusnya {total_pages_full} halaman).")
            else:
                 print(f"Total Lowongan MagangHub: {total_items}. Mengambil semua {total_pages_full} halaman.")
            
            # Jika total halaman asli lebih besar dari limit, kita hanya set total_pages ke limit
            total_pages = min(total_pages_full, MAX_PAGES_LIMIT) 
        
        # Ekstraksi data
        for item in data.get('data', []):
            kuota = item.get('jumlah_kuota', 0)
            pelamar = item.get('jumlah_terdaftar', 0)
            
            try:
                jurusan_raw = json.loads(item.get('program_studi', '[]') or '[]')
                jurusan = ', '.join([p['title'] for p in jurusan_raw])
            except (json.JSONDecodeError, TypeError):
                jurusan = item.get('program_studi', 'N/A')
            
            try:
                jenjang_raw = json.loads(item.get('jenjang', '[]') or '[]')
                jenjang = ', '.join([j['title'] for j in jenjang_raw])
            except (json.JSONDecodeError, TypeError):
                jenjang = item.get('jenjang', 'N/A')

            all_data.append({
                'id': item.get('id_posisi', ''),
                'posisi': item.get('posisi', 'N/A'),
                'perusahaan': item.get('perusahaan', {}).get('nama_perusahaan', 'N/A'),
                'kuota': kuota,
                'pendaftar': pelamar,
                'deskripsi': item.get('deskripsi_posisi', 'N/A'),
                'jenjang': jenjang,
                'jurusan': jurusan,
                'peluang': round(hitung_peluang(kuota, pelamar), 2),
            })
        
        print(f"-> Mengambil halaman {current_page}/{total_pages}...")
        current_page += 1
        time.sleep(0.5)

    LOWONGAN_CACHE = all_data
    LAST_SCRAPED = time.time()
    return all_data

# --- ENDPOINTS FLASK ---

@app.route('/')
def index():
    """Menampilkan halaman utama (frontend)."""
    return render_template('index.html')

@app.route('/api/data', methods=['GET'])
def get_lowongan_data():
    """Endpoint untuk mengambil dan mengklasifikasikan data."""
    
    lowongan_data = proses_data_api()
    
    # Tangani data kosong agar Pandas tidak KeyError
    if not lowongan_data:
        print("PERINGATAN: Data lowongan kosong dari API MagangHub.")
        return jsonify([])
    
    classification_map = classify_lowongan_gemini(lowongan_data)
    
    for item in lowongan_data:
        item['kategori'] = classification_map.get(item['id'], 'NON-IT') 
        
    df = pd.DataFrame(lowongan_data)
    df = df.sort_values(by=['peluang', 'kuota'], ascending=[False, False])
    
    return jsonify(df.to_dict('records'))

@app.route('/api/recommend', methods=['POST'])
def recommend_positions():
    """Endpoint untuk menerima CV dan memberikan rekomendasi AI."""
    
    if not gemini_client:
        return jsonify({"error": "Gemini API key is missing or invalid."}), 500
    
    if 'cv_file' not in request.files:
        return jsonify({"error": "Tidak ada file CV yang diunggah."}), 400
    
    file = request.files['cv_file']
    if file.filename == '' or file.mimetype != 'application/pdf':
        return jsonify({"error": "File tidak valid. Pastikan file berformat PDF."}), 400
        
    filename = secure_filename(file.filename)
    
    # Buat folder /tmp/uploads jika belum ada
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True) 

    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    uploaded_file = None
    try:
        # Gunakan data lowongan yang terbatas (hanya 1 halaman)
        all_lowongan = proses_data_api()
        
        if not all_lowongan:
             return jsonify({"error": "Gagal mendapatkan data lowongan untuk perbandingan. Coba lagi."}), 500

        df_all = pd.DataFrame(all_lowongan)
        
        # Ambil semua data dari hasil 1 halaman untuk dijadikan prompt
        df_top = df_all.sort_values(by=['peluang', 'kuota'], ascending=[False, False])
        lowongan_prompt = df_top[['id', 'posisi', 'perusahaan', 'deskripsi', 'peluang']].to_string(index=False)
        
        uploaded_file = gemini_client.files.upload(file=filepath)
        
        prompt = (
            "Kamu adalah konsultan karir profesional. Analisis CV yang diunggah dan bandingkan dengan daftar lowongan MagangHub di bawah. "
            "Rekomendasikan 20 lowongan yang paling COCOK KUALIFIKASI dan memiliki PELUANG DITERIMA tertinggi. "
            "Jawab hanya dalam format JSON ARRAY dengan struktur: "
            "[{\"id\": \"id_posisi_terkait\", \"posisi\": \"Nama Posisi\", \"alasan\": \"Alasan utama kecocokan dan peluang tinggi (maks 2 kalimat)\"}]."
            "Pastikan ID Posisi yang direkomendasikan ada di Daftar Lowongan Tersedia.\n\n"
            f"Daftar Lowongan Tersedia (ID | Posisi | Perusahaan | Deskripsi Singkat | Peluang):\n{lowongan_prompt}"
        )

        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, uploaded_file],
            config={"response_mime_type": "application/json"}
        )
        
        recommendations = json.loads(response.text)
        
        result = []
        for rec in recommendations:
            try:
                lowongan_detail = df_all[df_all['id'] == rec['id']].iloc[0].to_dict()
                result.append({
                    "posisi": rec['posisi'],
                    "perusahaan": lowongan_detail['perusahaan'],
                    "peluang": lowongan_detail['peluang'],
                    "alasan": rec['alasan']
                })
            except:
                continue 
                
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": f"Terjadi kesalahan: {e}"}), 500
    finally:
        # Hapus file yang diupload (sangat penting!)
        if uploaded_file:
            gemini_client.files.delete(name=uploaded_file.name)
        if os.path.exists(filepath):
            os.remove(filepath)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)