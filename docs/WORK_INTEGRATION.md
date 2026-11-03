# Work에 돌려주는 읽기 접합 메모

## 현재 기준

UTT-ACCESS-READ/0.1.0은 여전히 초안입니다. 기존 UTT-READMODEL-1, UTT-CANONICAL-1, D1.1 원형을 대체하거나 수정하지 않았습니다. 기존 네 읽기 모듈은 합성 시험만 유지하고 새 화면의 운영 처리에는 사용하지 않습니다. `baselineDataset()`의 고정 검증 문구도 사용하지 않습니다.

Codex에는 실제 DB·원문·원장·계정·경로를 전달하지 마세요. 실제 자료와 계약의 대조는 기존 Work 보관 체계 또는 승인된 사용자 PC에서 수행하고 비민감 결과만 반환합니다.

## 제공한 어댑터 경계

`app/provider.py`의 `DataProvider.read(request, sort='id_asc') -> envelope`가 경계입니다. 화면은 읽기 요청만 사용합니다. `ReadOnlyAdapter()`는 기본적으로 `CONNECTION_UNAVAILABLE`, `result=null`을 반환하며 위치 탐색·캐시 복원·합성 fallback을 하지 않습니다. `ReadOnlyAdapter(SyntheticProvider().read)`에 해당하는 주입 시험만 했습니다.

실제 전송수단은 미정입니다. 파일·서버·DB가 존재한다고 가정한 성공 응답, 실제 연결 코드, 인증 구현은 없습니다. 승인된 실행 환경에서 별도 구현이 필요하며 프런트엔드에 경로·비밀을 넣지 않습니다. 해당 구현은 전체 응답 스키마·의미·원천 계약 보존을 별도로 검증해야 합니다.

## 임시 개발 확장 — 합의 전

원래 JSON Schema는 그대로입니다. `/api/read`의 JSON 바깥 봉투는 `{request: <원래 요청>, sort: 'id_asc'|'id_desc', scenario: 'normal'|'stale'|'connection'}`입니다. HTTP POST는 읽기 전달 방식일 뿐 쓰기 명령이 아닙니다.

- `sort`: 요청 스키마에 정렬 선택 필드가 없어 임시 전송 옵션으로 분리했습니다. 검색은 dataset ID 고정 + record ID asc/desc, Unicode 순서와 일치하는 SQLite BINARY 비교를 사용합니다. projects/history 혼합 페이지는 item kind·dataset·item ID asc 고정입니다. 반환 `page.sort`에 실제 기준을 표시합니다. Work가 정식 정렬 요청·capability 표현을 합의해야 합니다.
- `scenario`: 합성 서버 오류 시험 전용입니다. 실제 접합 옵션이 아닙니다. 읽기 어댑터 모드에서는 가짜 성공을 만들지 않습니다.
- UI 전용 생성 자료: 합성 읽기 구성요소와 프로젝트 연결 1개, 같은 가상 본문을 가리키는 두 번째 출처, 선택적 7×copies 복제 기록. 실제 구성요소 crosswalk나 실제 규모를 의미하지 않습니다. 기존 fixture를 수정하지 않았습니다.
- 합성 검토 연습은 별도 `sessionStorage` namespace의 dataset/snapshot별 사건이며 원래 read schema나 D1.1 명령을 가장하지 않습니다. 사건은 원형과 분리되어 있고 정본 승인을 제공하지 않습니다. 현재는 보류·근거 요청·초안 연습뿐입니다.

## Work에서 확정할 읽기 모델

1. dataset ID·snapshot 생명주기, 안정적 record/project/component/source/event/relation ID와 기존 original ID·legacy payload 추적.
2. 프로젝트 관측 분류, 사용자 확정 소속, component crosswalk, 다대다·미해결 관계와 각 근거. 논리 영역을 별도 Site로 분할할 근거로 쓰지 않습니다.
3. 검색 대상(제목/요약/본문), 한국어 정규화·대소문자·정렬 규칙, 허용 문서 유형·검토 상태. 현재 합성 검색은 Python casefold 후 부분 문자열, 빈 검색 허용입니다.
4. 혼합 페이지를 포함한 불변 snapshot, 조건·정렬·페이지 크기에 묶인 cursor와 오류 매핑. 합성 HMAC 커서는 프로세스 종료 시 무효이며 영속 실제 snapshot을 보장하지 않습니다.
5. AVAILABLE 빈 문자열, MISSING, UNKNOWN, INACCESSIBLE, NOT_REQUESTED 구분. 목록 raw_text=null과 상세 한 기록 조회. 미확인 이력을 빈 배열만으로 완료 처리하지 않기.
6. 원문/source/version/hash와 locator target/hash/fragment hash, UTF-16·코드포인트·byte 단위 및 인코딩. 현재 화면 강조는 SYNTHETIC_SOURCE의 UTF-16만 지원하며 다른 단위는 명시적 미지원입니다. hash 불일치·surrogate 분할·범위 이탈은 강조하지 않습니다.
7. 매우 큰 한 기록의 본문 분할·스트리밍 계약. 현재 한 기록 전체를 상세에서 받으므로 큰 단일 본문에 대한 운영 준비가 끝난 것이 아닙니다. 관련 프로젝트·출처가 매우 많은 한 기록의 분할도 검토해야 합니다.
8. history_status, 사건의 event_contract·supersedes·payload 보존 상태, 권한·관계별 접근 가능 범위. 합성 사건 체인 검사는 운영 원장 무결성 증명이 아닙니다.

## 실제 자료가 있는 환경에서 수행할 시험

- 각 실제 읽기 모델과 projection의 필수 필드·null·관계 키·원형 상태 대조. 원문을 Codex로 복사하지 않기.
- dataset/snapshot 변경·재시작·만료·동시 갱신 중 페이지 중복/누락 및 cursor 조건 변조 거부.
- 실제 저장 본문/출처별 버전·해시와 각 단위의 위치 대조. 다른 버전의 본문에 이전 좌표 적용 금지. M0만으로 원문 좌표를 실증하지 않기.
- 권한 거부·연결 끊김·존재 미확인·실제 누락·빈 검색 결과를 구별. 예외·로그에 계정·경로·본문이 노출되지 않도록 확인.
- 대표 실제 규모와 매우 큰 단일 본문에서 페이지 비용·메모리·초기 전송량·상세 응답량 검증. 7,000건 합성 결과를 성능 근거로 사용하지 않기.
- 사용자 PC/Windows·브라우저 접근성 수용 시험과 장기 세션, 실제 방식의 읽기 전용 권한 확인.

쓰기·검토·승인·정정·취소·복원·일괄 제안은 별도 명령 계약, 권한, expected version, 충돌·불변 사건 보존, 사용자 확인 규칙이 필요합니다. 이번 구현은 운영 승인을 만들거나 쓰기를 연결하지 않았습니다. 실제 DB 전송이나 계정 연결을 이 준비의 전제로 요청하지 않습니다.

## 2026-11-04 보완: 범위 검증과 다음 인계의 담당

브라우저는 `ReadClient.read()`가 전송한 dataset_id/snapshot_id를 보존하고, 응답 snapshot 및 내부 자료의 dataset 범위를 대조합니다. 응답 시점의 선택 상태로 요청 범위를 바꾸지 않습니다. capabilities는 null인 요청 필드만 초기 선택을 허용하며 응답 봉투와 내부 dataset descriptor의 일치는 계속 검사합니다. `source`·`record`·`project`·`component`·`history`에는 기존 dataset_id를 사용합니다. 관계는 기존 from/to_dataset_id와 앵커를 사용하며 명시적인 교차 dataset 관계를 일괄 금지하지 않습니다.

실패 코드는 기존 버전 불일치 `VERSION_MISMATCH`, 브라우저 내부 전용 `RESPONSE_SCOPE_MISMATCH`(dataset/자료 범위), `INVALID_RESPONSE`(봉투 형식)로 구별합니다. 후자의 두 코드를 원래 JSON 응답 error enum에 추가하지 않았습니다. 서버 계약 변경·새 필드 요구는 없습니다. 이 검사는 전체 JSON Schema 검증을 대체하지 않고, 실제 자료의 정확성·권한을 증명하지 않습니다. 요청 세대 검사도 별도로 유지합니다.

| 기존 미지원 범위 | Work가 추가 인계할 검토 코드 | 필요한 계약·설정 | 담당 및 검증 방법 |
|---|---|---|---|
| A 인증·복원·캐시 | 데이터·키를 제거한 서버/worker/lib 인터페이스, 캐시 선택·초기화 로직 | 읽기 권한, 명시적 연결·캐시 namespace·오프라인 최신성 의미 | Work: 안전한 소스·계약/실제 환경 대조. Codex: 합성 transport와 캐시 경계 시험. 사용자: 승인된 PC에서 인증 수용 시험 |
| C 격리 파일 미리보기 | 안전한 parser/worker와 분리된 파일 검증 진입점 | 허용 형식·버전·크기·격리 namespace·실패 의미 | Work: 코드와 형식 계약. Codex: 합성 파일·잘못된 파일·운영 경로 자동 탐색 부재 시험 |
| D-0 실제 모델·출처 대조 | 읽기 projection·원문 분할·좌표 변환 경계 | dataset/snapshot/관계 키·NULL·좌표 단위·해시·본문 분할 | Work: 실제 원천 대조·비민감 결과. Codex: 합성 페이지/경계/오류 회귀. 실제 원문은 전달하지 않음 |
| D-1/D-1.1 실제 원장 판단·정정·취소 | 원형 사건 reader 및 데이터·경로를 분리한 명령 평가 코드 | 사건 버전·불변 payload·expected version·권한·충돌·승인 | Work: 사건 의미와 실제 원장 대조. Codex: 합성 체인·충돌·역호환 시험. 현재 쓰기는 계속 비활성 |
| D-1.1 백업·복원 | 경로·자료·인증이 제거된 검증기와 트랜잭션 경계 | 빈 원장 조건·무결성·크기 제한·원자성·복구 영수증 | Work: 복원 계약. Codex: 합성 파일·손상·부분 실패 시험. 사용자: 승인된 환경의 복구 수용 시험 |
| D-2 영향 분석·일괄 제안 | 유효 판단 판정·영향 분석·일괄 명령 경계의 검토 코드 | 판단 기준·차단 이유·버전 고정·전체 실패 원칙·별도 명령 승인 | Work: 계약/안전한 코드. Codex: 합성 적격·부적격·동시 변경 시험. 정본 승격을 가정하지 않음 |
| 실제 읽기 transport | 특정 경로·비밀을 뺀 호출 인터페이스와 오류 매핑 | 전송수단·인증 주입·권한·snapshot 유지·마이그레이션 | Work/사용자: 승인된 실행 환경 설정. Codex: 합성 주입 시험. 실제 연결·실제 규모는 Work 환경에서 수행 |
| Windows 실행기·사용자 수용 | 데이터·절대경로를 제거한 원래 시작 스크립트의 검토본 | 플랫폼·브라우저 버전·종료·오류 안내 | Codex: 가능한 정적/합성 시험. 사용자/Work: Windows 설치·실행·키보드·장기 세션 수용 |

이번 인계 복구는 코드 전용 원형 38개 파일의 보존과 검증입니다. 누락된 원본 서버·worker가 새로 제공된 것은 아니므로 위 항목이 완료됐다는 뜻이 아닙니다. 실제 DB 전송, Work 전용 ZIP·Site·Scrivener 탐색, 새 쓰기 활성화는 필요하지 않으며 수행하지 않습니다.
