@echo off
cd /d "C:\Users\miyak\affiliate_bot"
git add .
git commit -m "自動バックアップ %date% %time%"
git push origin main
echo バックアップ完了
