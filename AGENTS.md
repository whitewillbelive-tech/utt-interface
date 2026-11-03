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
