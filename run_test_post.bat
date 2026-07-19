@echo off
chcp 65001 >nul
rem 実投稿テスト: market-map を1件だけ強制投稿（このプロセス限定で POST_ENABLED=true）。
rem 実行すると X に1件投稿されます。本番前の疎通確認用。
cd /d "%~dp0"
if exist .venv\Scripts\python.exe ( set PY=.venv\Scripts\python.exe ) else ( set PY=python )
echo [警告] X に実際に1件投稿します。
set /p ANS=投稿しますか? (y/N):
if /i not "%ANS%"=="y" ( echo 中止しました。 & pause & exit /b 0 )
set POST_ENABLED=true
%PY% local_finance_bot.py force market-map
pause
