# app.py
from flask import Flask, jsonify, render_template
import requests
import pandas as pd
import json
import math
import time
import os

app = Flask(__name__)

# --- KONFIGURASI API ---
MAGANGHUB_BASE_URL = "https://maganghub.kemnaker.go.id/be/v1/api/list/vacancies-aktif"
MAGANGHUB_PARAMS = {
    'order_by': 'jumlah_kuota',
    'order_direction': 'DESC',
    'limit': 100,
    'kode_provinsi': 31 # Filter DKI Jakarta
}

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

# --- FUNGSI SCRAPING ---

def proses_data_api():
    """Mengambil semua halaman API MagangHub (dengan cache 1 jam)."""
    global LOWONGAN_CACHE, LAST_SCRAPED
    
    # Gunakan cache jika data belum kedaluwarsa (1 jam)
    if time.time() - LAST_SCRAPED < 3600 and LOWONGAN_CACHE:
        print("Menggunakan data dari cache.")
        return LOWONGAN_CACHE

    all_data = []
    current_page = 1
    total_pages = 1
    
    print("Memulai pengambilan data MagangHub (seluruh halaman)...")

    while current_page <= total_pages:
        params = MAGANGHUB_PARAMS.copy()
        params['page'] = current_page
        
        try:
            response = requests.get(
                MAGANGHUB_BASE_URL, 
                params=params, 
                timeout=30,
                headers={'User-Agent': 'Simple-Scraper/1.0'}
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            print(f"ERROR MagangHub API: Gagal mengambil halaman {current_page}. Berhenti. ({e})")
            break

        if current_page == 1:
            total_pages = data.get('meta', {}).get('pagination', {}).get('last_page', 1)
            total_items = data.get('meta', {}).get('pagination', {}).get('total', 0)
            print(f"Total Lowongan: {total_items}. Mengambil semua {total_pages} halaman.")
        
        # Ekstraksi data
        for item in data.get('data', []):
            kuota = item.get('jumlah_kuota', 0)
            pelamar = item.get('jumlah_terdaftar', 0)
            
            all_data.append({
                'id': item.get('id_posisi', ''),
                'posisi': item.get('posisi', 'N/A'),
                'perusahaan': item.get('perusahaan', {}).get('nama_perusahaan', 'N/A'),
                'kuota': kuota,
                'pendaftar': pelamar,
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
    """Endpoint untuk mengambil semua data dan mengirimkannya."""
    
    lowongan_data = proses_data_api()
    
    if not lowongan_data:
        return jsonify([])
        
    df = pd.DataFrame(lowongan_data)
    
    # Sortir default: Peluang tertinggi
    df = df.sort_values(by=['peluang', 'kuota'], ascending=[False, False])
    
    return jsonify(df.to_dict('records'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)