# ADR-003. 서버 인터프리터와 커널 인터프리터를 분리한다

## 상태 (Status)

채택됨 (Accepted) · 2026-07-23, v0.1.0

관련 결정: [ADR-002](adr-002-persistent-jupyter-kernel.md), [ADR-004](adr-004-portable-python-bundle.md), [ADR-019](adr-019-side-venv-installs.md)

## 맥락 (Context)

MCP 서버 프로세스가 필요로 하는 패키지는 mcp, jupyter_client, ipykernel 세 개뿐이다. 반면 커널에는 pandas부터 PyMuPDF까지 3GB가 넘는 데이터 스택이 필요하다.

둘을 반드시 한 인터프리터에 묶어야 한다면, 서버를 돌릴 수 있는 Python이 곧 그 무거운 포터블 Python 하나로 제한된다. 이미 라이브러리가 갖춰진 Python이 있는데 거기에 서버 의존성을 설치할 수 없는 경우(완전히 고정된 배포판 등)에는 방법이 없어진다.

또 jupyter_client는 기본적으로 시스템에 등록된 커널 명세(kernelspec)를 찾아 커널을 띄운다. 폐쇄망 PC에 어떤 커널 명세가 등록되어 있을지 알 수 없고, 새로 등록하려면 설치 작업이 필요하다.

## 결정 (Decision)

커널을 띄울 인터프리터의 경로를 설정값(`SANDBOX_KERNEL_PYTHON`)으로 받는다. 커널 관리자가 시스템의 커널 명세를 찾는 대신, 그 경로로 만든 실행 명령(`<python> -m ipykernel_launcher -f <연결 파일>`)을 쓰도록 명세를 직접 지정한다. 설정하지 않으면 서버 자신의 인터프리터를 쓴다.

서버 코드는 데이터 라이브러리를 하나도 import하지 않는다.

## 결과 (Consequences)

**좋아진 점**

- 서버는 가볍게 유지된다. 이미 만들어진 Python 배포판을 설정 하나로 가리키기만 하면 된다.
- 시스템에 커널 명세를 등록할 필요가 없다.
- 배포 패키지에서는 서버와 커널이 같은 WinPython을 쓰므로 설정할 것이 없다. 분리는 필요할 때 꺼내 쓰는 선택지로 남아 있다. 예를 들어 대상 PC의 포터블 Python에 아무것도 설치할 수 없다면, 서버는 다른 Python으로 돌리고 커널만 그 포터블 Python을 가리키게 하면 된다.
- 나중에 추가 설치용 venv를 만들 때도 같은 설정값을 기준으로 삼아, 커널과 venv의 Python이 어긋나지 않게 했다([ADR-019](adr-019-side-venv-installs.md)).

**치른 비용**

- 커널 쪽 인터프리터에도 ipykernel은 반드시 있어야 한다. 사전 점검 도구가 커널을 실제로 띄워 보는 것은 이 때문이다.
- 커널 관리자의 공개 API가 아니라 내부 속성을 덮어써서 실행 명령을 바꾼다. jupyter_client를 업그레이드할 때 가장 먼저 확인해야 할 지점이다.
- 두 인터프리터가 다를 때 Jupyter 메시징 프로토콜 버전의 호환은 사람이 맞춰야 한다.
