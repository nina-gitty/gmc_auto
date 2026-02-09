import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Tuple, List
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import re
import sys
import time
import random

from playwright.sync_api import sync_playwright

# 윈도우/리눅스 출력 인코딩 강제 설정
sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

def set_query_param(url: str, key: str, value: str) -> str:
    if value is None or str(value).strip() == "": return url
    u = urlparse(url)
    qs = parse_qs(u.query)
    qs[key] = [str(value)]
    new_query = urlencode(qs, doseq=True)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_query, u.fragment))

def parse_product_blob(blob: str) -> Tuple[Optional[str], Optional[str]]:
    if not blob: return None, None
    lines = [l.strip() for l in blob.splitlines() if l.strip()]
    url = None
    product_id = None
    for i, line in enumerate(lines):
        if line.lower() == "product page on your website":
            if i + 1 < len(lines):
                cand = lines[i + 1].strip()
                if cand.startswith("http"): url = cand
        if line.lower() == "product id":
            if i + 1 < len(lines): product_id = lines[i + 1].strip()
    if not url:
        for line in lines:
            if line.startswith("http"):
                url = line
                break
    return product_id, url

def resolve_regions_param(url: str, script_dir: Path) -> Tuple[List[str], str]:
    cfg_path = script_dir / "regions_config.json"
    if not cfg_path.exists(): return [], "region_id"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except: return [], "region_id"
    u = urlparse(url)
    seg = [s for s in u.path.split("/") if s]
    market = seg[0].lower() if seg else ""
    if market == "ca" and len(seg) >= 2: market = f"ca_{seg[1].lower()}"
    entry = cfg.get(market) or {}
    return entry.get("regions", []), entry.get("param", "region_id")

def force_remove_overlays(page) -> None:
    try:
        page.evaluate("""
        () => {
            const selectors = [
                '#onetrust-banner-sdk', '.c-pop-msg__dimmed', '.c-pop-msg',
                '#popEhfPopup', '#popNotifyMeSuccess', '#popStockAlert',
                '.cookie-banner', '.bv_mbox', 'div[class*="dimmed"]', 'div[class*="backdrop"]',
                '.osano-cm-window', '#credential_picker_container', 'iframe[title*="recaptcha"]'
            ];
            selectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => el.remove());
            });
            document.documentElement.style.overflow = 'auto';
            document.body.style.overflow = 'auto';
        }
        """)
    except: pass

def simulate_user_interaction(page, log_prefix):
    print(f"[PROGRESS] {log_prefix} Triggering lazy load...", flush=True)
    try:
        page.mouse.wheel(0, 500)
        time.sleep(0.5)
        page.mouse.wheel(0, -500)
        time.sleep(0.5)
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except: pass
    except: pass

def screenshot_first_view(page, url: str, out_path: Path, log_prefix: str) -> Tuple[bool, str]:
    # [수정] 최대 3번 재시도 (Retry) 로직 추가
    max_retries = 3
    success = False
    
    for attempt in range(1, max_retries + 1):
        try:
            print(f"[PROGRESS] {log_prefix} Navigating (Attempt {attempt}/{max_retries})...", flush=True)
            
            # [수정] 타임아웃 90초로 증가 (네트워크 느림 대비)
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            
            # 페이지가 떴으면 성공으로 간주하고 루프 탈출
            success = True
            break 
        except Exception as e:
            print(f"[PROGRESS] {log_prefix} ⚠️ Timeout/Error on attempt {attempt}: {e}", flush=True)
            if attempt < max_retries:
                print(f"[PROGRESS] {log_prefix} 🔄 Retrying in 5 seconds...", flush=True)
                time.sleep(5)
            else:
                print(f"[PROGRESS] {log_prefix} ❌ Failed after {max_retries} attempts.", flush=True)

    # 실패했더라도 스크린샷은 시도해봄 (에러 화면이라도 찍히게)
    
    time.sleep(random.uniform(2.0, 4.0)) # 봇 회피 대기
    
    # Access Denied 체크 (로그만)
    try:
        content = page.content()
        if "Access Denied" in content:
             print(f"[PROGRESS] {log_prefix} ⚠️ Warning: Access Denied Page Detected!", flush=True)
    except: pass

    force_remove_overlays(page)
    simulate_user_interaction(page, log_prefix)

    print(f"[PROGRESS] {log_prefix} Waiting for content...", flush=True)
    try:
        page.wait_for_selector(".price-top, .price-box--price, .cell-price, .amount, .c-price__purchase", state="visible", timeout=5000)
    except: pass 

    page.wait_for_timeout(1000)
    force_remove_overlays(page)

    print(f"[PROGRESS] {log_prefix} Taking screenshot...", flush=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(out_path), full_page=False)
    except Exception as e:
        return False, f"Screenshot failed: {e}"

    if not out_path.exists() or out_path.stat().st_size < 1000:
        return False, "Screenshot empty"
    
    return True, "ok"

def extract_jsonld_product_offer(page, log_prefix: str) -> Optional[Dict]:
    print(f"[PROGRESS] {log_prefix} Extracting JSON-LD...", flush=True)
    try:
        scripts = page.locator('script[type="application/ld+json"]').all_inner_texts()
        for raw in scripts:
            try:
                data = json.loads(raw)
                schema_type = data.get("@type")
                is_product = False
                if isinstance(schema_type, str) and schema_type.lower() == "product":
                    is_product = True
                elif isinstance(schema_type, list) and any(t.lower() == "product" for t in schema_type if isinstance(t, str)):
                    is_product = True
                
                if is_product and "offers" in data:
                    return data
            except: continue
    except: pass
    return None

def extract_visual_elements(page, log_prefix: str, current_url: str) -> Dict[str, str]:
    print(f"[PROGRESS] {log_prefix} Analyzing UI...", flush=True)
    result = {"visual_price": "", "buy_button_text": "", "meta_url": current_url}
    force_remove_overlays(page)

    try:
        price_selectors = [
            ".info-sticky .price-top span", ".price-top span", ".price-box--price .cell-price",
            ".cell-price.cheaperMA", ".PD0033 .cell-price.cheaperMA", ".price-area .c-price__purchase",
            ".amount", ".cell-price"
        ]
        found_price_text = ""
        for selector in price_selectors:
            elements = page.locator(selector).all()
            for el in elements:
                if not el.is_visible(): continue
                raw_text = el.inner_text().strip()
                if "%" in raw_text: continue
                if not any(char.isdigit() for char in raw_text): continue
                found_price_text = raw_text
                break
            if found_price_text: break
        if found_price_text:
            result["visual_price"] = re.sub(r'[^\d.,]', '', found_price_text)
    except Exception: pass

    try:
        found_text = ""
        sticky_btn = page.locator(".info-sticky .info-sticky--btn a, .info-sticky .info-sticky--btn button").first
        if sticky_btn.is_visible(): found_text = sticky_btn.inner_text().strip()
        
        if not found_text:
            btn_candidates = page.locator('a.btn-pdp:not(.hidden) span.button-text, button.btn-pdp:not(.hidden) span.button-text').all()
            for btn in btn_candidates:
                if btn.is_visible():
                    found_text = btn.inner_text().strip()
                    if found_text: break
        if not found_text:
            hl_btn = page.locator('.cta-wrap .highlight:visible').first
            if hl_btn.count() > 0: found_text = hl_btn.inner_text().strip()
        if not found_text:
            fallback_kws = ["out of stock", "sold out", "esgotado", "unavailable", "stock alert", "where to buy", "comprar", "buy now", "add to cart", "in stock", "pre-order", "vorbestellung", "beli sekarang"]
            for kw in fallback_kws:
                if page.get_by_text(kw, exact=False).first.is_visible():
                    found_text = kw.title()
                    break
        result["buy_button_text"] = found_text
    except Exception: pass
    return result

def generate_html_report(out_path: Path, product_id: str, base_url: str, blocks: List[Dict]):
    print(f"[PROGRESS] Generating HTML Report...", flush=True)
    html = [f"<html><body><h1>Audit Report: {product_id}</h1></body></html>"]
    with open(out_path, "w", encoding='utf-8') as f:
        f.write("\n".join(html))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product_id", default="")
    ap.add_argument("--url", required=True)
    ap.add_argument("--blob", default="")
    ap.add_argument("--regions", default="")
    ap.add_argument("--param", default="")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--no_open", action="store_true")
    args = ap.parse_args()

    blob_pid, blob_url = parse_product_blob(args.blob)
    final_main_url = args.url if args.url else blob_url
    script_dir = Path(__file__).resolve().parent
    auto_regions, auto_param = resolve_regions_param(final_main_url, script_dir)
    target_regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    if not target_regions: target_regions = auto_regions
    if "" not in target_regions: target_regions.insert(0, "") 
    param_key = args.param if args.param else auto_param

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = script_dir / "outs" / f"out_{run_ts}"
    img_dir = out_dir / "images"
    schema_dir = out_dir / "schema"
    img_dir.mkdir(parents=True, exist_ok=True)
    schema_dir.mkdir(parents=True, exist_ok=True)

    region_blocks = []

    with sync_playwright() as p:
        # [Headless=False + XVFB 조합]
        browser = p.chromium.launch(
            headless=False,
            args=[
                '--no-sandbox', 
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas',
                '--no-first-run',
                '--no-zygote',
                '--single-process',
                '--disable-gpu',
                '--window-size=1920,1080',
                '--disable-blink-features=AutomationControlled'
            ]
        )
        
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale='en-US',
            extra_http_headers={'Accept-Language': 'en-US,en;q=0.9'}
        )
        
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
        """)
        
        page = context.new_page()

        total = len(target_regions)
        for i, rid in enumerate(target_regions, 1):
            target_url = set_query_param(final_main_url, param_key, rid)
            region_tag = rid if rid else "default"
            log_prefix = f"<{i}/{total}> [{region_tag}]"

            img_name = f"region_{region_tag}__website_{run_ts}.png"
            schema_name = f"region_{region_tag}__schema_{run_ts}.json"
            scrape_name = f"region_{region_tag}__scrape_{run_ts}.json"
            
            # 스크린샷 함수 내에서 재시도 로직 수행
            screenshot_first_view(page, target_url, img_dir / img_name, log_prefix)

            p_schema = extract_jsonld_product_offer(page, log_prefix)
            v_data = extract_visual_elements(page, log_prefix, target_url)

            with open(schema_dir / schema_name, "w", encoding="utf-8") as f:
                json.dump(p_schema if p_schema else {}, f, indent=2)
            with open(schema_dir / scrape_name, "w", encoding="utf-8") as f:
                json.dump(v_data, f, indent=2)

            block_data = {
                "region_id": rid,
                "final_url": target_url,
                "website_png_rel": f"images/{img_name}",
                "schema_path_abs": str(schema_dir / schema_name),
                "schema_json_rel": f"schema/{schema_name}"
            }
            region_blocks.append(block_data)
            print(f"[RESULT_JSON] {json.dumps(block_data)}", flush=True)

        browser.close()

    report_path = out_dir / f"report_{run_ts}.html"
    generate_html_report(report_path, blob_pid, final_main_url, region_blocks)
    
    print(f"- Report: {report_path}", flush=True)
    print(f"- Images: {img_dir}", flush=True)
    print(f"- Schema: {schema_dir}", flush=True)

if __name__ == "__main__":
    main()