import os
import time
import random
import threading
import warnings
import sqlite3
import csv  # <<< 必須補上這一行
import serial
import re
import requests
from datetime import datetime
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# 忽略警告
warnings.filterwarnings("ignore", category=RuntimeWarning)

# 1. 系統設定
# ==========================================
FILE_PRODUCTS = "db_products.csv"
FILE_ORDERS = "db_orders.csv"
FILE_LOGS_PREFIX = "db_logs"      # API 用來產生 db_logs_Line 1.csv
FILE_LOGS = "db_logs_All.csv"     # 補上這個變數，防止 NameError
SQL_DB_NAME = "factory_data.db"

# 欄位定義
# 欄位定義
# 工單表：與您預期的 factory_data.db 格式完全對齊
ORDER_COLUMNS = ["產線", "排程順序", "工單號碼", "產品ID", "顯示內容", "品種", "密度", "準重", "預計數量", "已完成數量", "狀態", "建立時間"]

# 產品表：對應預期格式中的 products 表
PRODUCT_COLUMNS = ["產品ID", "客戶名", "溫度等級", "品種", "密度", "長", "寬", "高", "下限", "準重", "上限", "備註1", "備註2", "備註3"]

# 日誌表：用於分類歸納 4 台電腦傳來的數據
LOG_COLUMNS = ["時間", "產線", "工單號碼", "產品ID", "實測重", "判定結果", "NG原因"]
PRODUCTION_LINES = ["Line 1", "Line 2", "Line 3", "Line 4"]
st.set_page_config(page_title="產線管理主機 v13.30", layout="wide")
def force_sync_everything():
    try:
        with sqlite3.connect(SQL_DB_NAME, timeout=10) as conn:
            # 1. 強制從資料庫更新「工單進度」
            df_wo = pd.read_sql("SELECT * FROM work_orders", conn)
            if not df_wo.empty:
                st.session_state.work_orders_db = df_wo
            
            # 2. 強制從資料庫更新「生產紀錄」
            df_logs = pd.read_sql("SELECT * FROM production_logs ORDER BY 時間 DESC", conn)
            if not df_logs.empty:
                st.session_state.production_logs = df_logs
    except Exception as e:
        print(f"同步失敗: {e}")
# ==========================================
# 1. FastAPI 接收端設定 (主機背景收料口)
# ==========================================
api_app = FastAPI()

class ScaleUpload(BaseModel):
    line_name: str
    order_id: str
    product_id: str
    weight: str
    status: str
    reason: str = ""  # 📌 確保這裡有預設值，避免 Client 沒傳時報錯

def extract_weight(raw_val):
    try:
        if isinstance(raw_val, (int, float)): return float(raw_val)
        match = re.search(r"[-+]?\d*\.\d+|\d+", str(raw_val))
        return float(match.group()) if match else 0.0
    except: return 0.0

@api_app.post("/upload")
async def receive_weight(data: ScaleUpload):
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        clean_weight = extract_weight(data.weight)
        # 產線名稱驗證 (防止傳送 Unknown 進來)
        line = data.line_name if data.line_name in ["Line 1", "Line 2", "Line 3", "Line 4"] else "Unknown"
        
        # --- Part A: CSV 寫入 (保留您原本邏輯，完全不動) ---
        target_csv = f"db_logs_{line}.csv"
        row_to_write = [now, line, data.order_id, data.product_id, clean_weight, data.status, data.reason]
        
        try:
            file_exists = os.path.isfile(target_csv)
            with open(target_csv, mode='a', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                if not file_exists: 
                    writer.writerow(["時間", "產線", "工單號碼", "產品ID", "實測重", "判定結果", "NG原因"])
                writer.writerow(row_to_write)
        except Exception as csv_err:
            print(f"⚠️ CSV 寫入警告 ({line}): {csv_err}")

        # --- Part B: SQL 資料庫寫入 (修改處在下面) ---
        conn = sqlite3.connect(SQL_DB_NAME, timeout=30) 
        try:
            cursor = conn.cursor()
            # 1. 寫入日誌
            cursor.execute("""
                INSERT INTO production_logs (時間, 產線, 工單號碼, 產品ID, 實測重, 判定結果, NG原因)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (now, line, data.order_id, data.product_id, clean_weight, data.status, data.reason))
            
            # 2. 更新工單計數 (僅當 PASS 時)
            if data.status == "PASS":
                 cursor.execute("""
                UPDATE work_orders 
                SET 已完成數量 = 已完成數量 + 1, 
                    狀態 = CASE WHEN 狀態 = '待生產' THEN '生產中' ELSE 狀態 END
                WHERE 工單號碼 = ? AND 狀態 != '已完成'
            """, (data.order_id,))
            
            conn.commit()  # 記得 commit
        except Exception as sql_err:
            print(f"⚠️ SQL 寫入警告: {sql_err}")
        finally:
            conn.close()

        return {"status": "success", "line": line, "weight": clean_weight}

    except Exception as e:
        return {"status": "error", "message": str(e)}
# ------------------------------------------------
# 📌 修正點 3: 派單接口 (GET) 
# 注意：這個函式必須對齊最左邊，不能縮排在上面的函式裡！
# ------------------------------------------------
@api_app.get("/current_order/{line_name}")
def get_current_order(line_name: str):
    try:
        with sqlite3.connect(SQL_DB_NAME) as conn:
            # 🔴 改良版查詢：使用 JOIN 同時抓取 工單資訊 與 產品規格(上下限)
            query = """
                SELECT 
                    w.工單號碼, w.產品ID, 
                    p.下限, p.準重, p.上限
                FROM work_orders w
                LEFT JOIN products p ON w.產品ID = p.產品ID
                WHERE w.產線 = ? 
                  AND w.狀態 IN ('待生產', '生產中')
                  AND w.已完成數量 < w.預計數量
                ORDER BY w.排程順序 ASC LIMIT 1
            """
            df = pd.read_sql(query, conn, params=(line_name,))
            
            if not df.empty:
                row = df.iloc[0]
                return {
                    "order_id": row['工單號碼'],
                    "product_id": row['產品ID'],
                    # ✅ 將後台設定好的精準規格，全部傳給產線電腦
                    "target_weight": float(row['準重']) if pd.notna(row['準重']) else 25.0,
                    "min_weight": float(row['下限']) if pd.notna(row['下限']) else 0.0,
                    "max_weight": float(row['上限']) if pd.notna(row['上限']) else 999.0
                }
    except Exception as e:
        print(f"API Error: {e}")
    
    return {"order_id": None, "product_id": None}
def init_db():
    conn = sqlite3.connect(SQL_DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS production_logs (
            "時間" TEXT, "產線" TEXT, "工單號碼" TEXT, "產品ID" TEXT, 
            "實測重" REAL, "判定結果" TEXT, "NG原因" TEXT
        )""")
    
    # 建立完整工單表 (解決您提到的 factory_data2.db 只有兩欄的問題)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS work_orders (
            "產線" TEXT, "排程順序" INTEGER, "工單號碼" TEXT PRIMARY KEY, "產品ID" TEXT, 
            "顯示內容" TEXT, "品種" TEXT, "密度" REAL, "準重" REAL, 
            "預計數量" INTEGER, "已完成數量" INTEGER, "狀態" TEXT, "建立時間" TEXT
        )""")
    
    # 建立產品規格表 (確保 products 表也存在)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            "產品ID" TEXT PRIMARY KEY, "客戶名" TEXT, "溫度等級" TEXT, "品種" TEXT, 
            "密度" REAL, "長" REAL, "寬" REAL, "高" REAL, "下限" REAL, 
            "準重" REAL, "上限" REAL, "備註1" TEXT, "備註2" TEXT, "備註3" TEXT
        )""")
    
    conn.commit()
    conn.close()

# 修改 run_api_server 函式，加入 try-except 避免崩潰
def run_api_server():
    try:
        init_db()
        # 加入 log_level="error" 減少干擾
        uvicorn.run(api_app, host="127.0.0.1", port=8000, log_level="critical")
    except Exception as e:
        print(f"API 伺服器啟動失敗 (可能 Port 8000 已被佔用): {e}")

# 在 Streamlit 啟動處加入更嚴謹的判斷
if "api_thread_started" not in st.session_state:
    import socket
    # 檢查 8000 Port 是否已被佔用
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        is_port_open = s.connect_ex(('127.0.0.1', 8000)) != 0
    
    if is_port_open:
        thread = threading.Thread(target=run_api_server, daemon=True)
        try:
            from streamlit.runtime.scriptrunner import add_script_run_context
            add_script_run_context(thread)
        except:
            pass
        thread.start()
        st.session_state.api_thread_started = True
        print("✅ API 服務啟動成功於 Port 8000")
    else:
        st.session_state.api_thread_started = True # 標記為已啟動，避免重複報錯
        print("💡 API 服務已在背景執行，直接使用現有連線")

# ==========================================
# 2. Streamlit 系統核心啟動
# ==========================================


# 啟動背景 API (只啟動一次)
if "api_thread_started" not in st.session_state:
    threading.Thread(target=run_api_server, daemon=True).start()
    st.session_state.api_thread_started = True
def sync_data_from_sql():
    """強制同步：直接用 SQL 內容覆蓋網頁狀態，確保 100% 同步"""
    try:
        with sqlite3.connect(SQL_DB_NAME, timeout=10) as conn:
            # 1. 讀取最新工單
            df_orders = pd.read_sql("SELECT * FROM work_orders", conn)
            if not df_orders.empty:
                # 直接更新整個 dataframe，確保「已完成數量」與「狀態」同步
                st.session_state.work_orders_db = df_orders
            
            # 2. 讀取最新日誌 (這會讓下方的良品紀錄表格更新)
            df_logs = pd.read_sql("SELECT * FROM production_logs ORDER BY 時間 DESC", conn)
            if not df_logs.empty:
                st.session_state.production_logs = df_logs
    except Exception as e:
        pass

# [修正3] 將自動刷新頻率設為 2秒，並在刷新時觸發同步
#count = st_autorefresh(interval=2000, key="global_sync_refresh")
#if count > 0:
#   sync_data_from_sql()
# ------------------------------------------
# 3. 共享資料與資料庫邏輯 (保持您原有的功能)
# ------------------------------------------
@st.cache_resource
def get_shared_data():
    class ScaleData:
        def __init__(self):
            self.weight = 0.0
            self.offset = 0.0
            self.last_update = 0
            self.error = None
    return ScaleData()

shared_data = get_shared_data()
try:
    from streamlit.runtime.scriptrunner import add_script_run_context
except ImportError:
    try:
        from streamlit.runtime.scriptrunner.script_run_context import add_script_run_context
    except ImportError:
        # 如果都找不到，定義一個空的 dummy 函式避免 NameError
        def add_script_run_context(thread):
            pass

from streamlit.runtime.scriptrunner import get_script_run_ctx

# ==========================================
# 模擬磅秤執行緒 (測試與開發用)
# ==========================================


def scale_reader_thread():
    # 正式部署時，請根據現場磅秤調整 COM Port (例如 'COM3' 或 '/dev/ttyUSB0')
    COM_PORT = 'COM3' 
    BAUD_RATE = 9600
    
    while True:
        try:
            with serial.Serial(COM_PORT, BAUD_RATE, timeout=1) as ser:
                while True:
                    line = ser.readline().decode('ascii').strip()
                    if line:
                        # 這裡需要根據你磅秤輸出的格式解析字串
                        # 例如磅秤輸出 "ST,GS,+  12.5kg"，你需要抓取 12.5
                        import re
                        match = re.search(r"[-+]?\d*\.\d+|\d+", line)
                        if match:
                            shared_data.weight = float(match.group())
                            shared_data.last_update = time.time()
                    time.sleep(0.1) # 增加反應速度
        except Exception as e:
            shared_data.error = f"磅秤連線中斷: {str(e)}"
            time.sleep(5) # 斷線後隔5秒重試
# ==========================================

# ==========================================
# 新增：SQL 資料庫轉換邏輯
# ==========================================
def export_to_sql():
    """將目前的 Session 資料轉換為 SQLite 資料庫檔案 (修正版：防止覆蓋日誌)"""
    # 嘗試 3 次即可，不需要太久
    for i in range(3):
        try:
            conn = sqlite3.connect(SQL_DB_NAME, timeout=10)
            
            # 1. 轉換產品資料表 (主機端完全控制，可用 replace)
            if not st.session_state.products_db.empty:
                st.session_state.products_db.to_sql("products", conn, if_exists='replace', index=False)
                
            # 2. 轉換工單資料表
            if not st.session_state.work_orders_db.empty:
                wo_to_save = st.session_state.work_orders_db.copy()
                if "建立時間" in wo_to_save.columns:
                    wo_to_save["建立時間"] = pd.to_datetime(wo_to_save["建立時間"]).dt.strftime('%Y-%m-%d %H:%M:%S')
                wo_to_save.to_sql("work_orders", conn, if_exists='replace', index=False)
            
            # 【重要修改】這裡刪除了 production_logs 的寫入
            # 因為日誌現在改用 "逐筆插入 (INSERT)" 的方式，避免整張表覆蓋
                
            conn.commit()
            conn.close()
            return True 
            
        except sqlite3.OperationalError:
            time.sleep(0.5)
            continue
        except Exception as e:
            st.error(f"SQL 轉換錯誤: {e}")
            return False
    return False
# ==========================================
# 2. CSS 與 JS (視覺核心 - 雙重鎖定版)
# ==========================================
st.markdown("""
<style>
    .main .block-container { padding-top: 0.5rem; padding-bottom: 1rem; }
    .digital-font { font-family: 'Roboto Mono', 'Consolas', monospace; font-weight: 700; }
    h1, h2, h3 { margin-top: 0.5rem !important; margin-bottom: 0.5rem !important; }

    /* ============================================================ */
    /* 按鈕樣式核心邏輯                                             */
    /* ============================================================ */
    
    /* 1. 全域設定：任何 Disabled 的按鈕，優先權最高，強制變灰 */
    div.stButton > button:disabled {
        background-color: #bdc3c7 !important;
        border: 2px solid #95a5a6 !important;
        color: #7f8c8d !important;
        opacity: 1 !important;
        cursor: not-allowed !important;
        box-shadow: none !important;
    }

    /* 2. 一般 Primary 按鈕 (例如存檔、確認)，預設紅色 */
    div.stButton > button[kind="primary"] {
        background-color: #e74c3c;
        border: 2px solid #c0392b;
        color: white;
        box-shadow: 0 3px 6px rgba(0,0,0,0.2);
    }
    /* 3. 只有在沒被 Disabled 時，才有 Hover 效果 */
    div.stButton > button[kind="primary"]:hover:not(:disabled) {
        background-color: #ec7063;
        transform: translateY(-2px);
    }
    div.stButton > button[kind="primary"]:active:not(:disabled) {
        background-color: #c0392b;
        transform: translateY(1px);
    }

    /* 資訊卡 */
    .unified-spec-card {
        background-color: #2c3e50; border-radius: 12px; border-left: 8px solid #95a5a6;
        box-shadow: 0 4px 8px rgba(0,0,0,0.2); color: white; overflow: hidden;
        margin-bottom: 10px; border: 1px solid #455a64; height: 460px !important; 
        display: flex; flex-direction: column; justify-content: space-between;
    }
    .usc-header { background: rgba(0,0,0,0.3); padding: 8px 10px; text-align: center; border-bottom: 1px solid #455a64; flex: 0 0 auto; }
    .usc-header .u-label { color: #cfd8dc; font-size: 0.8rem; font-weight: bold; text-transform: uppercase; letter-spacing: 1px; }
    .usc-header .u-value { font-size: 2.4rem; font-weight: 900; color: #ffffff; margin-top: 0px; line-height: 1.1; text-shadow: 0 2px 4px rgba(0,0,0,0.5); }

    .usc-grid { display: flex; border-bottom: 1px solid #455a64; background-color: #34495e; flex: 0 0 auto; }
    .usc-item { flex: 1; text-align: center; padding: 5px; border-right: 1px solid #455a64; min-width: 0; display: flex; flex-direction: column; justify-content: center; }
    .usc-item:last-child { border-right: none; }
    .usc-item .u-label { color: #b0bec5; font-size: 0.75rem; font-weight: bold; text-transform: uppercase; margin-bottom: 2px; display: block; }
    .usc-item .u-value { font-size: 1.6rem; font-weight: bold; line-height: 1; white-space: nowrap; color: white; }

    .usc-size-row { background: #233140; padding: 8px 10px; text-align: center; border-bottom: 1px solid #455a64; flex: 0 0 auto; }
    .usc-size-row .u-label { color: #b0bec5; font-size: 0.8rem; font-weight: bold; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 2px; }
    .usc-size-row .u-value { font-size: 1.8rem; font-weight: 900; color: #ffffff !important; font-family: 'Roboto Mono', monospace; letter-spacing: 0.5px; white-space: nowrap; }

    .usc-range-row { background-color: #2c3e50; padding: 6px 15px; text-align: center; border-bottom: 1px solid #455a64; flex: 0 0 auto; }
    .usc-range-row .u-label { color: #95a5a6; font-size: 0.75rem; font-weight: bold; text-transform: uppercase; letter-spacing: 1px; }
    .usc-range-row .u-value { font-size: 1.3rem; font-weight: bold; color: #f1c40f; font-family: 'Roboto Mono', monospace; letter-spacing: 1px; }

    .usc-notes { background: rgba(255, 255, 255, 0.05); padding: 8px 15px; flex-grow: 1; display: flex; flex-direction: column; justify-content: flex-start; text-align: left; overflow-y: auto; }
    .usc-notes .u-label { color: #e74c3c; font-size: 0.75rem; font-weight: bold; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 3px; border-bottom: 1px solid #e74c3c; display: inline-block; }
    .usc-notes .u-content { color: #ecf0f1; font-size: 1.0rem; line-height: 1.3; font-weight: bold; }

    .status-container { padding: 5px; border-radius: 12px; text-align: center; display: flex; flex-direction: column; justify-content: center; align-items: center; transition: background-color 0.2s; height: 250px !important; }
    .status-pass { background-color: #2980b9; color: white; border: 4px solid #3498db; box-shadow: 0 0 10px rgba(41, 128, 185, 0.3); }
    .status-fail { background-color: #c0392b; color: white; border: 4px solid #e74c3c; box-shadow: 0 0 10px rgba(192, 57, 43, 0.3); }
    .status-ng-ready { background-color: #d35400; color: white; border: 4px solid #e67e22; } 
    
    .weight-display { font-size: 7rem; font-weight: 900; line-height: 1; text-shadow: 2px 2px 5px rgba(0,0,0,0.3); margin-top: 0px; margin-bottom: 0px; }
    .queue-header { font-size: 1.0rem; font-weight: bold; margin-bottom: 5px; color: #2c3e50; padding-bottom: 5px; }
    .history-header { font-size: 0.9rem; font-weight: bold; color: #7f8c8d; margin-bottom: 5px; border-bottom: 2px solid #ddd; }
    .countdown-box { background: rgba(0,0,0,0.2); padding: 2px 15px; border-radius: 8px; margin-bottom: 5px; backdrop-filter: blur(5px); }
    .countdown-label { font-size: 0.8rem; color: #ecf0f1; text-transform: uppercase; letter-spacing: 1px; opacity: 0.9; }
    .countdown-val { font-size: 1.8rem; font-weight: 900; color: #f1c40f; text-shadow: 1px 1px 2px rgba(0,0,0,0.5); line-height: 1; }
    .over-prod { color: #ff6b6b !important; }

    button[data-baseweb="tab"] { font-size: 1.2rem !important; font-weight: bold !important; padding: 10px 20px !important; }
</style>

<script>
const observer = new MutationObserver((mutations) => {
    const buttons = window.parent.document.querySelectorAll('button');
    buttons.forEach(btn => {
        const text = btn.innerText;
        // 現場作業按鈕 - 巨大化與強制變色邏輯
        if (text.includes("紀錄良品") || text.includes("紀錄 NG")) {
            btn.style.height = "130px"; btn.style.fontSize = "32px"; btn.style.fontWeight = "900"; btn.style.marginTop = "15px"; btn.style.borderRadius = "15px"; 
            
            // 使用 setProperty(..., 'important') 確保 JS 權重最高，壓過所有 CSS
            if (btn.disabled) {
                // 強制灰色 (雙重保險)
                btn.style.setProperty('background-color', '#bdc3c7', 'important');
                btn.style.setProperty('border-color', '#95a5a6', 'important');
                btn.style.setProperty('color', '#7f8c8d', 'important');
                btn.style.setProperty('cursor', 'not-allowed', 'important');
                btn.style.boxShadow = "none";
            } else {
                // 啟用狀態 - 強制上色
                if (text.includes("紀錄良品")) {
                    btn.style.setProperty('background-color', '#27ae60', 'important'); // 綠色
                    btn.style.setProperty('border-color', '#145a32', 'important');
                    btn.style.setProperty('color', 'white', 'important');
                    btn.style.boxShadow = "0 6px 12px rgba(0,0,0,0.2)";
                } else if (text.includes("紀錄 NG")) {
                    btn.style.setProperty('background-color', '#c0392b', 'important'); // 紅色
                    btn.style.setProperty('border-color', '#641e16', 'important');
                    btn.style.setProperty('color', 'white', 'important');
                    btn.style.boxShadow = "0 6px 12px rgba(0,0,0,0.2)";
                }
            }
        }
        // 後台儀表板按鈕 - 加大
        if ((text.includes("Line") && text.includes("待生產")) || text.includes("返回列表")) {
            btn.style.minHeight = "80px"; btn.style.fontSize = "20px"; btn.style.fontWeight = "bold"; btn.style.boxShadow = "0 4px 6px rgba(0,0,0,0.2)"; 
        }
    });
});
observer.observe(window.parent.document.body, { childList: true, subtree: true });
document.addEventListener('keydown', function(e) {
    if (e.code === 'Space') {
        var buttons = window.parent.document.querySelectorAll('button');
        for (var i = 0; i < buttons.length; i++) {
            if (buttons[i].innerText.includes("紀錄良品") && !buttons[i].disabled) { buttons[i].click(); break; }
        }
    }
});
</script>
""", unsafe_allow_html=True)

# ==========================================
# 3. 核心邏輯 (含自動正規化)
# ==========================================
def normalize_sequences(df):
    if df.empty: return df
    df = df.reset_index(drop=True)
    new_df = pd.DataFrame()
    for line in df['產線'].unique():
        line_df = df[df['產線'] == line].sort_values(by='排程順序')
        line_df['排程順序'] = range(1, len(line_df) + 1)
        new_df = pd.concat([new_df, line_df])
    return new_df

def load_data():
    if 'products_db' not in st.session_state:
        st.session_state.products_db = pd.DataFrame()
        if os.path.exists(FILE_PRODUCTS):
            try: 
                # 先嘗試用 UTF-8 讀取
                st.session_state.products_db = pd.read_csv(FILE_PRODUCTS, encoding='utf-8')
            except UnicodeDecodeError:
                # 如果失敗 (代表是 Windows Excel 存的)，改用 Big5 讀取
                st.session_state.products_db = pd.read_csv(FILE_PRODUCTS, encoding='cp950') # cp950 就是 Big5
            except Exception: 
                pass
        if st.session_state.products_db.empty:
            st.session_state.products_db = pd.DataFrame(columns=["產品ID", "客戶名", "溫度等級", "品種", "密度", "長", "寬", "高", "下限", "準重", "上限", "備註1", "備註2", "備註3"])
    
    if not st.session_state.products_db.empty:
        st.session_state.products_db["溫度等級"] = st.session_state.products_db["溫度等級"].astype(str)
        cols = ["備註1", "備註2", "備註3"]
        st.session_state.products_db[cols] = st.session_state.products_db[cols].fillna("NULL").replace(['', 'nan', 'None'], 'NULL')

    if 'work_orders_db' not in st.session_state:
        st.session_state.work_orders_db = pd.DataFrame()
        if os.path.exists(FILE_ORDERS):
            try: st.session_state.work_orders_db = pd.read_csv(FILE_ORDERS)
            except: pass
        if st.session_state.work_orders_db.empty:
            st.session_state.work_orders_db = pd.DataFrame(columns=ORDER_COLUMNS)
    
    if "產線" not in st.session_state.work_orders_db.columns: st.session_state.work_orders_db["產線"] = "Line 1"
    for col in ORDER_COLUMNS:
        if col not in st.session_state.work_orders_db.columns: st.session_state.work_orders_db[col] = ""
    st.session_state.work_orders_db = st.session_state.work_orders_db[ORDER_COLUMNS] 
    
    for col in ["排程順序", "預計數量", "已完成數量"]:
        st.session_state.work_orders_db[col] = pd.to_numeric(st.session_state.work_orders_db[col], errors='coerce').fillna(0).astype(int)
    
    st.session_state.work_orders_db = normalize_sequences(st.session_state.work_orders_db)

    if 'production_logs' not in st.session_state:
        st.session_state.production_logs = pd.DataFrame()
        try:
            conn = sqlite3.connect(SQL_DB_NAME)
            # 讀取所有產線的紀錄，並按時間倒序排列
            df_sql = pd.read_sql(f"SELECT * FROM production_logs ORDER BY 時間 DESC", conn)
            if not df_sql.empty:
                st.session_state.production_logs = df_sql
            conn.close()
        except Exception:
            pass
        if st.session_state.production_logs.empty and os.path.exists(FILE_LOGS):
            try: st.session_state.production_logs = pd.read_csv(FILE_LOGS)
            except: pass
            
        if st.session_state.production_logs.empty:
            st.session_state.production_logs = pd.DataFrame(columns=LOG_COLUMNS)
    
    if "產線" not in st.session_state.production_logs.columns: st.session_state.production_logs["產線"] = "Line 1"
    for col in LOG_COLUMNS:
        if col not in st.session_state.production_logs.columns: st.session_state.production_logs[col] = "NULL"
    st.session_state.production_logs = st.session_state.production_logs[LOG_COLUMNS]

# [找到 save_data 內的這段，並替換]
def save_data():
    if 'products_db' in st.session_state: st.session_state.products_db.to_csv(FILE_PRODUCTS, index=False)
    if 'work_orders_db' in st.session_state: st.session_state.work_orders_db.to_csv(FILE_ORDERS, index=False)
    
    # 修改：將 Streamlit 的操作存為總表，不影響 API 的分流檔案
    if 'production_logs' in st.session_state: 
        st.session_state.production_logs.to_csv(FILE_LOGS, index=False)

def get_temp_color(temp_str):
    t = str(temp_str).upper()
    if "1260" in t: return "#ffffff" 
    if "1200" in t: return "#bdc3c7"
    if "1300" in t: return "#5dade2"
    if "1400" in t: return "#f4d03f"
    if "1500" in t: return "#58d68d"
    if "BIO" in t: return "#d35400"
    return "#ecf0f1" 

def format_size(val):
    try: f = float(val); return str(int(f)) if f.is_integer() else str(val)
    except: return str(val)

def safe_format_density(val):
    try: return f"{float(val):.1f}"
    except: return str(val)

def safe_format_weight(val):
    try: return f"{float(val):.3f}"
    except: return str(val)

load_data()
# --- 這裡原本是用來讓主機自己讀磅秤的，現在改由客戶端上傳，所以這整段都要註解掉 ---
# if "thread_started" not in st.session_state:
#     try:
#         thread = threading.Thread(target=scale_reader_thread, daemon=True)
#         add_script_run_context(thread) 
#         thread.start()
#         st.session_state.thread_started = True
#     except Exception as e:
#         st.error(f"無法啟動磅秤讀取服務: {e}")
DENSITY_MAP = {64:(59.74,85.00),80:(74.03,93.75),96:(87.55,115.00),104:(96.24,121.88),112:(103.64,131.25),120:(111.05,140.63),128:(118.45,150.00),136:(125.85,159.38),144:(133.26,168.75),160:(154.50,175.50),192:(177.68,220.00),256:(226.60,312.00)}
DENSITY_OPTIONS = list(DENSITY_MAP.keys())
def get_p_label(d): return f"{d} ({d/16}P)"
SPECIAL_VARIETIES = ["BULK", "BUXD", "SB", "BIOSTAR"] 
ALL_VARIETIES = sorted(["ACPE", "ACBL", "BL", "BLOC(原反)", "RHK(S-F)"] + SPECIAL_VARIETIES)
TEMP_OPTIONS = ["1260", "1200", "1300", "1400", "1500", "BIOSTAR"]
# 1. 定義同步函式 (從 SQL 讀取最新資料覆蓋 Session)
def perform_global_sync():
    try:
        with sqlite3.connect(SQL_DB_NAME, timeout=10) as conn:
            # 更新工單狀態 (例如: 數量、狀態變更)
            df_wo = pd.read_sql("SELECT * FROM work_orders", conn)
            if not df_wo.empty:
                st.session_state.work_orders_db = df_wo
            
            # 更新生產日誌 (讓良品紀錄表格會跳動)
            df_log = pd.read_sql("SELECT * FROM production_logs ORDER BY 時間 DESC", conn)
            if not df_log.empty:
                st.session_state.production_logs = df_log
    except Exception as e:
        # 避免資料庫鎖定時報錯干擾畫面
        pass

# 2. 啟動唯一的自動刷新器 (每 1 秒刷新一次)
# 放在這裡，全域生效
st_autorefresh(interval=1000, key="master_heartbeat")

# 3. 【關鍵】每次網頁執行到這裡，都無條件執行同步
# 不管是自動刷新觸發，還是您按了按鈕觸發，保證資料都是最新的
perform_global_sync()

# ==========================================
# 5. 主選單
# ==========================================
with st.sidebar:
    st.markdown("### 🏭 產線系統 v13.30")
    menu = st.radio("功能導航", ["現場：產線秤重作業", "後台：系統管理中心"])
    st.divider()
    
    # 原有的儲存按鈕
    if st.button("💾 強制儲存資料 (CSV)", type="primary", use_container_width=True):
        st.session_state.work_orders_db = normalize_sequences(st.session_state.work_orders_db)
        save_data()
        st.toast("✅ 資料已同步至 CSV 檔案！")

    # --- 新增：SQL 生成按鈕 ---
    if st.button("🗄️ 生成 SQL 資料庫 (.db)", type="secondary", use_container_width=True):
        with st.spinner("正在生成資料庫..."):
            if export_to_sql():
                st.success(f"✅ 已成功生成 {SQL_DB_NAME}")
                st.toast("SQL 資料庫轉換成功！")
            else:
                st.error("❌ 資料庫生成失敗")
    # -----------------------
    
    st.markdown("### 📥 報表匯出")
    if not st.session_state.production_logs.empty:
        # 修正這裡：不要用 csv 當變數名
        log_csv_data = st.session_state.production_logs.to_csv(index=False).encode('utf-8-sig')
        st.download_button(
            label="下載生產紀錄 (CSV)", 
            data=log_csv_data,  # 這裡也要改
            file_name=f"生產日報表_{datetime.now().strftime('%Y%m%d')}.csv", 
            mime="text/csv"
        )
# ==========================================
# 功能 A: 後台管理
# ==========================================
if menu == "後台：系統管理中心":
    st.title("🛠️ 系統管理中心")
    tab1, tab2 = st.tabs(["📦 產品建檔與管理", "🗓️ 產能排程與佇列"])
    
    with tab1:
        st.subheader("1. 新增產品資料")
        
        # --- 緊湊版佈局 ---
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([1.5, 1, 1, 1.5])
            
            with c1:
                batch_client = st.text_input("客戶名", value="庫存")
            with c2:
                batch_temp = st.selectbox("溫度等級", TEMP_OPTIONS, index=0)
            with c3:
                batch_variety = st.selectbox("品種", [""] + ALL_VARIETIES, index=0)
            
            is_special = batch_variety in SPECIAL_VARIETIES
            fixed_weight_opt = 0
            batch_density = 0

            with c4:
                if is_special:
                    fixed_weight_opt = st.selectbox("固定包裝重 (kg)", [10, 15, 20, 25], index=0)
                else:
                    batch_density = st.selectbox("密度 (P數)", DENSITY_OPTIONS, format_func=get_p_label, index=6)

            st.write("")

            col_t1, col_t2 = st.columns([6, 1.5])
            with col_t1:
                st.markdown("##### 規格輸入")
            with col_t2:
                if st.button("🗑️ 重置表格", type="primary", use_container_width=True):
                    st.session_state.editor_df_clean = pd.DataFrame({"長": [0], "寬": [0], "高": [0], "下限": [0.0], "準重": [0.0], "上限": [0.0], "備註1": [""], "備註2": [""], "備註3": [""]})
                    st.rerun()

            if 'editor_df_clean' not in st.session_state:
                st.session_state.editor_df_clean = pd.DataFrame({"長": [0], "寬": [0], "高": [0], "下限": [0.0], "準重": [0.0], "上限": [0.0], "備註1": [""], "備註2": [""], "備註3": [""]})

            column_cfg_base = {"下限": st.column_config.NumberColumn(format="%.1f"), "上限": st.column_config.NumberColumn(format="%.1f")}
            column_cfg = {**column_cfg_base, "長": st.column_config.NumberColumn(disabled=is_special), "寬": st.column_config.NumberColumn(disabled=is_special), "高": st.column_config.NumberColumn(disabled=is_special), "準重": st.column_config.NumberColumn(format="%.3f")}

            st.session_state.editor_df_clean.index = range(1, len(st.session_state.editor_df_clean) + 1)
            
            # 使用固定行數，移除自動的灰色列
            edited_df = st.data_editor(st.session_state.editor_df_clean, num_rows="fixed", use_container_width=True, column_config=column_cfg, key="data_editor")
            
            # 【按鈕增加列】
            col_add, col_spacer = st.columns([1, 4])
            with col_add:
                if st.button("➕ 增加 1 列", type="primary", use_container_width=True):
                    current_data = edited_df
                    new_row = pd.DataFrame({"長": [0], "寬": [0], "高": [0], "下限": [0.0], "準重": [0.0], "上限": [0.0], "備註1": [""], "備註2": [""], "備註3": [""]})
                    st.session_state.editor_df_clean = pd.concat([current_data, new_row], ignore_index=True)
                    st.rerun()

            st.write("") 

            col_btn1, col_btn2 = st.columns([1, 3])
            
            with col_btn1:
                if st.button("🔄 計算重量", type="primary", use_container_width=True):
                    calc_df = edited_df.reset_index(drop=True)
                    for index, row in calc_df.iterrows():
                        if is_special:
                            w = float(fixed_weight_opt)
                            calc_df.at[index, "準重"], calc_df.at[index, "下限"], calc_df.at[index, "上限"] = w, w, w + 0.2
                        else:
                            if row["長"] > 0 and row["寬"] > 0 and row["高"] > 0:
                                vol = (row["長"]/1000) * (row["寬"]/1000) * (row["高"]/1000)
                                if batch_density in DENSITY_MAP:
                                    d_min, d_max = DENSITY_MAP[batch_density]
                                    calc_df.at[index, "準重"] = round(vol * batch_density, 3)
                                    calc_df.at[index, "下限"] = round(vol * d_min, 1)
                                    calc_df.at[index, "上限"] = round(vol * d_max, 1)
                    st.session_state.editor_df_clean = calc_df
                    st.rerun()

            with col_btn2:
                if st.button("💾 確認寫入資料庫", type="primary", use_container_width=True):
                    final_df = edited_df.reset_index(drop=True)
                    saved = 0
                    if not batch_variety: st.error("❌ 請選擇品種")
                    else:
                        for i, row in final_df.iterrows():
                            if row["準重"] > 0:
                                new_id = f"{batch_client}-{batch_variety}-{i}-{datetime.now().strftime('%M%S')}"
                                new_data = pd.DataFrame([[new_id, batch_client, batch_temp, batch_variety, batch_density if not is_special else "N/A", row["長"], row["寬"], row["高"], row["下限"], row["準重"], row["上限"], row["備註1"], row["備註2"], row["備註3"]]], columns=st.session_state.products_db.columns)
                                st.session_state.products_db = pd.concat([st.session_state.products_db, new_data], ignore_index=True)
                                saved += 1
                        if saved > 0:
                            cols = ["備註1", "備註2", "備註3"]
                            st.session_state.products_db[cols] = st.session_state.products_db[cols].fillna("NULL").replace(['', 'nan', 'None'], 'NULL')
                            save_data()
                            st.toast(f"✅ 匯入 {saved} 筆成功！"); st.session_state.editor_df_clean = pd.DataFrame({"長": [0], "寬": [0], "高": [0], "下限": [0.0], "準重": [0.0], "上限": [0.0], "備註1": [""], "備註2": [""], "備註3": [""]}); st.rerun()

        st.divider()
        st.subheader("2. 檢視與管理現有產品")
        if not st.session_state.products_db.empty:
            db_disp = st.session_state.products_db.copy()
            c_f1, c_f2, c_f3, c_f4, c_del = st.columns([2, 2, 2, 3, 2])
            f_cli = c_f1.selectbox("篩選客戶", ["全部"] + list(db_disp["客戶名"].unique()), key="db_f_cli")
            f_tmp = c_f2.selectbox("篩選溫度", ["全部"] + list(db_disp["溫度等級"].unique()), key="db_f_tmp")
            f_var = c_f3.selectbox("篩選品種", ["全部"] + list(db_disp["品種"].unique()), key="db_f_var")
            f_key = c_f4.text_input("關鍵字搜尋", placeholder="規格/備註...", key="db_f_key")

            if f_cli != "全部": db_disp = db_disp[db_disp["客戶名"] == f_cli]
            if f_tmp != "全部": db_disp = db_disp[db_disp["溫度等級"] == f_tmp]
            if f_var != "全部": db_disp = db_disp[db_disp["品種"] == f_var]
            if f_key:
                mask = db_disp.astype(str).apply(lambda x: x.str.contains(f_key, case=False, na=False)).any(axis=1)
                db_disp = db_disp[mask]

            db_disp.insert(0, "刪除", False)
            db_disp = db_disp.reset_index(drop=False) 
            
            cols_to_show_db = ["刪除", "客戶名", "溫度等級", "品種", "密度", "長", "寬", "高", "下限", "準重", "上限", "備註1", "備註2", "備註3"]
            edited_db = st.data_editor(
                db_disp[cols_to_show_db], 
                use_container_width=True, 
                column_config={
                    "刪除": st.column_config.CheckboxColumn(width="small"), 
                    "溫度等級": st.column_config.TextColumn(),
                    "下限": st.column_config.NumberColumn(format="%.1f"),
                    "準重": st.column_config.NumberColumn(format="%.3f"),
                    "上限": st.column_config.NumberColumn(format="%.1f")
                }
            )
            
            with c_del:
                st.write("") 
                st.write("")
                if st.button("🗑️ 刪除選取資料", type="primary", use_container_width=True):
                    selected_rows = edited_db[edited_db["刪除"] == True]
                    if not selected_rows.empty:
                        ids_to_remove = db_disp.loc[selected_rows.index, "產品ID"].tolist()
                        st.session_state.products_db = st.session_state.products_db[~st.session_state.products_db["產品ID"].isin(ids_to_remove)]
                        save_data()
                        st.toast(f"🗑️ 已刪除 {len(ids_to_remove)} 筆資料"); st.rerun()
        else: st.info("資料庫為空")

    # 後台管理 - 分層介面
    with tab2:
        if 'admin_line_choice' not in st.session_state:
            st.session_state.admin_line_choice = None

        if st.session_state.admin_line_choice is None:
            st.subheader("📊 選擇要管理的產線")
            st.markdown("請點選下方按鈕進入該產線的管理介面：")
            cols = st.columns(4)
            for i, line in enumerate(PRODUCTION_LINES):
                pending_count = len(st.session_state.work_orders_db[
                    (st.session_state.work_orders_db["產線"] == line) & 
                    (st.session_state.work_orders_db["狀態"] != "已完成")
                ])
                with cols[i]:
                    label = f"📍 {line}\n\n待生產: {pending_count} 筆"
                    if st.button(label, key=f"btn_sel_{line}", use_container_width=True, type="primary"):
                        st.session_state.admin_line_choice = line
                        st.rerun()
        
        else:
            target_line = st.session_state.admin_line_choice
            
            c_back, c_title = st.columns([1, 5])
            with c_back:
                if st.button("⬅️ 返回列表", type="primary"):
                    st.session_state.admin_line_choice = None
                    st.rerun()
            with c_title:
                st.subheader(f"⚙️ 正在管理：{target_line}")

            st.divider()

            # 1. 加入任務區塊 (詳細版)
            st.markdown("### ➕ 加入新任務")
            if not st.session_state.products_db.empty:
                db_select = st.session_state.products_db.copy()
                c_f1, c_f2, c_f3, c_f4 = st.columns(4)
                f_cli = c_f1.selectbox("篩選客戶", ["全部"] + list(db_select["客戶名"].unique()), key="sch_f_cli")
                f_tmp = c_f2.selectbox("篩選溫度", ["全部"] + list(db_select["溫度等級"].unique()), key="sch_f_tmp")
                f_var = c_f3.selectbox("篩選品種", ["全部"] + list(db_select["品種"].unique()), key="sch_f_var")
                f_key = c_f4.text_input("關鍵字搜尋", placeholder="規格/備註...", key="sch_f_key")

                if f_cli != "全部": db_select = db_select[db_select["客戶名"] == f_cli]
                if f_tmp != "全部": db_select = db_select[db_select["溫度等級"] == f_tmp]
                if f_var != "全部": db_select = db_select[db_select["品種"] == f_var]
                if f_key:
                    mask = db_select.astype(str).apply(lambda x: x.str.contains(f_key, case=False, na=False)).any(axis=1)
                    db_select = db_select[mask]
                
                db_select = db_select.reset_index(drop=False)
                view_df = pd.DataFrame()
                view_df["產品ID"] = db_select["產品ID"]
                view_df["客戶名"] = db_select["客戶名"]
                view_df["溫度"] = db_select["溫度等級"].astype(str)
                view_df["品種"] = db_select["品種"]
                view_df["📏 規格"] = db_select.apply(lambda x: f"{format_size(x['長'])}x{format_size(x['寬'])}x{format_size(x['高'])}", axis=1)
                
                view_df["下限"] = db_select["下限"]
                view_df["準重"] = db_select["準重"]
                view_df["上限"] = db_select["上限"]
                view_df["備註1"] = db_select["備註1"]
                view_df["備註2"] = db_select["備註2"]
                view_df["備註3"] = db_select["備註3"]
                
                view_df["📝 排程數量"] = 0 
                view_df.index = range(1, len(view_df) + 1)

                st.write("在表格最右側輸入「📝 排程數量」：")
                
                cols_to_display = ["客戶名", "溫度", "品種", "📏 規格", "下限", "準重", "上限", "備註1", "備註2", "備註3", "📝 排程數量"]
                # 1. 宣告表單
                with st.form(key=f"add_schedule_form_{target_line}"):
                    
                    # 2. 表格 (必須縮排)
                    edited_selection = st.data_editor(
                        view_df[cols_to_display], 
                        column_config={
                            "📝 排程數量": st.column_config.NumberColumn(min_value=0, step=1, required=True, format="%d"),
                            "客戶名": st.column_config.TextColumn(disabled=True),
                            "溫度": st.column_config.TextColumn(disabled=True),
                            "品種": st.column_config.TextColumn(disabled=True),
                            "📏 規格": st.column_config.TextColumn(disabled=True),
                            "下限": st.column_config.NumberColumn(disabled=True, format="%.1f"),
                            "準重": st.column_config.NumberColumn(disabled=True, format="%.3f"),
                            "上限": st.column_config.NumberColumn(disabled=True, format="%.1f"),
                            "備註1": st.column_config.TextColumn(disabled=True),
                            "備註2": st.column_config.TextColumn(disabled=True),
                            "備註3": st.column_config.TextColumn(disabled=True),
                        },
                        use_container_width=True
                    )
                    
                    st.write("")
                    
                    # 3. 👇 這裡必須縮排！讓它位於 with st.form 裡面
                    submit_btn = st.form_submit_button(f"⬇️ 確認加入至 {target_line} 的排程", type="primary", use_container_width=True)

                # 4. 👇 這裡要「取消縮排」(退回最左邊的層級)，檢查按鈕是否被按下
                if submit_btn:
                    items_index = edited_selection[edited_selection["📝 排程數量"] > 0].index
                    if not items_index.empty:
                        # ... (原本的處理邏輯) ...
                        global_count = len(st.session_state.work_orders_db)
                        new_orders = []
                        for idx in items_index:
                            qty = edited_selection.loc[idx, "📝 排程數量"]
                            original_row = db_select.iloc[idx-1]
                            global_count += 1
                            wo_id = f"WO-{datetime.now().strftime('%m%d')}-{global_count:04d}"
                            
                            # 處理備註與規格字串
                            note_text = str(original_row['備註1']) if pd.notna(original_row['備註1']) else ""
                            note_display = f" | {note_text}" if note_text else ""
                            spec_str = f"{format_size(original_row['長'])}x{format_size(original_row['寬'])}x{format_size(original_row['高'])}"
                            detail_str = f"[{original_row['客戶名']}] | {original_row['溫度等級']} | {original_row['品種']} | {spec_str} | {original_row['準重']}kg{note_display}"
                            
                            new_orders.append([
                                target_line, 9999, wo_id, original_row['產品ID'], detail_str, 
                                original_row['品種'], original_row['密度'], original_row['準重'], int(qty), 0, "待生產", datetime.now()
                            ])
                        
                        # 1. 更新 Session State
                        new_df = pd.DataFrame(new_orders, columns=ORDER_COLUMNS)
                        st.session_state.work_orders_db = pd.concat([st.session_state.work_orders_db, new_df], ignore_index=True)
                        st.session_state.work_orders_db = normalize_sequences(st.session_state.work_orders_db)
                        
                        # 2. 存入 CSV
                        save_data()
                        
                        # 3. 【關鍵修正】立刻寫入 SQL，防止被自動同步覆蓋！
                        export_to_sql()  
                        
                        st.toast(f"✅ 已成功加入 {len(new_orders)} 筆工單至 {target_line}！")
                        time.sleep(1.5)
                        st.rerun()
                    else: 
                        st.warning("請至少在一個項目輸入數量")
            else: st.warning("無產品資料")

            st.markdown("---")
            
            # 2. 佇列管理區塊 (簡化版)
            st.markdown(f"### 📋 {target_line} 佇列管理")
            
            active_wos = st.session_state.work_orders_db[
                (st.session_state.work_orders_db["狀態"] != "已完成") & 
                (st.session_state.work_orders_db["產線"] == target_line)
            ].copy().sort_values("排程順序")

            if not active_wos.empty:
                if not st.session_state.products_db.empty:
                      p_db = st.session_state.products_db.copy()
                      active_wos_view = active_wos.merge(p_db, on="產品ID", how="left")
                else: active_wos_view = active_wos.copy()
                
                display_df = pd.DataFrame()
                display_df["刪除"] = False
                display_df["排序"] = range(1, len(active_wos_view) + 1)
                
                if "客戶名" in active_wos_view.columns:
                    display_df["客戶名"] = active_wos_view["客戶名"]
                    display_df["品種"] = active_wos_view["品種_x"]
                    display_df["溫度"] = active_wos_view["溫度等級"].astype(str)
                    display_df["規格"] = active_wos_view.apply(lambda x: f"{format_size(x['長'])}x{format_size(x['寬'])}x{format_size(x['高'])}", axis=1)
                    display_df["準重"] = active_wos_view["準重_x"]
                else: display_df["內容"] = active_wos_view["詳細規格字串"]
                
                display_df["預計數量"] = active_wos_view["預計數量"]
                display_df["已完成"] = active_wos_view["已完成數量"]
                display_df.index = active_wos.index 

                col_q1, col_q2 = st.columns([4, 1])
                with col_q1:
                    edited_queue = st.data_editor(display_df, hide_index=True, use_container_width=True, key=f"q_editor_{target_line}",
                        column_config={
                            "刪除": st.column_config.CheckboxColumn(width="small"), 
                            "排序": st.column_config.NumberColumn(width="small", min_value=1, format="%d"),
                            "客戶名": st.column_config.TextColumn(disabled=True),
                            "品種": st.column_config.TextColumn(disabled=True),
                            "溫度": st.column_config.TextColumn(disabled=True),
                            "規格": st.column_config.TextColumn(disabled=True),
                            "準重": st.column_config.NumberColumn(disabled=True, format="%.3f"),
                            "預計數量": st.column_config.NumberColumn(disabled=True, format="%d"),
                            "已完成": st.column_config.NumberColumn(disabled=True, format="%d")
                        })
                with col_q2:
                    
                    # --- 🔄 更新排序 (這裡也可以順便優化，使用 on_click) ---
                    def action_update_sort(line, df):
                        for db_idx, row in df.iterrows():
                            # 更新 session state 中的排序
                            st.session_state.work_orders_db.at[db_idx, "排程順序"] = row["排序"]
                        # 正規化並存檔
                        st.session_state.work_orders_db = normalize_sequences(st.session_state.work_orders_db)
                        save_data()
                        export_to_sql()
                        st.toast(f"✅ {line} 排序已更新")

                    st.button(f"🔄 更新排序", 
                              type="primary", 
                              use_container_width=True, 
                              key=f"btn_upd_{target_line}",
                              on_click=action_update_sort,
                              args=(target_line, edited_queue)) # 把產線名稱和編輯後的表格傳進去

                    st.write("")
                    # ... (上面是 st.data_editor 的部分) ...

                    # 1. 定義刪除動作的函式 (放在這裡)
                    def action_delete_orders(line, df):
                        indices_to_remove = df[df["刪除"] == True].index.tolist()
                        if indices_to_remove:
                            st.session_state.work_orders_db = st.session_state.work_orders_db.drop(indices_to_remove)
                            st.session_state.work_orders_db = normalize_sequences(st.session_state.work_orders_db)
                            save_data()
                            export_to_sql()
                            st.toast("✅ 工單已移除")
                        else:
                            st.warning("⚠️ 未選擇任何工單")

                    # 2. 按鈕改成 on_click 模式 (注意：這裡不需要 if)
                    st.button(f"🗑️ 移除選中", 
                              type="primary", 
                              use_container_width=True, 
                              key=f"btn_del_{target_line}",
                              on_click=action_delete_orders,     # 綁定上面的函式
                              args=(target_line, edited_queue))  # 傳入參數

            # 3. 這裡是您想保留的 else，它屬於 "if not active_wos.empty"
            # 請確保這個 else 跟最上面的 if 對齊 (通常是往左退兩格)
            else: 
                st.info(f"{target_line} 目前無工單")

# ==========================================
# 功能 C: 現場秤重
# ==========================================

elif menu == "現場：產線秤重作業":
    # 每 1000 毫秒 (1秒) 自動刷新一次網頁，確保抓到最新模擬重量
    #from streamlit_autorefresh import st_autorefresh
    #st_autorefresh(interval=1000, key="weight_refresh")
    st.write("### 🏭 現場作業儀表板")
    op_tabs = st.tabs(PRODUCTION_LINES)
    
    for i, line_name in enumerate(PRODUCTION_LINES):
        with op_tabs[i]:
            mask = (st.session_state.work_orders_db["狀態"].isin(["待生產", "生產中"])) & \
                   (st.session_state.work_orders_db["產線"] == line_name)
            pending = st.session_state.work_orders_db[mask].sort_values(by="排程順序")
            curr = pending.iloc[0] if not pending.empty else None
            
            if not pending.empty:
                st.markdown(f'<div class="queue-header">📋 {line_name} 生產隊列</div>', unsafe_allow_html=True)
                if not st.session_state.products_db.empty:
                      p_db = st.session_state.products_db.copy()
                      queue_view = pending.merge(p_db, on="產品ID", how="left")
                else: queue_view = pending.copy()
                
                q_df = pd.DataFrame()
                q_df["序"] = range(1, len(queue_view) + 1)
                
                if "客戶名" in queue_view.columns:
                    q_df["客戶"] = queue_view["客戶名"]
                    q_df["溫度等級"] = queue_view["溫度等級"].astype(str)
                    q_df["品種"] = queue_view["品種_x"]
                    q_df["規格"] = queue_view.apply(lambda x: f"{format_size(x['長'])}x{format_size(x['寬'])}x{format_size(x['高'])}", axis=1)
                    q_df["下限"] = queue_view["下限"].apply(lambda x: f"{float(x):.1f}" if pd.notna(x) else "-")
                    q_df["準重"] = queue_view["準重_x"].apply(safe_format_weight)
                    q_df["上限"] = queue_view["上限"].apply(lambda x: f"{float(x):.1f}" if pd.notna(x) else "-")
                    q_df["備註1"] = queue_view["備註1"].fillna('')
                    q_df["備註2"] = queue_view["備註2"].fillna('')
                    q_df["備註3"] = queue_view["備註3"].fillna('')
                else: q_df["內容"] = queue_view["詳細規格字串"]
                
                q_df["進度"] = queue_view.apply(lambda x: f"{int(x['已完成數量'])} / {int(x['預計數量'])}", axis=1)
                
                pending["temp_sort"] = range(1, len(pending) + 1)
                pending["選單顯示"] = pending.apply(lambda x: f"【序{x['temp_sort']}】 {x['顯示內容']} (數:{int(x['預計數量'])})", axis=1)
                options_list = pending["選單顯示"].tolist()
                
                col_sel, col_finish_btn = st.columns([3, 1])
                with col_sel:
                    key_sel = f"sel_wo_{line_name}" 
                    current_idx = 0
                    if key_sel in st.session_state and st.session_state[key_sel] in options_list:
                        current_idx = options_list.index(st.session_state[key_sel])
                    wo_label = st.selectbox("👇 切換當前任務", options=options_list, index=current_idx, key=key_sel)
                
                curr_row_list = [row for index, row in pending.iterrows() if f"【序{row['temp_sort']}】 {row['顯示內容']} (數:{int(row['預計數量'])})" == wo_label]
                if curr_row_list: curr = curr_row_list[0]
                else: curr = pending.iloc[0]
                
                
            # 判斷是否有當前工單
                if curr is not None:
                    
                    # ========================================================
                    # 1. 右上角按鈕區 (垂直排列，不破壞版面)
                    # ========================================================
                    with col_finish_btn:
                        # (A) 🟢 正常結束按鈕
                        if st.button("🏁 生產結束", type="primary", use_container_width=True, key=f"fin_{line_name}"):
                            try:
                                # 1. SQL 直接寫入 (確保資料庫更新)
                                conn_btn = sqlite3.connect(SQL_DB_NAME, timeout=10)
                                c_btn = conn_btn.cursor()
                                c_btn.execute("UPDATE work_orders SET 狀態='已完成' WHERE 工單號碼=?", (curr["工單號碼"],))
                                conn_btn.commit(); conn_btn.close()
                                
                                # 2. 更新 Session State (讓畫面立刻反應)
                                idx_list = st.session_state.work_orders_db.index[st.session_state.work_orders_db["工單號碼"] == curr["工單號碼"]].tolist()
                                if idx_list:
                                    st.session_state.work_orders_db.at[idx_list[0], "狀態"] = "已完成"
                                    save_data() # 同步存入 CSV
                                
                                # 3. 清除選單快取並重整
                                if key_sel in st.session_state: del st.session_state[key_sel]
                                st.toast(f"✅ {line_name} 生產完成！"); time.sleep(0.5); st.rerun()
                            except Exception as e: st.error(str(e))

                        # (B) 🔴 取消/下錯單按鈕 (垂直排在下方)
                        if st.button("❌ 取消工單", type="secondary", use_container_width=True, key=f"can_{line_name}"):
                            try:
                                conn_btn = sqlite3.connect(SQL_DB_NAME, timeout=10)
                                c_btn = conn_btn.cursor()
                                c_btn.execute("UPDATE work_orders SET 狀態='已取消' WHERE 工單號碼=?", (curr["工單號碼"],))
                                conn_btn.commit(); conn_btn.close()

                                idx_list = st.session_state.work_orders_db.index[st.session_state.work_orders_db["工單號碼"] == curr["工單號碼"]].tolist()
                                if idx_list:
                                    st.session_state.work_orders_db.at[idx_list[0], "狀態"] = "已取消"
                                    save_data()

                                if key_sel in st.session_state: del st.session_state[key_sel]
                                st.toast(f"🗑️ {line_name} 工單已取消！"); time.sleep(0.5); st.rerun()
                            except Exception as e: st.error(str(e))

                    # ========================================================
                    # 2. 表格顯示區
                    # ⚠️ 關鍵：這裡必須「往左縮排」，跳出 with col_finish_btn
                    # 這樣表格才會顯示在下方，並且是全寬的
                    # ========================================================
                    def highlight_current(s):
                        return ['background-color: #d4e6f1' if str(s["客戶"]) in str(curr["顯示內容"]) else '' for v in s]
                    
                    st.dataframe(q_df.style.apply(highlight_current, axis=1), use_container_width=True, hide_index=True)
                    st.divider()
                    try:
                        spec = st.session_state.products_db[st.session_state.products_db["產品ID"] == curr["產品ID"]].iloc[0]
                        std, low, high = float(spec['準重']), float(spec['下限']), float(spec['上限'])
                        temp_val = str(spec['溫度等級'])
                        temp_color = get_temp_color(temp_val)
                        density_val = spec['密度']
                        try: density_show = f"{float(density_val):.1f}"
                        except: density_show = str(density_val).replace('N/A', '-')
                        size_show = f"{format_size(spec['長'])}x{format_size(spec['寬'])}x{format_size(spec['高'])}"
                        range_show = f"{low} - {std} - {high}"
                        notes_html = ""
                        for n in [spec['備註1'], spec['備註2'], spec['備註3']]:
                            if pd.notna(n) and str(n).strip() != "": notes_html += f"<div>• {n}</div>"
                        if not notes_html: notes_html = "<div style='opacity:0.5'>(無特殊備註)</div>"
                    except:
                        st.error("❌ 資料庫異常"); st.stop()
                    
                    col_left, col_right = st.columns([4, 6])
                    with col_left:
                        usc_html = f"""
<div class="unified-spec-card" style="border-left-color: {temp_color};">
    <div class="usc-header"><div class="u-label">Client / 客戶</div><div class="u-value">{spec['客戶名']}</div></div>
    <div class="usc-grid">
        <div class="usc-item"><span class="u-label">Temp / 溫度</span><span class="u-value" style="color: {temp_color}">{temp_val}</span></div>
        <div class="usc-item"><span class="u-label">Variety / 品種</span><span class="u-value">{spec['品種']}</span></div>
        <div class="usc-item"><span class="u-label">Density / 密度</span><span class="u-value">{density_show}</span></div>
    </div>
    <div class="usc-size-row"><div class="u-label">Size / 尺寸</div><div class="u-value">{size_show}</div></div>
    <div class="usc-range-row"><span class="u-label">Range</span><br><span class="u-value">{range_show}</span></div>
    <div class="usc-notes"><div class="u-label">Notes / 備註</div><div class="u-content">{notes_html}</div></div>
</div>
"""
                        st.markdown(usc_html, unsafe_allow_html=True)
                    # 修改後的秤重區塊
                    # --- 找到 with col_right: 之後的部分並替換 ---
                    with col_right:
                        # 這是要放在 with col_right: 裡面的輔助函式
                        def insert_log_to_sql(log_data):
                            try:
                                with sqlite3.connect(SQL_DB_NAME, timeout=10) as conn:
                                    cursor = conn.cursor()
                                    cursor.execute("""
                                        INSERT INTO production_logs (時間, 產線, 工單號碼, 產品ID, 實測重, 判定結果, NG原因)
                                        VALUES (?, ?, ?, ?, ?, ?, ?)
                                    """, log_data)
                                    conn.commit()
                            except Exception as e:
                                st.error(f"SQL寫入失敗: {e}")

                        def do_pass(c, v, ln):
                            # 1. 更新主機端畫面上的工單數量
                            idx = st.session_state.work_orders_db[st.session_state.work_orders_db["工單號碼"] == c["工單號碼"]].index[0]
                            st.session_state.work_orders_db.at[idx, "已完成數量"] += 1
                            st.session_state.work_orders_db.at[idx, "狀態"] = "生產中"
                            
                            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            log_values = (current_time, ln, c["工單號碼"], c["產品ID"], float(v), "PASS", "")
                            
                            # 2. 更新 Session (讓下面的表格立即顯示)
                            new_log = pd.DataFrame([list(log_values)], columns=LOG_COLUMNS)
                            st.session_state.production_logs = pd.concat([st.session_state.production_logs, new_log], ignore_index=True)
                            
                            # 3. 備份 CSV
                            save_data()
                            
                            # 4. 【關鍵】直接寫入 SQL (不會覆蓋別人的資料)
                            insert_log_to_sql(log_values)
                            # 順便更新工單狀態 (數量) 到 SQL
                            export_to_sql() 
                            
                            st.toast(f"✅ {ln} 良品紀錄成功: {v:.3f} kg")

                        def do_ng(c, v, ln):
                            reason = st.session_state.get(f"ng_sel_{ln}", "其他")
                            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            log_values = (current_time, ln, c["工單號碼"], c["產品ID"], float(v), "NG", reason)
                            
                            # 1. 更新 Session
                            new_log = pd.DataFrame([list(log_values)], columns=LOG_COLUMNS)
                            st.session_state.production_logs = pd.concat([st.session_state.production_logs, new_log], ignore_index=True)
                            
                            # 2. 備份 CSV
                            save_data()
                            
                            # 3. 【關鍵】直接寫入 SQL
                            insert_log_to_sql(log_values)
                            
                            st.toast(f"🔴 {ln} NG 紀錄成功: {v:.3f} kg")
                        # 2. 獲取數據邏輯 (修正了自動/手動衝突)
                        use_auto_scale = st.toggle("🔌 連接實體磅秤", value=True, key=f"auto_{line_name}")
                        
                        if use_auto_scale:
                            val = shared_data.weight - shared_data.offset
                            last_ts = shared_data.last_update
                            if last_ts > 0:
                                update_str = datetime.fromtimestamp(last_ts).strftime('%H:%M:%S')
                                st.info(f"🛰️ 磅秤連線中... 實測重量: {val:.3f} kg (同步: {update_str})")
                            if shared_data.error:
                                st.error(f"連線異常: {shared_data.error}")
                            
                            if st.button("⚖️ 歸零/去皮 (TARE)", key=f"tare_{line_name}", use_container_width=True):
                                shared_data.offset = shared_data.weight
                                st.rerun()
                        else:
                            # 手動模式：由 Slider 完全控制 val
                            val = st.slider(f"⚖️ 手動調整重量", low*0.8, high*1.2, std, 0.001, format="%.3f", key=f"slider_{line_name}")
                            st.warning("⚠️ 目前處於手動輸入模式")

                        # 3. 判定與顯示
                        is_pass = low <= val <= high
                        is_ng_valid = val > 0.05 # 只要有東西在秤上(大於50g)就允許記 NG

                        status_cls = "status-pass" if is_pass else ("status-ng-ready" if is_ng_valid else "status-fail")
                        rem_qty = curr['預計數量'] - curr['已完成數量']
                        over_cls = "over-prod" if rem_qty < 0 else ""

                        # 顯示大儀表板 (修正數位顯示到小數點三位)
                        st.markdown(f"""
                            <div class="status-container {status_cls}">
                                <div class="countdown-box"><span class="countdown-label">剩餘數量</span><div class="countdown-val {over_cls}">{int(rem_qty)}</div></div>
                                <div class="weight-display digital-font">{val:.3f}</div>
                                <div style="font-size:1.5rem; font-weight:bold;">kg</div>
                            </div>""", unsafe_allow_html=True)

                        st.write("") # 間隔

                        # 4. 操作按鈕區
                        b_l, b_r = st.columns([3, 1])
                        with b_l:
                            st.button("🟢 紀錄良品 (PASS)", 
                                      disabled=not is_pass, 
                                      type="primary", 
                                      use_container_width=True, 
                                      on_click=do_pass, 
                                      args=(curr, val, line_name), 
                                      key=f"btn_pass_{line_name}")

                        with b_r:
                            st.button("🔴 紀錄 NG", 
                                      disabled=not is_ng_valid, 
                                      type="primary", 
                                      use_container_width=True, 
                                      on_click=do_ng, 
                                      args=(curr, val, line_name),
                                      key=f"btn_ng_{line_name}")

                        if is_ng_valid and not is_pass:
                            st.selectbox("NG 原因說明", ["不足重尾數", "規格切換廢料", "外觀不良", "其他"], key=f"ng_sel_{line_name}")

                        st.divider()

                        # 5. 歷史紀錄 (修正排版，確保隨時可見)
                        line_logs = st.session_state.production_logs[st.session_state.production_logs["產線"] == line_name]
                        h_l, h_r = st.columns(2)
                        
                        with h_l:
                            st.markdown('<div class="history-header">✅ 良品紀錄 (最近5筆)</div>', unsafe_allow_html=True)
                            st.dataframe(line_logs[line_logs["判定結果"]=="PASS"].tail(5).sort_index(ascending=False), use_container_width=True, hide_index=True)
                        
                        with h_r:
                            st.markdown('<div class="history-header">🔴 NG 紀錄 (最近5筆)</div>', unsafe_allow_html=True)
                            st.dataframe(line_logs[line_logs["判定結果"]=="NG"].tail(5).sort_index(ascending=False), use_container_width=True, hide_index=True)

                        # 6. 撤銷功能
                        if st.button("↩️ 撤銷上一筆紀錄", type="primary", use_container_width=True, key=f"undo_{line_name}", disabled=line_logs.empty):
                            last_idx = line_logs.index[-1]
                            last_wo = line_logs.loc[last_idx, "工單號碼"]
                            if line_logs.loc[last_idx, "判定結果"] == "PASS":
                                wo_indices = st.session_state.work_orders_db.index[st.session_state.work_orders_db["工單號碼"] == last_wo].tolist()
                                if wo_indices:
                                    st.session_state.work_orders_db.at[wo_indices[0], "已完成數量"] -= 1
                            st.session_state.production_logs = st.session_state.production_logs.drop(last_idx)
                            save_data()
                            st.rerun()
                            
                            # [v13.29 修改] 顯示全產線紀錄 (不隨工單清空)
                            line_logs = st.session_state.production_logs[st.session_state.production_logs["產線"] == line_name]
                            
                            # 良品統計
                            pass_all = line_logs[line_logs["判定結果"] == "PASS"]
                            total_weight = 0.0
                            if not pass_all.empty:
                                wo_map = st.session_state.work_orders_db.set_index("工單號碼")["準重"].to_dict()
                                for _, row in pass_all.iterrows():
                                    w_std = wo_map.get(row["工單號碼"], 0)
                                    total_weight += float(w_std)

                            # NG 統計
                            ng_all = line_logs[line_logs["判定結果"] == "NG"]
                            total_ng = len(ng_all)

                            with h_l:
                                st.markdown(f'<div class="history-header">✅ 良品紀錄 (累計: {total_weight:.1f} kg)</div>', unsafe_allow_html=True)
                                # 使用全產線紀錄
                                c_logs = line_logs
                                
                                if not c_logs.empty: 
                                    pass_df = c_logs[c_logs["判定結果"]=="PASS"].copy()
                                    if not pass_df.empty:
                                        pass_df = pass_df.reset_index(drop=True)
                                        pass_df["序號"] = range(1, len(pass_df) + 1)
                                        display_cols = ["序號", "時間", "實測重"]
                                        st.dataframe(
                                            pass_df[display_cols].sort_index(ascending=False), 
                                            use_container_width=True, 
                                            hide_index=True,
                                            column_config={
                                                "實測重": st.column_config.NumberColumn(format="%.1f")
                                            }
                                        )
                                    else: st.info("尚無良品")
                                else: st.info("尚無生產紀錄")
                                    
                            with h_r:
                                st.markdown(f'<div class="history-header">🔴 NG 紀錄 (累計數量: {total_ng})</div>', unsafe_allow_html=True)
                                if not c_logs.empty: 
                                    ng_df = c_logs[c_logs["判定結果"]=="NG"].copy()
                                    if not ng_df.empty:
                                        ng_df = ng_df.reset_index(drop=True)
                                        ng_df["序號"] = range(1, len(ng_df) + 1)
                                        display_cols = ["序號", "時間", "NG原因"]
                                        st.dataframe(ng_df[display_cols].sort_index(ascending=False), use_container_width=True, hide_index=True)
                                    else: st.info("尚無NG品")
                                else: st.info("尚無生產紀錄")
                                
                                st.markdown("---")
                                # 撤銷功能：邏輯改為撤銷該產線最新的一筆
                                def do_undo():
                                    w = st.session_state.production_logs[st.session_state.production_logs["產線"] == line_name]
                                    if not w.empty:
                                        last = w.index[-1]
                                        last_wo = w.loc[last, "工單號碼"]
                                        # 嘗試回扣工單數量
                                        idx_list = st.session_state.work_orders_db.index[st.session_state.work_orders_db["工單號碼"] == last_wo].tolist()
                                        if idx_list:
                                            idx = idx_list[0]
                                            if w.loc[last, "判定結果"] == "PASS":
                                                if st.session_state.work_orders_db.at[idx, "已完成數量"] > 0:
                                                    st.session_state.work_orders_db.at[idx, "已完成數量"] -= 1
                                        
                                        st.session_state.production_logs = st.session_state.production_logs.drop(last)
                                        save_data(); st.toast("↩️ 已撤銷上一筆紀錄")
                            
                            st.button("↩️ 撤銷", type="primary", disabled=c_logs.empty, use_container_width=True, on_click=do_undo, key=f"undo_{line_name}")
            else:
                st.info(f"💤 {line_name} 目前閒置中，請至後台加入排程。")
# ==========================================
# ➕ 新增功能：雲端內建模擬器 (Cloud Simulator)
# 請將這段程式碼放在 apptest.py 的側邊欄 (Sidebar) 程式碼區域中
# ==========================================

# 1. 定義模擬器執行緒邏輯
def run_cloud_simulation():
    # 這裡的 URL 指向自己 (因為都在同一個雲端容器內)
    LOCAL_API_URL = "http://127.0.0.1:8000" 
    target_lines = ["Line 1", "Line 2", "Line 3", "Line 4"]
    
    while st.session_state.get("is_simulating", False):
        try:
            # 隨機選一條產線
            line = random.choice(target_lines)
            
            # A. 詢問該產線工單
            try:
                resp = requests.get(f"{LOCAL_API_URL}/current_order/{line}", timeout=2)
            except:
                time.sleep(1); continue # API 還沒醒來，稍等

            if resp.status_code == 200:
                data = resp.json()
                order_id = data.get("order_id")

                if order_id:
                    # B. 生成模擬數據
                    target = data.get("target_weight", 25.0)
                    fake_weight = round(target + random.uniform(-0.1, 0.1), 3)
                    
                    payload = {
                        "line_name": line,
                        "order_id": order_id,
                        "product_id": data.get("product_id"),
                        "weight": str(fake_weight),
                        "status": "PASS",
                        "reason": "Cloud_Sim_Test"
                    }

                    # C. 上傳數據
                    requests.post(f"{LOCAL_API_URL}/upload", json=payload, timeout=2)
                    print(f"🤖 [雲端模擬] {line} 上傳成功: {fake_weight}")
                
            time.sleep(random.uniform(0.5, 2.0)) # 隨機休息，製造自然感

        except Exception as e:
            print(f"模擬器錯誤: {e}")
            time.sleep(1)

# 2. 在側邊欄加入控制介面
with st.sidebar:
    st.divider()
    st.markdown("### 🤖 雲端壓力測試")
    
    # 使用 session_state 來記住開關狀態
    if "is_simulating" not in st.session_state:
        st.session_state.is_simulating = False
    
    # 顯示開關按鈕
    if st.toggle("啟動自動模擬 (Zombie Mode)", value=st.session_state.is_simulating, key="sim_toggle"):
        if not st.session_state.is_simulating:
            st.session_state.is_simulating = True
            # 啟動背景執行緒
            t = threading.Thread(target=run_cloud_simulation, daemon=True)
            # --- 新增這段來解決 Context 警告 ---
            try:
                from streamlit.runtime.scriptrunner import add_script_run_context
                add_script_run_context(t)
            except:
                pass
            # --------------------------------
            t.start()
            st.toast("🚀 雲端模擬器已啟動！")
    else:
        st.session_state.is_simulating = False




