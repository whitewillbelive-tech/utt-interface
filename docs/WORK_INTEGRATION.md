# UTT 공용 접합 계약·협업 상태 — 2026-11-04

## 현행 기준과 이관 중단 해제

이 문서가 07·08·09의 단일 접합 계약과 상태 기록이다. 과거 인계 문서의 임시 개발 중단·push/병합 보류는 사용자 지시에 따라 갱신한다. 정상화에 필요한 커밋·push·충돌 해결·기본 브랜치 병합을 진행한다. 강제 덮어쓰기·이력 삭제·보호 규칙 우회는 하지 않는다. 운영 배포와 실제 데이터 변경은 이번 코드 병합과 별개다.

조사 기준은 `fix/reader-remediation-20261104`의 `55dda98249e703017c559a85e60a0a871ce057eb`이다. 최초 원격 조회에서 이 브랜치의 더 최신 변경은 없었고, 09의 후속 UI는 `ui/09-read-navigation`의 `fdde5cd15f78b29a58be1a80b31afe5a6598257e`로 별도 존재했다. 후속 정상 변경을 유지한다. 이 SHA들을 향한 reset·강제 push는 하지 않는다. 최종 코드 기준은 병합된 기본 브랜치이며 실행 시 정확한 SHA를 고정한다. 09 UI는 [PR #1](https://github.com/whitewillbelive-tech/utt-interface/pull/1)로 08 통합 브랜치에 병합했다. 통합 코드 커밋은 `6ac6accf728261c2debe9491b25fa0f50b4c9c38`이며, 로컬 검증 코드와 Git tree `9b163c888163c3271285f4c9e8fdf5e75b32cb46`가 정확히 일치한다. 그 뒤의 상태 문서 갱신은 실행 코드 변경이 아니다.

| 담당 | 단독 변경 범위 | 현재 접합 상태 |
|---|---|---|
| 07 Work Chat | 비민감 원천 의미·projection 계약, 승인 데이터 환경의 설정·실자료 대조 | 새 원천 계약과 같은 코드 커밋의 실제 실행 결과는 아직 수신하지 않음 |
| 08 이 Chat | provider/server/transport·공용 스키마·백엔드 시험·루트 지침·최종 통합 | 명시적 HTTPS 읽기 transport, 인증 주입, 응답 검증, 오류 처리 구현. 실제 접속은 미수행 |
| 09 Codex Chat | `app/web/`, UI 전용 시험, 별도 UI 브랜치/PR | 위 UI 후속 커밋의 6개 파일을 직접 검토하고 PR #1 병합. UI 소스 직접 수정 없음 |

다른 Chat의 자동 전달·같은 파일시스템·접근 권한을 가정하지 않는다. 계약 변경과 비민감 결과는 이 저장소의 이 문서에 반영한다. 실제 DB·원문·출처 본문·원장·캐시·백업·계정·실주소·경로·키를 이 코드 환경, 이슈, PR, 로그에 요청하거나 전송하지 않는다. Scrivener·별도 Sites는 접근하지 않는다.

## 공유 API와 transport

공유 스키마는 `contracts/utt-read-api.schema.json`의 **UTT-ACCESS-READ/0.1.0**이다. UTT-READMODEL-1·UTT-CANONICAL-1·D1.1 원형을 대체하지 않는 초안이다. 이번 transport를 위해 필드나 error enum을 추가하지 않았다.

- 공급자 경계: `DataProvider.read(request, sort='id_asc') -> envelope`.
- 브라우저→루프백: `POST /api/read`, JSON `{request: <스키마 요청>, sort: 'id_asc'|'id_desc', scenario: 'normal'|'stale'|'connection'}`. `scenario`는 합성 모드 전용이다. readonly에서는 `normal` 이외를 거부한다.
- 서버→명시적 upstream: `HttpReadTransport`가 단일 HTTPS URL에 `POST`와 JSON `{request, sort}`를 전달한다. `scenario`는 보내지 않는다. 응답은 같은 스키마의 봉투다. URL은 기존 읽기 endpoint를 명시적으로 주입하며 DB·파일 위치를 추측하지 않는다.
- 인증은 서버측 `auth_headers()`에서 매 호출 주입한다. 환경 설정 구현은 `Authorization: Bearer …`를 사용하며 토큰 교체를 다음 요청에 반영한다. 브라우저/API mode 응답에는 URL·토큰·헤더를 제공하지 않는다. 다른 인증 방식은 07의 비민감 계약에 따라 이 콜백만 교체한다.
- TLS 기본 검증과 환경의 proxy/CA 설정을 유지한다. redirect·자동 재시도·연결 탐색·원문 캐시·합성 fallback은 없다. HTTP 오류 본문을 읽거나 전달하지 않는다.
- upstream 응답은 application/json, UTF-8, 중복 키·비유한 수 금지, 크기 제한이다. 현재 단일 상세 본문은 제한 내에서 전체 수신한다. 스트리밍·본문 분할·대형 한 기록의 운영 준비가 완료된 것은 아니다.

`ReadOnlyAdapter()`는 설정이 없으면 HTTP 503 / `CONNECTION_UNAVAILABLE`, `result=null`이다. 모듈 import와 일반 readonly 시작은 환경 설정을 읽거나 접속하지 않는다. `ReadOnlyAdapter(transport)`는 공급자 출력의 전체 Draft 2020-12 스키마와 아래 의미를 검사하고, 잘못된 응답을 성공으로 보정하지 않는다.

## snapshot·페이지·의미 보존

1. capabilities의 null 필드만 최초 범위 선택을 허용한다. 응답 snapshot과 내부 dataset descriptor가 일치해야 한다. 이후 dataset/snapshot은 전송한 요청에 고정한다. 다른 snapshot은 `VERSION_MISMATCH`, 다른 범위·잘못된 봉투는 `INTERNAL_ERROR`로 거부한다.
2. record/project/component/source/event의 dataset, 상세 record ID와 history event의 record ID를 대조한다. 관계는 기존 from/to_dataset_id와 앵커로 검사한다. 들어오는 관계와 명시적 교차 dataset 관계를 허용하며 임의의 relation.dataset_id를 만들지 않는다.
3. opaque cursor·필터·정렬·페이지 크기는 upstream에 그대로 전달한다. 로컬에서 cursor 생성·offset 변환·snapshot 교체·실패 시 처음 페이지로 재조회하지 않는다. upstream이 불변 snapshot과 모든 조건에 묶인 cursor를 보장해야 한다. 코드가 이 원천 보장을 대신하지 않는다.
4. 현행 임시 정렬은 search: dataset ID asc + record ID asc/desc, projects/history 혼합 페이지: item_kind·dataset·item ID asc다. page_size와 총 항목 수·page.sort, 페이지 내 ID 중복·순서, 입력 cursor를 그대로 돌려주는 정체를 검사한다. 여러 페이지에 걸친 중복·누락·순환 부재는 upstream/07 검증 책임이다. 정식 정렬·검색 규칙은 07 계약 수신 후 차이만 반영한다.
5. 목록은 raw_text/raw_text_sha256=null이다. 상세는 요청한 한 기록만 받는다. AVAILABLE 빈 문자열과 MISSING/UNKNOWN/INACCESSIBLE/NOT_REQUESTED를 보존한다. 빈 검색은 OK+빈 records이며 연결 실패의 대용으로 만들지 않는다. history_status와 빈 사건 배열은 별도로 유지한다.
6. state·original_ids·event_contract·supersedes·event_payload_preservation·관계 근거·출처 version/hash/location을 재분류하거나 원장에 쓰지 않는다. 제공된 본문 해시는 대조하지만 실자료의 정본성·전체 원장 무결성을 증명하지 않는다. 페이지 밖 supersedes 대상의 부재를 삭제로 해석하지 않는다.

| upstream 결과 | 루프백 결과와 UI 의미 |
|---|---|
| 200 + 정상 OK/ERROR 봉투 | 전체 검사 후 전달. 오류 code/retryable/snapshot 보존, message는 고정 문구로 교체 |
| 401 / 403 | 동일 HTTP status 보존. wire enum은 CONNECTION_UNAVAILABLE, UI가 HTTP로 인증 필요/접근 거부 구별 |
| 400 / 404 / 409·410 등 봉투 없는 비정상 HTTP | HTTP 503 + CONNECTION_UNAVAILABLE. 404를 자료 누락, 409를 snapshot 충돌로 추측하지 않음. 원천 상태 매핑은 07 계약 수신 후 반영 |
| timeout·연결 실패·5xx·redirect·잘못된 JSON·응답 초과 | HTTP 503 + CONNECTION_UNAVAILABLE. 결과·대체 자료 없음 |
| 스키마·범위·상세 ID·본문 해시 불일치 | INTERNAL_ERROR 또는 VERSION_MISMATCH 읽기 오류. provider 예외·주소·본문은 로그/오류에 없음 |

## 승인 데이터 실행환경에서의 동일 커밋 실행

아래는 **07이 승인된 데이터 실행환경에서만** 수행한다. 실주소·경로·키를 이 Chat에 보내거나 저장소 `.env`에 기록하지 않는다. 승인 표시는 네트워크/자료 접근 권한을 부여하지 않는다. upstream의 존재·접근권한·원천 projection은 아직 확인되지 않았다.

```sh
# 승인 환경의 저장소에서 병합된 코드 SHA를 확인·고정한다.
git rev-parse HEAD
python3 -m pip install -r requirements.txt
# 승인 환경의 secret/runtime 설정으로 아래 이름만 주입한다.
# UTT_RUNTIME_APPROVED=1
# UTT_READ_URL            (명시적인 HTTPS 읽기 endpoint, query/userinfo/fragment 금지)
# UTT_READ_TOKEN          (서버측 Bearer, 브라우저/CLI 인자/로그로 전달 금지)
# UTT_READ_TIMEOUT_SECONDS       (선택, 기본 10초, 0.1..60)
# UTT_READ_MAX_RESPONSE_BYTES    (선택, 기본 8 MiB, 1 KiB..64 MiB)
python3 -m app.server --mode readonly --connect-env --port 8765
```

승인 환경의 로컬 브라우저에서 `http://127.0.0.1:8765`를 연다. 다른 장치·공개 호스팅에 노출하는 서버가 아니다. 잘못된 설정은 값 없이 시작 실패한다. 시작 문구의 transport configured는 연결 검증 성공이 아니다. URL/토큰 없이 연결 성공을 만들지 않는다.

07이 이 문서에 반환할 **비민감 결과**:

- 실행 코드 SHA, 런타임/브라우저 버전, 각 단계 PASS/FAIL/NOT_RUN. URL·경로·키·검색어·ID·본문·원장·응답 전문·원본 로그는 제외.
- capabilities→검색→목록의 다음 페이지→상세→출처의 실제 동작 및 원천 대조 결과. 실제 출처 본문 열기/강조의 미지원은 별도 NOT_SUPPORTED.
- snapshot 유지/만료/재시작/조건 변경 cursor 거부, 빈 결과·누락·미확인·접근 불가·401/403·연결 단절의 구별.
- 원천 projection에서 dataset/snapshot/관계 키·검색 규칙·원문/출처 version/hash/좌표 단위/본문 분할·사건 보존 상태의 비민감 계약 차이.
- 실패 시 정확한 장애 종류(예: endpoint 미제공, 권한 거부, projection 불일치). 실제 설정값 없이 필요한 코드 차이만 전달.

08은 이 결과를 기다리는 동안 독립적인 코드·시험·UI 통합을 계속한다. ZIP·로그를 사용자에게 반복 운반하도록 요청하지 않는다.

## 실제 작동 범위와 남은 차단

코드상 연결 가능한 읽기는 capabilities·projects·search·detail·related_history이며 검색→페이지 목록→한 기록 상세·출처 메타데이터와 관계·이력을 같은 계약으로 전달한다. 검증은 합성 공급자와 stub HTTPS 응답 및 루프백/Chromium에서 수행하며 실제 upstream 연결 증거가 아니다.

**실제 읽기 운영 접합은 미완료**다. 07의 승인된 upstream·인증 방식과 원천 projection 계약, 같은 병합 커밋의 실제 실행 비민감 결과가 없다. 이 코드 환경의 승인 자료 연결은 구성하지 않았다. 실자료 위치를 찾아서 해결하지 않는다.

**출처 열기 차단:** 현 UI `sourceRange()`는 SYNTHETIC_SOURCE·UTF-16·현재 본문/조각 해시 일치만 처리한다. 기존 초안에는 별도 출처 본문 조회 operation이 없다. 실제 locator target·source version/body·권한·좌표/분할 의미가 확정되지 않아 외부 출처를 임의 URL/파일로 열지 않는다. 실제 상세·출처 메타데이터 수신과 실제 출처 본문 열기 완료를 구별한다. 07의 비민감 계약을 받으면 08은 공용 계약/백엔드 차이를, 09는 UI 차이를 별도 반영한다.

**DB 관리 차단:** 저장소에 승인된 운영 명령·원장 writer·백업/복원 서버가 제공되지 않았다. 쓰기·정정·취소·승인·복원·일괄 제안 API는 HTTP 405, writes_supported=false다. 기존 참조 코드를 실행하지 않는다. 연결 전에 명령별 권한과 사용자 명시 승인, expected version·동시 충돌, 불변 원장 보존, 취소/정정의 추가 사건, 복원의 빈 원장 조건·무결성·크기·원자성·영수증 계약이 필요하다. 이를 이유로 독립적인 실제 읽기 접합을 보류하지 않는다.

구 legacy 순수 모듈과 baselineDataset의 verified 문구는 실제 운영 처리/검증에 사용하지 않는다. 원본 provenance와 매니페스트 38개는 유지한다.

## 실행환경 접근·검증 기록

08 환경에서 GitHub plugin은 저장소 읽기/커밋/PR 병합에 사용 가능하다. shell `git fetch`는 `proxy:8080` 연결 거부로 실패했다. 프록시를 우회하지 않고 GitHub API로 SHA 검증한 Git 객체를 가져와 기존 이력을 보존했다. 이 장애는 07의 데이터 환경에 대한 장애를 뜻하지 않는다.

지원 검증: `PYTHONDONTWRITEBYTECODE=1 python3 tools/run_checks.py`. 원형 38개·현재 변경 해시, 활성 Python/Node 시험, 전체 스키마, 기존 Chromium 흐름과 09 UI 흐름을 실행한다. 환경의 sandbox에서 소켓이 막히면 합성 루프백 시험용 네트워크 권한만 사용한다. 실행별 결과는 무시 경로 `artifacts/`에 기록하며 실제 내용은 넣지 않는다. CI·Windows 수용·실제 규모·장기 세션은 별도 검증이다.

### 08 통합 검증 결과

실행 `20261104T091014Z-2a639190`에서 11개 단계 모두 PASS였다. Python 42개, Node 23개, Draft 2020-12 스키마/합성 봉투 61건, 기존 Chromium 흐름, 09 Chromium 8개 묶음(브라우저 오류 0), 원형 38개와 변경 해시 검사가 통과했다. 오류 상태의 추측 변환 제거와 페이지 cursor 정체·중복·순서 보완을 포함한다. 시험 코드 tree는 위 `6ac6acc…`와 동일하다. 로컬 시험이며 CI·실자료·운영 접합 완료를 의미하지 않는다.
