# UTT · 연구와 창작의 서가

합성 자료만 사용하는 통합 읽기 프로그램입니다. 기존 Python 3.11+ 서버 / 브라우저 ES modules 구조와 A 검색·D-0 검토·D-1 이력 흐름을 참조해 구현했습니다. 기존 전체 프로그램을 복원한 것은 아닙니다. 실제 DB, Dropbox, Scrivener, 운영 캐시·인증 설정을 찾거나 열지 않습니다.

## 실행

현재 체크아웃 `/workspace/utt-interface`에서 실행합니다. 다른 PC에서는 저장소 루트를 사용하세요. 런타임 의존성은 **Python 3.11+ 표준 라이브러리와 현대 브라우저**뿐입니다. npm/pip 설치나 환경변수·DB 경로 입력이 필요 없습니다.

```sh
cd /workspace/utt-interface
PYTHONDONTWRITEBYTECODE=1 python3 -m app.server
```

진입점은 로컬 브라우저 주소 `http://127.0.0.1:8765`입니다. 서버는 루프백에만 바인딩합니다. 종료는 Ctrl+C입니다. 클라우드에서 소켓 생성이 제한되면 실행 권한이 필요합니다. 외부 공개나 운영용 서버는 제공하지 않습니다.

- 왼쪽 프로젝트 또는 구성요소 선택 → 검색어·유형·검토 상태·정렬 → 다음 페이지 → 자료 상세.
- `가상 관측`: 긴 한국어 원문, 이모지, 두 출처. **출처 위치 확인**으로 현재 본문·조각 해시와 UTF-16 경계를 검사합니다.
- `가상 교차 관계`: 여러 프로젝트와 관측/미해결 관계. `가상 미분류`: 미확인·미분류 상태.
- 두 번째 페이지의 `가상 출처 누락`, `가상 이력`: 누락 상태와 원형 사건 체인·관련 자료 이동.
- 상세의 **합성 검토 연습**: 보류·근거 요청·제안 초안을 이 탭의 `sessionStorage`에만 기록합니다. 실제 사유·자료를 입력하지 마세요. 원형 상태·원형 이력·정본 승인은 바뀌지 않습니다.
- **검색 결과로**: 검색 조건·페이지·자료 버튼 초점을 유지합니다.
- 개발 시나리오에서 오래된 버전·연결 실패를 시험합니다. 정상으로 되돌린 뒤 필요하면 **기준 다시 조회**를 누릅니다.

추가 모드:

```sh
# 합성 규모 시험: 7 × copies건. 생성 자료는 서버 메모리에만 존재합니다.
PYTHONDONTWRITEBYTECODE=1 python3 -m app.server --port 8766 --copies 1000
# 실제 읽기 어댑터 경계: 설정되지 않았으므로 미연결 오류가 정상입니다.
PYTHONDONTWRITEBYTECODE=1 python3 -m app.server --port 8767 --mode readonly
```

`readonly`는 합성 공급자를 생성하거나 다른 위치로 자동 대체하지 않습니다. 실제 서버·API·DB 전송 구현은 없습니다.

## 검증

Node.js 24에서 아래 명령을 확인했습니다. 기존 인계 테스트는 변경하지 않았습니다.

```sh
# ZIP 원형과 현재 개발본을 분리 검증 (제외 옵션 없음)
python3 tools/verify_handoff.py
PYTHONDONTWRITEBYTECODE=1 python3 fixtures/synthetic/generate.py
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test*.py' -v
node --check app/web/app.mjs
node --check app/web/client.mjs
node --test --test-isolation=none source/tests/*.test.mjs
```

2026-11-04 보완 결과: Python 23개, Node 20개 통과. Node의 기본 격리 실행은 이 환경에서 파일 단위 결과만 보여 `--test-isolation=none`으로 개별 시험 실행을 확인합니다. 생성기는 새 임시 디렉터리에만 출력하며 기존 파일을 덮어쓰지 않습니다.

추가 검증 도구는 앱 의존성이 아닙니다. 이 환경에는 이미 설치되어 있습니다. 새 환경에서 필요하면 별도 가상환경에 설치합니다.

```sh
python3 -m venv /tmp/utt-test-venv
/tmp/utt-test-venv/bin/pip install -r tools/test-requirements.txt
# 아래 python3 대신 /tmp/utt-test-venv/bin/python을 사용해도 됩니다.
PYTHONDONTWRITEBYTECODE=1 python3 tools/validate_schema.py
PYTHONDONTWRITEBYTECODE=1 python3 tools/browser_check.py
```

브라우저 시험은 `/usr/bin/chromium`이 필요합니다(Linux에서는 배포판의 Chromium 패키지 설치). Playwright는 버전을 고정한 선택적 시험 도구입니다. 브라우저 시험은 자체 임시 루프백 서버 두 개를 시작·종료하고 Chromium에서 연속 흐름 13개 묶음을 검증합니다. 결과와 합성 스크린샷은 무시 경로 `artifacts/`에 저장합니다. 시스템 Chromium의 버전 차이는 재검증 대상입니다.

전체 Draft 2020-12 스키마 검증 61건도 수행했습니다. 이는 합성 형식 검증이며 실제 원천 대조나 의미 검증의 대체가 아닙니다. 7,000건 규모 시험은 100건씩 70페이지, ID 중복 없음, 검색 응답당 250KB 미만, 목록 본문 미포함을 검사합니다. 합성 시험을 실제 DB 성능으로 해석하지 않습니다.

## 구조와 연결 경계

- `app/provider.py`: 공통 `DataProvider.read(request, sort)` 프로토콜, `SyntheticProvider`, 미연결 기본값의 `ReadOnlyAdapter`.
- `app/server.py`: 명시적 정적 파일 allowlist, 읽기 전용 `/api/read`, 외부 Origin 거부, 요청 크기 제한, `no-store`·CSP. 요청/본문 로그 없음.
- `app/web/`: 프레임워크 없는 브라우저 ES modules. DOM `textContent` 사용, 늦은 응답 세대 검사·취소, 접근성 레이블·키보드·반응형 화면.
- 합성 공급자는 검증된 기존 fixture 생성 함수를 이용합니다. UI 전용 합성 구성요소·관계·두 번째 출처·복제 자료는 별도로 생성합니다. 인계 fixture와 구 모듈·스키마는 그대로 보존했습니다.
- 검색·목록은 본문 없이 페이지 단위로 전달합니다. 서버 내부 생성 자료는 SQLite `:memory:`에 보관하며 상세 원문은 요청한 한 기록에 한정합니다. 실제 전체 DB/전체 본문을 브라우저에 적재하지 않습니다.
- 커서는 서버 실행별 HMAC과 모든 조건·정렬·페이지 크기·스냅샷에 묶입니다. 서버 재시작 또는 조건 변경 후 재사용은 거부됩니다. ID 정렬은 SQLite BINARY 순서이며 최종 ID로 동률을 해소합니다.
- 합성 세션 namespace는 `utt.synthetic.session.v1`, 기록은 dataset/snapshot별로 구별합니다. 파일·실제 원장·다른 캐시를 사용하지 않습니다. 탭 종료 시 소멸하며 합성 연습 지우기 버튼을 제공합니다.

실제 어댑터 설정 지점은 `ReadOnlyAdapter(transport=...)`입니다. 이 저장소에서는 합성 공급자의 `read`만 주입해 시험했습니다. CLI에는 실제 경로나 URL·비밀 값을 받는 옵션이 없습니다. 향후 승인된 실행 환경에서 Work와 계약·인증·버전·원문 전달 방식을 합의한 뒤 별도 전송 구현을 주입해야 합니다. 현재 경계를 운영용 어댑터 완성으로 간주하지 마세요. 자세한 요구는 [Work 접합 메모](docs/WORK_INTEGRATION.md)를 참고하세요.

## 인계 기준과 한계

기준 커밋: `027b04e278f6ab605450c888f66b9abe63c50443` (`whitewillbelive-tech/utt-interface`, 원격 HEAD와 동일함을 확인). 시작 체크아웃 브랜치는 `work`, 변경 전 작업 트리는 깨끗했습니다. 작업 브랜치: `feat/synthetic-reader`.

원본 매니페스트는 변경하지 않았습니다. 초기 입력에서 누락됐던 두 파일은 검토된 원본 ZIP(SHA-256 `30137cea3bd7117f1a0dfe4a34ec0807b381a28e99cf5a072ad6c41a73623e48`)으로 복구했습니다. `provenance/handoff-v1/`는 ZIP에서 직접 추출한 불변 기준이며 매니페스트 대상 38개를 제외 없이 검증합니다. 현재 작업트리는 원형과 동일한 파일, 설명과 정확한 해시가 등록된 의도된 변경, 누락, 설명 없는 변경을 별도로 보고합니다. 마지막 두 상태는 실패입니다. `.gitignore`는 원본 제외 규칙에 `.venv/`·`artifacts/`만 추가했습니다. 정당한 차이는 `docs/HANDOFF_WORKTREE_POLICY.json`과 [보완 기록](docs/REMEDIATION_20261104.md)에 있습니다.

C 파일 미리보기·인증·복원, D-1.1 운영 원장 정정·백업·복원, D-2 실제 판단 기반 영향 분석·일괄 제안, 원본 Windows 실행기는 미인계 의존성과 별도 명령 계약이 필요합니다. 화면에 그 상태를 표시하며 원형 코드를 활성화하지 않았습니다. 실제 DB 연결·실제 규모·Windows 수용 시험·운영 배포는 수행하지 않았습니다.

## 2026-11-04 보완 및 재현 증거

현재 보완 브랜치는 `fix/reader-remediation-20261104`이며 `6520dc1`의 구현을 포함합니다. 응답은 전송한 request의 dataset/snapshot에 고정해 검사하며, capabilities 초기 null 범위에서도 내부 dataset descriptor를 검사합니다. 내부 레코드·프로젝트·구성요소·출처·사건의 dataset과 관계 endpoint 범위를 검사합니다. 관계에 계약에 없는 dataset_id를 요구하지 않으며 명시적인 교차 dataset 관계는 허용합니다. 상세 이동의 현재 자료와 목록에서 처음 선택한 자료를 구분해 관련 자료 이동 후에도 원래 목록 버튼으로 초점을 돌립니다.

```sh
# 모든 지원 시험을 실행하고 커밋·버전·명령·종료코드·로그를 실행별로 보존
PYTHONDONTWRITEBYTECODE=1 python3 tools/run_checks.py
```

결과는 `artifacts/<실행ID>/results.json`과 같은 실행 폴더의 로그에 있으며 `artifacts/latest-validation.json`이 최신 실행을 가리킵니다. 이 결과는 로컬 시험이며 CI가 아닙니다. 도구 누락·실패는 성공으로 처리하지 않습니다. 시험은 활성 `tests/`와 `source/tests/`만 명시적으로 탐색합니다. **저장소 루트에서 무제한 시험 탐색을 하지 마세요.** 불변 사본의 시험을 실행하지 않도록 `node --test`만 쓰는 대신 위 명령 또는 명시 경로 명령을 사용합니다.

`provenance/`는 import·HTTP·배포 입력이 아닙니다. 정적 서버는 해당 경로를 제공하지 않으며 Git export와 Docker context에서도 제외합니다. 불변 사본이 수정되면 고정된 원본 매니페스트 SHA와 파일 해시로 탐지합니다. Git 자체가 임의 편집을 물리적으로 막는다는 뜻은 아닙니다. `.gitignore` 가상 파일명 시험과 추적 파일명 혼입 검사도 수행하지만 이 검사만으로 실제 자료·비밀 부재를 보장하지 않습니다.
