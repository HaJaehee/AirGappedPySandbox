# 에어갭 Python 샌드박스 — MCP 서버

**버전: v0.4.1** ([변경 이력](#버전-이력))

MCP(Model Context Protocol)를 통해 LLM에 노출되는 **상태 유지형(stateful) 오프라인 Python 코드 인터프리터**입니다.
사내/기업용 LLM(AnythingLLM 에이전트 경유)이 수학 계산, 데이터 분석, 차트 생성, 문서 파싱을 위해
실제 Python 코드를 실행할 수 있게 해줍니다 — **인터넷 연결 없이, `pip install` 없이**, 미리 패키징된
포터블 Python 배포판을 대상으로 동작합니다.

- **상태 유지(Stateful):** 변수, import, 로드된 데이터프레임이 도구 호출 간에 유지됩니다. 따라서
  100MB짜리 Excel 파일을 후속 질문마다 다시 읽을 필요 없이 한 번만 읽습니다.
- **아티팩트 인식(Artifact-aware):** `./workspace/`에 기록된 모든 파일(`.png` 차트, `.csv`, `.docx` 등)이
  자동으로 감지되어 LLM에게 Markdown 링크/이미지로 반환됩니다.
- **격리 및 제한:** 실행은 `./workspace/`로 한정되며 타임아웃이 설정되어 있고, 멈춘(wedged) 커널은
  자동으로 복구됩니다.

---

## 아키텍처

```
AnythingLLM 에이전트
      │  MCP (stdio)
      ▼
server.py ── FastMCP 래퍼 ──────────────────────────────────┐
      │   • execute_python_code / write_workspace_file        │  안전 &
      │   • workspace 스냅샷 → 아티팩트 링크 (artifacts.py)    │  아티팩트 계층
      ▼                                                        │
kernel_manager.py ── StatefulKernel (jupyter_client) ─────────┘
      │  ZeroMQ
      ▼
IPython 커널  ← 4GB 포터블 Python으로 실행됨
      (pandas, numpy, sympy, pdfplumber, matplotlib, openpyxl, lxml, ...)
```

**핵심 설계 결정 — 두 개의 인터프리터로 분리.** MCP 서버 프로세스 자체는 `mcp` + `jupyter_client` +
`ipykernel`만 필요합니다. 서버가 실행하는 *커널*은 **다른** 인터프리터에서 돌릴 수 있는데, 바로 모든
데이터 사이언스 라이브러리를 담고 있는 4GB 포터블 Python입니다. 이 커널 인터프리터는
`SANDBOX_KERNEL_PYTHON` 환경 변수로 선택합니다. 이렇게 하면 서버는 가볍게 유지되고, 어떤 것도 재설치할
필요 없이 미리 빌드된 배포판을 그대로 가리키게 할 수 있습니다.

## 파일 구성

| 파일 | 역할 |
|------|------|
| `server.py` | MCP 진입점. 도구 등록 및 응답 포맷팅. |
| `kernel_manager.py` | 상태 유지형 IPython 커널 래퍼 (시작/실행/타임아웃/재시작). |
| `artifacts.py` | workspace 스냅샷 + 차이 비교 → Markdown 아티팩트 링크. |
| `config.py` | 모든 설정 값, 환경 변수로 재정의 가능. |
| `check_environment.py` | 사전 점검: 포터블 Python에 필수/권장 패키지가 모두 있고 커널을 띄울 수 있는지 검증. |
| `test_core.py` | 커널 + 아티팩트 계층 스모크 테스트 (MCP 불필요). |
| `requirements-server.txt` | *서버*가 필요로 하는 패키지 목록. |
| `requirements-kernel.txt` | *커널(포터블 파이썬)*에 pre-install 권장되는 전체 패키지 목록. |
| `anythingllm_mcp_config.example.json` | AnythingLLM MCP 설정 예시(그대로 붙여넣기 가능). |
| `start_server.ps1` | 테스트용 수동 실행 헬퍼. |
| `workspace/` | 격리된 입출력 디렉터리 (업로드 + 생성 아티팩트). |

## LLM에 노출되는 MCP 도구

- **`execute_python_code(code: str, namespace: str)`** — 해당 네임스페이스의 영속 커널에서 Python을
  실행합니다. `execution_status`(`SUCCESS` / `ERROR` / `TIMEOUT`), 캡처된 stdout, stderr(정리된 트레이스백
  포함), 새로 생성된 workspace 파일 Markdown 링크를 반환합니다. 응답 맨 위에 `active_namespace: ns-XXXX`가
  표시됩니다.
- **`run_python_file(file_path: str, namespace: str)`** — `./workspace` 안의 `.py` 파일을 해당 네임스페이스
  커널에서 스크립트로 실행합니다(`if __name__ == '__main__'` 블록 실행). 반환 형식은 `execute_python_code`와
  동일하며, 파일의 최상위 변수/함수는 실행 후에도 메모리에 남습니다. 경로는 반드시 `./workspace` 안이어야
  하며(격리), 바깥 경로는 거부됩니다.
- **`write_workspace_file(filename: str, content: str, overwrite: bool = False)`** — 텍스트(소스 코드, 메모,
  CSV, Markdown, JSON 등)를 `./workspace` 안의 파일로 **그대로 저장**합니다. 코드를 실행하지 않으며
  `namespace`도 필요 없습니다. 파일을 만들기 위해 "파일을 쓰는 파이썬 코드"를 다시 생성할 필요가 없어,
  작은 모델이 가장 자주 실패하는 자기참조 패턴을 피할 수 있습니다. 경로는 `./workspace` 안으로 강제되며
  (`..`·절대 경로·Windows 예약 장치명 거부), 기존 파일은 `overwrite=true` 없이는 덮어쓰지 않습니다.
- **`list_workspace_files()`** — `./workspace`의 업로드/출력 파일을 크기와 형식과 함께 나열합니다(모든
  네임스페이스가 공유).
- **`reset_kernel_state(namespace: str)`** — 지정한 네임스페이스의 커널만 재시작하여 그 대화의 메모리 상태를
  지웁니다(workspace 파일과 다른 네임스페이스는 유지).

> **네임스페이스(대화별 격리):** 하나의 MCP 서버 프로세스를 여러 AnythingLLM 대화가 공유하므로, 각 실행은
> `namespace`로 분리된 별도 커널에서 돌아갑니다. LLM은 첫 호출에 `namespace="new"`를 주고, 응답의
> `active_namespace: ns-XXXX`를 이후 호출마다 그대로 넘겨 자기 변수/데이터프레임을 유지합니다. 유휴
> 네임스페이스 커널은 `SANDBOX_NS_IDLE_TIMEOUT`(기본 30분) 후 회수되고, 동시 커널 수는
> `SANDBOX_MAX_NAMESPACES`(기본 8)로 제한됩니다. 자세한 배경은 `wiki/09-namespace-routing.md` 참고.

도구 설명(description)에는 LLM이 지켜야 할 운영 규칙이 내장되어 있습니다(no `pip`, 항상 `print()`,
플롯은 `plt.show()`가 아니라 `plt.savefig()`로 저장, 단순 상대 경로 사용, 오류 시 자가 수정).

---

## 설치

### 1. 포터블 Python 준비 (커널 인터프리터)

4GB 포터블 Python에는 데이터 사이언스 스택 **및** `ipykernel`이 포함되어 있어야 합니다. 다음이 이미 있어야
합니다: `pandas`, `numpy`, `scipy`, `sympy`, `matplotlib`, `seaborn`, `pdfplumber`, `pypdf`, `PyMuPDF`,
`openpyxl`, `xlsxwriter`, `xlrd`, `lxml`, `xmltodict`, `python-docx`, `markdown`, `beautifulsoup4`.
커널 프로세스가 실제로 실행하는 `ipykernel`도 반드시 포함되어야 합니다.

### 2. 서버 자체 의존성 제공 (오프라인 설치)

`server.py`를 실행하는 인터프리터에는 `mcp`, `jupyter_client`, `ipykernel`이 필요합니다. 가장 간단한 구성은
**포터블 Python으로 `server.py`를 실행**하고 이 세 개를 거기에 설치하는 것입니다.

이 패키지에는 이미 `offline_wheels/` 폴더에 **Python 3.11 / 3.12 / 3.13 (win_amd64)용 휠이 모두 번들**되어
있으므로, 에어갭 호스트에서 인터넷 없이 곧바로 설치할 수 있습니다:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_offline.ps1 -Python "C:\path\to\portable-python\python.exe"
```

(내부적으로 `pip install --no-index --find-links offline_wheels -r requirements-server.txt`를 실행하며,
대상 Python 버전에 맞는 휠이 자동 선택됩니다. 대상 Python 에는 `pip`이 있어야 합니다.)

> 대상 Python 이 3.11–3.13 이 아니거나 win_amd64 가 아니라면, 인터넷이 되는 PC에서 아래로 휠을 다시
> 받으세요: `pip download -r requirements-server.txt -d .\offline_wheels`
>
> 포터블 Python이 완전히 고정(frozen)되어 있다면, 이 세 패키지가 있는 아무 인터프리터로 서버를 실행하고
> `SANDBOX_KERNEL_PYTHON`을 포터블 Python으로 가리키게 하세요.

### 3. 환경 검증

```powershell
<portable-python>\python.exe check_environment.py
```

`RESULT: PASS`가 나와야 합니다. **필수** 패키지 누락은 치명적입니다(에어갭 호스트에는 나중에 고칠 `pip`가
없습니다).

### 4. AnythingLLM에 등록

`anythingllm_mcp_config.example.json`을 AnythingLLM의 MCP 서버 설정에 복사하세요
(**Settings → Agent Skills → MCP Servers**, 또는 `~/AnythingLLM/plugins/anythingllm_mcp_servers.json` 편집).
두 개의 절대 경로를 수정한 뒤 에이전트를 재시작하세요. 그리고 워크스페이스의 에이전트 스킬에서 도구를
활성화하세요.

아래 `<BASE>` 는 패키지 압축 해제 위치입니다(예: `D:\AirGappedPySandbox`). 포터블 Python 은
`<BASE>\WPy64-313130\python\python.exe`, 서버 코드는 `<BASE>\mcp-server\server.py` 에 있습니다.

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

---

## 설정 (환경 변수)

| 변수 | 기본값 | 의미 |
|------|--------|------|
| `SANDBOX_KERNEL_PYTHON` | 서버 자신의 `python` | 커널을 실행하는 인터프리터(포터블 Python을 가리키게 함). |
| `SANDBOX_WORKSPACE` | `./workspace` | 격리된 입출력 디렉터리. |
| `SANDBOX_EXEC_TIMEOUT` | `60` | 호출당 실행 타임아웃(초). |
| `SANDBOX_STARTUP_TIMEOUT` | `60` | 커널 부팅 타임아웃(초). |
| `SANDBOX_MAX_STREAM_CHARS` | `20000` | 반환 stdout/stderr 상한(0 = 무제한). |
| `SANDBOX_MAX_WRITE_BYTES` | `1048576` | `write_workspace_file` 1회 쓰기 상한(바이트, 0 = 무제한). |
| `SANDBOX_NS_IDLE_TIMEOUT` | `1800` | 유휴 네임스페이스 커널 회수까지의 시간(초). |
| `SANDBOX_MAX_NAMESPACES` | `8` | 동시 유지 네임스페이스 커널 수 상한(초과 시 LRU 회수). |
| `SANDBOX_LAZY_START` | 미설정 | 참(truthy)이면 시작 시 예열용 커널을 미리 부팅하지 않음. |

## 사용자가 샌드박스에 파일을 넣는 방법

대상 PDF / Excel / CSV 파일을 `workspace/` 디렉터리(AnythingLLM 설정의 `SANDBOX_WORKSPACE`가 가리키는
동일한 폴더)에 넣으세요. 커널의 작업 디렉터리가 곧 이 폴더이므로, LLM은 `list_workspace_files`로
파일을 발견하고 `data.xlsx` 같은 단순 상대 경로로 읽습니다(`./workspace/` 접두사 불필요). 생성된
출력물도 같은 폴더에 저장되어 링크로 돌아옵니다.

## 타임아웃 & 복구 동작

`SANDBOX_EXEC_TIMEOUT`을 초과한 셀은 인터럽트됩니다. 커널이 idle 상태로 돌아오면 **상태를 유지한 채**
재사용됩니다. 만약 멈춘 상태가 지속되면 — Windows에서 인터럽트가 파고들 수 없는 단일 장기 `time.sleep`
같은 블로킹 C 호출에서 발생합니다 — 커널을 **재시작**(상태 초기화)하여 다음 호출은 항상 응답 가능한
커널을 받도록 합니다. 응답에는 두 경우 중 무엇이 일어났는지 표시됩니다.

## 테스트

```powershell
# 커널 + 아티팩트 계층 (jupyter_client + ipykernel + pandas + matplotlib 필요):
<python> test_core.py
# 포터블 Python에 대한 환경 준비 상태 점검:
<portable-python>\python.exe check_environment.py
```

## 보안 참고

이것은 관습적(by convention) 샌드박스이며, 강화된 감옥(jail)이 아닙니다: 코드는 서버 프로세스와 동일한 OS
권한으로 실행됩니다. 여기서의 완화책은 workspace 상대 경로 입출력, 자동 복구가 있는 실행 타임아웃,
출력 크기 제한, 그리고 에어갭 그 자체(대상 환경에 네트워크 없음)입니다. 더 강한 격리가 필요하다면 서버
전체를 컨테이너나 제한된 OS 사용자 계정 안에서 실행하세요.

---

## 버전 이력

[유의적 버전(SemVer)](https://semver.org/lang/ko/)을 따릅니다. 1.0 이전(0.x)이라 아직 세부 동작은
바뀔 수 있습니다.

| 버전 | 변경 내용 |
|------|-----------|
| **v0.4.1** | 워크스페이스 경로 처리 수정 — 커널의 작업 디렉터리를 `SANDBOX_WORKSPACE`(프로젝트 밖이어도) 안으로 이동해, 코드는 `./workspace/` 접두사 없이 단순 상대 경로를 사용합니다. 상대값 `SANDBOX_WORKSPACE`는 호스트 cwd가 아닌 프로젝트 루트 기준으로 해석. `run_python_file`의 `./workspace/x.py` 해석과 채팅 링크 경로가 프로젝트 외부 워크스페이스에서도 정상 동작. |
| v0.4.0 | `write_workspace_file` 도구 추가 — 텍스트/코드를 코드 실행 없이 `./workspace`에 그대로 저장(경로 격리·덮어쓰기 보호·크기 상한). 작은 모델이 실패하던 '자기 소스를 인용하는 파이썬 생성' 패턴 제거. `run_python_file`은 이제 `.py`가 아닌 파일을 커널에 넘기기 전에 거부. |
| v0.3.0 | 대화별 **네임스페이스 라우팅**(필수 `namespace` 인자) 도입 — 한 서버 프로세스를 공유하는 여러 AnythingLLM 대화의 변수 충돌 방지. 네임스페이스별 커널 풀(웜 리저브·유휴 회수·LRU 상한), 응답 배너 되울림, 미래 `_meta` 대화 id 훅. |
| v0.2.0 | `run_python_file` 도구 추가(워크스페이스 `.py` 파일을 스크립트로 실행). |
| v0.1.0 | 최초 기능 릴리스: 상태 유지형 IPython 커널 코어, 아티팩트 인터셉터, FastMCP 도구, 포터블 패키지(WinPython 3.13) + 오프라인 검증. |
