@echo off
cd /d "%~dp0"
echo =======================================================
echo   Pushing Dashboard Updates to GitHub...
echo =======================================================
git remote set-url origin https://github.com/JRY-INFANT/APX-BOT.git
git add -A
git commit -m "Add Vercel entrypoint main.py and dashboard files"
echo Syncing with GitHub (force update)...
git push -u origin main --force
echo.
echo =======================================================
echo   SUCCESS! Pushed to GitHub.
echo   Vercel is now deploying your dashboard!
echo =======================================================
pause
