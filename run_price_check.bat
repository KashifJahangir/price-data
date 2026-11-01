@echo off
cd /d "C:\Users\PC\OneDrive\Documents\GitHub\price-data"

git pull --rebase --autostash

"C:\Users\PC\AppData\Local\Programs\Python\Python312\python.exe" price_checker.py products.json --store zah --no-backup --delay 2
if errorlevel 1 (
    echo.
    echo *** Price checker failed, not committing. ***
    pause
    exit /b 1
)

git add products.json
git diff --cached --quiet
if %errorlevel% neq 0 (
    git commit -m "Zah Computers price update %date%"
    git push
    echo.
    echo Zah prices updated and pushed to GitHub.
) else (
    echo.
    echo No Zah price changes to commit.
)
pause