<#
Construye el ejecutable de Wii Golf para Windows (64 bits) con PyInstaller y lo
deja listo para repartir:

    dist\WiiGolf\WiiGolf.exe     carpeta autocontenida (Python + dependencias + web)
    dist\WiiGolf-win64.zip       la misma carpeta comprimida, para enviar

    powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1

Quien lo reciba solo tiene que descomprimir y hacer doble clic en WiiGolf.exe:
no necesita instalar Python. Los datos se guardan en data\ junto al exe.
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { Write-Error "No existe $Python (crea el .venv e instala requirements.txt)"; exit 1 }

Set-Location $Root
& $Python -m pip install --quiet pyinstaller
& $Python -m PyInstaller (Join-Path $Root "packaging\wiigolf.spec") --noconfirm --clean --distpath (Join-Path $Root "dist") --workpath (Join-Path $Root "build\pyinstaller")
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller ha fallado"; exit 1 }

$Dist = Join-Path $Root "dist\WiiGolf"
Copy-Item (Join-Path $Root "packaging\LEEME.txt") (Join-Path $Dist "LEEME.txt") -Force
New-Item -ItemType Directory -Force (Join-Path $Dist "data") | Out-Null

# Zip con .NET y reintentos: justo despues de escribir cientos de archivos el
# antivirus suele tener alguno abierto y Compress-Archive falla.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$Zip = Join-Path $Root "dist\WiiGolf-win64.zip"
if (Test-Path $Zip) { Remove-Item $Zip -Force }
$ok = $false
for ($i = 1; $i -le 6 -and -not $ok; $i++) {
    try {
        [System.IO.Compression.ZipFile]::CreateFromDirectory($Dist, $Zip, [System.IO.Compression.CompressionLevel]::Optimal, $true)
        $ok = $true
    } catch {
        if (Test-Path $Zip) { Remove-Item $Zip -Force -ErrorAction SilentlyContinue }
        Write-Output "Zip: intento $i fallido ($($_.Exception.Message)); reintento en 5 s"
        Start-Sleep -Seconds 5
    }
}
if (-not $ok) { Write-Error "No se pudo crear el zip"; exit 1 }
$mb = [math]::Round((Get-Item $Zip).Length / 1MB, 1)
Write-Output ""
Write-Output "Listo:  $Dist\WiiGolf.exe"
Write-Output "        $Zip  ($mb MB)"
