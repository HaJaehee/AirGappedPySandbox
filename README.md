# 에어갭 Python 샌드박스 — MCP 서버

**버전: v0.8.0** ([변경 이력](#버전-이력))

본 프로젝트는 MCP(Model Context Protocol)를 통해 LLM에 제공되는 **상태 유지형(stateful) 오프라인 Python 코드 인터프리터**입니다.
사내망 및 폐쇄망 환경의 LLM(AnythingLLM 에이전트 등)이 수학 계산, 데이터 분석, 차트 시각화, 문서 파싱 등을 수행할 수 있도록 실제 Python 코드를 실행하는 환경을 제공합니다. **외부 인터넷 연결이나 추가적인 `pip install` 없이도**, 사전에 패키징된 포터블 Python 배포판을 기반으로 안정적으로 동작합니다.

- **상태 유지(Stateful):** 변수, import 내역, 메모리에 로드된 데이터프레임 등이 도구 호출 간에 계속 유지됩니다. 따라서 후속 질문마다 대용량(예: 100MB) Excel 파일을 매번 다시 읽어올 필요 없이, 최초 한 번만 로드하여 효율적으로 활용할 수 있습니다.
- **산출물 자동 인식(Artifact-aware):** `./workspace/` 디렉터리에 생성된 모든 파일(`.png` 차트 이미지, `.csv`, `.docx` 문서 등)을 자동으로 감지하여 LLM 및 사용자에게 Markdown 링크나 이미지 형태로 반환합니다.
- **경로 격리 및 안전 제한:** 코드 실행 및 파일 입출력은 `./workspace/` 내부로 엄격히 한정되며, 실행 시간 제한(타임아웃)이 적용됩니다. C 레벨 블로킹 등으로 응답이 멈춘(wedged) 커널은 자동으로 감지하여 복구합니다.

---

## 아키텍처

```
AnythingLLM 에이전트
      │  MCP (stdio)
      ▼
server.py ── FastMCP 래퍼 ──────────────────────────────────┐
      │   • execute_python_code / write·append_workspace_file │  안전 &
      │   • workspace 스냅샷 → 아티팩트 링크 (artifacts.py)    │  아티팩트 계층
      ▼                                                        │
kernel_manager.py ── StatefulKernel (jupyter_client) ─────────┘
      │  ZeroMQ
      ▼
IPython 커널  ← 4GB 포터블 Python으로 실행됨
      (pandas, numpy, sympy, pdfplumber, matplotlib, openpyxl, lxml, ...)
```

**핵심 설계 결정 — 두 개의 인터프리터 분리 구조:** MCP 서버 프로세스 자체에는 `mcp`, `jupyter_client`, `ipykernel`만 필요합니다. 반면 서버가 구동 및 관리하는 *커널*은 대규모 데이터 사이언스 라이브러리를 포함한 4GB 포터블 Python 등 **별도의 인터프리터**에서 실행할 수 있습니다. 이 커널 인터프리터 경로는 `SANDBOX_KERNEL_PYTHON` 환경 변수로 지정합니다. 이러한 분리 설계를 통해 서버 자체는 매우 가볍게 유지되며, 패키지를 재설치할 필요 없이 사전에 구축된 포터블 배포판을 그대로 가리켜 즉시 운영할 수 있습니다.

## 파일 구성

| 파일 | 역할 |
|------|------|
| `server.py` | MCP 진입점입니다. 도구 등록 및 클라이언트 응답 포맷팅을 담당합니다. |
| `kernel_manager.py` | 상태 유지형 IPython 커널 래퍼로 커널 구동, 코드 실행, 타임아웃 처리, 자동 복구(재시작)를 수행합니다. |
| `artifacts.py` | workspace 디렉터리의 파일 스냅샷을 비교하여 신규 생성된 파일을 감지하고 Markdown 링크로 변환합니다. |
| `config.py` | 시스템 전반의 기본 설정값을 정의하며, 환경 변수를 통한 재정의를 지원합니다. |
| `check_environment.py` | 사전 환경 점검 스크립트입니다. 포터블 Python 내 필수/권장 패키지 존재 여부와 커널 구동 가능 여부를 검증합니다. |
| `test_core.py` | 커널 및 아티팩트 계층의 정상 동작 여부를 확인하는 스모크 테스트 스크립트입니다 (MCP 환경 불필요). |
| `requirements-server.txt` | MCP 서버 구동에 필요한 최소 의존성 패키지 목록입니다. |
| `requirements-kernel.txt` | 커널(포터블 Python)에 사전 설치가 권장되는 전체 패키지 목록입니다. |
| `anythingllm_mcp_config.example.json` | AnythingLLM에서 즉시 적용 가능한 MCP 서버 설정 예시 파일입니다. |
| `start_server.ps1` | 로컬 테스트 및 디버깅을 위한 수동 서버 실행 스크립트입니다. |
| `workspace/` | 파일 업로드 및 결과 산출물이 저장되는 격리된 입출력 디렉터리입니다. |

## LLM에 노출되는 MCP 도구

- **`execute_python_code(code: str, namespace: str)`** — 지정된 네임스페이스의 영속 커널에서 Python 코드를 실행합니다. 실행 결과 판정값인 `execution_status`(`RAN_UNVERIFIED` / `RAN_NO_OUTPUT` / `RAN_WITH_ERRORS` / `FAILED` / `TIMEOUT`), 캡처된 표준 출력(stdout), 정리된 트레이스백이 포함된 표준 에러(stderr), 그리고 새로 생성된 workspace 파일의 Markdown 링크를 반환합니다. 응답의 **첫 줄에는 실행 판정 결과가, 마지막 줄에는 `next_action:` 후속 조치 지침**이 출력되며, 그사이에 `active_namespace: ns-XXXX`가 명시됩니다. 본 시스템에는 `SUCCESS`라는 판정값이 존재하지 않습니다. 샌드박스는 실패를 증명할 수는 있어도 의도한 과업의 논리적 성공까지 증명할 수는 없으며, 성능이 제한적인 소형 모델이 `SUCCESS` 단어를 보고 실제 결과 검증 없이 과업이 완료된 것으로 오인하는 현상을 방지하기 위함입니다.
- **`run_python_file(file_path: str, namespace: str)`** — `./workspace` 디렉터리 내의 `.py` 파일을 해당 네임스페이스 커널에서 스크립트 형태로 실행합니다(`if __name__ == '__main__'` 블록 포함 실행). 반환 형식은 `execute_python_code`와 동일하며, 스크립트의 최상위 변수 및 함수는 `sys.exit()`로 종료되거나 실행 중 예외가 발생하더라도 커널 메모리에 그대로 보존됩니다. `sys.exit(0)`은 정상 종료로 인정되며, 0이 아닌 종료 코드는 `FAILED`로 판정합니다. 경로는 격리 환경 보호를 위해 반드시 `./workspace` 내부여야 하며, 외부 경로 접근은 즉시 거부됩니다.
- **`write_workspace_file(filename: str, content: str, overwrite: bool = False)`** — 텍스트(소스 코드, 메모, CSV, Markdown, JSON 등)를 `./workspace` 내의 파일로 **직접 저장**합니다. 코드를 실행하지 않으므로 `namespace` 인자가 필요하지 않습니다. 파일을 생성하기 위해 "파일을 작성하는 Python 코드"를 모델이 다시 생성할 필요가 없으므로, 소형 모델에서 자주 나타나는 자기참조(self-referential) 코드 생성 오류를 근본적으로 방지합니다. 저장 경로는 `./workspace` 내부로 엄격히 제한되며(`..`, 절대 경로, Windows 예약 장치명 차단), 기존 파일이 이미 존재할 경우 `overwrite=True` 옵션을 명시하지 않으면 덮어쓰지 않습니다.
- **`append_workspace_file(filename: str, content: str)`** — `./workspace` 내 파일의 **끝에** 텍스트를 그대로 덧붙입니다(v0.8.0). 도구 호출의 인자도 응답 한 번의 출력 한도(`max_tokens`)를 함께 쓰므로, 한 번에 보낼 수 없는 긴 파일은 첫 부분을 `write_workspace_file`로 쓰고 나머지를 이 도구로 순서대로 덧붙입니다. 파일이 없으면 새로 만들고, 조각 사이에 아무것도 끼워 넣지 않습니다. 응답에는 파일 끝 8줄이 줄 번호와 함께 실려 다음 조각을 이어 쓸 위치를 확인할 수 있습니다. 경로 제한과 크기 상한은 `write_workspace_file`과 같으며, 빈 내용은 거부합니다. ([상세 매뉴얼](#대용량-파일-분할-작성-매뉴얼-append_workspace_file))
- **`install_python_packages(packages: str, namespace: str)`** — 사내 PyPI 미러 서버로부터 패키지를 설치합니다. `packages` 인자에는 공백이나 쉼표로 구분된 패키지명을 전달하며, 버전 명시도 가능합니다(예: `"polars, rapidfuzz>=3.0"`). 임의의 URL, 파일 경로, editable 옵션, 기타 pip 플래그는 보안을 위해 거부되며, 인덱스 주소를 비롯한 pip 설정은 서버 환경 설정값으로 안전하게 고정됩니다. 패키지는 **번들된 포터블 Python이 아닌 별도의 격리 가상환경**(`SANDBOX_VENV`)에 설치되므로, 커널 재시작 없이도 모든 활성 네임스페이스에서 즉시 import하여 활용할 수 있습니다(기존에 로드된 메모리 데이터도 유지됩니다). 응답은 `install_status: INSTALLED` / `FAILED` / `RUNNING` 상태값으로 시작하며, `INSTALLED`는 pip의 반환 코드뿐만 아니라 **커널이 직접 해당 패키지의 실제 import 및 버전을 확인**했음을 의미합니다. 설치 작업은 백그라운드 스레드에서 진행되며 도구는 항상 `SANDBOX_TOOL_BUDGET` 제한 시간 내에 응답하므로, 미러 응답이 지연되더라도 클라이언트 측 타임아웃으로 인한 연결 종료가 발생하지 않습니다. 시간 내에 작업이 완료되지 않은 경우에는 실패가 아닌 `RUNNING` 상태를 반환하며, 동일한 인자로 다시 도구를 호출하여 최종 결과를 확인하도록 안내합니다. 설치 실패 시에는 실패 원인을 명확한 문장으로 설명해 드립니다. 특히 가장 빈번한 원인인 "사내 미러 미설정"의 경우, 인덱스 접속 상태를 확인한 후 수정해야 할 환경 변수명을 친절히 짚어 줍니다.
- **`list_workspace_files()`** — `./workspace` 디렉터리에 존재하는 업로드 파일 및 산출물 파일 목록을 파일 크기, 확장자 형식 정보와 함께 반환합니다(모든 네임스페이스가 공용으로 참조합니다).
- **`reset_kernel_state(namespace: str)`** — 지정한 네임스페이스의 커널만 단독으로 재시작하여 해당 대화의 메모리 상태를 초기화합니다(workspace 내 파일 및 타 네임스페이스 커널 상태는 유지됩니다).

> **네임스페이스(대화별 격리):** 단일 MCP 서버 프로세스를 여러 AnythingLLM 대화 세션이 동시에 공유할 수 있으므로, 각 실행 세션은 `namespace`로 분리된 개별 커널 인스턴스에서 구동됩니다. LLM은 최초 호출 시 `namespace="new"`를 전달하며, 응답으로 수신한 `active_namespace: ns-XXXX` 값을 후속 호출 시 그대로 전달하여 고유한 변수 및 데이터프레임 상태를 유지합니다. 유휴(idle) 상태의 네임스페이스 커널은 `SANDBOX_NS_IDLE_TIMEOUT`(기본 30분) 경과 후 자동으로 회수되며, 동시 구동 커널 수는 `SANDBOX_MAX_NAMESPACES`(기본 8개)로 안전하게 제한됩니다. 자세한 아키텍처 배경은 `wiki/09-namespace-routing.md` 문서를 참고하시기 바랍니다.

도구 설명(description 메타데이터)에는 LLM이 준수해야 할 필수 운영 규칙(`pip` 직접 실행 금지, 결과 확인을 위해 항상 `print()` 사용, 시각화 시 `plt.show()` 대신 `plt.savefig()`로 저장, 단순 상대 경로 사용, 오류 발생 시 능동적 자가 수정 등)이 사전에 내장되어 있습니다.

---

## 설치 방법

### 1. 포터블 Python 준비 (커널 인터프리터)

커널용 4GB 포터블 Python 배포판에는 데이터 사이언스 분석 패키지 일체 **및** `ipykernel`이 포함되어 있어야 합니다. 다음 주요 패키지가 사전에 구비되어 있어야 합니다:
`pandas`, `numpy`, `scipy`, `sympy`, `matplotlib`, `seaborn`, `pdfplumber`, `pypdf`, `openpyxl`, `xlsxwriter`, `xlrd`, `lxml`, `xmltodict`, `python-docx`, `markdown`, `beautifulsoup4`.
또한 커널 프로세스 구동의 핵심이 되는 `ipykernel` 역시 반드시 설치되어 있어야 합니다.

### 2. 서버 구동용 의존성 설치 (오프라인 환경)

`server.py`를 구동하는 Python 환경에는 `mcp`, `jupyter_client`, `ipykernel` 패키지가 필요합니다. 가장 단순하고 권장되는 구성 방식은 **포터블 Python 환경 자체로 `server.py`를 구동**하면서 위 3개 패키지를 해당 인터프리터에 설치하는 것입니다.

본 저장소에는 `offline_wheels/` 디렉터리에 **Python 3.11 / 3.12 / 3.13 (win_amd64)용 휠(.whl) 파일이 모두 사전에 번들**되어 있으므로, 폐쇄망(에어갭) 호스트에서도 외부 인터넷 연결 없이 즉시 오프라인 설치가 가능합니다:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_offline.ps1 -Python "C:\path\to\portable-python\python.exe"
```

(내부적으로 `pip install --no-index --find-links offline_wheels -r requirements-server.txt` 명령이 실행되며, 대상 Python 버전에 부합하는 휠 파일이 자동으로 선택됩니다. 대상 Python 환경에 `pip` 모듈이 구비되어 있어야 합니다.)

> 대상 Python 버전이 3.11~3.13 버전이 아니거나 아키텍처가 `win_amd64`가 아닌 경우에는, 인터넷 연결이 가능한 PC에서 다음 명령어를 실행하여 호환 휠 파일을 사전에 내려받으시기 바랍니다:
> `pip download -r requirements-server.txt -d .\offline_wheels`
>
> 만약 포터블 Python 환경이 읽기 전용 등으로 완전히 고정(frozen)되어 추가 패키지 설치가 불가능하다면, 해당 3개 패키지가 설치된 별도의 Python 환경으로 서버를 구동하고 `SANDBOX_KERNEL_PYTHON` 환경 변수가 포터블 Python 경로를 가리키도록 설정하시기 바랍니다.

### 3. 실행 환경 사전 검증

다음 스크립트를 실행하여 환경 구성을 검증합니다:

```powershell
<portable-python>\python.exe check_environment.py
```

출력 결과의 마지막 줄에 `RESULT: PASS`가 표시되어야 합니다. 폐쇄망 환경에서는 런타임에 누락된 패키지를 인터넷을 통해 설치할 수 없으므로, **필수(CRITICAL)** 패키지의 누락 여부를 반드시 사전에 점검하시기 바랍니다.

### 4. AnythingLLM 연동 등록

`anythingllm_mcp_config.example.json` 파일의 내용을 AnythingLLM의 MCP 서버 설정에 등록하십시오
(**Settings → Agent Skills → MCP Servers** 메뉴를 이용하거나, `~/AnythingLLM/plugins/anythingllm_mcp_servers.json` 설정 파일을 직접 편집합니다).
설정 내 두 곳의 절대 경로를 실제 시스템 경로에 맞게 수정한 후 에이전트를 재시작하십시오. 이후 워크스페이스의 에이전트 스킬 설정 화면에서 해당 도구들을 활성화해 주시면 됩니다.

아래 설정 예시에서 `<BASE>`는 프로젝트 압축 해제 루트 디렉터리를 의미합니다(예: `D:\AirGappedPySandbox`). 포터블 Python 경로는 `<BASE>\WPy64-313130\python\python.exe`이며, MCP 서버 코드는 `<BASE>\mcp-server\server.py`에 위치합니다.

```jsonc
{
  "mcpServers": {
    "air-gapped-python-sandbox": {
      "command": "D:\\AirGappedPySandbox\\WPy64-313130\\python\\python.exe",
      "args": ["D:\\AirGappedPySandbox\\mcp-server\\server.py"],
      "env": {
        "SANDBOX_KERNEL_PYTHON": "D:\\AirGappedPySandbox\\WPy64-313130\\python\\python.exe",
        "SANDBOX_WORKSPACE": "D:\\AirGappedPySandbox\\mcp-server\\workspace",
        "SANDBOX_EXEC_TIMEOUT": "45"
      }
    }
  }
}
```

---

## 환경 변수 설정 안내

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `SANDBOX_KERNEL_PYTHON` | 서버 자체의 `python` | 커널 코드를 실제로 실행할 Python 인터프리터 경로입니다(포터블 Python 경로를 지정합니다). |
| `SANDBOX_WORKSPACE` | `./workspace` | 파일 입출력 및 산출물이 격리 보관되는 디렉터리 경로입니다. |
| `SANDBOX_TOOL_BUDGET` | `45` | **단일 도구 호출이 응답을 지연할 수 있는 최대 대기 시간(초)입니다.** MCP 클라이언트는 자체 타임아웃(공식 SDK 기본값 60초)을 초과한 요청을 일방적으로 취소하며, AnythingLLM에서는 이를 `Context server request timeout`으로 표시합니다. 이 값을 클라이언트 타임아웃보다 낮게 유지해야 클라이언트 측 강제 종료 대신 서버가 제공하는 상세한 원인 및 후속 조치 안내가 정상 반환됩니다. |
| `SANDBOX_EXEC_TIMEOUT` | `45` | 단일 호출당 코드 실행 제한 시간(초)입니다. 반드시 `SANDBOX_TOOL_BUDGET` 이하로 설정해야 합니다. (이전 기본값인 60초는 클라이언트 측 타임아웃과 경합하여 원인 파악이 어려운 클라이언트 취소 오류를 유발할 수 있었습니다.) |
| `SANDBOX_STARTUP_TIMEOUT` | `60` | 커널 최초 구동(부팅) 제한 시간(초)입니다. |
| `SANDBOX_MAX_STREAM_CHARS` | `20000` | 반환할 stdout 및 stderr 문자열의 최대 길이입니다(0으로 설정 시 무제한). |
| `SANDBOX_MAX_WRITE_BYTES` | `1048576` | `write_workspace_file`·`append_workspace_file` 도구의 1회 쓰기 최대 크기(바이트)입니다(0으로 설정 시 무제한). |
| `SANDBOX_NS_IDLE_TIMEOUT` | `1800` | 사용되지 않는 유휴 네임스페이스 커널을 자동 회수하기까지의 대기 시간(초)입니다. |
| `SANDBOX_MAX_NAMESPACES` | `8` | 동시에 유지할 수 있는 네임스페이스 커널의 최대 개수입니다(초과 시 LRU 알고리즘으로 회수). |
| `SANDBOX_LAZY_START` | 미설정 | 설정할 경우(truthy), 서버 시작 시점에 예열용 커널을 미리 구동하지 않고 첫 요청 시 구동합니다. |
| `SANDBOX_PIP_INDEX_URL` | 미설정 | **사내 PyPI 미러 서버 주소**입니다(예: `http://pypi.사내/simple`). 설정하지 않으면 기본 pip 설정(pip.ini)이나 외부 공식 PyPI에 접근하게 되어 폐쇄망 환경에서는 패키지 설치가 실패합니다. |
| `SANDBOX_PIP_TRUSTED_HOST` | 미설정 | 사내 미러 서버가 평문 HTTP이거나 자체 사설 CA 인증서를 사용할 때 지정하는 신뢰 호스트명입니다. 누락 시 pip가 해당 인덱스를 **보안 문제로 무시**합니다. |
| `SANDBOX_VENV` | `./.sandbox-venv` | 새로 설치된 패키지가 격리 저장되는 가상환경 경로입니다. 이 디렉터리만 삭제하면 패키지 설치 내역을 초기 상태로 손쉽게 되돌릴 수 있습니다. |
| `SANDBOX_PIP_TIMEOUT` | `300` | 패키지 설치 1회당 최대 허용 시간(초)입니다. |
| `SANDBOX_PIP_NETWORK_TIMEOUT` | `15` | pip가 미러 인덱스 서버 응답을 대기하는 시간(초)입니다. |
| `SANDBOX_PIP_RETRIES` | `0` | pip 연결 재시도 횟수입니다. 폐쇄망 환경에서 미러 미설정 시 불필요한 장시간 대기를 방지하기 위해 기본값 0으로 설정되어 즉시 실패를 보고합니다. |
| `SANDBOX_MAX_PACKAGES_PER_CALL` | `20` | 단일 설치 요청에서 한 번에 처리할 수 있는 최대 패키지 개수입니다. |

## 샌드박스 파일 공유 및 사용 방법

분석 대상인 PDF, Excel, CSV 등의 문서를 `workspace/` 디렉터리(AnythingLLM 설정에서 `SANDBOX_WORKSPACE`로 지정한 폴더)에 배치하십시오. 커널의 기본 작업 디렉터리가 해당 폴더로 자동 설정되므로, LLM은 `list_workspace_files` 도구로 파일 목록을 파악한 후 `./workspace/` 접두사 없이 `data.xlsx`와 같은 단순 상대 경로로 직접 접근할 수 있습니다. 코드 실행을 통해 새로 생성된 결과 파일 역시 동일한 폴더에 저장되며, 사용자에게는 Markdown 다운로드 링크 형태로 반환됩니다.

## 대용량 파일 분할 작성 매뉴얼 (`append_workspace_file`)

`append_workspace_file`은 한 번의 LLM 응답 출력 토큰 한도(`max_tokens`)를 초과하는 대규모 소스 코드(Python 스크립트, React/TSX 컴포넌트, HTML 보고서 등)나 장문 문서를 안전하게 조각(Chunk) 단위로 이어 붙여 완성할 수 있도록 설계된 전용 도구입니다 (v0.8.0, [ADR-022](lectures/adr/adr-022-append-tool.md)).

### 1. 도입 배경 및 필요성
- **출력 토큰(`max_tokens`) 제약 극복:** LLM이 도구를 호출할 때 전달하는 인자(`content`)는 모델의 1회 응답 출력 한도에 포함됩니다. 1,000줄 이상의 방대한 코드나 보고서를 한 번에 생성하려고 하면 인자가 도중에 잘려 구문 오류나 도구 호출 실패가 발생합니다.
- **덮어쓰기(`write_workspace_file`) 반복의 한계:** `overwrite=True`를 이용해 기존 내용을 포함한 채 매번 전체 파일을 다시 덮어쓰는 방식을 취하면, 회차가 거듭될수록 전송해야 할 페이로드가 계속 누적되어 결국 동일한 출력 한도에 도달하게 됩니다.
- **단일 책임 도구 분리:** `write_workspace_file`에 `append` 플래그를 추가하는 대신 별도의 `append_workspace_file` 도구로 분리함으로써, `overwrite`와 `append` 옵션이 충돌하거나 소형 모델이 모드를 오인하여 호출하는 위험을 원천 차단했습니다.

### 2. 도구 시그니처 및 매개변수

```python
append_workspace_file(filename: str, content: str) -> str
```

- **`filename: str`**
  - `./workspace` 기준의 파일 상대 경로입니다 (예: `long_analysis.py`, `reports/summary.md`).
  - 접두사(`./workspace/`, `workspace/`)는 자동으로 정규화됩니다.
  - 상위 디렉터리 탐색(`..`), 드라이브 절대 경로, Windows 예약 장치명(`CON`, `PRN`, `AUX`, `NUL`, `COM1~9`, `LPT1~9`) 등은 시스템 격리 및 안전을 위해 엄격히 차단됩니다.
- **`content: str`**
  - 파일 끝에 덧붙일 UTF-8 텍스트 원문(verbatim)입니다.
  - 빈 문자열(`""`)은 유의미한 쓰기 작업이 아니므로 오류(`write_status: ERROR`)로 거부됩니다.
- **`namespace` 불필요:**
  - 본 도구는 영속 커널 메모리를 점유하거나 Python 코드를 실행하지 않는 순수 파일 입출력 도구이므로 `namespace` 인자가 필요하지 않습니다.

### 3. 반환 메시지 및 꼬리(Tail) 피드백

도구 호출이 성공하면 파일 전체를 반환하는 대신, 컨텍스트 절약을 위해 **파일 끝 8줄(Tail)**을 줄 번호와 함께 반환합니다:

```text
write_status: SUCCESS

--- file ---
./workspace/data_pipeline.py  (appended 1450 B; now 4820 B, 156 lines)

--- tail (last 8 lines) ---
149: def transform_features(df):
150:     # Feature normalization and scaling
151:     df['norm_val'] = (df['val'] - df['val'].mean()) / df['val'].std()
152:     return df
153: 
154: if __name__ == '__main__':
155:     main()
156: 

--- artifacts ---
[data_pipeline.py](workspace/data_pipeline.py)  (4.7 KB)

Next: run it with run_python_file(file_path="data_pipeline.py", namespace=<your namespace>).
```

- **꼬리 미리보기 (`--- tail ---`):** 파일 전체를 반환하면 클라이언트의 수신 컨텍스트를 불필요하게 낭비하므로, 마지막 8줄만 번호와 함께 제공하여 모델과 사용자가 직전 작성 위치(들여쓰기, 괄호 닫힘 상태 등)를 확인하고 다음 청크를 안전하게 이어 쓸 수 있도록 안내합니다.
- **줄바꿈 누락 주의 안내:** 직전 청크 끝이 줄바꿈(`\n`) 없이 끝난 상태에서 새 청크가 바로 이어 붙은 경우, 아래와 같은 주의 메시지가 응답에 함께 출력됩니다:
  > `note: the file did not end with a newline, so this chunk continued its last line. Start the next chunk with a newline if that was not intended.`

### 4. 표준 분할 작성 워크플로우

긴 파일을 안정적으로 작성할 때는 다음 4단계 패턴을 권장합니다:

1. **1단계 (초기화 및 1차 청크 작성):**
   - 새 파일을 작성할 때는 반드시 **`write_workspace_file`**에 `overwrite=True`를 명시하여 시작합니다.
   - *주의:* 대상 파일이 없으면 `append_workspace_file`도 새 파일을 생성하지만, 이전에 실패한 잔여 파일이나 동일한 이름의 파일이 이미 존재할 경우 그 뒤에 내용이 그대로 이어 붙으므로, 새로 작성할 때는 반드시 `write_workspace_file`로 시작해야 합니다.
   ```text
   write_workspace_file(
       filename="data_pipeline.py",
       content="# Part 1: Imports & Setup\nimport pandas as pd\nimport numpy as np\n\n",
       overwrite=True
   )
   ```
2. **2단계 (후속 청크 순차 전송):**
   - 이어질 내용을 적절한 크기(권장: 100~300줄 또는 2,000~4,000 토큰 단위)로 나누어 **`append_workspace_file`**을 순차적으로 호출합니다.
   ```text
   append_workspace_file(
       filename="data_pipeline.py",
       content="# Part 2: Data Extraction\ndef extract_records():\n    return pd.read_csv('raw.csv')\n\n"
   )
   ```
3. **3단계 (꼬리 확인 및 자가 점검):**
   - 매 호출 응답의 `--- tail ---` 블록을 확인하여 마지막 줄 번호, 함수/클래스 블록 연결 상태를 점검한 후 다음 청크를 전송합니다.
4. **4단계 (완성 및 실행 검증):**
   - 작성이 완료된 스크립트(`.py`)는 `run_python_file(file_path="data_pipeline.py", namespace=...)` 도구를 호출하여 세션 네임스페이스의 영속 커널에서 실행합니다.

### 5. 핵심 주의사항 요약
- **명시적 개행(`\n`) 포함:** 서버는 청크 사이에 줄바꿈을 임의로 끼워 넣지 않습니다(있는 그대로 연결). 따라서 줄 단위 구분이 필요한 경우 각 청크의 끝에 반드시 `\n`을 포함해야 합니다.
- **재작업 시 초기화:** 작업 중단 등으로 인해 파일을 처음부터 다시 작성해야 할 때는 반드시 `write_workspace_file(overwrite=True)`로 시작하십시오.
- **1회 전송 크기 상한:** 1회 전송 크기는 `SANDBOX_MAX_WRITE_BYTES`(기본값 1 MB)로 제한되며, 초과 시 오류와 함께 청크를 더 작게 나누라는 안내가 반환됩니다.
- **중복 전송 주의:** 네트워크 재시도 등으로 동일한 조각을 2회 호출하면 파일 끝에 그대로 중복 추가됩니다. 응답의 누적 바이트 수와 행 번호(`now X B, Y lines`)를 반드시 확인하십시오.

## 타임아웃 및 장애 복구 동작

`SANDBOX_EXEC_TIMEOUT` 제한 시간을 초과한 코드 셀은 실행이 강제 인터럽트(중단)됩니다. 인터럽트 후 커널이 유휴(idle) 상태로 정상 복귀하면 기존 메모리 **상태를 그대로 유지한 채** 후속 작업에 재사용됩니다. 만약 Windows 환경에서 시그널 인터럽트가 개입할 수 없는 장기 `time.sleep` 등 C 레벨 블로킹 호출로 인해 커널 멈춤(wedged) 상태가 지속되는 경우에는, 커널을 **자동 재시작**(메모리 초기화)하여 다음 요청이 항상 정상적으로 응답 가능한 커널을 할당받도록 보장합니다. 최종 응답 메시지에는 이러한 복구 과정 중 어떤 조치가 취해졌는지 명확하게 표시됩니다.

## 테스트 및 검증 방법

다음 명령어를 통해 핵심 기능 및 환경 구성 상태를 사전에 검증할 수 있습니다:

```powershell
# 커널 및 아티팩트 계층 스모크 테스트 (jupyter_client, ipykernel, pandas, matplotlib 필요):
<python> test_core.py

# 포터블 Python 환경 구성 및 필수 패키지 점검:
<portable-python>\python.exe check_environment.py
```

## 보안 및 격리 안내

본 시스템은 엄격한 커널 레벨 격리 감옥(jail)이 아닌, **규약 기반(by convention) 샌드박스**입니다. 실행되는 코드는 MCP 서버 프로세스와 동일한 운영체제 권한을 가집니다. 시스템 보호를 위한 기본 완화책으로 workspace 디렉터리 내 상대 경로 입출력 강제, 자동 복구 기능이 결합된 실행 타임아웃, 출력 버퍼 크기 제한, 그리고 외부 네트워크가 차단된 에어갭 환경 자체의 특성을 활용하고 있습니다. 보다 엄격한 보안 격리가 요구되는 환경이라면 MCP 서버 전체를 격리된 컨테이너나 권한이 제한된 전용 OS 사용자 계정에서 구동하시기 바랍니다.

---

## 버전 이력

본 프로젝트는 [유의적 버전(SemVer)](https://semver.org/lang/ko/) 규약을 준수합니다. 정식 1.0 릴리스 이전인 0.x 버전 단계에서는 세부 인터페이스나 동작 방식이 일부 조정될 수 있습니다.

| 버전 | 변경 내용 |
|------|-----------|
| **v0.8.0** | `append_workspace_file` 도구 추가: 응답 한 번의 출력 한도보다 긴 파일을 쌓을 수 있도록, 첫 부분은 `write_workspace_file`로 쓰고 나머지는 조각 단위로 파일 끝에 덧붙이는 도구를 추가했습니다. 내용은 받은 그대로 붙이며(조각 사이에 아무것도 넣지 않음) 파일이 없으면 새로 만듭니다. 응답에 파일 끝 8줄을 줄 번호와 함께 돌려주어 다음 조각을 이어 쓸 위치를 확인할 수 있습니다. 경로 제한과 크기 상한은 저장 도구와 같고 빈 내용은 거부합니다. 저장 도구에 인자를 더하지 않고 별도 도구로 둔 이유는 ADR-022에 정리했습니다. |
| v0.7.0 | MCP 클라이언트 타임아웃 대응: 패키지 설치 작업을 백그라운드 스레드로 분리하고, 도구가 `SANDBOX_TOOL_BUDGET`(45초) 내에 `RUNNING` 상태로 즉시 응답하도록 개선했습니다. 격리 가상환경(venv)을 서버 시작 시 사전 생성하도록 최적화하였으며, `SANDBOX_EXEC_TIMEOUT` 기본값을 기존 60초에서 45초로 단축했습니다. |
| v0.6.0 | `install_python_packages` 도구 추가: 사내 PyPI 미러를 통해 별도 격리 가상환경(venv)에 패키지를 설치하는 기능을 도입했습니다. 커널이 직접 import를 수행하여 정상 설치 여부를 교차 검증하며, 미러 미설정·네트워크 단절·평문 HTTP 무시 등 실패 원인을 정밀하게 구분하여 안내합니다. |
| v0.5.0 | 코드 실행 판정 체계 전면 개편: 모델의 착각을 유발하는 모호한 'SUCCESS' 판정을 제거했습니다. 명시적 예외 없이 실패하는 케이스(백그라운드 스레드 크래시, 서브프로세스 비정상 종료, 삼켜진 트레이스백 등)의 탐지 로직을 추가하고, `sys.exit(0)` 오판 수정 및 `run_python_file` 실행 후 커널 상태 보존 처리를 적용했습니다. |
| v0.4.1 | 워크스페이스 경로 해석 개선: 커널의 작업 디렉터리를 `SANDBOX_WORKSPACE` 내부로 일원화하여, 실행 코드가 `./workspace/` 접두사 없이 단순 상대 경로로 파일에 접근할 수 있도록 수정했습니다. 상대 경로 지정 시 호스트 작업 디렉터리가 아닌 프로젝트 루트를 기준으로 해석하도록 보완하였으며, 프로젝트 외부 워크스페이스 환경에서도 `run_python_file` 파일 해석 및 결과 링크가 정상 작동하도록 개선했습니다. |
| v0.4.0 | `write_workspace_file` 도구 추가: 별도의 코드 실행 없이 텍스트 및 파일을 `./workspace`에 직접 저장할 수 있는 도구를 추가했습니다(경로 탈출 차단, 덮어쓰기 방지, 파일 크기 상한 적용). 소형 모델에서 빈번히 실패하던 '자기 코드를 참조하여 파일 작성 코드를 생성하는 패턴'을 근본적으로 제거했습니다. 아울러 `run_python_file` 호출 시 `.py` 확장자가 아닌 파일은 커널 전달 전에 사전 차단하도록 보완했습니다. |
| v0.3.0 | 대화 세션별 **네임스페이스 라우팅**(필수 `namespace` 인자) 도입: 단일 서버 프로세스를 다수의 AnythingLLM 대화 세션이 공유할 때 발생하는 변수 오염 및 충돌을 방지했습니다. 네임스페이스별 독립 커널 풀(사전 예열 웜 리저브, 유휴 자원 자동 회수, LRU 기반 상한 관리), 응답 배너 에코, 향후 `_meta` 대화 ID 연동 훅을 마련했습니다. |
| v0.2.0 | `run_python_file` 도구 추가: 워크스페이스 내의 독립된 `.py` 스크립트 파일을 직접 구동할 수 있는 기능을 추가했습니다. |
| v0.1.0 | 초기 기능 릴리스: 상태 유지형 IPython 커널 코어, 산출물 자동 감지 인터셉터, FastMCP 기반 도구 연동, 포터블 배포판(WinPython 3.13) 구성 및 오프라인 환경 검증 체계를 구축했습니다. |

---

## 라이선스 (License)

본 프로젝트는 [MIT License](LICENSE)에 따라 배포됩니다. 자세한 내용은 [`LICENSE`](LICENSE) 파일을 참고하십시오.
