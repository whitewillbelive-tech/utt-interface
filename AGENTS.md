# UTT 인터페이스 인계 — 작업 규칙

## 목적과 경계
이 디렉터리는 UTT의 인터페이스와 데이터 연결 코드 개발을 위한 소스 일부·읽기 계약 초안·처음부터 만든 합성 시험 자료다. 실제 DB, 원문, 출처 본문, 사용자 결정 원장, 운영 캐시, 백업, 인증정보를 가져오거나 조회·로그·API·Git/LFS 등으로 전달하지 않는다. 실제 데이터의 보관·정돈·검증은 별도 Work 환경에서 수행한다. 기존 전문 Site 및 실행기 개발은 범위 밖이다. Scrivener는 이름 기반 탐색·목록·메타데이터를 포함해 접근하지 않는다.

source/legacy-reference는 변경된 정적 참조이며 실행 가능한 완성 앱이 아니다. `.reference.txt` 속 네트워크 요청, 업로드, 인증, 원장 쓰기/백업/복원·초안 생성 코드를 그대로 실행하지 않는다. source/safe_modules는 구 계약의 인메모리 합성 입력 시험에만 사용한다. `baselineDataset()`의 verified 문구는 실제 검증 결과가 아니며 실제 데이터에 사용하지 않는다. source/SOURCE_README.md를 먼저 읽는다.

contracts/의 UTT-ACCESS-READ/0.1.0은 초안이며 원본 READMODEL, CANONICAL 및 D1.1 계약을 대체하지 않는다. 사용자가 원하지 않은 실제 자료 탐색·운영 접속·데이터 변경·정본 승인·배포는 금지한다. 초안 cursor·snapshot/버전 충돌과 스트리밍은 구현 및 실제 공급자 검증 전이다. 데이터 없음·미확인·연결 실패를 구별한다.

## 검증
Python 3.11+ 및 Node.js 필요. 합성 자료 생성기는 입력 DB 경로를 받지 않는다.

```sh
python3 fixtures/synthetic/generate.py
python3 -m unittest discover -s tests -p 'test_synthetic*.py' -v
node --test source/tests/pure_legacy_synthetic.test.mjs
```

부분 schema 검사와 실제 표준 전체 검증은 구별한다. 다음 개발은 합성 공급자 기반의 UI/읽기 어댑터 및 테스트에서 시작한다. 별도 명령 계약과 충돌 처리 확인 전 쓰기를 연결하지 않는다. 실제 접합을 가장하지 않는다.

## 2026-11-04 개발 재개 및 통합 지시 (현행)

이 절과 `docs/WORK_INTEGRATION.md`가 이전 이관용 임시 중단·병합 보류 지시를 갱신한다. 현행 정상 구현과 후속 변경을 개발 출발점으로 삼고 필요한 커밋·push·충돌 해결·기본 브랜치 병합을 진행한다. 강제 덮어쓰기, 이력 삭제, 보호 규칙 우회는 하지 않는다. 기존 조사 SHA로 후속 변경을 되돌리지 않는다. 과거 보고의 "병합하지 않음"은 당시 상태이지 현재 금지 지시가 아니다.

- 08: `app/provider.py`, `app/server.py`, transport·공용 스키마·백엔드 시험·루트 지침·최종 병합. 09: `app/web/`와 UI 전용 시험. UI 작업은 별도 브랜치/PR에서 검토해 08이 병합한다. 07: 비민감 원천 계약과 승인된 데이터 실행환경에서의 동일 커밋 시험.
- 실주소·경로·키는 07의 승인 환경에서만 명시적으로 주입한다. 이 코드 환경에서 실제 DB·원문·원장·캐시·인증값·실제 경로를 요청·탐색·전송하지 않는다. Scrivener와 별도 Sites는 접근하지 않는다.
- 기본 readonly 실행은 미연결이다. `--connect-env`만 서버측 환경 설정을 읽는다. `UTT_RUNTIME_APPROVED=1`은 배치 선택 표시이며 권한을 부여하는 장치가 아니다. 자동 탐색·인증 시도·합성 fallback·캐시 복원은 없다.
- 검색→목록→상세 읽기를 관리 기능과 독립적으로 접합한다. 명령 계약·권한·명시적 승인·expected version·동시 충돌·불변 원장 보존·복원 원자성 근거가 없는 쓰기/복원은 미연결로 유지한다. 합성 성공으로 운영 완료를 선언하지 않는다.
- `provenance/handoff-v1/`와 원본 매니페스트는 수정하지 않는다. 루트 지침 변경은 정확한 해시와 사유를 `docs/HANDOFF_WORKTREE_POLICY.json`에 등록한다. 새 런타임 의존성은 `requirements.txt`에 고정한다.
