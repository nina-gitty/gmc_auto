import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Tuple, List
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import re
import sys
import time

from playwright.sync_api import sync_playwright

# 윈도우 출력 인코딩 강제 설정
sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

def set_query_param(url: str, key: str, value: str) -> str:
    if value is None or str(value).strip() == "":
        return url
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
                '.cookie-banner', '.bv_mbox', 'div[class*="dimmed"]', 'div[class*="backdrop"]'
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
    try:
        print(f"[PROGRESS] {log_prefix} Navigating...", flush=True)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        force_remove_overlays(page)
        simulate_user_interaction(page, log_prefix)

        print(f"[PROGRESS] {log_prefix} Waiting for content...", flush=True)
        try:
            page.wait_for_selector(".price-top, .price-box--price, .cell-price, .amount, .c-price__purchase", state="visible", timeout=5000)
        except:
            pass 

        page.wait_for_timeout(1000)
        force_remove_overlays(page)

        print(f"[PROGRESS] {log_prefix} Taking screenshot...", flush=True)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out_path), full_page=False)

        if not out_path.exists() or out_path.stat().st_size < 5000:
            return False, "Screenshot error"
        return True, "ok"
    except Exception as e:
        return False, str(e)

def extract_jsonld_product_offer(page, log_prefix: str, max_wait_ms: int = 15000) -> Optional[Dict]:
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

# [수정] % 할인율 텍스트 무시 로직 추가
def extract_visual_elements(page, log_prefix: str, current_url: str) -> Dict[str, str]:
    print(f"[PROGRESS] {log_prefix} Analyzing UI...", flush=True)
    
    result = {
        "visual_price": "",
        "buy_button_text": "",
        "title_text": "",
        "meta_url": current_url
    }
    
    force_remove_overlays(page)

    # 1. 가격 추출
    try:
        price_selectors = [
            ".info-sticky .price-top span",    # 1순위: Sticky Top
            ".price-top span",
            ".price-box--price .cell-price",
            ".cell-price.cheaperMA",           # BR 구형
            ".PD0033 .cell-price.cheaperMA",
            ".price-area .c-price__purchase",  # Global 신규
            ".amount",
            ".cell-price"
        ]

        found_price_text = ""

        for selector in price_selectors:
            # 해당 선택자에 맞는 모든 요소를 가져옴
            elements = page.locator(selector).all()
            
            for el in elements:
                if not el.is_visible():
                    continue
                
                raw_text = el.inner_text().strip()
                
                # [방어 로직]
                # 1. "%"가 있으면 할인율 배지일 확률이 높음 -> 건너뜀
                if "%" in raw_text:
                    continue
                
                # 2. 숫자가 하나도 없으면 가격 아님 -> 건너뜀
                if not any(char.isdigit() for char in raw_text):
                    continue

                # 여기까지 왔으면 가격일 가능성 높음
                found_price_text = raw_text
                break # 내부 for문 종료 (요소 찾음)
            
            if found_price_text:
                break # 외부 for문 종료 (선택자 찾음)

        if found_price_text:
            clean_price = re.sub(r'[^\d.,]', '', found_price_text)
            result["visual_price"] = clean_price
            
    except Exception as e:
        print(f"[DEBUG] Price error: {e}", flush=True)

    # 2. 버튼 텍스트 추출
    try:
        found_text = ""
        sticky_btn = page.locator(".info-sticky .info-sticky--btn a, .info-sticky .info-sticky--btn button").first
        if sticky_btn.is_visible():
            found_text = sticky_btn.inner_text().strip()
        
        if not found_text:
            btn_candidates = page.locator('a.btn-pdp:not(.hidden) span.button-text, button.btn-pdp:not(.hidden) span.button-text').all()
            for btn in btn_candidates:
                if btn.is_visible():
                    found_text = btn.inner_text().strip()
                    if found_text: break
        
        if not found_text:
            hl_btn = page.locator('.cta-wrap .highlight:visible').first
            if hl_btn.count() > 0:
                found_text = hl_btn.inner_text().strip()

        if not found_text:
            fallback_kws = [
                "out of stock", "sold out", "esgotado", "unavailable", "stock alert", "where to buy",
                "comprar", "buy now", "add to cart", "in stock", "pre-order", "vorbestellung"
            ]
            for kw in fallback_kws:
                if page.get_by_text(kw, exact=False).first.is_visible():
                    found_text = kw.title()
                    break
        
        result["buy_button_text"] = found_text

    except Exception as e:
        print(f"[DEBUG] Button error: {e}", flush=True)

    return result

def generate_html_report(out_path: Path, product_id: str, base_url: str, blocks: List[Dict]):
    print(f"[PROGRESS] Generating HTML Report...", flush=True)
    
    html = []
    html.append(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>GMC Region Audit: {product_id}</title>
        <style>
            body {{ font-family: sans-serif; margin: 20px; }}
            h1 {{ margin-bottom: 5px; }}
            .meta {{ color: #666; margin-bottom: 20px; }}
            .block {{ border: 1px solid #ccc; margin-bottom: 30px; padding: 15px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }}
            .block h2 {{ margin-top: 0; background: #eee; padding: 8px; border-radius: 4px; }}
            .grid {{ display: flex; gap: 20px; flex-wrap: wrap; }}
            .screenshot img {{ max-width: 500px; border: 1px solid #ddd; display: block; }}
            .data-table {{ border-collapse: collapse; width: 100%; max-width: 400px; }}
            .data-table th, .data-table td {{ border: 1px solid #eee; padding: 8px; text-align: left; }}
            .data-table th {{ background: #f9f9f9; }}
            .link {{ display: block; margin-bottom: 10px; word-break: break-all; }}
        </style>
    </head>
    <body>
        <h1>Audit Report: {product_id}</h1>
        <div class="meta">Target URL: <a href="{base_url}">{base_url}</a></div>
    """)

    for b in blocks:
        rid = b["region_id"]
        if not rid: rid = "Default"
        
        s_price, s_avail = "-", "-"
        v_price, v_btn = "-", "-"
        
        try:
            with open(b["schema_path_abs"], "r", encoding="utf-8") as f:
                sd = json.load(f)
                offers = sd.get("offers", {})
                if isinstance(offers, list): offers = offers[0] if offers else {}
                s_price = offers.get("price", "-")
                s_avail = offers.get("availability", "-").replace("https://schema.org/", "")
        except: pass

        try:
            scrape_path = Path(b["schema_path_abs"]).parent / Path(b["schema_json_rel"]).name.replace("__schema_", "__scrape_")
            with open(scrape_path, "r", encoding="utf-8") as f:
                vd = json.load(f)
                v_price = vd.get("visual_price", "-")
                v_btn = vd.get("buy_button_text", "-")
        except: pass

        html.append(f"""
        <div class="block">
            <h2>Region: {rid}</h2>
            <div class="link"><a href="{b['final_url']}" target="_blank">{b['final_url']}</a></div>
            <div class="grid">
                <div class="screenshot">
                    <strong>Screenshot</strong><br>
                    <img src="{b['website_png_rel']}" alt="Screenshot">
                </div>
                <div class="data">
                    <strong>Extracted Data</strong>
                    <table class="data-table">
                        <tr><th>Field</th><th>Visual (Scrape)</th><th>Schema (JSON-LD)</th></tr>
                        <tr><td>Price</td><td>{v_price}</td><td>{s_price}</td></tr>
                        <tr><td>Avail/Btn</td><td>{v_btn}</td><td>{s_avail}</td></tr>
                    </table>
                </div>
            </div>
        </div>
        """)

    html.append("</body></html>")
    
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
    final_pid = blob_pid if blob_pid else args.product_id
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
        browser = p.chromium.launch(
            headless=True,  # 인자값에 상관없이 항상 True로 설정
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        total = len(target_regions)
        for i, rid in enumerate(target_regions, 1):
            target_url = set_query_param(final_main_url, param_key, rid)
            region_tag = rid if rid else "default"
            log_prefix = f"<{i}/{total}> [{region_tag}]"

            img_name = f"region_{region_tag}__website_{run_ts}.png"
            schema_name = f"region_{region_tag}__schema_{run_ts}.json"
            scrape_name = f"region_{region_tag}__scrape_{run_ts}.json"

            img_path = img_dir / img_name
            
            ok, msg = screenshot_first_view(page, target_url, img_path, log_prefix)
            if not ok:
                print(f"[!] Screenshot failed: {msg}", flush=True)

            product_schema = extract_jsonld_product_offer(page, log_prefix)
            visual_data = extract_visual_elements(page, log_prefix, target_url)

            with open(schema_dir / schema_name, "w", encoding="utf-8") as f:
                json.dump(product_schema if product_schema else {}, f, indent=2)
            
            with open(schema_dir / scrape_name, "w", encoding="utf-8") as f:
                json.dump(visual_data, f, indent=2)

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
    generate_html_report(report_path, final_pid, final_main_url, region_blocks)

    print(f"- Report: {report_path}", flush=True)
    print(f"- Images: {img_dir}", flush=True)
    print(f"- Schema: {schema_dir}", flush=True)

if __name__ == "__main__":
    main()