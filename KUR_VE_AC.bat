@echo off
rem === AI Monitor - tek tik kurulum ===
rem Bu dosyaya cift tikla: bagimliliklari kurar, butonlari kaydeder, dashboard'u acar.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python bulunamadi. Once Python kurun: https://www.python.org/downloads/
  echo  Kurulum sirasinda "Add Python to PATH" kutusunu isaretleyin, sonra bu dosyayi tekrar calistirin.
  echo.
  pause
  exit /b 1
)

echo Kurulum basliyor (1-2 dakika)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_machine.ps1"
if errorlevel 1 (
  echo Kurulumda hata olustu - yukaridaki mesaja bakin.
  pause
  exit /b 1
)

start "" "%~dp0dashboard.html"
echo.
echo  Tamam! Dashboard acildi.
echo  - REFRESH / ASK butonuna ilk basista tarayici "Open PowerShell?" diye sorar: "Always allow" deyin.
echo  - Veriyi tazelemek icin GA4 sekmesindeki REFRESH yeterli (ucretsiz, ~30 sn).
echo.
pause
