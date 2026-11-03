# 구현·검증·변경 기록

## 코드 및 입력 기준

- 저장소: `whitewillbelive-tech/utt-interface`.
- 기준: `027b04e278f6ab605450c888f66b9abe63c50443`. 로컬 `work` 브랜치와 원격 HEAD를 확인했습니다. 기준 시점 미커밋·미추적 변경 없음.
- 작업: `feat/synthetic-reader`. 다른 프로젝트·이력 내용·운영 자료를 탐색하지 않았습니다. 별도 worktree를 만들지 않았습니다.
- 원본 HANDOFF_MANIFEST.json 38개 항목 중 36개 크기·SHA-256 일치, `.env.example` 및 `.gitignore` 누락. 매니페스트 자체는 해시 대상에서 제외합니다. 누락 파일은 설정·실행 입력으로 사용하지 않았습니다. 새 `.gitignore`는 독립적으로 작성한 개발 보조 파일입니다.
- 원본 `AGENTS.md`, `README_HANDOFF.md`, `source/SOURCE_README.md`, 계약·스키마, fixture·생성기, 네 구 읽기 모듈, 기존 테스트·정적 참조는 변경하지 않았습니다. 최초 체크섬 대조 후에만 앱 실행·시험을 진행했습니다.

## 기존 구조 조사와 기능 대응

원형 진입점은 A의 `Start_Reader.cmd` → `serve_local.py --auth` → ES modules·worker/lib·Dropbox SDK, C 및 D 계열의 각 Python 서버입니다. 현재 인계에는 서버·시작 스크립트·인증·worker/lib·원장·캐시가 없습니다. 기존 잠금 파일과 앱 의존성 매니페스트도 없습니다. 네 순수 구 모듈은 구 계약 인메모리 함수로서 합성 시험에만 유지했습니다.

| 원형 기능 | 이번 결과 | 남은 범위 |
|---|---|---|
| A 통합 열람·검색 | 프로젝트·유형·검토·검색어 필터, ID 정렬·초기화·페이지, 상세·결과 복귀 | 실제 검색 모델·정규화·권한별 검색 |
| C 미승인 로컬 미리보기 | 원형 파일을 보존하고 미지원 사유 표시 | 안전하게 분리된 입력·기존 worker/lib·독립 명령 계약 |
| D-0 검토·출처·관계 | 원문/출처 상태 구별, 복수 출처, 한국어·이모지 좌표, 프로젝트 관계 | 원본별 모델 매핑, 실제 근거·좌표 대조, 대본문 분할 |
| D-1/D-1.1 결정·이력 | 합성 원형 사건·supersedes 조회, 관련 자료 이동, 별도 합성 세션 연습 | 운영 원장 정정·취소·불변 체인·복원·백업과 명령 계약 |
| D-2 영향·제안 | 합성 제안 초안 연습만 별도로 제공 | 실제 유효 판단·영향 분석·일괄 제안은 미지원 |
| 정본 승인·인증·복원·운영 동기화 | 활성화하지 않음, 화면에 명시 | 사용자 승인과 별도 설계·실행 환경에서 검증 |

합성 UI는 전체 프로그램 이관·복원 완료를 주장하지 않습니다. THEOLOGIA 등 실제 논리 영역의 매핑을 추정하거나 별도 Site로 만들지 않았습니다.

## 변경 파일

- `app/__init__.py`, `provider.py`, `server.py`: 생성 합성 DB, 공통 읽기 프로토콜·미연결 어댑터, 명시적 읽기 HTTP 전달.
- `app/web/index.html`, `style.css`, `app.mjs`, `client.mjs`: 통합 탐색·검색·상세·출처·이력·합성 세션·오류·반응형/키보드 UI.
- `tests/test_reader.py`, `source/tests/reader_client.test.mjs`: 계약·관계·페이지·대량 합성·어댑터·좌표·최신 요청 테스트.
- `tools/verify_handoff.py`, `validate_schema.py`, `browser_check.py`, `test-requirements.txt`: 재현 가능한 입력 점검·전체 스키마·실제 Chromium 시험.
- `.gitignore`: 새로운 개발 산출물·세션 외부 파일 제외 규칙. 실제 설정이나 비밀 값은 추가하지 않음.
- `README.md`, `docs/WORK_INTEGRATION.md`, 이 문서와 `docs/PR_DESCRIPTION.md`: 실행·검증·누락·임시 확장·검토 기록.

## 수행한 검증

환경: Python 3.12.14, Node.js 24.22.1, Playwright 1.62.0, 설치된 시스템 Chromium, jsonschema 4.26.0. 런타임 의존성 설치는 필요 없었습니다. 기존 환경의 도구를 사용했으며 새 가상환경 설치는 이번에 실행하지 않았습니다. README의 선택적 설치 명령은 고정된 시험 도구 요구 목록을 제공합니다.

| 실행 명령 / 확인 | 실제 결과 |
|---|---|
| 최초 매니페스트 대조 | 36 일치, 2 누락. 누락 입력 사용 제외 |
| `python3 tools/verify_handoff.py --exclude .env.example --exclude .gitignore` | 36 일치, 명시적 제외 2, 나머지 실패 0 |
| `PYTHONDONTWRITEBYTECODE=1 python3 fixtures/synthetic/generate.py` | 새 임시 폴더 생성, 기존 fixture 쓰기 없음 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test*.py' -v` | 18개 통과: 기존 11 + 새 7 |
| `node --test --test-isolation=none source/tests/*.test.mjs` | 10개 통과: 기존 5 + 새 5 |
| `node --check app/web/app.mjs`, `node --check app/web/client.mjs` | 둘 다 성공 |
| `PYTHONDONTWRITEBYTECODE=1 python3 tools/validate_schema.py` | Draft 2020-12 스키마 자체 검증 및 합성 요청/응답 61회 통과 |
| `PYTHONDONTWRITEBYTECODE=1 python3 tools/browser_check.py` | Chromium 연속 사용자 흐름 10개 묶음 통과, 브라우저 JS 오류 0 |
| 신규 코드/문서 credential 패턴 검사 | 비밀키 헤더·GitHub/AWS 키·하드코딩 자격 패턴 발견 0. 합성 생성 경로와 입력·변경 파일도 검토 |
| `git diff --check` | 성공 |
| `git ls-remote origin HEAD` | 기준 SHA와 동일한 원격 HEAD 확인 |
| `gh api repos/whitewillbelive-tech/utt-interface ...` | `api.github.com` 접근 Forbidden. PR 생성 API 차단, 새 인증정보 요청하지 않음 |

최초 브라우저 실행에서 기본 fetch 함수를 객체 메서드로 호출해 `Illegal invocation`이 발생했습니다. globalThis에 바인딩하도록 수정하고 전체 UI 시험을 다시 통과했습니다. 처음 로컬 소켓 생성은 샌드박스 제한으로 거부됐으며, 승인된 실행 권한으로 루프백 서버·Chromium 검증을 수행했습니다. Node 기본 격리 runner의 파일 단위 집계 대신 개별 시험 10개 실행을 확인하는 모드를 사용했습니다. 기존 테스트를 삭제·완화하지 않았습니다.

브라우저 검증 세부:

1. 프로젝트·합성 구성요소·dataset/snapshot 표시 및 페이지 목록.
2. 정적 파일 allowlist·읽기 외 메서드 거부·외부 Origin 거부·검색 응답 본문 미포함.
3. 페이지 이동, 출처 누락·미확인 본문, 상세 복귀 페이지·키보드 초점 유지.
4. 기존 합성 사건 정정 체인, 관련 자료 이동, 두 출처, 긴 한국어·이모지 UTF-16 강조.
5. 합성 세션만 기록·재조회·제거, 원형 정본 미승인 상태 유지.
6. 미분류·복합 필터·내림차순·초기화·빈 결과·본문 검색.
7. 오래된 버전과 연결 실패의 독립 표시 및 이전 결과 제거.
8. 지연된 이전 검색 요청 후 최신 검색 결과 유지.
9. 키보드 Enter/Tab, 데스크톱·390px 화면에서 가로 넘침 없음.
10. 미연결 readonly 모드에서 결과·합성 연습 미노출, fallback 없음.

대량 합성 시험은 생성기 입력을 1,000번 복제해 7,000건을 만들고 100건씩 70페이지를 실제 끝까지 읽었습니다. 중복·누락 0, 응답당 250KB 미만, raw_text=null, 복제 자료 상세 조회를 확인했습니다. 서버는 생성 데이터를 메모리에 보관하는 개발 구현이며 운영 규모 성능 또는 실제 DB 스트리밍을 보장하지 않습니다.

스크린샷과 브라우저 결과 JSON은 `artifacts/`의 합성 산출물입니다. 원문·DB·비밀은 수집하지 않았습니다. 패턴 검사는 알려지지 않은 모든 비밀이나 모든 민감 자료 부재의 수학적 증명이 아닙니다.

## 미수행·제한 및 완료 분류

1. 코드 개발 이관 및 환경 재현: 현재 제공된 부분 소스에서 재현 가능한 새 합성 프로그램 확보. 원본 전체 이관은 서버·worker/lib·인증 등의 누락으로 부분 상태 유지.
2. 합성 인터페이스: 구현·검증 완료. 미지원 원형 기능은 위 표와 화면에서 구분.
3. 실제 접합 어댑터: 공통 읽기 경계·미연결 처리·합성 transport 주입 시험 완료. 실제 전송·인증·원천 매핑 구현은 Work 계약 후 별도 작업.
4. 실제 DB 연결·실제 규모·실제 원문·원장 검증: 이번 작업에서 수행하지 않음. Windows·스크린리더 전체 수용 시험도 미수행.
5. 운영 배포: 별도 작업. 환경 초안 수정·게시, 기본 브랜치 병합·배포 없음.

최종 커밋 및 원격 저장 결과는 최종 답변에서 정확한 식별자로 제공합니다. Draft PR API 접근이 차단된 경우 같은 검토 정보를 PR 설명 문서와 로컬 Git 패치로 제공합니다.
