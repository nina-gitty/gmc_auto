import sys
import time
import json
import queue
import threading
import subprocess
import shutil
import base64
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import streamlit as st
import pandas as pd
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import re

st.set_page_config(page_title="GMC Region Mismatch Audit Tool", layout="wide")

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "region_mismatch.py"
TRANS_FILE = HERE / "translation.json"

# =========================================================
# 1. Session State Initialization
# =========================================================
if "running" not in st.session_state:
    st.session_state.running = False
    st.session_state.proc = None
    st.session_state.log_q = None
    st.session_state.lines = []
    st.session_state.realtime_results = []
    st.session_state.target_product_id = ""
    st.session_state.target_url = ""
    st.session_state.status_text = ""
    st.session_state.progress_val = 0.0
    st.session_state.progress_label = ""
    st.session_state.analysis_df = None
    st.session_state.saved_blob = ""
    st.session_state.report_path = None
    st.session_state.images_dir = None
    st.session_state.schema_dir = None
    st.session_state.returncode = None
    st.session_state.stdout_all = ""
    st.session_state.started_at = None


# =========================================================
# 2. CSS Styling
# =========================================================
st.markdown("""
    <style>
    .rotating-icon { display: inline-block; animation: rotate-60-deg 3s infinite steps(6); font-size: 1.2rem; margin-right: 8px; }
    .comp-table { width: 100%; border-collapse: collapse; font-size: 0.9rem; margin-bottom: 10px; border: 1px solid #eee; }
    .comp-table th { text-align: left; color: #444; background-color: #f9fafb; border-bottom: 2px solid #eee; padding: 8px 12px; font-weight: 600; }
    .comp-table td { border-bottom: 1px solid #f0f0f0; padding: 10px 12px; vertical-align: top; color: #222; }
    div[data-testid="stTable"] table { border-radius: 8px; overflow: hidden; border: 1px solid #e0e0e0; }
    .status-box { padding: 1rem; border-radius: 0.5rem; background-color: #e8f0fe; color: #1a73e8; border: 1px solid #d2e3fc; margin-bottom: 10px; }
    .status-header { display: flex; align-items: center; margin-bottom: 8px; }
    .status-text { font-size: 0.9rem; color: #444; word-break: break-word; }
    .analysis-container { margin-top: 20px; padding: 15px; border: 1px solid #e0e0e0; border-radius: 10px; background-color: #f8f9fa; }
    </style>
    """, unsafe_allow_html=True)


# --- Helpers ---
def set_query_param(url: str, key: str, value: str) -> str:
    if not url: return ""
    if value is None: return url
    u = urlparse(url)
    qs = parse_qs(u.query)
    qs[key] = [str(value)]
    new_query = urlencode(qs, doseq=True)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_query, u.fragment))

def parse_report_paths(stdout_text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    report_path = None
    images_dir = None
    schema_dir = None
    for line in (stdout_text or "").splitlines():
        s = line.strip()
        if s.startswith("- Report:"):
            report_path = s.split(":", 1)[1].strip()
        elif s.startswith("- Images:"):
            images_dir = s.split(":", 1)[1].strip()
        elif s.startswith("- Schema:"):
            schema_dir = s.split(":", 1)[1].strip()
    return report_path, images_dir, schema_dir

def safe_read_json(path: Path) -> Optional[Any]:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except: return None

def clean_currency(val):
    if not val: return ""
    return re.sub(r'[^\d.,]', '', str(val)).strip()

def extract_info_from_blob(blob_text):
    info = {"price": "", "availability": ""}
    if not blob_text: return info
    lines = [l.strip() for l in blob_text.splitlines() if l.strip()]
    sale_price_found = ""
    regular_price_found = ""
    for i, line in enumerate(lines):
        line_lower = line.lower()
        if "sale price" in line_lower and i + 1 < len(lines):
            val = lines[i+1]
            if any(c.isdigit() for c in val): sale_price_found = val
        elif "price" == line_lower and i + 1 < len(lines):
            val = lines[i+1]
            if any(c.isdigit() for c in val): regular_price_found = val
        elif "availability" in line_lower and i + 1 < len(lines):
            info["availability"] = lines[i+1]
    raw_price = sale_price_found if sale_price_found else regular_price_found
    info["price"] = clean_currency(raw_price)
    return info

def generate_standalone_html(groups, target_url, target_pid):
    html_parts = [f"<html><body><h1>Result for {target_pid}</h1>"]
    for g in groups:
        html_parts.append(f"<div>{g.get('region_id')}</div>")
    html_parts.append("</body></html>")
    return "\n".join(html_parts)

# --- Translation & Parsing Helpers (Internal) ---
def get_market_from_url(url):
    try:
        match = re.search(r'lg\.com/([a-z]{2}(?:_[a-z]{2})?)/', url)
        if match: return match.group(1)
    except: pass
    return "global"

def translate_status_with_format(text, market):
    if not text: return ""
    
    trans_map = {}
    if TRANS_FILE.exists():
        try:
            trans_map = json.loads(TRANS_FILE.read_text(encoding="utf-8"))
        except: pass
    
    text_clean = " ".join(text.split()).lower()
    
    found_status = None
    market_rules = trans_map.get("market_map", {}).get(market, {})
    for k, v in market_rules.items():
        if k in text_clean: 
            found_status = v
            break
            
    if not found_status:
        global_rules = trans_map.get("global_map", {})
        for k, v in global_rules.items():
            if k in text_clean: 
                found_status = v
                break
    
    if found_status:
        return f"{found_status} ({text})"
    else:
        return text

def normalize_gmc_status(val):
    v = val.lower().strip()
    if "in" in v and "stock" in v and "out" not in v: return "InStock"
    if "out" in v: return "OutOfStock"
    if "pre" in v: return "PreOrder"
    return val

# --- [Internal] Post-Audit Logic ---
def run_post_audit_internal(schema_dir_str, mode, default_gmc, regional_text):
    schema_dir = Path(schema_dir_str)
    if not schema_dir.exists():
        st.error("Schema directory missing.")
        return

    # 1. Parse Regional Text
    regional_map = {}
    if regional_text:
        raw_text = regional_text.replace("\t", "\n").replace("\r", "\n")
        lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
        
        current_key = None
        for line in lines:
            if "KST" in line or "GMT" in line or "AM" in line or "PM" in line or re.search(r'\d{2}:\d{2}', line):
                continue

            l_lower = line.lower()
            is_status = any(k in l_lower for k in ["in stock", "out of stock", "instock", "outofstock", "limited", "preorder"])
            
            if is_status:
                if current_key:
                    std_val = normalize_gmc_status(line)
                    regional_map[current_key] = std_val
                    current_key = None
            else:
                current_key = line

    # 2. Files & Data Aggregation
    rows = []
    files = sorted(schema_dir.glob("*__schema_*.json"))
    
    for json_file in files:
        try:
            fname = json_file.name
            parts = fname.split("__")
            if len(parts) < 2: continue
            
            region_part = parts[0]
            rid = region_part.replace("region_", "")
            
            display_rid = rid
            lookup_key = rid
            if rid == "default":
                display_rid = "Default (No Param)"
                lookup_key = ""

            schema_data = safe_read_json(json_file)
            offers = schema_data.get("offers", {})
            if isinstance(offers, list): offers = offers[0] if offers else {}
            
            s_price = clean_currency(offers.get("price", ""))
            s_avail = offers.get("availability", "").replace("https://schema.org/", "")

            scrape_fname = fname.replace("__schema_", "__scrape_")
            scrape_file = json_file.parent / scrape_fname
            visual_data = safe_read_json(scrape_file) if scrape_file.exists() else {}
            
            v_price = visual_data.get("visual_price", "")
            raw_btn_text = visual_data.get("buy_button_text", "")
            target_url = visual_data.get("meta_url", "")

            # Translate
            market_code = get_market_from_url(target_url)
            v_avail_formatted = translate_status_with_format(raw_btn_text, market_code)

            # GMC Value
            gmc_val = normalize_gmc_status(default_gmc)
            if lookup_key:
                for k, v in regional_map.items():
                    if k.lower() == lookup_key.lower():
                        gmc_val = v
                        break
            
            rows.append({
                "Region": display_rid,
                "GMC": gmc_val,
                "Visual_Standard": v_avail_formatted.split('(')[0].strip() if '(' in v_avail_formatted else v_avail_formatted,
                "Visual_Full": v_avail_formatted,
                "Visual_Price": v_price,
                "Schema": s_avail if mode == "Availability" else s_price,
            })
        except: continue

    st.session_state.analysis_df = pd.DataFrame(rows)


# --- [Process] Main Crawl ---
def start_process(cmd: List[str]) -> None:
    q: "queue.Queue[str]" = queue.Queue()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", cwd=str(HERE), bufsize=1, universal_newlines=True)
    def reader():
        try:
            for line in proc.stdout: q.put(line.rstrip("\n"))
        except: pass
        finally: 
            try: proc.stdout.close()
            except: pass
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    st.session_state.running = True
    st.session_state.proc = proc
    st.session_state.log_q = q
    st.session_state.lines = []
    st.session_state.started_at = time.time()
    st.session_state.stdout_all = stdout_text = ""
    st.session_state.returncode = None
    st.session_state.realtime_results = []
    st.session_state.status_text = "Starting..."
    st.session_state.progress_val = 0.0
    st.session_state.progress_label = "Initializing..."
    st.session_state.analysis_df = None

def stop_process() -> None:
    proc = st.session_state.get("proc")
    if not proc: return
    try:
        proc.terminate()
        time.sleep(0.5)
        if proc.poll() is None: proc.kill()
    except: pass

def drain_logs() -> None:
    q = st.session_state.get("log_q")
    if not q: return
    while True:
        try:
            line = q.get_nowait()
            if "[PROGRESS]" in line:
                clean_msg = line.replace("[PROGRESS]", "").strip()
                match = re.search(r"<(\d+)/(\d+)>", clean_msg)
                if match:
                    current, total = map(int, match.groups())
                    st.session_state.progress_val = current / total
                    st.session_state.progress_label = f"Region {current} of {total}"
                    clean_msg = re.sub(r"<(\d+)/(\d+)>", "", clean_msg).strip()
                st.session_state.status_text = clean_msg
            elif "[RESULT_JSON]" in line:
                try:
                    json_str = line.replace("[RESULT_JSON]", "").strip()
                    block_data = json.loads(json_str)
                    st.session_state.realtime_results.append(block_data)
                except: pass
            st.session_state.lines.append(line)
        except queue.Empty:
            break

def finalize_if_done() -> None:
    proc = st.session_state.get("proc")
    if not proc: return
    rc = proc.poll()
    if rc is None: return
    st.session_state.returncode = rc
    st.session_state.running = False
    stdout_text = "\n".join(st.session_state.lines)
    st.session_state.stdout_all = stdout_text
    st.session_state.report_path, st.session_state.images_dir, st.session_state.schema_dir = parse_report_paths(stdout_text)

# --- Recover Results ---
def reload_results_from_disk():
    schema_dir_str = st.session_state.get("schema_dir")
    if not schema_dir_str: return
    schema_dir = Path(schema_dir_str)
    if not schema_dir.exists(): return
    if st.session_state.realtime_results: return
    
    recovered = []
    for f in sorted(schema_dir.glob("*__schema_*.json")):
        try:
            fname = f.name
            region_part = fname.split("__")[0].replace("region_", "")
            img_name = fname.replace("__schema_", "__website_").replace(".json", ".png")
            block = {
                "region_id": region_part if region_part != "default" else "",
                "final_url": "", 
                "website_png_rel": f"images/{img_name}",
                "schema_path_abs": str(f),
                "schema_json_rel": f"schema/{fname}"
            }
            recovered.append(block)
        except: pass
    if recovered:
        st.session_state.realtime_results = recovered

# --- Rendering Helper ---
def render_realtime_results():
    if not st.session_state.running and not st.session_state.realtime_results:
        reload_results_from_disk()

    groups = st.session_state.get("realtime_results", [])
    target_pid = st.session_state.get("target_product_id", "N/A")
    target_url = st.session_state.get("target_url", "")

    if not groups and not st.session_state.running: return

    st.markdown("### 📸 Audit Results")
    if not groups and st.session_state.running:
        st.info("Waiting for first result...")
        return

    st.divider()
    for g in groups:
        rid = g.get("region_id") or ""
        region_display = f"region_{rid}" if rid else "Default (No Param)"
        st.markdown(f"#### {region_display}")
        c_img, c_schema = st.columns([65, 35], gap="large")
        with c_img:
            schema_path = g.get("schema_path_abs", "")
            img_path = Path(schema_path).parent.parent / "images" / Path(g.get("website_png_rel")).name if schema_path else None
            if img_path and img_path.exists():
                st.image(str(img_path), use_container_width=True)
            else:
                st.info("Generating screenshot...")
        with c_schema:
            schema_path = g.get("schema_path_abs", "")
            sd = safe_read_json(Path(schema_path)) if schema_path else {}
            offers = sd.get("offers", {}) if sd else {}
            if isinstance(offers, list): offers = offers[0] if offers else {}
            s_price = clean_currency(offers.get('price', '-'))
            s_avail = offers.get("availability", "-").replace("https://schema.org/", "")
            st.markdown(f"""<table class="comp-table"><thead><tr><th>Field</th><th>Schema</th></tr></thead><tbody><tr><td>Price</td><td>{s_price}</td></tr><tr><td>Avail</td><td>{s_avail}</td></tr></tbody></table>""", unsafe_allow_html=True)
            with st.expander("JSON"): st.json(sd)
        st.divider()

# --- Main Layout ---
col_t1, col_t2 = st.columns([0.85, 0.15])
with col_t1: st.title("GMC Region Mismatch Audit Tool")
left_col, right_col = st.columns([0.25, 0.75], gap="large")

# === LEFT COLUMN: Controls ===
with left_col:
    st.subheader("1. Input Data")
    blob = st.text_area("Blob/URL", height=200, disabled=st.session_state.running)
    
    b1, b2 = st.columns(2)
    with b1: run_btn = st.button("Run Audit", type="primary", use_container_width=True, disabled=st.session_state.running)
    with b2: stop_btn = st.button("Stop", use_container_width=True, disabled=not st.session_state.running)
    
    status_container = st.empty()

    # [NEW] Post-Audit Input Section (Visible only if Audit Data Exists)
    schema_dir = st.session_state.get("schema_dir")
    # 실행 중이 아니고, 결과 디렉토리가 있을 때만 표시
    if not st.session_state.running and schema_dir:
        st.markdown("---")
        st.subheader("2. Post-Audit Settings")
        
        with st.container():
            st.markdown("""<div class="analysis-container">""", unsafe_allow_html=True)
            
            audit_mode = st.radio("Comparision Mode", ["Price", "Availability"], horizontal=True)
            
            # Default Value
            blob_info = extract_info_from_blob(st.session_state.get("saved_blob", ""))
            auto_val = blob_info["price"] if audit_mode == "Price" else blob_info["availability"]
            default_gmc = st.text_input("Default GMC Value", value=auto_val)
            
            # Regional Inventory
            regional_text = ""
            if audit_mode == "Availability":
                regional_text = st.text_area("Regional Inventory (Paste from GMC)", height=150, placeholder="Paste data here...")
            
            st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)
            
            # Generate Button (Left Side)
            if st.button("Generate Table", type="primary", use_container_width=True):
                run_post_audit_internal(st.session_state.get("schema_dir"), audit_mode, default_gmc, regional_text)
            
            st.markdown("</div>", unsafe_allow_html=True)


# === RIGHT COLUMN: Results ===
with right_col:
    result_area = st.container()
    
    # 1. Real-time Crawl Results
    with result_area:
        render_realtime_results()

    # 2. Post-Audit Table (Visible only if DataFrame exists)
    if st.session_state.analysis_df is not None:
        st.markdown("### 📊 Analysis Table")
        
        # Checkbox Layout (Above Table)
        c_sp1, c_sp2, c_chk, c_sp3 = st.columns([1, 1, 2, 1])
        
        show_original = False
        if "Availability" in str(st.session_state.get("analysis_df", "")): # Check mode roughly or pass it
             pass 
        
        # Mode를 session state에 저장하지 않았으므로, 데이터프레임 컬럼으로 유추하거나
        # 위쪽 radio button 값은 rerun 되어야 알 수 있음.
        # 심플하게: Visual_Full 컬럼이 있으면 Availability 모드임.
        is_avail_mode = "Visual_Full" in st.session_state.analysis_df.columns
        
        if is_avail_mode:
            with c_chk:
                show_original = st.checkbox("Show Original Text", value=False)

        # Display Data Preparation
        df_display = st.session_state.analysis_df.copy()
        
        if is_avail_mode:
            if show_original:
                df_display['LG.com (Visual)'] = df_display['Visual_Full']
            else:
                df_display['LG.com (Visual)'] = df_display['Visual_Standard']
        else:
            df_display['LG.com (Visual)'] = df_display['Visual_Price']

        df_display = df_display[["Region", "GMC", "LG.com (Visual)", "Schema"]]

        # Highlighting
        def highlight_mismatch(row):
            gmc = str(row.get('GMC', '')).strip().lower()
            vis_full = str(row.get('LG.com (Visual)', '')).strip()
            # 괄호 앞부분만 추출 (표준값)
            vis_std = vis_full.split('(')[0].strip().lower()
            
            if gmc and vis_std and (gmc != vis_std):
                return ['background-color: #ffe6e6; color: #b30000'] * len(row)
            return [''] * len(row)

        st.dataframe(
            df_display.style.apply(highlight_mismatch, axis=1), 
            use_container_width=True, 
            hide_index=True
        )
        
        # Download Button (Bottom Right)
        st.download_button(
            "📄 Download Result Report", 
            generate_standalone_html(st.session_state.realtime_results, st.session_state.target_url, st.session_state.target_product_id),
            f"audit_report.html", 
            "text/html"
        )


# === Logic Execution ===
if run_btn:
    result_area.empty()
    st.session_state.analysis_df = None
    st.session_state.saved_blob = blob
    if not SCRIPT.exists(): st.stop()
    cmd = [sys.executable, str(SCRIPT), "--no_open"]
    
    final_url = ""
    lines = [l.strip() for l in blob.splitlines() if l.strip()]
    for l in lines:
        if l.startswith("http"): final_url = l
    if not final_url: st.error("URL not found"); st.stop()
    
    final_pid = ""
    for i, l in enumerate(lines):
        if l.lower() == "product id" and i+1 < len(lines): final_pid = lines[i+1]

    cmd += ["--url", final_url]
    if final_pid: cmd += ["--product_id", final_pid]
    if "product page" in blob.lower(): cmd += ["--blob", blob]
    
    st.session_state.target_product_id = final_pid
    st.session_state.target_url = final_url
    start_process(cmd)

if stop_btn: stop_process()

was_running = st.session_state.running
drain_logs()
finalize_if_done()

# Status Bar Update (Left)
if st.session_state.running:
    status_text = st.session_state.get("status_text", "Starting...")
    p_val = st.session_state.get("progress_val", 0.0)
    with status_container.container():
        st.markdown(f"""<div class="status-box"><div class="status-header"><span class="rotating-icon">⏳</span><b>Running...</b></div><div class="status-text">{status_text}</div></div>""", unsafe_allow_html=True)
        st.progress(p_val)
else:
    rc = st.session_state.get("returncode")
    if rc == 0: status_container.success("Done")
    elif rc is not None: status_container.error("Failed")

if st.session_state.running: time.sleep(0.5); st.rerun()
elif was_running: st.rerun()