# UTT 데이터 접근 계약 초안 — UTT-ACCESS-READ/0.1.0

상태: **설계 초안**, 기존 프로그램에 구현·합의된 계약이 아니다. 기존 UTT-READMODEL-1·UTT-CANONICAL-1이나 D1.1 사건을 교체하거나 개정하지 않는다. 전달 방식은 미정이며 파일, 함수 또는 향후 승인된 전송수단을 배제하거나 약속하지 않는다. 실제 DB와 이 초안 간 전수 매핑, 원문·출처의 재검증 및 실제 접합은 완료되지 않았다.

## 확인한 기존 계약과 관계

| 기존 계약/근거 | 확인된 사실 | 이 초안과의 관계 |
|---|---|---|
| UTT-READMODEL-1/1.0.0 | A/B의 기존 레코드 표현. dataset, record, original_ids, 본문 블록, 출처, project_links, stored_state, 분류, provenance와 review_issues 포함 | 기존 표현과 의미를 유지한다. 이 초안은 목록·검색·상세 질의의 상위 접근 계층이다. 이름이 같다는 이유로 동일한 직렬화 형식이라고 주장하지 않는다. |
| UTT-READMODEL-1/2.0.0 | A-legacy-v1, B-legacy-v1 어댑터, dataset_kind, baseline_link, legacy_payload, author_adoption. B는 LOCAL_REORGANIZED_PREVIEW 및 UNVERIFIED 기준 연결, 원문과 좌표 맵을 별도 보존 | 원래 adapter·legacy_payload 및 확인 상태가 별도 원형으로 보존돼야 한다. 로컬 미리보기를 운영 기준으로 승격하지 않는다. |
| UTT-CANONICAL-1/1.0.0 | dataset·identity·state·content·projects·sources·lineage·classification·provenance·legacy; 원문 타깃, hash, 위치 단위와 보존된 원본 표시 | 원문과 기존 locator를 재해석·덮어쓰지 않는다. 이 초안 record/source는 조회용 투영이며 canonical 대체본이 아니다. legacy와 변환 정보를 원천에서 추적할 수 있어야 한다. |
| D1.1 사건 | 무버전 D1 이벤트와 UTT-D1-EVENT/2. 정정·취소는 후속 이벤트이며, 이전 payload·ID·체인은 그대로 유지 | 변경 불가한 이력의 읽기 투영만 허용한다. `LEGACY_MISLABELED_PARENT_HASH`는 진단이지 payload 수정이 아니다. 구버전의 `b_db_sha256`을 B Quality 값으로 재해석하지 않는다. |
| D2 | 로컬 제안·영향 분석, 유효한 사용자 판단을 별도 재확인 | 후보 승인·제안·정본 승인을 같은 것으로 취급하지 않는다. 이 초안은 읽기만 정의한다. |

기존 코드에서 B의 `source_refs`, `project_links` 및 canonical `projects`, `sources`에는 근거 상태와 기존 표현이 별도 필드로 있다. 이 초안의 관계·출처 형식에 투영할 때 기존 값과 provenance를 Work에서 대조해야 한다. M0의 관측 프로젝트 연결과 승인된 component crosswalk는 별도 관계다. 확정 연결이 기록되지 않은 것은 부재의 확정이 아니다. 기존 명칭과 사용자 확정 소속은 구분하여 원형을 보존한다.

## 메시지와 의미

`utt-read-api.schema.json`은 JSON Schema 2020-12. `$defs/request`와 `$defs/response` 또는 최상위 `oneOf`를 검사한다. 이 스키마는 형식 검증이며 관계 참조 무결성·좌표의 실제 일치·인증·접근 통제·스냅샷 일관성을 증명하지 않는다.

모든 요청은 `contract_version`, `operation`, `snapshot_id`, `dataset_id`, `project_id`, `record_id`, `query`, `document_role`, `review_status`, `page_size`, `cursor`를 갖는다. 해당 없는 선택 조건은 null이다. `query: ""`은 명시적인 빈 검색어이며 `query: null`은 검색어 미지정이다. 공급자는 정책상 빈 검색을 지원하지 않을 수 있으나 잘못된 요청으로 일관되게 보고해야 한다. 내부 데이터 위치나 실제 경로를 전송할 필요는 없다.

| operation | 입력 조건 | OK의 `result` |
|---|---|---|
| `capabilities` | 읽을 dataset 선택; 처음 snapshot은 null 가능 | dataset 식별·종류·snapshot, legacy 계약 식별자, 지원 operation·sort, 최대 page size, writes_supported=false |
| `projects` | dataset과 고정 snapshot. 선택적인 프로젝트 조건 | 프로젝트·구성요소·명시적 관계 및 page |
| `search` | dataset·snapshot, 프로젝트·문서유형·검토상태·검색어 선택 | record 목록과 page. 일치 0건은 `OK` 및 빈 records |
| `detail` | dataset·snapshot·record_id | record, 관련 projects, sources. 원문·출처 접근 상태는 별도로 표시 |
| `related_history` | dataset·snapshot·record_id, pagination | 관련 records·relations·읽기 전용 events·history_status와 page |

`status=ERROR`는 result=null과 error의 code·message·retryable. `ITEM_NOT_FOUND`는 해당 snapshot에서 대상 ID를 찾지 못함, `SOURCE_MISSING`은 요청한 출처가 없거나 접근 경로의 누락을 확인함, `CONNECTION_UNAVAILABLE`은 읽기 연결 실패, `VERSION_MISMATCH`는 snapshot/커서가 오래되거나 서로 다름, `UNSUPPORTED_CONTRACT`는 계약 미지원, `INVALID_REQUEST`는 잘못된 요청이다. 검색 결과 없음, 자료가 없다고 확인된 것, 존재 여부 미상, 접근 불능을 혼동하지 않는다. 원천이 해당 사실을 구분할 수 없으면 `UNKNOWN`으로 유지한다. exception 텍스트에 원문·계정·민감 경로를 넣지 않는다.

## 식별과 상태

record_id, project_id, source_id, component_id, event_id는 비어 있지 않은 opaque 문자열이고 각 dataset 범위에서만 해석한다. 안정성은 공급자가 보장한 snapshot/version 범위에 한정한다. 재생성·재수집·새 dataset에서 같은 ID라고 동등하거나 다르다고 별개라고 단정하지 않는다. 원래 식별자를 `original_ids`로 보존하고 관계는 `from_dataset_id`/`from_id`, `to_dataset_id`/`to_id`를 명시한다. 동명이인·중복 이름·본문 유사성은 자동 병합 근거가 아니다.

state의 observation·review·proposal·adoption은 서로 독립이며 null은 확인 불가/정보 없음, 문자열은 출처가 명시한 상태 그대로다. 하나를 다른 상태로 변환하지 않는다. `APPROVE_FOR_FUTURE_PROPOSAL`은 CANON이나 운영 정본 승인이 아니다. labels와 original은 조회 목록에 전부 표현되지 않을 수 있으므로 모든 legacy 정보가 초안 record에 들어 있다고 주장하지 않는다. 변경 및 기록 조회 시 원천 계약을 함께 보존한다.

값이 없는 것과 빈 컬렉션을 구분한다. source_status, project_relations_status, history_status에 AVAILABLE/MISSING/UNKNOWN/INACCESSIBLE/NOT_REQUESTED를 표시한다. AVAILABLE + 빈 배열은 **해당 snapshot 및 조회 범위에 기록된 항목 없음**이며 전체 세계에서 부재를 입증하지 않는다. 미확인 데이터를 []만 반환해 완료처럼 보이게 하지 않는다. 원문은 AVAILABLE인데 빈 문자열일 수 있다. MISSING/UNKNOWN/INACCESSIBLE/NOT_REQUESTED이면 raw_text는 null로 표시하고 파생 요약으로 원문을 대체하지 않는다. 이를 스키마 외 의미 검증으로 검사한다.

## 원문·출처·좌표

출처 ID는 dataset 범위다. source version/hash와 locator target/version/hash는 출처가 제공한 근거를 유지하고 없으면 null·UNKNOWN이다. locator는 명시적 target과 `unit = unicode_code_point | UTF-16 | byte`, 0-based `[start,end)`(end 제외), 대상 해시, fragment 해시, verification으로 구성된다. 바이트 위치는 원천의 인코딩 및 바이트 시퀀스와 묶어야 하며, 미확정이면 이용하지 않는다. UTF-16의 오프셋은 16-bit code unit 수이며 code point·바이트와 동등하지 않다. 변환, 자모 결합, 줄바꿈 변경 이후에는 원문 해시와 구간 일치를 다시 확인해야 한다. `start<=end`와 원문 범위, hash, 코드포인트/UTF-16 surrogate 경계, 조각 일치는 의미 검증이 필요하다. 상속 좌표나 이전 검증 주장을 당회 검증으로 승격하지 않는다.

## 페이지 일관성

최대 `page_size`는 100. 공급자는 실제 지원하는 더 낮은 한도를 capabilities에 표시한다. 동일 snapshot에서 정렬 기준을 지정하고 최종 동률 해소에 데이터셋 범위 stable record/project/event ID를 사용한다. 문자열 비교의 Unicode 정렬 방식과 정렬 방향은 공급자가 명시·고정해야 한다. `sort` 배열은 사용한 정렬 필드와 방향, 최종 ID tie-breaker를 순서대로 기록한다. 필터·정렬·dataset·snapshot·page_size와 연결된 opaque cursor를 사용하고 변조·범위 불일치는 거부한다. 다음 페이지 전 데이터가 바뀌면 동일 snapshot이 유지되는 경우에만 계속하고, 유지가 불가능하면 `VERSION_MISMATCH`로 중단하여 처음부터 다시 조회한다. `next_cursor: null`은 해당 snapshot에서 끝이다. 기존 공급자가 안정적인 snapshot 또는 cursor를 지원하지 않는다면 capability 미지원으로 표시하고 보장했다고 주장하지 않는다.

## 검증과 쓰기 경계

이 형식의 schema 문법·예제 시험과 실제 DB 대조는 분리한다. 실제 자료의 프로젝트 관계·기록·출처·좌표·상태를 Work 내에서 각 데이터셋과 기준 버전에 대해 대조해야 하며, 이 계약 문서와 합성 시험만으로 이를 PASS로 간주하지 않는다. 특히 M0는 본문·세부 위치를 포함하는 DB가 아니며, M0만으로 D-0 좌표 대응을 실증할 수 없다. 현재 기준 및 스냅샷 능력, source availability, 정렬 구현, 전체 관계 조인, history 투영은 각 어댑터 검증 전 미확정이다.

후속 쓰기·승인·정정·취소는 별도의 명령 계약이 필요하다. 기존 이벤트·승인 및 동시성 규칙의 평가와 사용자 승인 후 별도 설계한다. 이 초안은 새 정본, 원장, API 서버, 동기화 또는 운영 배포를 만들지 않는다.

## 페이지의 항목 범위와 기존 D-0 대조

`relation_id`는 관계가 출처 안에서 안정적으로 식별 가능한 경우 그 dataset의 범위 내 ID다. 동일 양끝이라도 서로 다른 근거·범위의 관계는 별개 ID를 쓴다. 안정적 관계 ID가 없는 공급자는 임의의 영구 ID나 연속 읽기 보장을 만들어내지 말고 해당 정렬 capability를 지원하지 않는다고 밝혀야 한다. `projects`의 page는 프로젝트·구성요소·관계의 합쳐진 순서에 적용한다. `related_history`는 관련 기록·관계·사건의 합쳐진 순서에 적용한다. 두 응답은 하나의 페이지를 종류별 배열로 나눠 담으며, page.sort는 항목 종류, dataset ID, 그 종류의 고유 ID를 포함한다. 각 배열의 순서는 전체 순서에서의 상대 순서다. 조회 도중 바뀌지 않는 고유 ID와 동률 해소 기준을 확보할 수 없으면 해당 페이지 기능은 지원하지 않는다. 검색 page는 record 배열에 적용한다.

Work 전용 D-0 보존본의 이번 점검에서는 records와 상세 JSON의 출처·프로젝트 관계를 조인해 대조하고 기존 offset/limit 방식의 페이지 순서를 같은 불변 보존본에서 점검했다. B 본문에 대한 코드포인트·UTF-16의 반열린 구간 및 fragment hash도 별도로 대조했다. 이는 B 저장 본문에 대한 확인이고 Recovery 원본의 출처 좌표를 확인한 것이 아니다. 결정 원장은 이번 대조에서 열지 않았다. 현재 Dropbox의 최신성·실제 접합 결과는 확인하지 않았다. 기존 D-0의 offset/limit 페이지가 이 초안의 snapshot-pinned cursor를 이미 구현한다는 뜻도 아니다. 정렬·cursor·페이지 전체 불변성과 오류 매핑은 어댑터 구현 후 별도 검증해야 한다.

`event_contract`는 구버전의 원본 필드 부재를 표시하기 위해 투영에서 `LEGACY_V1_UNVERSIONED`와 같은 명시적 식별자를 사용할 수 있다. 이 값은 원본 필드에 쓰지 않는다. `event_payload_preservation`은 공급자가 확인 가능한 값만 반환하고 확인 불가 시 UNVERIFIED 또는 NOT_AVAILABLE로 둔다. 체인의 내부 무결성은 전체 재작성이나 끝 삭제를 단독으로 증명하지 않는다.

실제 record·원문·원장 내용은 Codex 테스트, 원격 API, 로그 또는 fixture로 전송하지 않는다. 실제 자료 대조는 Work 측에서 수행하고 비민감 결과만 인계한다.

목록·검색에서는 요청하지 않은 원문을 `content_availability=NOT_REQUESTED`, `raw_text=null`로 표시할 수 있다. 상세 원문 응답은 한 기록에 한정하고 공급자는 직렬화와 읽기를 스트리밍할 수 있어야 한다. 단일 기록의 분할 본문 조회는 이 초안에 아직 정의되지 않았으며, 매우 큰 단일 본문에서의 동작은 별도 계약과 시험이 필요하다.
