import sys
import time
import json
import queue
import threading
import subprocess
import shutil
import base64
import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import streamlit as st
import pandas as pd
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import re
import scheduler

@st.cache_resource
def run_once():
    scheduler.cleanup_old_folders()

run_once()

st.set_page_config(page_title="GMC Region Mismatch Audit Tool", layout="wide")

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "region_mismatch.py"
TRANS_FILE = HERE / "translations.json"

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
    st.session_state.schema_dir = None
    st.session_state.returncode = None
    st.session_state.started_at = None
    st.session_state.final_duration = None

# =========================================================
# 2. CSS Styling
# =========================================================
st.markdown("""
    <style>
    .rotating-icon { display: inline-block; animation: rotate-60-deg 3s infinite steps(6); font-size: 1.2rem; margin-right: 8px; }
    .comp-table { width: 100%; border-collapse: collapse; font-size: 0.9rem; margin-bottom: 10px; border: 1px solid #eee; }
    .comp-table th { text-align: left; color: #444; background-color: #f9fafb; border-bottom: 2px solid #eee; padding: 8px 12px; font-weight: 600; }
    .comp-table td { border-bottom: 1px solid #f0f0f0; padding: 10px 12px; vertical-align: top; color: #222; }
    .status-box { padding: 1rem; border-radius: 0.5rem; border: 1px solid #d2e3fc; margin-bottom: 10px; }
    .status-running { background-color: #e8f0fe; color: #1a73e8; border-color: #d2e3fc; }
    .status-done { background-color: #e6fffa; color: #047481; border-color: #b2f5ea; }
    .status-header { display: flex; align-items: center; margin-bottom: 8px; font-weight: 600; font-size: 1rem; }
    .status-text { font-size: 0.9rem; color: #444; }
    .time-text { font-size: 0.85rem; color: #666; margin-top: 5px; font-family: monospace; }
    .analysis-container { margin-top: 20px; padding: 15px; border: 1px solid #e0e0e0; border-radius: 10px; background-color: #f8f9fa; }
    .audit-info-box { background-color: #f1f3f4; padding: 15px; border-radius: 8px; margin-bottom: 25px; border-left: 5px solid #1a73e8; }
    .audit-info-box p { margin: 5px 0; font-size: 0.95rem; }
    </style>
    """, unsafe_allow_html=True)

# --- Helpers ---
def parse_report_paths(stdout_text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    report_path = images_dir = schema_dir = None
    for line in (stdout_text or "").splitlines():
        s = line.strip()
        if s.startswith("- Report:"): report_path = s.split(":", 1)[1].strip()
        elif s.startswith("- Images:"): images_dir = s.split(":", 1)[1].strip()
        elif s.startswith("- Schema:"): schema_dir = s.split(":", 1)[1].strip()
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
    sale_p = reg_p = ""
    for i, line in enumerate(lines):
        ln = line.lower()
        if "sale price" in ln and i + 1 < len(lines):
            if any(c.isdigit() for c in lines[i+1]): sale_p = lines[i+1]
        elif ln == "price" and i + 1 < len(lines):
            if any(c.isdigit() for c in lines[i+1]): reg_p = lines[i+1]
        elif "availability" in ln and i + 1 < len(lines):
            info["availability"] = lines[i+1]
    info["price"] = clean_currency(sale_p if sale_p else reg_p)
    return info

def generate_standalone_html(df, groups, target_url, target_pid):
    css = """<style>body { font-family: sans-serif; padding: 20px; } table { border-collapse: collapse; width: 100%; } th, td { border: 1px solid #ddd; padding: 8px; } th { background-color: #f2f2f2; }</style>"""
    html = [f"<html><head>{css}</head><body><h1>Audit Report: {target_pid}</h1>"]
    if df is not None: html.append(df.to_html())
    html.append("</body></html>")
    return "\n".join(html)

def translate_status_with_format(text, market):
    if not text: return ""
    trans_map = {}
    if TRANS_FILE.exists():
        try: trans_map = json.loads(TRANS_FILE.read_text(encoding="utf-8"))
        except: pass
    text_clean = " ".join(text.split()).lower()
    found = None
    for k, v in trans_map.get("market_map", {}).get(market, {}).items():
        if k in text_clean: found = v; break
    if not found:
        for k, v in trans_map.get("global_map", {}).items():
            if k in text_clean: found = v; break
    return f"{found} ({text})" if found else text

def normalize_gmc_status(val):
    v = val.lower().strip()
    if "in" in v and "stock" in v and "out" not in v: return "InStock"
    if "out" in v: return "OutOfStock"
    if "pre" in v: return "PreOrder"
    return val

def run_post_audit_internal(schema_dir_str, mode, default_gmc, regional_text):
    schema_dir = Path(schema_dir_str)
    if not schema_dir.exists(): return
    regional_map = {}
    if regional_text:
        lines = [l.strip() for l in regional_text.replace("\t", "\n").splitlines() if l.strip()]
        cur_key = None
        for line in lines:
            if any(x in line for x in ["KST", "GMT", "AM", "PM"]) or ":" in line: continue
            if any(k in line.lower() for k in ["in stock", "out of stock", "instock", "outofstock", "limited", "preorder"]):
                if cur_key: regional_map[cur_key] = normalize_gmc_status(line); cur_key = None
            else: cur_key = line
    rows = []
    # [수정] 밀리초 폴더 대응을 위한 와일드카드 유지
    for f in sorted(schema_dir.glob("*__schema_*.json")):
        try:
            rid = f.name.split("__")[0].replace("region_", ""); rid = "" if rid == "default" else rid
            sd = safe_read_json(f); off = sd.get("offers", {})
            if isinstance(off, list): off = off[0] if off else {}
            s_p, s_a = clean_currency(off.get("price", "")), off.get("availability", "").replace("https://schema.org/", "")
            scrape_f = f.parent / f.name.replace("__schema_", "__scrape_")
            vis = safe_read_json(scrape_f) if scrape_f.exists() else {}
            gmc_v = regional_map.get(rid, normalize_gmc_status(default_gmc)) if rid else normalize_gmc_status(default_gmc)
            row = {"Region": rid if rid else "Default", "GMC": gmc_v, "Schema": s_a if mode == "Availability" else s_p}
            if mode == "Availability":
                fmt = translate_status_with_format(vis.get("buy_button_text", ""), urlparse(st.session_state.target_url).path.split("/")[1])
                row["Visual_Standard"], row["Visual_Full"] = (fmt.split('(')[0].strip() if '(' in fmt else fmt), fmt
            else: row["Visual_Price"] = vis.get("visual_price", "")
            rows.append(row)
        except: continue
    st.session_state.analysis_df = pd.DataFrame(rows)

def start_process(cmd):
    q = queue.Queue()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", cwd=str(HERE), bufsize=1)
    def reader():
        try:
            for line in proc.stdout: q.put(line.rstrip("\n"))
        finally: proc.stdout.close()
    threading.Thread(target=reader, daemon=True).start()
    st.session_state.update({"running": True, "proc": proc, "log_q": q, "lines": [], "started_at": time.time(), "realtime_results": [], "analysis_df": None})

def drain_logs():
    q = st.session_state.get("log_q")
    if not q: return
    while True:
        try:
            line = q.get_nowait()
            if "[PROGRESS]" in line:
                m = re.search(r"<(\d+)/(\d+)>", line)
                if m: st.session_state.progress_val, st.session_state.progress_label = int(m.group(1))/int(m.group(2)), f"Region {m.group(1)} of {m.group(2)}"
                st.session_state.status_text = line.split("]", 1)[-1].strip()
            elif "[RESULT_JSON]" in line: st.session_state.realtime_results.append(json.loads(line.replace("[RESULT_JSON]", "").strip()))
            st.session_state.lines.append(line)
        except queue.Empty: break

def finalize_if_done():
    proc = st.session_state.get("proc")
    if proc and proc.poll() is not None and st.session_state.running:
        if st.session_state.started_at: st.session_state.final_duration = time.time() - st.session_state.started_at
        st.session_state.update({"returncode": proc.poll(), "running": False})
        st.session_state.report_path, st.session_state.images_dir, st.session_state.schema_dir = parse_report_paths("\n".join(st.session_state.lines))
        st.rerun()

# --- [Main Layout] ---
st.title("GMC Region Mismatch Audit Tool")

left_col, right_col = st.columns([0.35, 0.65], gap="large")

with left_col:
    st.subheader("1. Start Audit")
    blob = st.text_area("Blob/URL", height=200, disabled=st.session_state.running)
    b1, b2 = st.columns(2)
    run_btn = b1.button("Run Audit", type="primary", use_container_width=True, disabled=st.session_state.running)
    if b2.button("Stop", use_container_width=True, disabled=not st.session_state.running):
        if st.session_state.proc: st.session_state.proc.terminate()
    
    if st.session_state.running:
        elapsed = time.time() - st.session_state.started_at if st.session_state.started_at else 0
        st.markdown(f'<div class="status-box status-running"><div class="status-header">⏳ {st.session_state.progress_label}</div><div class="status-text">{st.session_state.status_text}</div><div class="time-text">Time: {elapsed:.1f}s</div></div>', unsafe_allow_html=True)
        st.progress(st.session_state.progress_val)
    elif st.session_state.returncode == 0:
        st.markdown(f'<div class="status-box status-done"><div class="status-header">✅ Done</div><div class="status-text">Audit completed successfully.</div><div class="time-text">Total Time: {st.session_state.final_duration:.1f}s</div></div>', unsafe_allow_html=True)

    if not st.session_state.running and st.session_state.schema_dir:
        st.markdown("---")
        st.subheader("3. Comparison Table")
        with st.container():
            st.markdown('<div class="analysis-container">', unsafe_allow_html=True)
            audit_mode = st.radio("Mode", ["Price", "Availability"], horizontal=True, label_visibility="collapsed")
            b_info = extract_info_from_blob(st.session_state.saved_blob)
            def_gmc = st.text_input("Default GMC Value", value=b_info["price"] if audit_mode=="Price" else b_info["availability"])
            reg_txt = st.text_area("Regional Inventory (Paste from GMC)", height=150) if audit_mode=="Availability" else ""
            show_orig = st.checkbox("Show LG.com Original", value=False)
            if st.button("Generate Table", type="primary", use_container_width=True):
                run_post_audit_internal(st.session_state.schema_dir, audit_mode, def_gmc, reg_txt)
            st.markdown("</div>", unsafe_allow_html=True)
        if st.session_state.analysis_df is not None:
            df_disp = st.session_state.analysis_df.copy()
            if audit_mode == "Availability":
                df_disp['LG.com'] = df_disp['Visual_Full'] if show_orig else df_disp['Visual_Standard']
            else:
                df_disp['LG.com'] = df_disp['Visual_Price']
            def highlight(row):
                g, l = str(row['GMC']).lower(), str(row['LG.com']).split('(')[0].strip().lower()
                return ['background-color: #ffe6e6; color: #b30000'] * len(row) if g and l and g!=l else ['']*len(row)
            st.dataframe(df_disp[["Region", "GMC", "LG.com", "Schema"]].style.apply(highlight, axis=1), use_container_width=True, hide_index=True)
            st.download_button("📄 Download Result Report", generate_standalone_html(df_disp, st.session_state.realtime_results, st.session_state.target_url, st.session_state.target_product_id), "report.html", "text/html", use_container_width=True)

with right_col:
    st.subheader("2. Audit Result")
    if st.session_state.target_product_id:
        st.markdown(f'<div class="audit-info-box"><p><b>PRODUCT ID:</b> {st.session_state.target_product_id}</p><p><b>PRODUCT LINK:</b> <a href="{st.session_state.target_url}" target="_blank">{st.session_state.target_url}</a></p><p><b>RUNNED AT:</b> {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p></div>', unsafe_allow_html=True)
    
    for g in st.session_state.realtime_results:
        rid = g.get("region_id") or "Default"
        lnk = g.get("final_url", "#")
        st.markdown(f"#### region_{rid}" if rid != "Default" else "#### Default")
        if lnk != "#": st.markdown(f"🔗 [Open Product Page]({lnk})")
        c1, c2 = st.columns([0.6, 0.4])
        img_p = Path(g.get("schema_path_abs")).parent.parent / "images" / Path(g.get("website_png_rel")).name
        if img_p.exists(): c1.image(str(img_p), use_container_width=True)
        sd = safe_read_json(Path(g.get("schema_path_abs")))
        off = sd.get("offers", [{}])[0] if sd and isinstance(sd.get("offers"), list) else (sd.get("offers", {}) if sd else {})
        c2.markdown(f'<table class="comp-table"><tr><th>Field</th><th>Schema</th></tr><tr><td>Price</td><td>{off.get("price")}</td></tr><tr><td>Avail</td><td>{str(off.get("availability","")).split("/")[-1]}</td></tr></table>', unsafe_allow_html=True)
        with c2.expander("JSON"): st.json(sd)
        st.divider()

if run_btn:
    st.session_state.saved_blob = blob
    lines = [l.strip() for l in blob.splitlines() if l.strip()]
    url = next((l for l in lines if l.startswith("http")), ""); pid = ""
    for i, l in enumerate(lines):
        if "product id" in l.lower() and i+1 < len(lines): pid = lines[i+1]
    if not url: st.error("URL not found"); st.stop()
    st.session_state.target_product_id, st.session_state.target_url = pid, url
    start_process([sys.executable, str(SCRIPT), "--no_open", "--url", url, "--blob", blob])

drain_logs()
finalize_if_done()
if st.session_state.running:
    time.sleep(0.5)
    st.rerun()
