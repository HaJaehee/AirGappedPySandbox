# 11 — 실행 판정(Execution Verdict) 개선 계획

> **상태: Phase 1·2·4 구현 완료 (v0.5.0). Phase 3(`expect` 검증)은 미구현.**
> 작성 2026-09-22 · 진단 기준 v0.4.1 · 대상 파일: `kernel_manager.py`, `server.py`, `test_core.py`
>
> §1 의 측정표는 **수정 전(v0.4.1)** 의 동작이다. 수정 후 동작은 §6 을 보라.
>
> 배경: `execute_python_code` / `run_python_file` 가 "파이썬이 예외 없이 끝나기만 하면"
> `SUCCESS` 를 돌려주기 때문에, 약한 내부 LLM(sLLM)이 **코드 실행을 성공한 것으로 착각**한다.

---

## 1. 측정 결과 — 무엇이 실제로 깨져 있나

`kernel_manager.py` 의 `_execute_locked()` 는 iopub 에 `error` 메시지가 왔거나 타임아웃일
때만 status 를 바꾼다. 그 외는 전부 `SUCCESS` 다. 실제 커널을 띄워 실패 모양별로 측정한 결과
(재현 스크립트는 부록 A):

| 실행 코드 | 현재 status | 실제 |
|---|---|---|
| `try: 1/0 / except: print('handled')` | SUCCESS | 원리상 관측 불가 (모델 책임) |
| `logging.error('db unreachable')` | **SUCCESS** | stderr 에 에러가 찍혔는데 성공 |
| `traceback.print_exc()` | **SUCCESS** | stderr 에 풀 트레이스백인데 성공 |
| 백그라운드 스레드에서 예외 | **SUCCESS** | stderr 에 트레이스백인데 성공 |
| `subprocess.run(...)` → rc=3 | **SUCCESS** | 종료코드 무시 |
| `print('FAIL: expected 5')` | **SUCCESS** | 테스트가 깨졌는데 성공 |
| `x = 1` (출력·산출물 전무) | **SUCCESS** | 아무 일도 안 했는데 성공 |
| `sys.exit(0)` | **ERROR** | ← 반대 방향 오판 |
| `sys.exit(1)` | ERROR | 맞지만 종료코드 1/2 구분 불가 |

### 1.1. `run_python_file` 의 이중 결함

마지막 두 줄이 `run_python_file` 에서 특히 아프다. `if __name__ == '__main__': sys.exit(main())`
으로 끝나는 평범한 스크립트는

1. **성공해도 `ERROR` 로 보고**되고,
2. `server.py` 의 runpy 래퍼에서 `SystemExit` 이 `globals().update(...)` **앞에서** 터지므로
   **스크립트의 변수·함수가 커널에 남지 않는다.**

측정: `ok_exit0.py` 실행 후 `print('main' in dir())` → `False`.
도구 설명이 약속한 *"The file's top-level variables and functions are kept in memory afterwards"*
가 지켜지지 않고 있다.

### 1.2. 근본 원인

> **`status` 는 "인터프리터가 예외 없이 끝났는가" 만 뜻하는데, 이름이 `SUCCESS` 라서
> sLLM 은 이를 "과업 성공" 으로 읽는다.**

---

## 2. 설계 원칙

1. **"실행됨(ran)" 과 "성공함(succeeded)" 을 분리한다.** 서버는 전자만 알 수 있다.
2. **서버가 모르는 것을 `SUCCESS` 라고 말하지 않는다.** 기본값은 "미검증" 이어야 한다.
3. 규칙 문장(`_RULES` 의 "5. SELF-CORRECT")으로 약한 모델을 설득하려 하지 말고,
   **상태값 자체로 강제**한다. 약한 모델은 규칙을 읽지 않지만 첫 줄은 읽는다.

---

## 3. 단계별 계획

### Phase 1 — 판정 로직 교체 (`kernel_manager.py`) — ✅ 구현됨

`ExecResult` 에 `exit_code: int | None`, `signals: list[str]` 추가. 판정 순서:

1. `error` 메시지 중 `ename != "SystemExit"` → `FAILED`
2. `SystemExit` → `evalue` 에서 종료코드 파싱
   - `0` / `None` → 정상 종료로 되돌리고, stderr 에서
     `"An exception has occurred, use %tb to see the full traceback."` 잡음 제거
   - `0` 이 아님 → `FAILED (exit code N)`
3. 타임아웃 → `TIMEOUT` (현행 유지)
4. 그 외 → `RAN`, Phase 2 신호로 등급 조정

`server.py` 의 `run_python_file` 래퍼 코드는 `try/except SystemExit` 로 감싸 종료코드를
변수에 담고, `globals().update(...)` 를 **`finally` 로 옮겨** 종료코드와 무관하게 상태가
보존되게 한다. (§1.1 의 두 번째 결함 해소)

### Phase 2 — 관측 가능한 실패 신호 수집 — ✅ 구현됨

정규식 추측이 아니라 **결정적으로 잡을 수 있는 것**부터:

- **사건 장부(incident ledger)** — `_STARTUP_CODE` 에 `threading.excepthook` 과
  `subprocess.run` / `Popen` 래퍼를 심어 백그라운드 예외와 0 아닌 종료코드를
  `_sandbox_incidents` 리스트에 기록. 셀 실행 직후 서버가 조용한 프로브 셀로 이 리스트를
  비우며 읽어온다 → `RAN_WITH_ERRORS`
- **stderr 트레이스백 / `ERROR:` · `CRITICAL:` 로그 감지** → `RAN_WITH_ERRORS`
- **빈 실행 감지** — stdout · `result_repr` · artifact 가 전부 없음 → `RAN_NO_OUTPUT`
  (현재는 SUCCESS + 안내 문구뿐이라 무시당한다)
- stdout 의 `FAIL` / `Traceback` / `AssertionError` 마커는 **상태를 바꾸지 않고**
  힌트 줄로만 노출 (오탐 억제)

### Phase 3 — 증거 기반 검증 (핵심 처방) — ❌ 미구현 (별도 릴리스)

`execute_python_code` / `run_python_file` 에 선택 인자 `expect: str = ""` 추가.
모델이 성공 조건을 파이썬 식으로 적으면 (`assert len(df) == 1200`,
`Path('chart.png').exists()`) 서버가 본 셀 직후 **별도 셀로 평가**해
`VERIFIED_OK` / `FAILED (check failed)` / `CHECK_ERROR` 를 판정한다.

`expect` 가 비어 있으면 최상위 상태는 **`RAN_UNVERIFIED`**.
즉 **sLLM 은 서버로부터 "성공" 이라는 단어를 공짜로 얻을 수 없고**, 받으려면 검증식을
제출해야 한다. `_RULES` 에 한 줄짜리 템플릿을 넣어 약한 모델도 따라 쓰게 한다.

### Phase 4 — 응답 포맷 (착각 유발 지점 제거) — ✅ 구현됨

- 상태 어휘: `VERIFIED_OK` / `RAN_UNVERIFIED` / `RAN_NO_OUTPUT` / `RAN_WITH_ERRORS` /
  `FAILED` / `TIMEOUT`.
  **`SUCCESS` 토큰은 Phase 3 검증을 통과했을 때만 등장**시킨다.
- sLLM 은 응답의 처음과 끝에 앵커링하므로, **첫 줄 판정 + 마지막 줄 `next_action:`**
  명령형 지시를 넣는다.
  예: `next_action: 이 결과를 성공으로 보고하지 마십시오. stderr 의 트레이스백을 읽고 수정 후 같은 namespace 로 재실행하십시오.`
- stderr 블록을 stdout **앞으로** 올려 판정 근거가 바로 보이게 재배치
  (`server.py` 의 `_format_response`)
- `execution_status` **키 이름은 유지**하고 값만 확장 → 기존 AnythingLLM 프롬프트·문서 호환

### Phase 5 — 테스트와 문서 — ✅ Phase 1·2·4 범위만

- `test_core.py` 에 §1 의 9개 케이스를 판정 매트릭스 테스트로 추가 (부록 A 를 이식)
- 갱신 대상: `wiki/03-mcp-tools.md`, `wiki/tool-specs.xml`,
  `wiki/07-decisions-and-gotchas.md`, `wiki/06-testing-verification.md`,
  `CLAUDE.md` §4 의 output schema, `README.md` 버전 히스토리
- `bump_version.py` 로 **0.5.0**
- **동기화 잊지 말 것**: 저장소 루트 `*.py` 수정 후
  `dist/AirGappedPySandbox/mcp-server/` 복사본에 반영 (wiki README 골든룰 2번).
  `D:\MultiAgentOrchestrator\dist\multiagentorchestrator_bundle\mcp_sandbox\` 에도
  같은 사본이 있다.

---

## 4. 우선순위와 트레이드오프

**Phase 1·2·4 가 비용 대비 효과가 가장 크다.** 서버 내부만 고치므로 도구 스키마가 바뀌지
않아 AnythingLLM 재설정이 필요 없고, `sys.exit(0)` 오판과 상태 소실 버그까지 같이 잡힌다.
Phase 3 은 스키마 변경이라 별도 릴리스로 분리 가능.

**주의할 위험은 과잉 경보다.** `RAN_WITH_ERRORS` 를 "실패" 로 단정하면
(`logging.warning` 수준에도) sLLM 이 무한 재시도 루프를 돌 수 있다. 그래서 이 상태의 문구는
"실패" 가 아니라 **"사용자에게 보고하기 전 확인 필요"** 로 정하고, `next_action` 에
재시도 1회 권고를 명시한다.

## 5. 범위 밖 (원리상 불가)

**모델이 `try/except` 로 삼켜버린 예외는 서버가 볼 수 없다.** 커널 밖에서는 관측 수단이
없다. 이것만은 Phase 3 의 `expect` 검증으로만 커버된다.

---

## 6. 구현 결과 (v0.5.0)

`_verdict()` 가 내리는 판정과, §1 의 각 케이스가 지금 어떻게 나오는지.
전부 `test_core.py` 의 `check_execution_verdicts()` / `check_run_python_file()` 로 고정되어 있다.

| 실행 코드 | v0.4.1 | v0.5.0 |
|---|---|---|
| `print('answer', 42)` | SUCCESS | `RAN_UNVERIFIED` |
| `x = 1` (출력·산출물 전무) | SUCCESS | `RAN_NO_OUTPUT` |
| `logging.error(...)` | SUCCESS | `RAN_WITH_ERRORS` |
| `traceback.print_exc()` | SUCCESS | `RAN_WITH_ERRORS` |
| 백그라운드 스레드 예외 | SUCCESS | `RAN_WITH_ERRORS` |
| `subprocess.run(...)` → rc≠0 | SUCCESS | `RAN_WITH_ERRORS` |
| `1/0` | ERROR | `FAILED` |
| `sys.exit(1)` | ERROR | `FAILED` |
| `sys.exit(0)` | **ERROR** | `RAN_UNVERIFIED` (+ stderr 잡음 제거) |
| `sys.exit(main())` 스크립트 실행 후 상태 | **소실** | 보존 |

구현 메모:

- **`SUCCESS` 라는 값은 더 이상 존재하지 않는다.** 샌드박스는 실패를 증명할 수 있어도 성공을
  증명할 수 없고, 약한 모델은 그 한 단어를 "과업 완료" 로 읽는다. 낙관적 판정의 상한은
  `RAN_UNVERIFIED` 다. Phase 3 이 들어오면 여기에 `VERIFIED_OK` 가 추가된다.
- **사건 장부**는 `_STARTUP_CODE` 가 `threading.excepthook` · `subprocess.run/call/Popen.wait` ·
  `os.system` 을 감싸 기록하고, `_PROBE_CODE` 가 매 실행 직후 비우며 읽어간다. 각 훅은 원본을
  그대로 호출하므로 모델이 보던 출력이 사라지지 않는다. 비용은 실행당 커널 왕복 1회.
- 장부는 **실패한 셀에서도 반드시 비운다.** 안 그러면 남은 항목이 다음 호출의 유령 실패로
  붙는다 (`incident ledger is drained between calls` 체크).
- `run_python_file` 은 `runpy.run_path` 를 버리고 소스를 커널 네임스페이스에 직접 `exec` 한다.
  IPython 사용자 네임스페이스는 이미 `__name__ == "__main__"` 이라 `__main__` 블록이 돌고,
  이름이 바인딩되는 즉시 남으므로 `sys.exit()` 나 중간 예외에도 상태가 보존된다.
- 스트림 순서가 바뀌었다: **stderr 가 stdout 위**. 판정 근거를 먼저 읽히기 위해서다.

남은 위험: 백그라운드 스레드가 프로브 **이후에** 터지면 그 사건은 다음 호출에 붙어 보고된다.
장부 항목에 트레이스백이 그대로 들어 있어 모델이 구분할 수는 있지만, 귀속은 한 칸 밀린다.

---

## 부록 A — 재현 스크립트

```python
import sys, os
sys.path.insert(0, r"D:\AirGappedPySandbox")
os.environ["SANDBOX_WORKSPACE"] = r"<임시 워크스페이스 경로>"
from kernel_manager import StatefulKernel

k = StatefulKernel(); k.start()
cases = {
 "caught exception": "try:\n    1/0\nexcept Exception as e:\n    print('handled:', e)\n",
 "logging error":    "import logging; logging.error('database unreachable')",
 "sys.exit(1)":      "import sys; sys.exit(1)",
 "sys.exit(0)":      "import sys; sys.exit(0)",
 "traceback print":  "import traceback\ntry:\n    raise ValueError('boom')\nexcept Exception:\n    traceback.print_exc()\n",
 "test fail print":  "ok = (2+2==5)\nprint('PASS' if ok else 'FAIL: expected 5')",
 "silent nothing":   "x = 1",
 "thread fail":      "import threading\ndef f(): raise RuntimeError('bg')\nt=threading.Thread(target=f); t.start(); t.join(); print('done')",
 "subprocess fail":  "import subprocess,sys\nr=subprocess.run([sys.executable,'-c','import sys;sys.exit(3)'])\nprint('rc ignored')",
}
for name, code in cases.items():
    r = k.execute(code, timeout=20)
    print(f"[{name}] status={r.status} err={r.error_name!r} "
          f"stdout={r.stdout.strip()[:60]!r} stderr={r.stderr.strip()[:80]!r}")
k.shutdown()
```

`run_python_file` 의 상태 소실은 워크스페이스에 아래 스크립트를 두고 runpy 래퍼로 실행한 뒤
`print('main' in dir())` 로 확인한다 (현재 결과: `False`).

```python
# ok_exit0.py
def main():
    print('all good')
    return 0
if __name__ == '__main__':
    import sys; sys.exit(main())
```
