import argparse
import json
import re
import sys
from pathlib import Path

# 윈도우/리눅스 출력 인코딩 강제 설정
sys.stdout.reconfigure(encoding='utf-8')

def clean_currency(val):
    if not val: return ""
    return re.sub(r'[^\d.,]', '', str(val)).strip()

def safe_read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except:
        return {}

# [NEW] URL에서 국가 코드 추출 (예: lg.com/id/... -> id)
def get_market_from_url(url):
    try:
        match = re.search(r'lg\.com/([a-z]{2}(?:_[a-z]{2})?)/', url)
        if match:
            return match.group(1)
    except: pass
    return "global"

# [NEW] 번역 로직 (버튼 텍스트 -> 표준 상태값)
def translate_status(text, market, trans_map):
    if not text: return ""
    text_lower = text.lower().strip()
    
    # 1. 해당 국가 맵 확인
    market_rules = trans_map.get("market_map", {}).get(market, {})
    for k, v in market_rules.items():
        if k in text_lower: return v
            
    # 2. 글로벌 맵 확인
    global_rules = trans_map.get("global_map", {})
    for k, v in global_rules.items():
        if k in text_lower: return v
            
    # 3. 매칭 안되면 원본 반환 (나중에 확인용)
    return text

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema_dir", required=True)
    parser.add_argument("--mode", required=True, choices=["Price", "Availability"])
    parser.add_argument("--default_gmc", default="")
    parser.add_argument("--regional_map_text", default="")
    args = parser.parse_args()

    schema_dir = Path(args.schema_dir)
    if not schema_dir.exists():
        print("[]")
        return

    # [1] Translation JSON 로드
    trans_map = {}
    try:
        trans_file = Path(__file__).parent / "translation.json"
        if trans_file.exists():
            trans_map = json.loads(trans_file.read_text(encoding="utf-8"))
    except: pass

    # [2] Regional Map 파싱 (타임스탬프 무시 강화)
    regional_map = {}
    if args.regional_map_text:
        raw_text = args.regional_map_text.replace("\t", "\n").replace("\r", "\n")
        lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
        
        current_key = None
        for line in lines:
            # 날짜/시간 포맷 무시 (KST, GMT, 2026 등)
            if any(x in line for x in ["KST", "GMT", "UTC", "AM", "PM"]) or re.search(r'\d{4}', line):
                continue

            l_lower = line.lower()
            # 상태값 키워드 확인
            is_status = any(k in l_lower for k in ["in stock", "out of stock", "instock", "outofstock", "limited", "preorder"])
            
            if is_status:
                if current_key:
                    # GMC 값 표준화 (In stock -> InStock)
                    std_val = "InStock" if "in" in l_lower and "stock" in l_lower else "OutOfStock"
                    regional_map[current_key] = std_val
                    current_key = None
            else:
                current_key = line

    # [3] 파일 순회 및 데이터 취합
    results = []
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

            # Schema Data
            schema_data = safe_read_json(json_file)
            offers = schema_data.get("offers", {})
            if isinstance(offers, list): offers = offers[0] if offers else {}
            s_price = clean_currency(offers.get("price", ""))
            s_avail = offers.get("availability", "").replace("https://schema.org/", "")

            # Visual Data
            scrape_fname = fname.replace("__schema_", "__scrape_")
            scrape_file = json_file.parent / scrape_fname
            visual_data = safe_read_json(scrape_file) if scrape_file.exists() else {}
            
            v_price = visual_data.get("visual_price", "")
            raw_btn_text = visual_data.get("buy_button_text", "")
            target_url = visual_data.get("meta_url", "")

            # [번역 수행]
            market_code = get_market_from_url(target_url)
            v_avail_translated = translate_status(raw_btn_text, market_code, trans_map)

            # GMC 값 결정
            gmc_val = args.default_gmc
            # GMC Default 값도 표준화
            if "in" in gmc_val.lower() and "stock" in gmc_val.lower(): gmc_val = "InStock"
            elif "out" in gmc_val.lower(): gmc_val = "OutOfStock"

            if lookup_key:
                for k, v in regional_map.items():
                    if k.lower() == lookup_key.lower():
                        gmc_val = v
                        break
            
            row = {
                "Region": display_rid,
                "GMC": gmc_val,
                "LG.com (Visual)": v_avail_translated if args.mode == "Availability" else v_price,
                "Schema": s_avail if args.mode == "Availability" else s_price,
            }
            results.append(row)
            
        except: continue

    print(json.dumps(results))

if __name__ == "__main__":
    main()