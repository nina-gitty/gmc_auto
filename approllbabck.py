import sys
import time
import json
import queue
import threading
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import streamlit as st
import pandas as pd
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import re

st.set_page_config(page_title="GMC Region Mismatch Audit Tool", layout="wide")

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
    .rotating-icon {
        display: inline-block;
        animation: rotate-60-deg 3s infinite steps(6);
        font-size: 1.2rem;
        margin-right: 8px;
    }
    a { text-decoration: none; color: #0068c9; transition: color 0.2s; }
    a:hover { color: #004280; text-decoration: underline; }
    
    .meta-container { margin-top: 6px; margin-bottom: 16px; }
    .meta-row { display: flex; margin-bottom: 8px; align-items: baseline; }
    .meta-row:last-child { margin-bottom: 0; }
    .meta-label { color: #555; font-weight: 600; font-size: 0.95rem; width: 140px; min-width: 140px; flex-shrink: 0; }
    .meta-value { color: #111; font-size: 1rem; font-weight: 400; word-break: break-all; line-height: 1.5; }
    .pid-text { font-family: 'Source Code Pro', 'Courier New', monospace; font-weight: 600; color: #222; }

    .comp-table { width: 100%; border-collapse: collapse; font-size: 0.9rem; margin-bottom: 10px; border: 1px solid #eee; }
    .comp-table th { text-align: left; color: #444; background-color: #f9fafb; border-bottom: 2px solid #eee; padding: 8px 12px; font-weight: 600; }
    .comp-table td { border-bottom: 1px solid #f0f0f0; padding: 10px 12px; vertical-align: top; color: #222; }
    
    div[data-testid="stTable"] table { border-radius: 8px; overflow: hidden; border: 1px solid #e0e0e0; }
    
    .path-box { background-color: #f8f9fa; border: 1px solid #eee; padding: 15px; border-radius: 8px; margin-top: 30px; }
    .path-row { display: flex; margin-bottom: 8px; font-family: 'Source Code Pro', monospace; font-size: 0.85rem; }
    .path-label { font-weight: 600; color: #555; width: 80px; flex-shrink: 0; }
    .path-val { color: #333; word-break: break-all; }
    
    .status-box {
        padding: 1rem;
        border-radius: 0.5rem;
        background-color: #e8f0fe;
        color: #1a73e8;
        border: 1px solid #d2e3fc;
        margin-bottom: 10px;
    }
    .status-header { display: flex; align-items: center; margin-bottom: 8px; }
    .status-text { font-size: 0.9rem; color: #444; word-break: break-word; }
    .status-bold { font-weight: 700; color: #000; font-size: 1rem; }

    .analysis-container {
        margin-top: 40px;
        padding: 20px;
        border: 1px solid #e0e0e0;
        border-radius: 10px;
        background-color: #ffffff;
        box-shadow: 0 4px 6px rgba(0,0,0,0.05);
    }
    </style>
    """, unsafe_allow_html=True)

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "region_mismatch.py"
SUMMARY_SCRIPT = HERE / "generate_summary.py"


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
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return None

def clean_currency(val):
    if not val: return ""
    return re.sub(r'[^\d.,]', '', str(val)).strip()

def extract_info_from_blob(blob_text):
    info = {"price": "", "availability": ""}
    if not blob_text:
        return info
    
    lines = [l.strip() for l in blob_text.splitlines() if l.strip()]
    sale_price_found = ""
    regular_price_found = ""
    
    for i, line in enumerate(lines):
        line_lower = line.lower()
        if "sale price" in line_lower and i + 1 < len(lines):
            val = lines[i+1]
            if any(c.isdigit() for c in val):
                sale_price_found = val
        elif "price" == line_lower and i + 1 < len(lines):
            val = lines[i+1]
            if any(c.isdigit() for c in val):
                regular_price_found = val
        elif "availability" in line_lower and i + 1 < len(lines):
            info["availability"] = lines[i+1]

    raw_price = sale_price_found if sale_price_found else regular_price_found
    info["price"] = clean_currency(raw_price)
    return info


# --- Main Logic ---
def start_process(cmd: List[str]) -> None:
    q: "queue.Queue[str]" = queue.Queue()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", cwd=str(HERE), bufsize=1, universal_newlines=True)
    
    def reader():
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                q.put(line.rstrip("\n"))
        except Exception: pass
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
    st.session_state.stdout_all = ""
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


# --- Post Audit ---
def run_post_audit(schema_dir, mode, default_gmc, regional_text=""):
    if not SUMMARY_SCRIPT.exists():
        st.error("generate_summary.py not found!")
        return

    cmd = [
        sys.executable, str(SUMMARY_SCRIPT),
        "--schema_dir", schema_dir,
        "--mode", mode,
        "--default_gmc", default_gmc,
        "--regional_map_text", regional_text
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0:
            data = json.loads(result.stdout)
            st.session_state.analysis_df = pd.DataFrame(data)
        else:
            st.error(f"Analysis failed: {result.stderr}")
    except Exception as e:
        st.error(f"Error running analysis: {e}")


# --- Rendering ---
def render_result() -> None:
    report_path = st.session_state.get("report_path")
    images_dir = st.session_state.get("images_dir")
    schema_dir = st.session_state.get("schema_dir")
    groups = st.session_state.get("realtime_results", [])
    target_pid = st.session_state.get("target_product_id", "N/A")
    target_url = st.session_state.get("target_url", "")

    if not groups and not st.session_state.running:
        return

    st.markdown("### Result")
    
    if not groups and st.session_state.running:
        st.info("Waiting for first result...")
        return

    run_ts = st.session_state.get("started_at")
    ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(run_ts)) if run_ts else "-"
    link_html = f'<a href="{target_url}" target="_blank">{target_url}</a>' if target_url else "N/A"
    
    st.markdown(f"""
    <div class="meta-container">
        <div class="meta-row"><div class="meta-label"> Product ID</div><div class="meta-value"><span class="pid-text">{target_pid}</span></div></div>
        <div class="meta-row"><div class="meta-label"> Product Link</div><div class="meta-value">{link_html}</div></div>
        <div class="meta-row"><div class="meta-label"> Run Time</div><div class="meta-value">{ts_str}</div></div>
    </div>
    """, unsafe_allow_html=True)
    
    st.divider()

    for g in groups:
        rid = g.get("region_id") or ""
        final_link = set_query_param(target_url, "region_id", rid) if rid else target_url
        region_display = f"region_{rid}" if rid else "Default (No Param)"
        st.markdown(f"#### {region_display}")
        
        if final_link:
            st.markdown(f"<a href='{final_link}' target='_blank' style='font-size:0.9rem;'>{final_link}</a>", unsafe_allow_html=True)

        c_img, c_schema = st.columns([65, 35], gap="large")

        with c_img:
            schema_path = g.get("schema_path_abs", "")
            img_path = None
            if schema_path:
                img_path = Path(schema_path).parent.parent / "images" / Path(g.get("website_png_rel")).name
            if img_path and img_path.exists():
                st.image(str(img_path), width='stretch')
            else:
                st.info("Generating screenshot...")

        with c_schema:
            schema_path = g.get("schema_path_abs", "")
            sd = safe_read_json(Path(schema_path)) if schema_path else {}
            offers = sd.get("offers", {}) if sd else {}
            if isinstance(offers, list): offers = offers[0] if offers else {}
                
            raw_s_price = offers.get('price', '-')
            s_price = clean_currency(raw_s_price)
            s_avail = offers.get("availability", "-").replace("https://schema.org/", "")
            
            table_html = f"""
            <table class="comp-table">
                <thead><tr><th>Field</th><th>Schema Data</th></tr></thead>
                <tbody><tr><td>Price</td><td>{s_price}</td></tr><tr><td>Availability</td><td>{s_avail}</td></tr></tbody>
            </table>
            """
            st.markdown(table_html, unsafe_allow_html=True)
            if sd:
                with st.expander("Show raw Schema JSON"):
                    st.json(sd)
        st.divider()

    if not st.session_state.running and report_path:
        st.markdown(f"""
        <div class="path-box">
            <h5 style="margin: 0 0 12px 0;">📂 Output Directories</h5>
            <div class="path-row"><div class="path-label">Report</div><div class="path-val">{report_path if report_path else "-"}</div></div>
            <div class="path-row"><div class="path-label">Images</div><div class="path-val">{images_dir if images_dir else "-"}</div></div>
            <div class="path-row"><div class="path-label">Schema</div><div class="path-val">{schema_dir if schema_dir else "-"}</div></div>
        </div>
        """, unsafe_allow_html=True)

    # Post-Audit
    if not st.session_state.running and schema_dir:
        st.markdown("<div style='height: 40px;'></div>", unsafe_allow_html=True)
        with st.container():
            st.markdown("""
            <div class="analysis-container">
                <h3 style="margin-top:0;">📊 Post-Audit Analysis Tool</h3>
                <p style="color:#666; font-size:0.95rem;">Compare <strong>GMC Target</strong> vs Collected Data.</p>
            """, unsafe_allow_html=True)
            
            c_opt, c_act = st.columns([3, 1])
            with c_opt:
                audit_mode = st.radio("Comparison Mode", ["Price", "Availability"], horizontal=True)
                
                saved_blob = st.session_state.get("saved_blob", "")
                blob_info = extract_info_from_blob(saved_blob)
                
                auto_val = ""
                if audit_mode == "Price":
                    auto_val = blob_info["price"]
                else:
                    auto_val = blob_info["availability"]

                default_gmc = st.text_input("Default GMC Value", value=auto_val)
                
                regional_text = ""
                if audit_mode == "Availability":
                    regional_text = st.text_area("Regional Overrides", height=120, placeholder="nss\nOut of stock")

            with c_act:
                st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                if st.button("Generate Comparison Table", type="primary", width='stretch'):
                    run_post_audit(schema_dir, audit_mode, default_gmc, regional_text)
            
            st.markdown("</div>", unsafe_allow_html=True)

            if st.session_state.analysis_df is not None:
                st.divider()
                st.dataframe(st.session_state.analysis_df, width='stretch', hide_index=True)


# --- Layout ---
# [NEW] Header Layout (Title + Help Button)
col_t1, col_t2 = st.columns([0.85, 0.15])

with col_t1:
    st.title("GMC Region Mismatch Audit Tool")

with col_t2:
    # 제목 폰트 높이에 맞추기 위해 약간의 margin 추가
    st.markdown('<div style="margin-top: 25px;"></div>', unsafe_allow_html=True)
    with st.popover("📖 How to Use?"):
        st.markdown("""
        **1. 데이터 입력 (Input Data)**
        * **Merchant Center**에서 복사한 제품 데이터 전체(Blob)를 붙여넣으세요.
        * 또는 **LG.com URL** 만 넣어도 작동합니다.
        
        **2. 실행 옵션 (Run Options)**
        * `Show Browser`: 체크하면 브라우저가 팝업되며, 크롤링 과정을 눈으로 볼 수 있습니다. (속도는 느려짐)
        
        **3. 실행 (Run Audit)**
        * `Run Audit` 버튼을 누르면 자동으로 국가를 감지하고 지역별 크롤링을 시작합니다.
        
        **4. 결과 분석 (Post-Audit)**
        * 크롤링이 끝나면 하단에 **비교 테이블 생성 도구**가 나타납니다.
        * **Price / Availability** 중 선택하여 데이터를 비교할 수 있습니다. 
        * 지역이 여러 개인 경우 **regional inventory** 데이터를 GMC에서 복사해 붙여넣어주세요.
        * **Generate Table** 버튼을 누르면 최종 비교표가 생성됩니다.
        """)

left_col, right_col = st.columns([0.25, 0.75], gap="large")

with left_col: 
    st.subheader("Input")
    blob = st.text_area(
        "Input Data (Blob or URL)", 
        height=200, 
        placeholder="Product page on your website\nhttps://...\n...\nProduct ID\n...", 
        disabled=st.session_state.running
    )
    
    st.divider()
    headed = st.checkbox("Show Browser", False, disabled=st.session_state.running)
    
    btn_row = st.columns([1, 1], gap="small")
    with btn_row[0]: run_btn = st.button("Run Audit", type="primary", width='stretch', disabled=st.session_state.running)
    with btn_row[1]: stop_btn = st.button("Stop", width='stretch', disabled=not st.session_state.running)

    status_container = st.empty()

with right_col: 
    result_area = st.empty()


# actions
if run_btn:
    result_area.empty()
    st.session_state.analysis_df = None
    st.session_state.saved_blob = blob

    if not SCRIPT.exists():
        st.error(f"region_mismatch.py not found: {SCRIPT}")
        st.stop()

    cmd = [sys.executable, str(SCRIPT), "--no_open"]
    if headed: cmd.append("--headed")

    final_pid = ""
    final_url = ""

    if blob.strip():
        lines = [l.strip() for l in blob.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if line.lower() == "product id" and i+1 < len(lines): 
                final_pid = lines[i+1]
            if line.startswith("http://") or line.startswith("https://"):
                if not final_url: final_url = line

        if "product page on your website" in blob.lower() or "product id" in blob.lower():
            cmd += ["--blob", blob]

    if not final_url:
        st.error("Could not find a valid URL in the input.")
        st.stop()

    cmd += ["--url", final_url]
    if final_pid:
        cmd += ["--product_id", final_pid]

    st.session_state.target_product_id = final_pid
    st.session_state.target_url = final_url
    start_process(cmd)

if stop_btn:
    stop_process()

# Loop
was_running = st.session_state.running
drain_logs()
finalize_if_done()

# Update Status
with left_col:
    if st.session_state.running:
        status_text = st.session_state.get("status_text", "Starting...")
        p_val = st.session_state.get("progress_val", 0.0)
        p_label = st.session_state.get("progress_label", "Running...")
        with status_container.container():
            st.markdown(f"""
            <div class="status-box">
                <div class="status-header"><span class="rotating-icon">⏳</span><span class="status-bold">{p_label}</span></div>
                <div class="status-text">{status_text}</div>
            </div>""", unsafe_allow_html=True)
            st.progress(p_val)
    else:
        rc = st.session_state.get("returncode")
        if rc is None: status_container.caption("Idle")
        elif rc == 0: status_container.success("Done", icon="✅")
        else: status_container.error(f"Failed (code={rc})", icon="❌")
    
    if st.session_state.get("started_at"):
        elapsed = time.time() - float(st.session_state.started_at)
        st.caption(f"Time: {elapsed:.1f}s")

# Update Result
with right_col:
    if not st.session_state.running and st.session_state.get("returncode") is not None and st.session_state.returncode != 0:
        st.error("Process Failed")
        st.code(st.session_state.get("stdout_all") or "", language="text")
    with result_area.container():
        render_result()

if st.session_state.running:
    time.sleep(0.5)
    st.rerun()
elif was_running:
    st.rerun()