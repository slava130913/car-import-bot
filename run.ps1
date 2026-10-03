# Запуск бота на Windows: правый клик → Run with PowerShell, или в терминале: .\run.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if (-not (Test-Path ".venv")) {
    Write-Host "Создаю виртуальное окружение..."
    python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Создан .env. Откройте его, вставьте BOT_TOKEN, ADMIN_IDS и BOT_USERNAME, затем запустите снова."
    exit 1
}

$envText = Get-Content ".env" -Raw
if ($envText -notmatch "BOT_TOKEN=\S+") {
    Write-Host "В .env не заполнен BOT_TOKEN. Получите токен у @BotFather и впишите."
    exit 1
}

& .\.venv\Scripts\python.exe scripts\build_web.py
Write-Host "Запускаю бота. Остановить: Ctrl+C"
& .\.venv\Scripts\python.exe -m bot.main
