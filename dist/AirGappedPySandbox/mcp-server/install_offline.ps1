# 서버 의존성(mcp, jupyter_client, ipykernel)을 인터넷 없이 오프라인으로 설치합니다.
#
# 사용법 (포터블 Python 경로를 지정):
#   powershell -ExecutionPolicy Bypass -File .\install_offline.ps1 -Python "C:\path\to\portable-python\python.exe"
#
# 이 스크립트는 번들된 offline_wheels\ 폴더의 휠(wheel)만 사용하며 네트워크에 접속하지 않습니다.
# offline_wheels 에는 Python 3.11 / 3.12 / 3.13 (win_amd64)용 휠이 들어 있으므로 대상 Python 버전에
# 맞는 휠이 자동으로 선택됩니다. (대상 Python 에는 pip 이 있어야 합니다.)

param(
    [Parameter(Mandatory = $true)]
    [string]$Python
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Wheels = Join-Path $Here "offline_wheels"
$Req = Join-Path $Here "requirements-server.txt"

if (-not (Test-Path $Python)) {
    throw "지정한 Python 을 찾을 수 없습니다: $Python"
}

Write-Host "오프라인 설치 시작..."
Write-Host "  대상 Python : $Python"
Write-Host "  휠 폴더     : $Wheels"

& $Python -m pip --version
& $Python -m pip install --no-index --find-links $Wheels -r $Req

Write-Host ""
Write-Host "설치 완료. 다음으로 환경을 검증하세요:"
Write-Host "  $Python check_environment.py"
