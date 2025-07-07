# ⚙️ CONFIG
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = "$HOME\VSCode_PIO_Backup_$timestamp"
$vscodeUser = "$env:APPDATA\Code\User"
$pioHome = "$env:USERPROFILE\.platformio"
$workspace = Get-Location

# 🗂️ CREATE BACKUP FOLDER STRUCTURE FIRST
$vsFolder = "$backupRoot\vscode_user"
$pioFolder = "$backupRoot\platformio"
$wsFolder = "$backupRoot\workspace"

New-Item -ItemType Directory -Force -Path $vsFolder, $pioFolder, $wsFolder | Out-Null

Write-Host "`n🔄 Backing up VS Code user settings..."
Copy-Item "$vscodeUser\*" $vsFolder -Recurse -Force

Write-Host "🔄 Backing up VS Code extensions list..."
code --list-extensions > "$backupRoot\vscode_extensions.txt"

Write-Host "🔄 Backing up PlatformIO user configs..."
Copy-Item "$pioHome\*" $pioFolder -Recurse -Force

Write-Host "🔄 Backing up current workspace folder..."
if ($workspace.FullName -ne $backupRoot) {
    Copy-Item "$workspace\*" $wsFolder -Recurse -Force -Exclude "*.ps1"
} else {
    Write-Host "⚠️ Skipping workspace backup to avoid recursive copy."
}

# 🔍 Optional: Python env
if (Get-Command python -ErrorAction SilentlyContinue) {
    Write-Host "🔄 Backing up global Python packages..."
    python -m pip freeze > "$backupRoot\python_packages.txt"
}

Write-Host "`n✅ ALL DONE! Backup created at: $backupRoot" -ForegroundColor Green
