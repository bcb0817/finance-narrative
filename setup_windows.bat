@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === finance-narrative セットアップ (Windows) ===
echo 作業フォルダ: %CD%

where python >nul 2>nul
if errorlevel 1 (
  echo [エラー] python が見つかりません。https://www.python.org からインストールしてください。
  echo （インストール時に "Add python.exe to PATH" にチェックを入れること）
  pause
  exit /b 1
)
python --version

set "REBUILD_VENV=0"
if not exist .venv\Scripts\python.exe set "REBUILD_VENV=1"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import sys" >nul 2>nul
  if errorlevel 1 set "REBUILD_VENV=1"
)
if "%REBUILD_VENV%"=="1" (
  echo 仮想環境を作成または再構築します (.venv)...
  python -m venv --clear .venv
  if errorlevel 1 ( echo [エラー] venv作成失敗 & pause & exit /b 1 )
)

echo 依存パッケージをインストールします（数分かかります）...
call .venv\Scripts\python -m pip install --upgrade pip
call .venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 ( echo [エラー] pip install 失敗 & pause & exit /b 1 )

if not exist .env (
  copy .env.example .env >nul
  echo.
  echo [重要] .env を作成しました。notepad .env でAPIキーを記入してください。
  echo        POST_ENABLED=false のままなら実投稿されません（動作確認用）。
)

echo.
echo === セットアップ完了 ===
echo 次: run_status.bat をダブルクリックして状態確認
pause
