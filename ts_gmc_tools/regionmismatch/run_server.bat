@echo off
title GMC Region Mismatch Audit Tool Server
cd /d "C:\Users\nina.ahn\Desktop\ninas_gemini_cli_workspace\gmc_auto\ts_gmc_tools\regionmismatch"
echo Starting Streamlit Server...
python -m streamlit run app.py
pause