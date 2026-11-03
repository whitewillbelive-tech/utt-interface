# UTT 인터페이스 개발 인계 — 부분 준비

상태: **SOURCE_REVIEW_PARTIAL**. 기존 D-2 사용자용 프로그램에서 여섯 화면의 HTML/CSS/JS를 검토용으로 분리했다. HTML의 스크립트가 제거됐고 JS는 실행되지 않는 참고 텍스트로 보존된다. 기존 구버전 읽기 모듈 4개는 원본 그대로 합성 입력에 한해 사용할 수 있다. `source/SOURCE_README.md`와 `source/SOURCE_ORIGIN.json`에 출처, 변경 전후 파일별 해시, 진입점, 제외 사유 및 제한이 있다. 완전한 앱, 기존 프로그램의 실행 검증 및 실제 DB 연결은 완료되지 않았다.

원본에는 Python 3.11+, 브라우저 ES modules, 일부 검증에서 Node.js가 필요했다. 기존 화면은 A 통합 검색, C 로컬 B 미리보기, D-0 검토, D-1·D-1.1 결정 및 출처, D-2 영향·제안 기능으로 구성됐다. 구 실행 서버·시작 스크립트·인증·캐시·카탈로그·worker 및 데이터 종속 테스트는 실제 경로·쓰기·인증·데이터 결합 때문에 제외했다. 원본과 기존 데이터 의존 테스트는 Work 측에 남는다. 잠금 파일은 원본에서 확인하지 못했다. 임의의 새 앱을 기존 앱으로 위장하지 않았다.

## 인계 내용
- `source/`: 원본에서 선별된 제한적 화면 참조와 합성 전용 구 읽기 모듈.
- `contracts/`: 별도 읽기 계약 초안 UTT-ACCESS-READ/0.1.0과 JSON Schema. 기존 계약을 대체하지 않는다.
- `fixtures/synthetic/`, `tests/`: 실제 자료를 읽지 않고 처음부터 만든 가상 자료와 시험.
- `AGENTS.md`, `.env.example`, `.gitignore`, `HANDOFF_MANIFEST.json`, `VALIDATION_SUMMARY.md`.

## 시작 순서
1. AGENTS.md 및 source/SOURCE_README.md의 범위와 기존 흐름·미인계 의존성을 읽는다.
2. Python 3.11 이상과 Node.js에서 다음을 실행한다. 설치·초기화·Dropbox 인증이나 실제 DB 경로 검색은 필요하지 않다.

```sh
python3 fixtures/synthetic/generate.py
python3 -m unittest discover -s tests -p 'test_synthetic*.py' -v
node --test source/tests/pure_legacy_synthetic.test.mjs
```

3. 첫 과제는 기존 화면 흐름과 읽기 요구를 기준으로 합성 공급자와 인터페이스를 연결하고 오류·빈 결과·페이지·출처 상태를 시험하는 것이다. 실운영 백엔드와 사용자 원장을 찾거나 연결하지 않는다.
4. 부족한 기존 서버·worker·데이터 종속 테스트의 결합을 Work에서 추가 검토해 안전한 소스만 이후에 인계해야 한다. 구 UI 참고 텍스트는 쓰기·연결 코드가 포함되어 그대로 활성화할 수 없다. 초안 계약의 cursor와 streaming도 구현 전이다. 향후 데이터별 어댑터, 상세 원문 분할, 데이터 버전 변화, 별도 쓰기 명령 계약을 검토·시험해야 한다.

표준 JSON Schema 전체 검증, 기존 앱 설치·완전 실행, Windows 사용자 수용 및 실제 데이터 연결은 수행하지 않았다. 입력 목록·원문과 Work 측 자료는 이 패키지에 없다. 원본 이력과 UI 보존본이 일부만 제공됐으므로 전체 프로그램 이관 완료로 간주하지 않는다.
