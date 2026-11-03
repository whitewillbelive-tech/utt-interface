# 2026-11-04 보완 기록

## 기준과 작업 범위

저장소 `whitewillbelive-tech/utt-interface`, 시작 브랜치 `feat/synthetic-reader`, 로컬 HEAD `6520dc1c6f8e21c71ae7930c7448552c4773bd5a`. 작업 시작 시 미커밋·미추적 변경은 없었습니다. `git ls-remote origin refs/heads/main refs/heads/feat/synthetic-reader`로 원격 main이 `027b04e278f6ab605450c888f66b9abe63c50443`, 구현 브랜치가 `6520dc1`이며 추가 관련 커밋이 없음을 확인했습니다. 구현을 포함한 현재 HEAD에서 `fix/reader-remediation-20261104`를 만들었습니다. 강제 초기화·기본 브랜치에서 재시작·기존 작업 덮어쓰기는 없습니다.

`UTT_INDEPENDENT_REVIEW_20261104.txt`는 현재 저장소와 사용자 첨부 코드 전용 ZIP에 없었습니다. 문서를 읽었다고 주장하지 않으며, 이번 메시지의 모든 보완 지시를 기준으로 작업했습니다. 첨부 문서 속 지시를 별도 사용자 요청으로 취급하지 않았습니다. 실제 DB·원문·운영 원장·캐시·인증정보, Work 전용 ZIP·Site·Scrivener는 조사·API 응답·시험 입력에 사용하지 않았습니다. 실제 자료를 인계 복구에 요구하지 않았습니다.

## 1. 응답 범위 검증

수정: `app/web/client.mjs`, 새 `app/web/response-validation.mjs`, 서버의 정적 모듈 allowlist, 새 `source/tests/response_scope.test.mjs`.

**수정 전:** 응답 시점의 `this.dataset.snapshot_id`만 비교하여 다른 dataset과 같은 snapshot이면 허용했습니다. 요청 이후 사용자가 dataset을 바꾸면 정상적인 이전 응답을 잘못 거부하거나 새 선택과 일치하는 잘못된 응답을 받아들일 수 있었습니다. 초기 capabilities 내부 dataset과 봉투·내부 자료 범위도 검증하지 않았습니다.

실제 기준 코드만 `/tmp/utt-response-before-*`에 복사하고 새 합성 회귀시험 10개를 실행했습니다. 결과는 **1 통과·9 실패, 종료코드 1**입니다. 작업 파일을 기준 커밋으로 되돌리지 않았습니다.

**수정 후:** `makeRequest()`로 실제 구성·전송한 request를 함수 내부에서 보존·고정합니다. dataset/snapshot 비교에 응답 시점의 선택 상태를 사용하지 않습니다. 요청에서 null인 capabilities 범위만 최초 응답으로 선택할 수 있고, 응답 dataset descriptor와 내부 descriptor의 일치는 계속 검사합니다. 성공 응답의 다른 operation은 null 범위를 허용하지 않습니다. 정상 오류 응답은 result=null을 확인하고, snapshot이 제공됐으면 요청 범위도 대조합니다.

봉투 필드·operation·status·성공/오류 구조·페이지 구조와 dataset descriptor를 검사하고, 레코드·프로젝트·구성요소·출처·사건의 기존 dataset_id, 상세 record_id 및 관계의 명시적 endpoint/앵커를 확인합니다. 관계에 존재하지 않는 dataset_id나 내부 자료에 존재하지 않는 snapshot_id를 요구하지 않습니다. 명시적인 교차 dataset 관계는 로컬 앵커를 유지하면 허용합니다. 범위가 맞더라도 UI의 `LatestRequest`/boot/list/detail 세대 검사는 별도로 유지합니다.

다른 dataset/same snapshot, same dataset/different snapshot, 각 내부 배열의 잘못된 dataset, 초기 null capabilities, 요청별 override, 잘못된 봉투·상세 ID, 늦은 이전 dataset 응답과 세대 폐기를 회귀시험으로 확인했습니다. 현재 코드는 새 회귀 **10/10**, 기존 Node 시험 **10/10** 통과입니다. Chromium에서도 다른 dataset/same snapshot 및 내부 레코드 범위 변조를 거부하고 목록을 비우는 것을 확인했습니다.

호환성: 원래 read JSON Schema와 서버 error enum은 변경하지 않았습니다. 브라우저 내부 오류 `RESPONSE_SCOPE_MISMATCH`와 `INVALID_RESPONSE`를 기존 `VERSION_MISMATCH`, `CONNECTION_UNAVAILABLE`, 빈 결과와 구별합니다. 이 코드는 서버 wire enum에 추가되지 않습니다. 이는 응답 형식·범위에 대한 부분 의미 검사이며 전체 JSON Schema 구현·원천 정확성·접근권한 검증의 대체가 아닙니다.

## 2. 인계 원형 복구와 현재 개발본

수정: `provenance/handoff-v1/`, 출처 메타데이터, 루트 `.env.example`, `.gitignore`, `tools/verify_handoff.py`, `docs/HANDOFF_WORKTREE_POLICY.json`, `.gitattributes`, `.dockerignore`, 새 `tests/test_handoff.py`.

사용자 첨부 `UTT_INTERFACE_HANDOFF.zip`의 SHA-256은 **30137cea3bd7117f1a0dfe4a34ec0807b381a28e99cf5a072ad6c41a73623e48**로 지정값과 일치했습니다. ZIP의 파일 목록이 매니페스트 38개 + 매니페스트 자체 1개와 정확히 같은지 확인했습니다. 압축 해제 전에 크기·파일 해시를 대조하고, ZIP에서 직접 원본 바이트를 복사했습니다. 현재 저장소나 기억으로 누락 파일을 재구성하지 않았습니다.

불변 기준은 `provenance/handoff-v1/`에 39개 파일로 보존합니다. 원본 매니페스트는 수정하지 않았고 자신을 파일 목록에 넣지 않았습니다. 그 매니페스트의 SHA-256 **8f3b19a4c17c250e7f618da2935226ad3aad64c2307401020728f98ad9930f4d**는 검증 도구에 별도로 고정했습니다. 원형 변경·누락·예상 밖 파일·안전하지 않은 심볼릭 링크·매니페스트 변경은 실패입니다. 초기 저장 시 파일을 읽기 전용으로 두었지만 Git이 모든 체크아웃에서 임의 편집을 물리적으로 막는다는 뜻은 아닙니다.

작업 루트 `.env.example`은 원형과 바이트 동일하게 복구했습니다. `.gitignore`는 원본 전체 규칙(DB·원형 압축·백업·캐시·비밀 제외 및 `.env.example` 예외)을 유지하고 `.venv/`·`artifacts/`만 추가했습니다. 이 정확한 변경 해시와 원래 해시·이유를 정책 파일에 기록했습니다. 일반적인 파일명 예외나 `--exclude` 옵션은 없습니다.

현재 결과:

- 원형: 매니페스트에서 계산한 대상 **38개 모두 크기·SHA-256 일치**, 누락·불일치 0.
- 현재 원형 대응 파일: **37개 변경 없음, `.gitignore` 1개 의도한 변경**, 누락·설명 없는 변경 0. 개수를 코드의 영구 상수로 고정하지 않았습니다.
- 그 밖의 개발 추가 파일은 명시된 개발 경로 정책으로 별도 보고합니다. 설명 없는 추가 파일도 실패입니다. 실제 추적 파일명 혼입 검사는 Git ignore와 독립적입니다.
- 가상 파일명으로 DB·백업·압축·캐시·비밀·개발 산출물 제외와 `.env.example` 허용을 시험했습니다. 임시 코드 사본에서 원형 변조·현재 파일 누락·설명 없는 수정이 실제로 실패하는지도 시험했습니다.

불변 사본은 앱이 import하지 않고, 지원 시험 runner는 활성 `tests/`와 `source/tests/`만 명시적으로 탐색합니다. 루트 무제한 자동 탐색은 지원하지 않습니다. HTTP의 provenance 파일 요청은 404를 확인했습니다. Git export-ignore와 Docker context 제외 규칙도 추가했습니다. 임의의 외부 빌드·배포 도구가 이 규칙을 무시하는 경우까지 보장하지 않습니다. `.gitignore`·파일명·일부 자격 패턴 검사를 실제 자료·비밀 부재의 절대 증명으로 해석하지 않습니다.

## 3. 상세를 거친 목록 초점 복귀

수정: `app/web/app.mjs`, `tools/browser_check.py`.

**수정 전 실제 재현:** 기준 `6520dc1`의 `app.mjs`만 임시 Chromium 응답으로 제공했습니다. 2페이지의 가상 이력 → 관련 가상 관측 → 결과 복귀 후 activeElement의 record ID는 **null**이었고, 기대한 `synthetic-record-006`이 아니었습니다. 코드를 작업트리에 덮어쓰지 않았습니다. 원인은 상세마다 lastId를 덮어쓰고 관련 자료 ID가 원래 페이지에 없으면 초점을 못 찾는 것이었습니다.

**수정 후:** 목록 버튼 클릭에서만 `listOriginId`를 저장하고, 현재 상세는 `currentDetailId`로 분리했습니다. 관련 자료를 거쳐도 원래 목록 선택을 덮어쓰지 않습니다. 상세 응답에는 요청 세대와 현재 상세 ID 검사도 유지합니다.

실제 브라우저에서 직접 가상 이력 → 복귀 및 가상 이력 → 관련 가상 관측 → 복귀를 모두 검사했습니다. 검색어 `가상`, 유형 `SYNTHETIC_NOTE`, 프로젝트/검토 필터, ID 오름차순, **2페이지**가 유지되며 **가상 이력 버튼으로 초점 복귀**를 확인했습니다. 기존 출처 누락 자료의 직접 복귀 검사도 보존했습니다.

## 4. 실행·검증 및 재현 기록

런타임은 여전히 Python 표준 라이브러리·브라우저 ES modules이며 설치할 앱 패키지가 없습니다. 기존 시험 도구가 설치돼 있어 재설치는 필요 없었습니다. 확인 버전: Python **3.12.14**, Node **24.22.1**, Playwright **1.62.0**, jsonschema **4.26.0**, Chromium **155.0.8059.79 (Debian 13)**.

권장 전체 명령:

```sh
cd /workspace/utt-interface
PYTHONDONTWRITEBYTECODE=1 python3 tools/run_checks.py
```

runner가 각 실행 ID별 `artifacts/<실행ID>/results.json`에 실제 HEAD, 작업트리 변경 유무, 버전, 명령·종료코드·로그를 연결합니다. `artifacts/latest-validation.json`은 최근 실행의 위치만 가리킵니다. 실패·도구 누락은 실패이며 이전 결과를 성공으로 재사용하지 않습니다. 최종 커밋에서도 이 명령을 다시 실행해 해당 HEAD가 기록된 증거를 남깁니다. 임시 서버와 브라우저는 시험 뒤 종료합니다.

| 실제 실행 항목 | 결과 |
|---|---|
| `python3 tools/verify_handoff.py` | 원형·현재 작업트리·추적 파일명 검사 성공, 제외 없음 |
| 합성 `generate.py` | 새 임시 경로에 생성, 기존 fixture 변경 없음 |
| `python3 -m unittest discover -s tests -p 'test*.py' -v` | 기존 18 + 인계 회귀 5 = **23 통과**, 실패/건너뜀 0 |
| `node --check` 활성 app/web 모듈 3개 | 모두 통과 |
| `node --test --test-isolation=none source/tests/*.test.mjs` | 기존 10 + 응답 회귀 10 = **20 통과**, 실패/건너뜀 0 |
| `python3 tools/validate_schema.py` | 전체 Draft 2020-12 스키마 자체 및 합성 요청/응답 **61회 통과** |
| `python3 tools/browser_check.py` | 기존 10개 흐름 + 복귀 2개·응답 범위 거부 1개 = **13개 묶음 통과** |
| 7,000건 생성 자료 페이지 검사 | 기존 시험 유지: 70페이지, 중복/누락 0, 목록 raw_text=null |
| `git diff --check` | 통과 |

Chromium은 프로젝트·구성요소·검색·복합 필터·정렬·초기화·페이지·상세·복수 출처·긴 한국어·이모지 좌표·이력·합성 세션·빈 결과·오래된 버전·연결 실패·지연 검색·키보드·390px·readonly 미연결을 검사했습니다. readonly의 자동 합성 fallback이 없고 실제 DB·인증·캐시 자동 탐색 코드가 추가되지 않았음을 유지 확인했습니다. 원형·기존 시험을 삭제하거나 약화하지 않았습니다.

이 결과는 **로컬 실행**이며 CI PASS가 아닙니다. CI는 실행·확인하지 않았으므로 CI 링크도 제공하지 않습니다. Windows·실제 데이터·실제 규모·운영 인증·실제 어댑터 연결·스크린리더 전체 수용 시험은 미수행입니다.

## 5. 제출과 남은 단계

기존 관련 PR을 확인하기 위한 `gh pr list --repo whitewillbelive-tech/utt-interface --state open --json ...`는 `https://api.github.com/graphql`에서 **Forbidden**으로 실패했습니다. 기존 PR의 유무를 단정하지 않으며 생성되지 않은 PR URL을 제공하지 않습니다. 새 토큰이나 실제 계정·자료를 요청하거나 네트워크 정책을 우회하지 않았습니다. 변경 커밋과 브랜치를 원격에 저장하고 로컬 패치·PR 설명을 제공합니다. 기본 브랜치 병합·배포·공개 범위 변경·환경 게시는 수행하지 않습니다.

[WORK_INTEGRATION.md](WORK_INTEGRATION.md)에 미지원 기능별 추가 인계 코드·필요 계약·담당·검증 방법을 갱신했습니다. 새 쓰기·승인·실제 접합을 활성화하지 않았습니다. **이번 세 결함과 인계 원형 보완 완료는 기존 UTT 전체 기능 이관 완료를 뜻하지 않습니다.**
