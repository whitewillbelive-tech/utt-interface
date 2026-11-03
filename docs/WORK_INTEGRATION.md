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
