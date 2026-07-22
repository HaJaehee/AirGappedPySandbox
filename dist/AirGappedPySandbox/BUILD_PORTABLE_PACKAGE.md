# 포터블 패키지 제작 매뉴얼 (에어갭 환경 반입용)

이 문서는 **인터넷이 되는 빌드 PC**에서 "포터블 Python + 이 MCP 서버 + 모든 라이브러리"를 하나의
자체 완결형 폴더로 만들어, **인터넷이 없는 에어갭 호스트**로 반입(USB/승인된 파일 전송)하는 전체 절차를
설명합니다.

핵심 원칙: **에어갭 호스트에서는 `pip install`이 불가능**하므로, 필요한 모든 것(Python 인터프리터 +
데이터 라이브러리 + 서버 의존성)을 **빌드 PC에서 미리** 포터블 Python 안에 넣어 통째로 옮깁니다.

---

## 0. 전체 그림

```
[ 인터넷 되는 빌드 PC ]                          [ 에어갭 호스트 ]
 1. 포터블 Python 준비 (WinPython 등)             통째로 복사
 2. 데이터 라이브러리 설치 (requirements-kernel)   ──── ZIP/USB ────▶   압축 해제
 3. 서버 의존성 설치 (requirements-server)                              경로만 수정
 4. 오프라인 동작 검증 (네트워크 끊고 테스트)                            AnythingLLM 등록
 5. 하나의 폴더로 패키징
```

최종 산출물(레이아웃 — 이 저장소의 `dist\AirGappedPySandbox\` 가 실제로 이 구조입니다):

```
AirGappedPySandbox\
├─ PACKAGE_INFO.txt             ← 패키지 개요/빠른 시작
├─ BUILD_PORTABLE_PACKAGE.md    ← 이 매뉴얼
├─ WPy64-313130\                ← 포터블 Python 본체 (WinPython, 모든 라이브러리 포함, 이게 "40GB")
│   └─ python\python.exe            * 실제 인터프리터 경로
└─ mcp-server\                  ← 서버 코드 (이 폴더가 매뉴얼의 mcp-server\)
    ├─ server.py, kernel_manager.py, artifacts.py, config.py
    ├─ check_environment.py, test_core.py
    ├─ requirements-kernel.txt, requirements-server.txt
    ├─ anythingllm_mcp_config.example.json
    ├─ install_offline.ps1, start_server.ps1
    ├─ offline_wheels\
    ├─ README.md
    └─ workspace\
```

> 포터블 Python 폴더 이름은 배포판에 따라 다릅니다(WinPython 은 `WPy64-XXXXX\`, 내부 인터프리터는
> `...\python\python.exe`). 아래 절차의 `<PPY>` 는 이 실제 python.exe 경로를 의미합니다.

---

## 1. 빌드 PC 준비 사항

- **OS/아키텍처를 에어갭 호스트와 일치**시킬 것. 컴파일된 휠(numpy, scipy, pydantic-core, pyzmq,
  PyMuPDF 등)은 플랫폼 종속적입니다. 호스트가 Windows 64비트(win_amd64)면 빌드 PC도 동일해야 합니다.
- **Python 마이너 버전을 고정**할 것(예: 3.12). 포터블 Python과 빌드 시 사용하는 버전이 같아야 컴파일
  휠이 맞습니다. 이 저장소가 기본 검증한 버전은 3.11 / 3.12 / 3.13 입니다.
- 인터넷 연결.

---

## 2. 포터블 Python 확보 (방법 택1)

### 방법 A — WinPython (권장)

`python.exe`, `pip`, 그리고 다수의 과학 패키지를 이미 포함한 **진짜 포터블** 배포판입니다. 압축 해제만
하면 어디서든 실행되어 반입에 가장 적합합니다.

1. 빌드 PC에서 WinPython(원하는 Python 버전, 64비트)을 내려받아 압축 해제.
2. 내부 `python.exe` 경로를 확인(이하 `<PPY>` 로 표기). 예: `WPy64-31241\python\python.exe`
3. pip 확인:
   ```powershell
   & "<PPY>" -m pip --version
   ```

### 방법 B — python.org "embeddable" + pip 부트스트랩

가장 작지만 손이 더 갑니다(embeddable 배포판은 기본적으로 pip 이 없고 `site` 가 비활성).

1. "Windows embeddable package (64-bit)" 를 받아 `python\` 로 압축 해제.
2. `python3xx._pth` 파일을 열어 `#import site` 의 주석(`#`)을 제거하여 `import site` 로 만든다
   (pip 및 서드파티 패키지 인식에 필요).
3. `get-pip.py` 를 받아 실행하여 pip 설치:
   ```powershell
   & "<PPY>" get-pip.py
   ```

### 방법 C — conda-pack (Anaconda/Miniconda 사용 시)

conda 환경을 만들고 `conda-pack` 으로 재배치 가능한 tarball 로 묶는 방식. conda 생태계를 이미 쓰는
경우에 적합.

```powershell
conda create -n airgap python=3.12 -y
conda activate airgap
pip install -r requirements-kernel.txt -r requirements-server.txt
conda install conda-pack -y
conda pack -n airgap -o airgap-python.tar.gz
```
에어갭 호스트에서 압축을 풀고 `conda-unpack` 을 실행하면 경로가 보정됩니다.

> 참고: 순수 `python -m venv` 로 만든 가상환경은 **재배치가 보장되지 않습니다**(절대 경로/셔뱅에 의존).
> 포터블 반입 목적에는 방법 A/B/C 를 사용하세요.

---

## 3. 데이터 라이브러리 설치 (커널용)

빌드 PC에서, 위의 포터블 Python 에 데이터 사이언스 스택을 설치합니다. 이 단계가 용량의 대부분("40GB")을
차지합니다.

```powershell
& "<PPY>" -m pip install -r requirements-kernel.txt
```

`requirements-kernel.txt` 에는 pandas, numpy, scipy, sympy, matplotlib, seaborn, pdfplumber, pypdf,
PyMuPDF, openpyxl, xlsxwriter, xlrd, lxml, xmltodict, python-docx, markdown, beautifulsoup4, 그리고
커널 런타임인 ipykernel 이 포함되어 있습니다.

---

## 4. 서버 의존성 설치 (mcp / jupyter_client / ipykernel)

서버(`server.py`)를 **포터블 Python 하나로** 실행하는 것이 가장 단순합니다. 같은 포터블 Python 에 서버
의존성도 설치하세요.

- **온라인 설치(가장 간단):**
  ```powershell
  & "<PPY>" -m pip install -r requirements-server.txt
  ```
- **또는** 이미 만들어 둔 오프라인 휠(`dist/AirGappedPySandbox/offline_wheels/`)로 설치할 수도 있습니다:
  ```powershell
  & "<PPY>" -m pip install --no-index --find-links .\offline_wheels -r requirements-server.txt
  ```

이렇게 하면 서버와 커널이 **같은 인터프리터**를 쓰므로, 나중에 `SANDBOX_KERNEL_PYTHON` 을 굳이 지정하지
않아도 됩니다(기본값이 서버 자신의 Python).

---

## 5. 오프라인 동작 검증 (반입 전, 빌드 PC에서)

실제 에어갭 상황을 흉내 내기 위해 **네트워크를 끊은 뒤** 검증하는 것이 가장 확실합니다.

1. (선택) Wi-Fi/랜 분리 또는 방화벽으로 아웃바운드 차단.
2. 환경 점검 — 모든 필수 패키지 존재 + 커널 부팅 확인:
   ```powershell
   & "<PPY>" check_environment.py
   ```
   → `RESULT: PASS` 가 나와야 함.
3. 코어 스모크 테스트(상태 유지/아티팩트/타임아웃):
   ```powershell
   & "<PPY>" test_core.py
   ```
   → 모든 항목 `PASS`.

여기서 `MISS` 가 뜨면 3~4단계로 돌아가 누락 패키지를 설치하세요. **에어갭 호스트에서는 고칠 수 없습니다.**

---

## 6. 하나의 폴더로 패키징

`0. 전체 그림`의 레이아웃대로 포터블 Python 폴더(예: `WPy64-313130\`)와 서버 코드(`mcp-server\`)를 한 폴더에
모읍니다. 그런 다음 폴더 전체를 ZIP 으로 압축합니다.

```powershell
Compress-Archive -Path .\AirGappedPySandbox\* -DestinationPath .\AirGappedPySandbox.zip
```

> `mcp-server\workspace\` 안의 임시 파일이나 `__pycache__`, 개발용 `.devvenv\` 는 포함하지 마세요.

---

## 7. 에어갭 호스트로 반입 & 설정

1. 승인된 매체(USB 등)로 ZIP 을 옮겨 원하는 위치(`<BASE>`, 예: `D:\AirGappedPySandbox`)에 압축 해제.
2. 절대 경로 확인 — 포터블 Python 의 `python.exe`(`<BASE>\WPy64-313130\python\python.exe`)와
   `<BASE>\mcp-server\server.py`.
3. AnythingLLM MCP 설정에 등록(`mcp-server\anythingllm_mcp_config.example.json` 참고). 경로를 실제 값으로 수정:
   ```jsonc
   {
     "mcpServers": {
       "air-gapped-python-sandbox": {
         "command": "D:\\AirGappedPySandbox\\WPy64-313130\\python\\python.exe",
         "args": ["D:\\AirGappedPySandbox\\mcp-server\\server.py"],
         "env": {
           "SANDBOX_KERNEL_PYTHON": "D:\\AirGappedPySandbox\\WPy64-313130\\python\\python.exe",
           "SANDBOX_WORKSPACE": "D:\\AirGappedPySandbox\\mcp-server\\workspace",
           "SANDBOX_EXEC_TIMEOUT": "60"
         }
       }
     }
   }
   ```
4. 호스트에서 최종 점검(인터넷 없이):
   ```powershell
   D:\AirGappedPySandbox\WPy64-313130\python\python.exe D:\AirGappedPySandbox\mcp-server\check_environment.py
   ```
5. AnythingLLM 에이전트 재시작 후, 워크스페이스 에이전트 스킬에서 도구 활성화 → 수학/문서분석/차트 테스트.

---

## 부록 A. "휠만 반입" 경량 방식 (포터블 Python 을 옮기지 않을 때)

에어갭 호스트에 **이미** 포터블 Python 이 있고(라이브러리 포함), 서버 의존성 3종만 넣으면 되는 경우에는
전체 인터프리터를 옮길 필요 없이 **오프라인 휠만** 반입하면 됩니다. 이 저장소의 `dist/AirGappedPySandbox/`
패키지가 바로 그 용도이며, 빌드 PC에서 다음으로 휠을 (재)생성합니다:

```powershell
# 대상 Python 버전들에 맞춰 휠 다운로드 (win_amd64 예시)
pip download -r requirements-server.txt --only-binary=:all: --platform win_amd64 --python-version 3.12 -d .\offline_wheels
```

호스트에서 설치:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_offline.ps1 -Python "C:\path\to\portable-python\python.exe"
```

## 부록 B. 아키텍처/버전 불일치 체크리스트

- 컴파일 휠 오류(`is not a supported wheel on this platform`) → 빌드 PC와 호스트의 **OS/비트/파이썬
  마이너 버전** 일치 여부 확인.
- `ModuleNotFoundError: platformdirs` 류 → 전이 의존성 누락. 빌드 PC에서 `requirements-server.txt`
  전체를 다시 설치.
- `fitz` import 실패 → `PyMuPDF` 미설치(패키지명은 PyMuPDF, import 명은 fitz).
- embeddable Python 에서 서드파티 인식 안 됨 → `python3xx._pth` 의 `import site` 활성화 여부 확인.
