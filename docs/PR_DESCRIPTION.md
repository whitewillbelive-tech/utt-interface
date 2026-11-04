# 명시적 읽기 transport와 08 통합 기준

ReadOnlyAdapter가 합성 주입만 받던 상태에서 승인된 데이터 환경의 단일 HTTPS 읽기 endpoint와 서버측 인증을 명시적으로 주입할 수 있게 합니다. snapshot·페이지·상세·출처·관계·이력의 기존 스키마와 의미를 검증하고 401/403을 UI에 보존합니다. 자동 탐색·합성 fallback·원장 쓰기·복원은 연결하지 않습니다.

현행 정상 구현을 개발 출발점으로 유지하고 이관용 임시 중단을 해제합니다. 필요한 커밋·push·충돌 해결·기본 브랜치 병합을 요청하며 강제 덮어쓰기·이력 삭제·보호 규칙 우회를 사용하지 않습니다. 09의 UI 후속 변경은 별도 PR로 직접 검토·병합합니다.

검증은 활성 Python/Node 시험·전체 스키마·원형 38개·실제 Chromium의 합성 흐름입니다. 실제 upstream·원천 projection·운영 출처 본문/locator·관리 명령·Windows 수용은 미검증/미연결입니다. 실행 결과와 접합 차단은 [WORK_INTEGRATION.md](WORK_INTEGRATION.md)에 기록합니다. 이 PR은 실제 DB 접합이나 운영 배포 완료를 주장하지 않습니다.
