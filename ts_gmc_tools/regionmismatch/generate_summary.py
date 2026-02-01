import argparse
import json
from pathlib import Path
from urllib.parse import urlparse
import re
import sys

# [수정] 외부 JSON 로드 함수
def load_translation_map():
    # 스크립트와 같은 폴더에 있는 translations.json 찾기
    script_dir = Path(__file__).resolve().parent
    json_path = script_dir / "translations.json"
    
    if json_path.exists():
        try:
            return json.loads(json_path.read_text(encoding="utf-8"))
        except:
            return {"market_map": {}, "global_map": {}}
    return {"market_map": {}, "global_map": {}}

def clean_currency(val):
    if not val: return ""
    return re.sub(r'[^\d.,]', '', str(val)).strip()

def detect_market_from_url(url):
    if not url: return "unknown"
    try:
        u = urlparse(url)
        path_parts = [p for p in u.path.split("/") if p]
        if not path_parts: return "unknown"
        
        market = path_parts[0].lower()
        if market == "ca" and len(path_parts) > 1:
            if path_parts[1] in ["en", "fr"]:
                market = f"ca_{path_parts[1]}"
        elif market == "sa" and len(path_parts) > 1:
             if path_parts[1] == "en": market = "sa_en"
        elif market == "ae" and len(path_parts) > 1:
             if path_parts[1] == "en": market = "ae_en"
        elif market == "hk" and len(path_parts) > 1:
             if path_parts[1] == "en": market = "hk_en"
        elif market == "eg" and len(path_parts) > 1:
             if path_parts[1] == "en": market = "eg_en"
        
        return market
    except:
        return "unknown"

# [수정] JSON에서 로드한 맵을 인자로 받음
def get_english_translation(text, market, trans_data):
    if not text: return ""
    clean_text = str(text).lower().strip()
    
    market_map = trans_data.get("market_map", {})
    global_map = trans_data.get("global_map", {})

    # 1. Market Specific Search
    if market in market_map:
        if clean_text in market_map[market]:
            return market_map[market][clean_text]
    
    # 2. Global Fallback
    for k, v in global_map.items():
        if k in clean_text:
            return v
            
    return ""

def parse_regional_text(text):
    mapping = {}
    if not text: return mapping
    text = text.replace("\\n", "\n")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i in range(0, len(lines), 2):
        if i + 1 < len(lines):
            rid = lines[i]
            val = lines[i+1]
            mapping[rid] = val
    return mapping

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema_dir", required=True)
    parser.add_argument("--mode", required=True, choices=["Price", "Availability"])
    parser.add_argument("--default_gmc", default="")
    parser.add_argument("--regional_map_text", default="", help="Raw text for regional overrides")
    args = parser.parse_args()

    schema_dir = Path(args.schema_dir)
    if not schema_dir.exists():
        print(json.dumps([]))
        return

    # [수정] 번역 데이터 로드
    trans_data = load_translation_map()
    regional_overrides = parse_regional_text(args.regional_map_text)
    summary_rows = []

    for schema_file in sorted(schema_dir.glob("region_*__schema_*.json")):
        filename = schema_file.name
        try:
            region_part = filename.split("__schema_")[0]
            rid = region_part.replace("region_", "")
            if rid == "default": rid = "Default (No Param)"
        except:
            rid = "Unknown"
        
        pure_rid = rid if rid != "Default (No Param)" else ""
        target_gmc = regional_overrides.get(pure_rid, args.default_gmc)
        if pure_rid == "" and not target_gmc: target_gmc = args.default_gmc

        # Load Schema
        s_val = "-"
        try:
            with open(schema_file, 'r', encoding='utf-8') as f:
                sd = json.load(f)
                offers = sd.get("offers", {})
                if isinstance(offers, list): offers = offers[0] if offers else {}
                if args.mode == "Price":
                    s_val = str(offers.get('price', '-'))
                else:
                    s_val = offers.get("availability", "-").replace("https://schema.org/", "")
        except: pass

        # Load Visual & URL
        l_val = "-"
        market_code = "unknown"
        scrape_file = schema_dir / filename.replace("__schema_", "__scrape_")
        if scrape_file.exists():
            try:
                with open(scrape_file, 'r', encoding='utf-8') as f:
                    vd = json.load(f)
                    meta_url = vd.get("meta_url", "")
                    market_code = detect_market_from_url(meta_url)
                    
                    if args.mode == "Price":
                        l_val = vd.get("visual_price", "-")
                    else:
                        l_val = vd.get("buy_button_text", "-")
            except: pass

        # Display Formatting
        display_gmc = target_gmc
        display_lg = l_val
        display_schema = s_val

        if args.mode == "Price":
            display_gmc = clean_currency(target_gmc)
            display_lg = clean_currency(l_val)
            display_schema = clean_currency(s_val)
        else:
            # [수정] 로드된 번역 데이터를 인자로 전달
            eng_status = get_english_translation(l_val, market_code, trans_data)
            if eng_status and l_val != "-":
                display_lg = f"{l_val} ({eng_status})"

        summary_rows.append({
            "Region": rid,
            "GMC": display_gmc if display_gmc else "(empty)",
            "LG.com (Visual)": display_lg,
            "Schema": display_schema
        })

    print(json.dumps(summary_rows))

if __name__ == "__main__":
    main()