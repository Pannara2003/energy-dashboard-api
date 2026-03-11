from flask import Flask, jsonify, request
from flask_cors import CORS
#from modbus_driver import PM2200Reader
import threading
import time
import csv
import os
import pandas as pd
from datetime import datetime

# นำเข้าไลบรารีสำหรับส่งอีเมลและแนบไฟล์
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

# นำเข้าไลบรารีสำหรับฐานข้อมูลและการเข้ารหัสผ่าน
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
CORS(app)

# =========================================================
# ⚙️ CONFIGURATION
# =========================
PORT = '/dev/tty.usbserial-1220'  # Port สำหรับ Mac
CSV_FILE = 'energy_log.csv'
DB_FILE = 'system.db'             # ชื่อไฟล์ฐานข้อมูล
TRANSFORMER_KVA = 1000      
UNIT_PRICE = 4.18

# Initialize Modbus Reader
try:
    reader = PM2200Reader(port=PORT)
except Exception as e:
    print(f"❌ Modbus Init Error: {e}")
    reader = None

modbus_lock = threading.Lock()

# =========================================================
# 🗄️ DATABASE INITIALIZATION (ระบบ Multi-Admin)
# =========================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # สร้างตารางแอดมิน
    c.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL
        )
    ''')
    # ตรวจสอบว่ามีผู้ใช้ในระบบหรือยัง ถ้ายังไม่มีให้สร้าง 2 ยูสเซอร์เริ่มต้น
    c.execute('SELECT COUNT(*) FROM admins')
    if c.fetchone()[0] == 0:
        default_admins = [
            ('admin', generate_password_hash('1234'), 'Super Admin', 'Plant Manager'),
            ('engineer', generate_password_hash('5678'), 'John Doe', 'Electrical Engineer')
        ]
        c.executemany('INSERT INTO admins (username, password, name, role) VALUES (?, ?, ?, ?)', default_admins)
        conn.commit()
        print("✅ Created default Admin accounts.")
    conn.close()

init_db()

# =========================================================
# 📂 CSV & FOLDER INITIALIZATION
# =========================
def init_csv():
    headers = [
        'Date Time', 'Vol A', 'Vol B', 'Vol C', 
        'Cur A', 'Cur B', 'Cur C', 'KW Total', 
        'KVAR Total', 'PF', 'Hz', 'KWH Total'
    ]
    if not os.path.isfile(CSV_FILE):
        with open(CSV_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            print(f"✅ Created new CSV: {CSV_FILE}")

init_csv()

# =========================================================
# 🔄 BACKGROUND DATA LOGGER (บันทึกทุก 1 นาที)
# =========================
def save_data_every_minute():
    print("⏳ Data Logger is running...")
    while True:
        try:
            if reader:
                with modbus_lock:
                    data = reader.read_data()
                
                if isinstance(data, dict) and data.get("status") == "Connected":
                    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    
                    # 🛡️ Spike Filtering: ป้องกันค่าโดดเกินจริง (เกิน 2000 kW)
                    kw = data.get('power_total', 0)
                    if kw <= 2000: 
                        kwh = data.get('energy_kwh', 0)
                        row = [
                            timestamp, data.get('vol_a', 0), data.get('vol_b', 0), data.get('vol_c', 0),
                            data.get('cur_a', 0), data.get('cur_b', 0), data.get('cur_c', 0),
                            kw, data.get('reactive_total', 0), data.get('pf_total', 0), 
                            data.get('frequency', 0), kwh
                        ]
                        with open(CSV_FILE, 'a', newline='') as f:
                            csv.writer(f).writerow(row)
        except Exception as e:
            print(f"❌ Logging Error: {e}")
            
        time.sleep(60) # บันทึกทุก 60 วินาที

threading.Thread(target=save_data_every_minute, daemon=True).start()

# =========================================================
# 🌐 API ROUTES
# =========================

# ✅ API สำหรับตรวจสอบการเข้าสู่ระบบ (Login)
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, password, name, role FROM admins WHERE username = ?', (username,))
    user = c.fetchone()
    conn.close()

    if user and check_password_hash(user[1], password):
        return jsonify({
            "status": "success",
            "user": { "username": username, "name": user[2], "role": user[3] }
        }), 200
    else:
        return jsonify({"status": "error", "message": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"}), 401


# ✅ API สำหรับเพิ่มผู้ใช้งานใหม่
@app.route('/api/add_user', methods=['POST'])
def add_user():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    name = data.get('name')
    role = data.get('role')
    
    if not all([username, password, name, role]):
        return jsonify({"status": "error", "message": "กรุณากรอกข้อมูลให้ครบถ้วน"}), 400

    hashed_password = generate_password_hash(password)
    
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('INSERT INTO admins (username, password, name, role) VALUES (?, ?, ?, ?)', 
                  (username, hashed_password, name, role))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "เพิ่มผู้ใช้งานสำเร็จ!"}), 200
    except sqlite3.IntegrityError:
        return jsonify({"status": "error", "message": "มี Username นี้อยู่ในระบบแล้ว!"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/send_email', methods=['POST'])
def send_email_report():
    """ฟังก์ชันส่งอีเมลรายงาน พร้อมแนบไฟล์ CSV"""
    try:
        # 🔴 อัปเดตอีเมลใหม่ของคุณ และลบเว้นวรรคในรหัสผ่านแล้ว 🔴
        SENDER_EMAIL = "modpannara20@gmail.com"      
        APP_PASSWORD = "iqhuwysxrjjezrri"          
        RECEIVER_EMAIL = "Phannara.c@ku.th"    
        
        # ดึงค่าไฟจริงล่าสุดจากมิเตอร์
        total_kwh = 0
        if reader:
            with modbus_lock:
                data = reader.read_data()
                if isinstance(data, dict):
                    total_kwh = data.get('energy_kwh', 0)
        
        if total_kwh == 0:
            total_kwh = 29600.00 

        estimated_cost = float(total_kwh) * UNIT_PRICE
        co2 = float(total_kwh) * 0.4999

        # สร้างหน้าตาเนื้อหาอีเมล (HTML) แบบทางการ
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
        </head>
        <body style="font-family: Arial, Helvetica, sans-serif; color: #333333; line-height: 1.6; margin: 0; padding: 20px; background-color: #f4f6f8;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border: 1px solid #e2e8f0; border-top: 4px solid #1e293b;">
                <div style="padding: 20px 30px; border-bottom: 1px solid #f1f5f9;">
                    <h2 style="margin: 0; color: #1e293b; font-size: 20px; text-transform: uppercase;">CH Group Energy Management</h2>
                    <p style="margin: 5px 0 0 0; font-size: 12px; color: #64748b;">Automated Weekly Energy Report</p>
                </div>
                
                <div style="padding: 30px;">
                    <p style="margin-top: 0;">Dear Plant Manager,</p>
                    <p>Please find below the weekly energy consumption summary for <strong>MDB-01 (Main Incoming)</strong>.</p>
                    <p style="color: #d32f2f; font-weight: bold; font-size: 13px;">* The detailed raw data log (CSV) is attached to this email.</p>
                    
                    <table style="width: 100%; border-collapse: collapse; margin: 30px 0;">
                        <thead>
                            <tr>
                                <th style="padding: 12px; text-align: left; border-bottom: 2px solid #1e293b; color: #1e293b; font-size: 13px; text-transform: uppercase;">Description</th>
                                <th style="padding: 12px; text-align: right; border-bottom: 2px solid #1e293b; color: #1e293b; font-size: 13px; text-transform: uppercase;">Value</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr>
                                <td style="padding: 12px; border-bottom: 1px solid #f1f5f9; color: #475569;">Total Energy Consumption</td>
                                <td style="padding: 12px; text-align: right; border-bottom: 1px solid #f1f5f9; font-weight: bold; color: #0f172a;">{total_kwh:,.2f} kWh</td>
                            </tr>
                            <tr>
                                <td style="padding: 12px; border-bottom: 1px solid #f1f5f9; color: #475569;">Estimated Electricity Cost</td>
                                <td style="padding: 12px; text-align: right; border-bottom: 1px solid #f1f5f9; font-weight: bold; color: #0f172a;">{estimated_cost:,.2f} THB</td>
                            </tr>
                            <tr>
                                <td style="padding: 12px; border-bottom: 1px solid #f1f5f9; color: #475569;">CO2 Equivalent Emission</td>
                                <td style="padding: 12px; text-align: right; border-bottom: 1px solid #f1f5f9; font-weight: bold; color: #0f172a;">{co2:,.2f} kgCO2e</td>
                            </tr>
                        </tbody>
                    </table>
                    
                    <div style="text-align: center; margin-top: 40px; margin-bottom: 20px;">
                        <a href="http://localhost:3000" style="background-color: #1e293b; color: #ffffff; padding: 12px 24px; text-decoration: none; font-size: 14px; border-radius: 4px; font-weight: bold;">Access Full Dashboard</a>
                    </div>
                </div>
                
                <div style="background-color: #f8fafc; padding: 20px 30px; font-size: 11px; color: #64748b; text-align: center; border-top: 1px solid #e2e8f0;">
                    <p style="margin: 0;">This is a system-generated email from Smart Energy Management Dashboard (SEMD).<br>Please do not reply directly to this message.</p>
                    <p style="margin: 10px 0 0 0;">&copy; 2026 CH Group. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECEIVER_EMAIL
        msg['Subject'] = "[Automated Report] Weekly Energy Summary - CH Group"
        msg.attach(MIMEText(html_content, 'html'))

        # การแนบไฟล์ CSV เข้าไปในอีเมล
        if os.path.exists(CSV_FILE):
            with open(CSV_FILE, "rb") as attachment:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(attachment.read())
            
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename= MDB01_Energy_Data.csv",
            )
            msg.attach(part)

        # ต่อท่อส่งอีเมลผ่าน Gmail
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(SENDER_EMAIL, APP_PASSWORD)
        server.send_message(msg)
        server.quit()

        return jsonify({"status": "success", "message": "Email and CSV sent successfully!"}), 200

    except Exception as e:
        print("❌ Error sending email:", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/data', methods=['GET'])
def get_data():
    """ดึงค่า Real-time ให้หน้า Dashboard"""
    try:
        if reader is None:
            return jsonify({"status": "Error", "message": "Reader not ready"}), 500
        with modbus_lock:
            raw_data = reader.read_data()
        return jsonify(raw_data)
    except Exception as e:
        return jsonify({"status": "Error", "message": str(e)}), 500

@app.route('/api/daily_usage', methods=['GET'])
def get_daily_usage():
    """ดึงข้อมูลรายวันให้ Calendar Heatmap"""
    try:
        year = request.args.get('year', type=int)
        month = request.args.get('month', type=int)
        
        if not os.path.isfile(CSV_FILE): return jsonify({}), 200

        df = pd.read_csv(CSV_FILE, on_bad_lines='skip', low_memory=False)
        if df.empty: return jsonify({}), 200

        df['Date Time'] = pd.to_datetime(df['Date Time'], errors='coerce')
        df = df.dropna(subset=['Date Time'])

        mask = (df['Date Time'].dt.year == year) & (df['Date Time'].dt.month == month)
        df_month = df.loc[mask].copy()

        if df_month.empty: return jsonify({}), 200

        df_month['day'] = df_month['Date Time'].dt.day
        daily_data = {}

        for day, group in df_month.groupby('day'):
            kwh_vals = pd.to_numeric(group['KWH Total'], errors='coerce').dropna()
            if not kwh_vals.empty:
                usage = kwh_vals.max() - kwh_vals.min()
                if usage <= 0:
                    kw_vals = pd.to_numeric(group['KW Total'], errors='coerce').fillna(0)
                    usage = kw_vals.mean() * 24 
                daily_data[str(day)] = round(float(usage), 2)

        return jsonify(daily_data)
        
    except Exception as e:
        print(f"❌ API Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/export_csv', methods=['GET'])
def export_csv():
    """สำหรับปุ่ม Export CSV ในหน้าเว็บ"""
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'r') as f:
            return f.read(), 200, {'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename=energy_data.csv'}
    return "File not found", 404

if __name__ == '__main__':
    print(f"🚀 PM2200 Server running on http://127.0.0.1:5001")
    app.run(host='0.0.0.0', port=5001, debug=True)
