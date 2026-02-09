import os
import shutil
import time
from pathlib import Path
from datetime import datetime, timedelta

# 결과 폴더들이 저장되는 상위 폴더 경로
HERE = Path(__file__).resolve().parent
OUTS_DIR = HERE / "outs"

# 기준 시간 설정 (3일)
DAYS_TO_KEEP = 3

def cleanup_old_folders():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 오래된 결과 폴더 정리 시작...")
    
    if not OUTS_DIR.exists():
        print("outs 폴더가 존재하지 않습니다.")
        return

    deleted_count = 0
    now = datetime.now()
    threshold_date = now - timedelta(days=DAYS_TO_KEEP)

    # outs 폴더 안의 모든 항목 조사
    for folder in OUTS_DIR.iterdir():
        # 폴더이면서 이름이 'out_'으로 시작하는 경우만 대상
        if folder.is_dir() and folder.name.startswith("out_"):
            try:
                # 폴더명 예시: out_20260209_181319
                # 'out_' 뒤의 날짜 부분(8자리)만 추출
                date_str = folder.name.split('_')[1]
                folder_date = datetime.strptime(date_str, "%Y%m%d")

                # 폴더 날짜가 7일보다 이전이면 삭제
                if folder_date < threshold_date:
                    shutil.rmtree(folder)
                    print(f"삭제됨: {folder.name} (생성일: {date_str})")
                    deleted_count += 1
            except (IndexError, ValueError):
                # 폴더명 형식이 맞지 않는 경우 무시
                continue
            except Exception as e:
                print(f"삭제 실패 ({folder.name}): {e}")

    print(f"정리 완료. 총 {deleted_count}개의 폴더가 삭제되었습니다.")

if __name__ == "__main__":
    cleanup_old_folders()