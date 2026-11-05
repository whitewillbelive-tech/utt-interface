# UTT 공용 접합 계약·협업 상태

현행 UI 통합 기준은 `main`의 PR #5 병합 커밋 `6c21789dd6a374fc8ccfff4c54bb398299aa7603`이다. 이 기준 이후 상태 문서만 갱신했다. 실제 원천 접합과 운영 적용은 아직 확인되지 않았다. 아래 이전 날짜의 기준과 검증은 당시 기록이다.

## 현행 기준과 이관 중단 해제

이 문서가 07·08·09의 단일 접합 계약과 상태 기록이다. 과거 인계 문서의 임시 개발 중단·push/병합 보류는 사용자 지시에 따라 갱신한다. 정상화에 필요한 커밋·push·충돌 해결·기본 브랜치 병합을 진행한다. 강제 덮어쓰기·이력 삭제·보호 규칙 우회는 하지 않는다. 운영 배포와 실제 데이터 변경은 이번 코드 병합과 별개다.

조사 기준은 `fix/reader-remediation-20261104`의 `55dda98249e703017c559a85e60a0a871ce057eb`이다. 최초 원격 조회에서 이 브랜치의 더 최신 변경은 없었고, 09의 후속 UI는 `ui/09-read-navigation`의 `fdde5cd15f78b29a58be1a80b31afe5a6598257e`로 별도 존재했다. 후속 정상 변경을 유지한다. 이 SHA들을 향한 reset·강제 push는 하지 않는다. 최종 코드 기준은 병합된 기본 브랜치이며 실행 시 정확한 SHA를 고정한다. 09 UI는 [PR #1](https://github.com/whitewillbelive-tech/utt-interface/pull/1)로 08 통합 브랜치에 병합했다. 통합 코드 커밋은 `6ac6accf728261c2debe9491b25fa0f50b4c9c38`이며, 로컬 검증 코드와 Git tree `9b163c888163c3271285f4c9e8fdf5e75b32cb46`가 정확히 일치한다. 그 뒤의 상태 문서 갱신은 실행 코드 변경이 아니다.

| 담당 | 단독 변경 범위 | 현재 접합 상태 |
|---|---|---|
| 07 Work Chat | 비민감 원천 의미·projection 계약, 승인 데이터 환경의 설정·실자료 대조 | 관리 Chat이 전달한 07 원천 매핑을 반영. 같은 코드 커밋의 실제 실행 결과는 아직 수신하지 않음 |
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

**출처 열기 상태:** PR #5 UI는 독립 `UTT-SOURCE-READ/0.1.0`으로 출처 메타데이터, 별도 대상 본문 및 검증 상태를 구분한다. 본문·해시·좌표를 재검증한 VERIFIED 응답만 해당 본문에서 강조한다. 합성 환경에서 검증했으며 실제 locator target·source version/body·권한·좌표/분할 의미는 07 확인 전이다. 실제 출처 본문을 열었거나 원출처 좌표를 검증했다고 주장하지 않는다.

**DB 관리 차단:** 저장소에 승인된 운영 명령·원장 writer·백업/복원 서버가 제공되지 않았다. 쓰기·정정·취소·승인·복원·일괄 제안 API는 HTTP 405, writes_supported=false다. 기존 참조 코드를 실행하지 않는다. 연결 전에 명령별 권한과 사용자 명시 승인, expected version·동시 충돌, 불변 원장 보존, 취소/정정의 추가 사건, 복원의 빈 원장 조건·무결성·크기·원자성·영수증 계약이 필요하다. 이를 이유로 독립적인 실제 읽기 접합을 보류하지 않는다.

구 legacy 순수 모듈과 baselineDataset의 verified 문구는 실제 운영 처리/검증에 사용하지 않는다. 원본 provenance와 매니페스트 38개는 유지한다.

## 실행환경 접근·검증 기록

08 환경에서 GitHub plugin은 저장소 읽기/커밋/PR 병합에 사용 가능하다. shell `git fetch`는 `proxy:8080` 연결 거부로 실패했다. 프록시를 우회하지 않고 GitHub API로 SHA 검증한 Git 객체를 가져와 기존 이력을 보존했다. 이 장애는 07의 데이터 환경에 대한 장애를 뜻하지 않는다.

지원 검증: `PYTHONDONTWRITEBYTECODE=1 python3 tools/run_checks.py`. 원형 38개·현재 변경 해시, 활성 Python/Node 시험, 전체 스키마, 기존 Chromium 흐름과 09 UI 흐름을 실행한다. 환경의 sandbox에서 소켓이 막히면 합성 루프백 시험용 네트워크 권한만 사용한다. 실행별 결과는 무시 경로 `artifacts/`에 기록하며 실제 내용은 넣지 않는다. CI·Windows 수용·실제 규모·장기 세션은 별도 검증이다.

### 08 통합 검증 결과

실행 `20261104T091014Z-2a639190`에서 11개 단계 모두 PASS였다. Python 42개, Node 23개, Draft 2020-12 스키마/합성 봉투 61건, 기존 Chromium 흐름, 09 Chromium 8개 묶음(브라우저 오류 0), 원형 38개와 변경 해시 검사가 통과했다. 오류 상태의 추측 변환 제거와 페이지 cursor 정체·중복·순서 보완을 포함한다. 시험 코드 tree는 위 `6ac6acc…`와 동일하다. 로컬 시험이며 CI·실자료·운영 접합 완료를 의미하지 않는다.

## 2026-11-05 — 07 계약 접수와 원천 의미 대조

이 절이 위의 과거 상태 중 상충하는 부분보다 우선한다. 07의 `UTT_DATA_CONTRACT_PACKAGE/1`에 대해 관리 Chat은 원문 전체를 대조하여 `original_json_schema`, `original_contract_draft_markdown`, `synthetic_examples`가 각각 현행 0.1 스키마·초안·합성 예제와 동일하다고 전달했다. `/1`은 포장 버전이고 실제 읽기 규격은 여전히 초안 `UTT-ACCESS-READ/0.1.0`이다. 08이 Library 링크에 직접 접근했을 때 로그인 화면만 확인했으며 JSON 원문은 읽지 못했다. 위 대조는 **전달받은 관리 Chat의 검증 결과**이며 08의 원문 독립 대조는 아니다. READMODEL·CANONICAL·D1.1을 대체하지 않는다.

기존 HTTPS transport·서버측 인증·명시적 설정·전체 스키마/범위 검증 및 401/403은 구현됐고 이전의 「08 transport·인증 구현 미제공」 차단은 해소됐다. 이전 전체 스키마 합성 검증도 유효하다. 실제 제공 프로그램의 존재나 실자료에서의 동작을 입증한 것은 아니다. 원래 D 계열 서버·설정은 소스 인계에서 제외됐고, 07 승인 환경에 현재 실행 가능한 제공 프로그램이 존재하는지는 미확인이다.

| 원천 의미 | 현재 구현·대응 | 미확정 및 다음 담당 |
|---|---|---|
| D0 text·tag·project·source 필터. 누락·빈 q는 무필터이나 공백만 있는 q는 그대로 LIKE 검색. 초안은 null·빈 문자열을 구별 | 기존 0.1 요청은 각각 전달하고 조용히 변환하지 않는다. 별도 정규화 snapshot 제공자의 검색 정책과 미지원 필터는 아래에 명시한다. | 07: D0 handler에는 role/review가 없음을 확인. 나머지 projection·대상 범위를 추가 확인. 09: 지원되지 않은 필터는 제공하지 않음. |
| D0 limit/offset은 불변 snapshot이 아님 | 원천 provider에 snapshot과 모든 질의 조건에 결속된 cursor 책임이 있다. 기존 응답 검증의 페이지 내부 검사와 별개다. | 07: 원천 변경·재시작·동시 갱신 때 불변성/중복/누락 검증. |
| 원천 문자열 비교·관계 ID 안정성 | 기존 검색 dataset asc + record ID asc/desc, 혼합 item kind/dataset/item ID asc를 유지한다. 조회 투영의 ID는 원천에서 제공한 값을 사용한다. | 07: 원천 ID의 dataset 범위·안정성, tie-breaker·문자열 순서 확인. |
| 관측·후보·확정 관계 및 evidence, D0 lineage | assertion 상태·양끝 dataset·evidence는 받은 그대로 유지한다. lineage/후보를 결정 사건으로 바꾸지 않는다. 미확인 history를 AVAILABLE 빈 배열로 채우지 않는다. | 07: endpoint 유형과 evidence 식별 범위, 실제 원장 읽기 지원/권한 확인. |
| 검색 근거·원래 locator·parse issue·lineage | 조회 투영은 원형을 덮어쓰지 않는다. 0.1에 없는 필드는 투영만으로 보존을 주장하지 않는다. | 07: 원천 추적 식별과 손실 없는 별도 보존 경계 제시. |
| B 저장 본문·원출처·좌표 | 원천이 제공한 대상과 버전·해시·단위를 분리한다. B 저장 본문 검증을 Recovery 원출처 검증으로 승격하지 않는다. | 07: source identity/version, 대상·본문 접근·인코딩·좌표별 비민감 매핑. |
| 대형 기록 | 기존 HTTPS transport 수신 제한 기본 8 MiB, 설정 범위 1–64 MiB. 제한 초과는 실패이며 잘린 성공이나 빈 본문이 아니다. | 07: 실제 필요성과 안전한 분할 규격. 우선 가능한 단일 기록 읽기를 진행한다. |

D0 참조 화면에 있는 검색/상세 응답 외형은 실제 파일·DB 스키마가 아니다. 관리 Chat이 07 계약에서 기존 D0 루프백 제공 프로그램의 존재를 확인해 전달했다. 07에 필요한 것은 승인된 실행환경의 **불변 snapshot 증거, 응답 필드/NULL/ID/상태 매핑, source/version/대상/좌표 매핑**이다. 실제 자료·경로·주소·키·원본 로그는 보내지 않는다. 07은 별도 Chat이므로 이 문서의 항목을 실제로 받았거나 작업을 수행했다고 가정하지 않는다.

### 출처 읽기 확장과 09 작업 조건

기존 `UTT-ACCESS-READ/0.1.0`의 요청·응답과 스키마는 그대로다. 별도 `contracts/utt-source-read.schema.json`의 `UTT-SOURCE-READ/0.1.0`을 사용한다. `POST /api/source` 요청은 정확히 다음 여섯 필드이고, 값은 모두 비어 있지 않은 문자열이다. `source_version`을 모르면 본문을 추측하지 않고 기존 detail 메타데이터만 표시한다.

```json
{"contract_version":"UTT-SOURCE-READ/0.1.0","dataset_id":"fictional-dataset","snapshot_id":"fictional-snapshot","record_id":"fictional-record","source_id":"fictional-source","source_version":"fictional-source-v1"}
```

응답은 `{contract_version,status,result,error}`다. OK의 result는 다섯 identity를 그대로 반영하고 `source`(변경 없는 0.1 출처 메타데이터), `content`, `locator_verification`을 포함한다. `content`는 `availability`, `target_kind`, `target_id`, `target_version`, `locator_target_version`, `text`, `text_sha256`이다. AVAILABLE은 대상·버전·완전한 UTF-8 텍스트와 그 텍스트의 SHA-256을 갖는다. SOURCE_BODY의 target_id는 source_id, RECORD_BODY의 target_id는 record_id이며 기록 본문을 출처 본문으로 자동 대체하지 않는다. source.version과 target_version은 서로 다를 수 있다. metadata의 `content_sha256`은 원본 파일 바이트 해시일 수 있어 텍스트 해시와 자동 동일시하지 않는다. 사용할 수 없는 본문은 null이며 MISSING·UNKNOWN·INACCESSIBLE·NOT_SUPPORTED를 구분한다.

`locator_verification.status`는 VERIFIED·UNVERIFIED·NOT_SUPPORTED·MISMATCH·NOT_AVAILABLE 중 하나다. 현재 UTF-16 반열린 범위만 다시 검증한다. 기존 locator target이 정확히 SOURCE_BODY·RECORD_BODY 또는 합성 전용 SYNTHETIC_SOURCE로 명시되고 content 대상과 맞으며, 별도로 제공된 `locator_target_version`이 `target_version`과 같고 대상/조각 해시와 UTF-16 경계가 모두 맞아야 VERIFIED다. 기존 metadata의 VERIFIED만으로 당회 검증을 통과시키지 않는다. byte/코드포인트와 다른 target은 미지원이며 원래 locator는 보존한다. `locator_target_version`이 null이면 UNVERIFIED다.

형식 오류와 구 버전 이름으로 확장 호출은 400 + INVALID_REQUEST/UNSUPPORTED_CONTRACT, 명시 제공자 미연결은 200 + NOT_SUPPORTED 오류 봉투다. version/snapshot 불일치와 연결 실패를 구분한다. 검증되지 않은 오류 문구는 고정된 비민감 문구로 바꾼다. 기존 HTTPS transport에는 이 확장이 연결되어 있지 않으며 미지원 응답이다. source endpoint는 실제 경로나 URL을 받지 않는다.

09는 이 저장소에 반영된 출처 계약 커밋을 기준으로 UI 전용 브랜치를 만들고 `app/web/`과 UI 전용 시험만 변경한다. detail에 표시된 source metadata의 정확한 identity/version을 사용해 `/api/source`를 호출하고 응답 범위·상태를 확인한다. 메타데이터 확인, SOURCE_BODY 실제 본문 표시, RECORD_BODY 대상, 위치 VERIFIED를 각각 표시한다. 미지원·누락·미확인·접근 불가·연결 실패·버전 불일치를 구분하고, VERIFIED일 때만 표시된 정확한 대상 텍스트를 강조한다. 실제 URL/파일 경로를 열거나 현재 기록 본문을 출처 본문으로 대체하지 않는다. 기존 검색·페이지·상세 UI를 재개발하지 않는다. 합성 대상·별도 출처 본문, 다중 출처, 응답 경쟁, 누락 버전/해시, 이모지 경계, 각 실패 상태를 시험한다. PR에 기준 계약 커밋과 합성 결과를 명시하면 08이 브랜치·변경 범위·결과를 직접 확인하고 병합한다. 자동 전달·PR 제출을 가정하지 않는다.

### 동일 프로세스 정규화 snapshot 제공자

`SnapshotProvider`는 새 `UTT-NORMALIZED-SNAPSHOT/0.1.0` 형식의 **명시적으로 지정된 단일 UTF-8 JSON 파일**을 읽는다. 이는 D0 원형 로더가 아니며, 07이 native D0 필드와의 대응을 확인하기 전 실제 호환을 주장하지 않는다. 현재 원천 데이터를 이 형식으로 복사·변환하는 절차는 제공하거나 자동 실행하지 않는다.

입력 최상위 필드는 `format`, `dataset`, `records`, `projects`, `components`, `relations`, `history`, 선택적 `source_contents`다. dataset은 기존 dataset 필드 중 snapshot_id만 빼고, 각 record/project/component/relation/event는 기존 0.1 스키마 형식이다. history 항목은 `{record_id,history_status,related_record_ids,relations,events}`다. 항목이 없는 기록의 history는 UNKNOWN이다. `source_contents` 항목은 `{record_id,source_id,source_version,content}`이며 content 형식은 위 source 확장과 같다. 해당 source는 그 record의 source_refs에 같은 ID·version으로 있어야 한다. source_contents가 없으면 NOT_SUPPORTED이며 기록 본문 대체가 없다. 합성 생성 예제는 `tests/test_snapshot_provider.py`의 `normalized_payload()`이며 실자료 변환 예제가 아니다. Locator target은 경로·URL을 브라우저로 내보내지 않도록 안전한 영문 기호 식별자만 허용한다. 07이 ID·evidence·원천 trace에 실주소/경로를 넣지 않도록 별도 확인해야 한다.

최대 입력 크기는 32 MiB이며 단일 파일을 메모리에 보유하고, 요청마다 지정한 같은 파일을 다시 읽어 바이트 SHA-256을 비교한다. 파일 내용이 바뀌면 그 인스턴스는 영구적으로 VERSION_MISMATCH가 되어 명시적으로 재시작해야 한다. snapshot ID는 캡처된 정규화 파일 바이트의 SHA-256이다. cursor는 256-bit 무작위 불투명 서버측 핸들이며 프로세스 메모리에 offset·키가 있는 조건 HMAC·만료 시각만 보관한다. dataset·snapshot·원래 query 값·모든 필터·sort·page_size를 묶고 15분 또는 재시작 후 거부한다. 활성 핸들은 최대 10,000개이며 한도에서 기존 핸들을 지우지 않고 새 발급을 실패시킨다. 이 보장은 캡처된 정규화 이미지에만 적용하며 원천 입력의 모든 범위, 캡처 전·중 ABA 부재나 native D0 불변성을 증명하지 않는다.

이 제공자의 검색은 title·display_summary·AVAILABLE raw_text에 대해 NFKC+casefold 부분 문자열로 수행한다. query null, 빈 문자열, 정규화 후 공백만은 결과 필터를 적용하지 않되 cursor의 키가 있는 조건 결속에는 각 원래 값을 따로 반영한다. 이는 이 제공자에 한정된 정책이고 D0 실제 검색 규칙은 아니다. document_role/review_status는 항상 INVALID_REQUEST다. 0.1에 tag/source 필드는 없으며 조용히 버리지 않는다. project 필터는 모든 기록의 관계 상태가 AVAILABLE이고 관계가 USER_CONFIRMED인 경우에만 적용하며, 불확정 관계를 없는 것으로 처리할 상황이면 INVALID_REQUEST다. 정렬은 Unicode 코드포인트 문자열 순서다.

07의 승인된 실행환경에 이미 이 형식의 명시적 단일 입력이 있고 접근이 승인된 경우에만 runtime 설정으로 `UTT_RUNTIME_APPROVED=1`과 `UTT_SNAPSHOT_PATH`를 주입하고 `python3 -m app.server --mode readonly --snapshot-env`를 실행한다. 값은 코드·명령 인자·로그·GitHub로 보내지 않는다. 디렉터리 탐색, 새 자료 이전, 자동 복원·캐시 생성을 하지 않는다. `--snapshot-env`와 `--connect-env`는 동시에 쓸 수 없다. 운영 입력·원천 호환·권한 분리·실행환경 선택 및 실제 실행은 아직 07 확인 전이다.

### 계약 커밋과 비민감 검증 결과

09의 요청·응답 구현 기준과 07에 전달할 실행 코드 기준은 `29bf52ee937896ef958dd55a3d9935cae79c1f96`이다. 이 커밋에는 이 절의 상태 문서만 빠져 있으며 provider/server/스키마/시험은 이후 상태 문서 커밋과 동일하다. 07은 승인 환경에서 이 정확한 코드 SHA를 확인해 실행하고, 실행환경 식별·버전과 각 단계 PASS/FAIL/NOT_RUN/NOT_SUPPORTED만 반환한다. 메타데이터, 별도 출처 본문 열기, 위치 검증을 따로 보고한다. 07의 실자료 시험 결과는 아직 받지 않았다. 09 출처 UI는 PR #5로 검토·병합했다. 실제 출처 본문과 좌표 검증은 07의 승인 환경 결과가 필요하다.

합성 검증 `20261104T163049Z-d04c9719`: 11단계 PASS. Python 74개, Node 23개, 기존 전체 스키마 검증 61건, 원형 38개, 기존 Chromium 및 09 기존 UI의 Chromium 8개 묶음이 통과했다. 새 파일 제공자를 직접 사용하는 합성 파일→같은 프로세스 서버→검색·두 페이지·상세→별도 출처 본문 흐름, cursor 결속·재시작·파일 변경, 미지원 필터, 출처 대상/버전/해시/좌표 및 오류를 확인했다. 출처 확장의 독립 스키마·의미 시험도 포함한다. 실제 D0 호환·실자료·운영 접속의 증거는 아니다.

## 2026-11-05 — Work 후속 매핑 정정

관리 Chat이 `UTT_DATA_CONTRACT_v1.json`의 `work_mapping_update_2026_11_05`를 읽고 아래 비민감 내용을 전달했다. 08은 Library 링크에서 로그인 화면만 보았고 파일 원문을 직접 읽지 못했다. 이 결과는 관리 Chat이 전달한 근거다. 07은 기존 D0 루프백 HTTP 읽기 프로그램의 존재와 고정 자료 검사·offset 페이지를 확인했다. 해당 세션이나 offset은 `UTT-ACCESS-READ/0.1.0`의 불변 snapshot/cursor를 증명하지 않는다. 07이 이 절을 이미 받거나 같은 코드를 실행했다고 가정하지 않는다.

- D0 `q`는 제목·본문·record ID의 SQLite LIKE 검색이고 역슬래시·퍼센트·밑줄을 이스케이프한다. 누락/빈 q만 무필터다. **공백만 있는 q는 trim하지 않고 LIKE 조건으로 사용한다.** Unicode 정규화·casefold는 확인되지 않았다. 정규화 snapshot 제공자의 NFKC+casefold·공백 정책은 이 D0 동작과 다르다.
- `tag`는 태그 일치, `project`는 프로젝트 ID 일치, `source`는 source ID 일치 또는 source 이름 부분 검색이다. 독립 필터는 함께 적용된다. D0 handler에는 document_role·review_status 필터가 없으므로 tag/category/review_state를 임의로 대응하지 않는다. 기존 `priority/title/id/status` 정렬과 초안 ID 정렬 및 아직 미확인인 Unicode 비교를 구분한다.
- 기존 프로젝트 관계 테이블에는 독립 영구 relation ID가 없다. ID를 만들어 영구성을 주장하지 않는다. null을 빈 문자열로 만든 항목은 원래 detail을 참조해야 한다. 하나의 source에 여러 evidence·locator·검증 이력이 있으면 모두 원형과 연결해 보존하며 첫 근거만 선택하거나 임의 병합하지 않는다.
- 기존 원장 투영·원출처 본문/좌표·승인 실행환경과 개발 도구 접근 차단은 미확인이고 실제 사용자 흐름은 전부 NOT_RUN이다. B 저장 본문에서 확인한 좌표를 Recovery 원출처 좌표로 취급하지 않는다.

07에 필요한 비민감 확인은 (1) 현재 설치된 D0 프로그램이 인계 참조의 세션·records/detail 응답과 같은지, (2) 고정 자료 검사가 대상 전체와 요청 전후 갱신·교체를 어떻게 감지하며 불변 snapshot을 어떻게 입증하는지, (3) detail의 필드·NULL·record/project/source/evidence 식별·다중 locator 대응과 허용 오류 의미, (4) 관계 영구 ID가 없을 때 지원 가능한 안정적 조회 범위, (5) source 본문·대상 버전·인코딩·좌표 및 승인 환경의 개발 도구 접근 차단 여부다. 실제 자료·주소·경로·키·원본 로그는 요청하지 않는다.

### 필터 지원 표기 — 독립 계약

기존 `UTT-ACCESS-READ/0.1.0` 스키마·요청 필드를 바꾸지 않고 같은 origin의 `GET /api/features`가 `UTT-READ-FEATURES/0.1.0`을 반환한다. 응답은 `{contract_version,filters}`이고 filters의 `query`, `project`, `document_role`, `review_status`, `tag`, `source`는 각각 `SUPPORTED`, `CONDITIONAL`, `UNSUPPORTED`, `UNKNOWN` 중 하나다. 이 표기는 코드 기능이지 연결 성공이나 원천 권한 확인이 아니다. tag/source는 기존 0.1 요청에 해당 필드가 없으므로 D0에 별도 필터가 있더라도 이 경로에서는 UNSUPPORTED다. 임의 callback이나 HTTPS 원격 공급자는 query/project/role/review가 UNKNOWN이다.

정규화 snapshot 제공자의 합성 응답 예:

```json
{"contract_version":"UTT-READ-FEATURES/0.1.0","filters":{"query":"SUPPORTED","project":"CONDITIONAL","document_role":"UNSUPPORTED","review_status":"UNSUPPORTED","tag":"UNSUPPORTED","source":"UNSUPPORTED"}}
```

기존 합성 공급자는 query/project/document_role/review_status가 SUPPORTED이고 tag/source는 UNSUPPORTED다. 미연결·미확인 원격의 예는 query/project/document_role/review_status가 UNKNOWN이고 tag/source는 UNSUPPORTED다. 자동 fallback은 없다. `CONDITIONAL` project는 모든 원천 관계가 필요한 완전성 조건을 만족해야 하며, 현재 per-dataset 사전 확인 수단은 없다. 09는 document_role·review_status가 SUPPORTED일 때만 해당 선택을 활성화한다. UNSUPPORTED는 비활성화하고 UNKNOWN은 미확인으로 표시한다. CONDITIONAL project는 성공을 가정하지 말고 현재는 비활성화한다. query가 UNKNOWN이어도 검색어 없는 목록 읽기를 막지 않으며, 사용자 검색 시도는 지원 미확인임을 표시하고 제공자의 명시적 성공·실패를 따른다. 09가 별도의 정식 조건 표현을 제안하면 08과 계약을 갱신한다. `/api/features`를 읽을 수 없거나 버전·형식이 맞지 않으면 미확인으로 처리한다. 필터 UI만 허용된 범위에서 수정하고 기존 `/api/source` UI 제출과 함께 별도 브랜치/PR로 08에 인계한다.

### 기존 D0 루프백 호출 경계

`app/d0_adapter.py`의 `D0Client`는 승인 환경에서 명시적으로 지정된 같은 호스트의 literal `127.0.0.1` HTTP endpoint에 한해 인계 참조의 session·records·detail·summary·filters GET을 호출한다. 지연 세션 취득과 `X-UTT-D0-Session`은 서버측 메모리에만 유지하며 redirect·프록시·재시도·export·쓰기·탐색·캐시는 하지 않는다. 이 별도 루프백 경계는 기존 HTTPS transport의 TLS 검증을 완화하지 않는다. 원천 응답의 모든 evidence·locator·필드·NULL은 원형 JSON으로 보존하고 오류에 주소·질의·본문·세션을 노출하지 않는다. 보존된 응답은 승인 환경을 벗어나거나 로그/GitHub로 전송하면 안 된다.

`records`는 D0의 q/tag/project/source/sort/offset/limit을 그대로 전송하며 null을 생략하고 빈 문자열과 공백을 서로 바꾸지 않는다. 기존 D0 서버가 LIKE 이스케이프·검색·정렬을 수행하므로 중복으로 변환하지 않는다. `document_role`/`review_status`는 거부한다. 상세 조회는 원형을 읽으며 임의 관계 ID, cursor, 정본 상태, 승인 사건, 출처 본문을 만들지 않는다.

**아직 UI의 실제 D0 읽기 제공자는 아니다.** 기존 offset에 불변 snapshot 증거가 없고 프로젝트 관계의 영구 ID·상세 투영/NULL/다중 evidence 의미가 확인되지 않아 `D0Client.read()`는 네트워크 호출 없이 `UNSUPPORTED_CONTRACT`를 반환한다. 따라서 위 클라이언트의 합성 D0 서버 검사는 기존 프로그램과의 실제 접속이나 검색→목록→상세의 운영 접합을 뜻하지 않는다. 07이 위 질문의 비민감 답과 같은 코드 커밋의 실행 결과를 제공하면, 08이 현재 공급자를 재사용해 충족 가능한 부분을 투영하고 충족할 수 없는 기능은 명시적으로 미지원으로 유지한다.

### 09·07 전달 기준과 검증

필터 계약·D0 루프백 클라이언트·UTF-16 경계 수정을 포함한 코드 기준 커밋은 `7ca090812f343d197aeba979261213f8c370362d`다. 09는 이 커밋을 포함하는 최신 main을 기준으로 기존 출처 UI에 더해 위 `/api/features` 조건을 반영한다. 09는 app/web과 UI 전용 시험만 별도 브랜치에서 수정하고 PR에 기준 SHA·필터 상태별 시험과 출처 조회 결과를 명시한다. 08이 PR을 직접 읽고 검토·통합한다. 09 UI는 PR #5로 검토·병합했다.

07은 접근 분리된 승인 환경에서 동일 코드 SHA를 확인하고, 위 D0 호출 경계와 필요한 불변성·필드 매핑에 대해 비민감 결과만 제공한다. 실제 연결이나 UI 조회 성공을 가정하지 않는다. 기존 프로그램의 native 형식 호출을 시험할 때도 원문·실제 ID·요청·응답·경로·주소·세션·원본 로그를 개발 환경이나 GitHub로 반환하지 않는다.

이번 합성 검증은 변경된 D0·출처·필터·서버 경계와 인계 무결성 55개 시험 PASS다. 합성 loopback D0 서버에서 세션, 조건을 유지한 검색, 두 offset 페이지, 원형 상세를 실제 `D0Client`로 읽었고, q의 null/빈/공백, 필터 병용·역슬래시/퍼센트/밑줄 전달, 오류·redirect·응답 제한을 확인했다. 별도 출처 검증에서 `앞 🧭 동쪽\n끝`의 시작=끝=3은 MISMATCH/INVALID_RANGE이며 정상 UTF-16 경계의 빈 구간과 빈 본문은 유지된다. 이는 07의 실제 연결이나 운영 적용이 아니다.


## 2026-11-05 — 09 UI 통합과 07의 다음 확인

- UI 브랜치 `ui/09-source-read`는 [PR #5](https://github.com/whitewillbelive-tech/utt-interface/pull/5)로 검토·병합했다. 병합 커밋은 `6c21789dd6a374fc8ccfff4c54bb398299aa7603`, UI head는 `eab317110f1ef89c2d08cfc375535a816cdcf913`이다. 기존 공용 계약과 D0Client.read()의 `UNSUPPORTED_CONTRACT` 안전장치는 변경하지 않았다.
- 검토 중 미지정 검색어를 빈 문자열로 보내던 문제를 수정해, 빈 입력은 `null`, 비어 있지 않은 공백·검색어는 그대로 보낸다. 지원 미확인·미지원 role/review는 활성화하거나 요청하지 않는다. 조건부 project는 비활성화한다.
- 출처 metadata version·원본 바이트 hash와 target_version·UTF-8 text_sha256을 각각 유지한다. 요청의 dataset/snapshot/record/source/version을 검증하고 대상/버전·본문/조각 해시와 UTF-16의 시작·끝 경계를 다시 확인한 VERIFIED에서만 그 응답 본문을 강조한다. 카드별 재시도·다른 기록 이동·목록 복귀 후 늦은 응답을 차단한다.
- `tools/browser_check.py` 변경은 새로운 독립 source 본문과 기존 기록 본문을 구분해 검사하기 위한 UI 회귀 시험 수정이다. 신규 UI 시험 파일을 원형 검증 정책에 등록하고 필터·출처 Chromium 시험을 정식 runner에 추가했다.
- 합성 검증 `20261104T183347Z-07388c13`: 13단계 PASS, Python 93개, Node 46개, 스키마 61건, Chromium 기존 13·탐색 8·필터 7·출처 7묶음 및 인계 원형 38개. 테스트한 코드 tree `112a94dd36bc95ea82dc48704d3e35c8da68f219`은 UI 병합 커밋과 동일하다. 실제 원천 접속이나 운영 검증은 아니다.

07이 승인된 자료 환경에서 확인해 **비민감 결과만** 전달할 다음 항목:

1. **snapshot:** D0 고정 자료 검사가 검사하는 파일·구성 범위와 변경·교체 감지 시점, 모든 목록/상세/출처 요청을 동일한 불변 기준에 묶을 수 있는 식별·버전 근거. 재시작·만료·동시 갱신 때 cursor를 거부하거나 유지하는 규칙과 페이지 간 누락·중복 검사 결과.
2. **relation identity:** 영구 relation ID가 없는 기존 테이블에서 양끝의 유형·dataset 범위, 같은 양끝의 복수 근거 구분과 조회 중 안정성을 어떻게 보장하는지. 부족하면 관계 페이지를 미지원으로 둘 수 있는 범위. 임의 영구 ID는 만들지 않는다.
3. **detail/source:** record와 source·복수 evidence/locator의 연결 필드 및 cardinality, 원래 NULL·빈 문자열 의미, source metadata version과 별도 본문 target/version·원본 바이트 hash·텍스트 hash의 대응, locator 대상·인코딩·단위·범위·fragment hash와 권한/누락 상태. B 저장 본문과 Recovery 원출처를 별도로 확인한다.
4. **history:** 실제 원장 읽기 지원 여부·접근 범위와 빈 이력/미확인/접근 불가 구분, 원래 event contract·supersedes·payload 보존 및 사용자 승인 의미. D0 lineage·후보를 결정 사건으로 변환하지 않는다.

07은 동일 실행 코드 SHA·환경 종류/버전, 검색→목록→다음 페이지→상세·출처 메타데이터→별도 출처 본문→위치 검증의 PASS/FAIL/NOT_RUN/NOT_SUPPORTED와 필요한 코드 차이만 반환한다. 실제 검색어·ID·본문·원장·주소·경로·키·원본 로그는 반환하지 않는다. 실행환경의 개발 도구 접근 차단과 운영 적용도 별도로 확인한다. 현재 07 동일 코드 실제 실행과 운영 적용은 미확인이다.

## 2026-11-05 — 07 원천 검증 v2 수신

관리 Chat은 `UTT_DATA_CONTRACT_v1.json` 버전 2의 `work_source_verification_2026_11_05`를 읽고 아래 비민감 결과를 전달했다. 이는 전달받은 근거이며, 08이 계약 원문이나 실제 원장을 직접 열어 확인한 것은 아니다.

- 보존 데이터용 D0 로컬 API는 존재하지만 승인된 사용자 실행환경에 현재 설치·실행 중인지는 미확인이다. 시작 시 DB 해시 검사는 있으나 매 요청의 교체·변경 감지는 없다. 따라서 native offset을 snapshot-pinned cursor로 승격할 근거가 없다. `D0Client.read()`의 `UNSUPPORTED_CONTRACT`를 유지한다.
- 프로젝트 관계의 독립 영구 ID는 확립되지 않았고 기존 테이블의 일부 NULL·빈 문자열은 합쳐졌을 수 있다. 임의 영구 ID·원형 값 복원·다중 evidence 병합을 하지 않는다.
- 출처 metadata와 실제 접근 가능한 원출처 본문은 별개다. 저장 본문을 Recovery 원출처로 대체하지 않는다. 본문·바이트 hash·텍스트 hash·locator 단위와 대상 버전을 구분한다.
- 실제 D1.1 원장은 열지 않았다. 기존 D1.1 이력 조회 보조 함수는 원장 부재 시 디렉터리와 빈 SQLite 원장을 만들 수 있으므로 읽기 전용으로 그대로 사용하지 않는다.
- 사용자 전용 승인 환경과 개발 도구 접근 차단은 미검증이다. 실제 검색→목록→다음 페이지→상세→출처는 전부 NOT_RUN이다. 실제 운영 접속·배포를 주장하지 않는다.

원천 전체 UTT 투영에 필요한 최소 비민감 근거는 (1) 동일 dataset/snapshot의 불변성·변경 감지와 페이지 수명, (2) 관계의 안정적 식별·복수 evidence 및 원본 NULL 대응, (3) detail/source 본문·metadata·대상 version/hash/좌표/접근 상태, (4) D1.1 native SQLite DDL·사건 직렬화·순서/정정/취소/무결성 규칙과 사용자 원장 읽기 권한, (5) 승인 실행환경의 개발 도구 차단과 실제 단계별 결과다. 실제 원장·자료·경로·주소·인증정보·로그는 받지 않는다.

### 독립 읽기 계층과 경계

`D0-STRUCTURAL-INSPECTION/0.1.0`은 이미 호출자가 가진 native D0 JSON을 메모리에서 구조적으로 살피는 별도 형식이다. 원래 문서의 필드·순서·NULL/빈 문자열·반복 관계·여러 evidence/locator를 수정·병합하지 않고 보존한다. 문서 내부 JSON pointer는 한 응답의 위치일 뿐 영구 관계 ID가 아니다. B 저장 본문을 원출처 본문으로 승격하지 않으며 원출처 접근성·history·관계 ID·snapshot은 UNKNOWN, 의미 매핑은 UNVERIFIED다. 이 계층은 검색을 실행하거나 기존 READ/0.1 OK 응답·cursor·승인 사건을 만들지 않는다.

`D11Reader`는 **새로 정의한** `UTT-D11-PORTABLE-HISTORY/0.1.0` 형식만 아는 독립적 비변경 reader다. 기존 native D1.1 원장 테이블/사건 직렬화 규격이 저장소에 없어 호환을 주장하지 않으며 기존 부작용 있는 보조 함수를 호출하지 않는다. 기존 원장에 이 형식의 스키마를 만들거나 변환·복사하는 동작은 제공하지 않는다. 생성 시 실제 파일을 열지 않고, 호출자가 승인된 오프라인 불변 파일임을 명시한 뒤 정확한 파일을 OS 읽기 전용으로만 캡처한다. 원본 경로를 SQLite에 넘기지 않으며 캡처 바이트만 메모리 SQLite에 역직렬화한다. WAL 모드나 `-wal`/`-shm`/`-journal`이 있으면 자동 복구·체크포인트 없이 거부한다. 디렉터리·원장·SQLite sidecar·디스크 캐시 생성 기능은 없다. OS/runtime의 필요한 안전 수단이 없으면 미지원이다.

새 portable format은 메타 1행과 순서·ID·사건 원형 UTF-8 JSON BLOB을 가진 두 물리 테이블을 명시적으로 요구한다. 사건 payload의 계약 버전·판단·정정·취소·supersedes·기존 provenance/진단을 변경하지 않는다. 포맷이 선언한 ordinal·사건 ID·record별 decision_version·선행 사건의 결손은 PARTIAL, 중복/역순/순서 충돌은 CONFLICT로 나타낸다. 원장 파일 부재 MISSING, 접근 거부 INACCESSIBLE, WAL/미지원 원형 NOT_SUPPORTED 등은 빈 이력과 다르다. AVAILABLE은 이 휴대용 포맷에서 구조적으로 읽혔다는 뜻일 뿐 삭제된 말미 부재·원장 진위·운영 승인을 증명하지 않는다. 반환한 내용은 승인 환경의 메모리에서만 다루며 실제 원장·ID·hash·경로를 Codex/GitHub/로그에 보내지 않는다.

이 두 계층은 아직 `app/server.py`·UI·실제 D0/D1.1 원장에 연결하지 않는다. 실제 원형 D1.1 스키마·직렬화·체인 시작과 순서·부분 이력·접근/동시성 규약을 07이 비민감하게 확인하기 전 새로운 포맷의 결과를 원형 호환 또는 운영 이력으로 표시하지 않는다. 동일 승인 환경의 실제 단계별 검증도 NOT_RUN이다.

합성 통합 검증 `20261105T073514Z-f1818646`: 전체 13단계 PASS, Python 120개, Node 46개, 기존 스키마 61건, Chromium 기존 13·탐색 8·필터 7·출처 7묶음, 원형 38개. D1 휴대용 입력에서 원본 파일·sidecar 불변, 부재·접근 거부·부분·순서 충돌, WAL 거부·읽기 중 교체 감지와 payload 보존을 검사했고 D0 중간 구조의 NULL/빈 값·다중 근거 보존도 검사했다. 실행 당시 변경된 작업트리에서 시험했으며, 결과를 커밋/원격 코드 tree와 대조한다. 실제 D0/D1.1 원장·원출처·운영 환경에 대한 시험은 NOT_RUN이다.

## 2026-11-05 — D0 읽기 경계 후속

사용자가 처음 `gate_real_read_1_2026_11_05` 반영을 요청했을 때 본문은 요청과 저장소에 없었고 Library 링크는 로그인 화면만 반환했다. 이후 사용자가 비민감 계약 JSON을 첨부해 08이 해당 gate와 후속 원천/환경 결과를 직접 읽었다. 아래 원문 수신·추가 판정이 현행이다. 이 항목은 불변성·필드 대응 승인이 아니며 실제 자료·주소·경로·키·로그는 요청하지 않는다.

확인된 이전 결과에 따라 D0 프로그램의 시작 시 파일 해시 검사는 매 요청·교체 감지를 증명하지 않는다. `D0Client.read()`는 기존 READ/0.1의 snapshot·cursor·정렬·관계 요구를 충족한다는 증거가 없으므로 `UNSUPPORTED_CONTRACT`를 유지한다. 실제 데이터셋의 내용이나 사용자 승인 상태를 추측해 연결 성공을 만들지 않는다.

### native D0 탐색 — 불변성 미확인

`app/d0_browse.py`의 `D0BrowseSession`은 승인된 같은 호스트에서 명시적으로 주입된 기존 `D0Client`를 사용한다. 별도 native 경계로 `search(q, tag, project, source, sort)` → `next_page(page)` → `detail(page, record_id)`를 제공하며 각 결과는 기존 `D0Evidence`와 `D0-STRUCTURAL-INSPECTION/0.1.0`을 통해 원형 JSON 및 구조를 보존한다. 검색어·NULL·빈 문자열·공백·tag/project/source·D0 정렬은 정규화하거나 UTT 필터로 바꾸지 않는다. 원래 source의 복수 evidence/locator와 B 저장 본문을 보존하고 원출처나 승인된 사건으로 승격하지 않는다. 반환 원문·ID·질의는 승인 환경 안에서만 다루며 로그·Codex·GitHub에 보내지 않는다.

결과의 `consistency=UNVERIFIED`는 고정이다. 이는 UTT-ACCESS-READ/0.1.0 응답·snapshot·cursor가 아니다. UI/HTTP에 연결하지 않았고 `D0Client.read()`는 `UNSUPPORTED_CONTRACT`를 유지한다. native offset 페이지 이동은 순서대로 현재 페이지에서만 허용된다. 이미 본 ID 중복, 정수/문자열 ID의 상세 요청 충돌, 총건수 변화, 조기 빈/짧은 페이지가 관찰되면 중단하고 새 검색을 요구한다. 이전 query의 페이지 객체·다른 세션의 페이지·현재 페이지에 없는 기록 ID는 거부한다. 보이는 ID 집합은 제한을 초과하면 중단하며 이전 ID를 버려 검사를 약화하지 않는다. 반환 결과와 현재 페이지만 보유하고 이전 응답·상세 본문을 세션에 캐시하지 않는다. 중복 검사용 ID 상태는 세션 비밀키로 생성한 고정 길이 HMAC만 보관한다. 이 API는 코드 호출용이며 browser/HTTP에 연결하지 않았다.

이 검사는 **불변성 보장이 아니다**. 같은 건수의 미확인 교체나 원천 변경은 중복·건수 검사에서 누락될 수 있다. 상세의 원래 identity 의미도 07 대조 전 UNVERIFIED이며 결과는 `IDENTITY_UNVERIFIED` 원형 증거로 표시한다. `canonical_record.id`가 명시되어 요청 ID와 다르면 원본 의미가 완전히 확정되지 않았더라도 보수적으로 충돌을 거부한다. ID가 같다고 원천 identity 매핑을 검증한 것은 아니다. 검색 결과가 비어도 원천 전체의 부재·최신성이나 페이지 사이 불변성을 증명하지 않는다. native 상세 원형에 lineage나 후보가 있어도 결정 사건·승인 상태로 바꾸지 않는다. 안정적인 UI 페이지가 필요한 READ/0.1 경로는 증거가 들어오기 전 계속 미지원이다.

합성 D0 HTTP 시험 서버는 `tests/synthetic_d0_server.py`에만 있고 명시적 임시 가상 파일과 marker를 요구한다. 실제 D0 schema나 원천 snapshot의 증거가 아니다. 별도 정규화 `SnapshotProvider`의 기존 합성 불변성 시험은 바이트 내용·조건에 묶인 cursor를 사용한다. 파일 바이트가 변경되면 거부하고 이전 바이트로 복구해도 기존 provider는 stale을 유지한다. 정확히 같은 바이트의 파일 교체는 같은 내용 snapshot을 유지하되, 프로세스 재시작 후 이전 cursor는 거부한다. 이 동작은 D0 원천에 적용하지 않는다.

07이 gate 본문을 전달하면 최소한 (1) 원천 불변성 및 서버가 동일 대상에 결속되는 증거, (2) 목록/상세 원본 ID와 NULL 및 각 검색·정렬·관계·출처 필드의 대응, (3) 다중 evidence/locator와 history 접근 상태를 대조한다. 사용자 실행환경의 실제 검색·목록·다음 페이지·상세·출처는 현재 NOT_RUN이다. 원천 파일·주소·경로·인증·로그는 반환하지 않는다.

### Work gate 원문 수신·추가 판정

사용자가 첨부한 비민감 `UTT_DATA_CONTRACT_v1.json`의 `gate_real_read_1_2026_11_05`와 `work_followup_source_and_host_2026_11_05`를 08이 직접 읽었다. 포장 버전은 `UTT_DATA_CONTRACT_PACKAGE/1`이고, 첨부 원문 스키마·초안·합성 예제는 현재 저장소 파일과 각각 동일하다. 이전의 gate 미수신 기록은 당시 상태다. 이 gate는 실제 자료가 없는 부분 원천 근거이며 기준 코드 `b47031e933c82e425381a20f56e021a4a213c61b`에 대한 실제 실행은 전 단계 NOT_RUN이다.

Gate는 실제 고정 dataset의 **전체 입력 범위**와 파일 교체·SQLite 보조 파일을 포함한 세션 내 불변성을 증명해야만 snapshot/cursor를 허용한다. 일시적 요청 전후 해시나 변경 후 복구(ABA)를 배제할 수 없는 검사, D0 startup 해시, 가변 DB의 SQLite immutable 옵션만으로는 불충분하다. 순서·고유 tie-breaker·모든 실제 필터/정렬/페이지 크기에 결속하고 위조·만료·불일치를 거부해야 하며, 민감 필터를 열람 가능한 cursor에 담지 않는다. 이 근거는 아직 확인되지 않았고 실제 native D0를 READ/0.1에 연결하지 않는다.

기존 D0의 관찰 관계와 중복 근거는 원형으로 보존한다. 원래 NULL이 이미 빈 문자열로 변환된 부분은 원형 근거 없이 되돌리지 않는다. 기존 원출처 본문 및 locator의 접근성은 미확인이다. D1.1 진단이 application_id `0x55544431`, events 테이블, seq/chain_hash를 기대했다는 사실은 전체 native DDL·직렬화·event 체계·호환성의 증거가 아니다. 07 추가 탐색에서도 안전한 source-only 원형은 찾지 못했고 실제 원장은 열지 않았다. portable D11 reader를 기존 원장에 연결하거나 실제 이력의 유효성을 추론하지 않는다.

사용자 전용 승인 환경·기존 데이터 접근·개발 도구와 자격증명/로그에 대한 OS 차단은 아직 확인되지 않았다. loopback, canary 시험, Work 보관, Python 실행만으로 권한 분리를 증명하지 않는다. 필요 시 07은 민감값 없이 환경 종류와 분리 검증 PASS/FAIL/UNKNOWN, 같은 코드의 실제 단계별 결과만 반환한다. 새 호스트·계정 생성이나 실제 데이터 이동은 이 gate에서 승인되지 않았다.

합성 공급자 `SyntheticProvider`와 정규화 `SnapshotProvider`는 각각 프로세스 내부의 무작위 불투명 cursor 핸들을 쓴다. 클라이언트 cursor에는 질의·필터 원문이나 무키 fingerprint가 없다. 핸들은 최대 10,000개, 고정 수명 15분이며 유효한 이전 페이지 핸들을 용량 확보를 위해 자동 퇴출하지 않는다. 정규화 파일에서 관찰된 읽기 실패·변경 뒤 복원은 기존 세션을 재개하지 않는다. 관찰하지 못한 A→B→A는 감지하지 못하나 이전에 캡처한 메모리 이미지를 계속 제공한다. 이것을 D0 원천의 불변성·ABA 차단 검증으로 해석하지 않는다.

이번 합성 검증 `20261105T090046Z-5ff498ce`: 전체 13단계 PASS, Python 165개, Node 46개, 기존 전체 스키마 61건, Chromium 기존 13·탐색 8·필터 7·출처 7묶음, 인계 원형 38개. 임시 가상 D0 파일과 loopback 서버로 새 native 탐색의 검색→두 페이지→상세 원형 조회를 실행했고 query/필터/정렬/원래 ID/NULL/복수 근거를 대조했다. 파일 변경·교체·재시작과 관찰 가능한 중복·누락을 시험하고, 동일 건수의 미관찰 교체는 판별할 수 없음을 확인했다. 별도 정규화 합성 이미지의 cursor 위조·문맥·만료·용량·재시작·ABA 한계를 시험했다. 시험은 커밋 전 작업트리에서 실행했으며 원격 commit tree와 비교한다. 원천 접속·보호된 환경·운영 적용의 증거가 아니다.

이번 native D0 탐색·불투명 cursor 구현의 코드 기준은 `25fd6f8edb770d087c53f4809bc460327504e5cb`이다. 이후 이 문서의 상태 기록만 추가했다. 07에서 동일 코드를 실행할 수 있는 승인 환경·개발 도구 차단이 확인되면 이 SHA와 비민감 단계 결과를 대조한다. 현재는 기존 UI의 실제 D0 검색/목록/다음/상세 연결과 별도 원출처 본문이 모두 NOT_RUN이며 운영 배포도 수행하지 않았다.
