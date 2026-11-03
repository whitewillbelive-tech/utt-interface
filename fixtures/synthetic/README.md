# 독립 합성 자료

이 폴더의 모든 이름·식별자·내용·사건·관계는 이 시험을 위해 새로 만든 가상 값입니다. 실제 UTT DB, 사용자 문서, 결정 원장, 운영 캐시, 인증정보를 읽거나 발췌·익명화하지 않습니다. 생성기는 표준 라이브러리만 이용하며 실제 데이터 경로를 입력받지 않습니다.

프로젝트 루트에서 실행:

```sh
python3 fixtures/synthetic/generate.py
python3 -m unittest discover -s tests -p 'test_synthetic*.py' -v
```

생성기는 기본적으로 새 임시 디렉터리를 만들고 출력 위치를 표시합니다. 원하는 빈 폴더에는 `python3 fixtures/synthetic/generate.py --output-dir <새-디렉터리>`로 만들 수 있으며 같은 이름의 파일이 이미 있으면 덮어쓰지 않습니다. 동봉된 두 JSON은 재생성 값과 비교 검증합니다.

시험은 합성 fixture와 작은 모의 응답만 확인합니다. 스키마 검사는 이 초안에서 사용한 키워드에 대한 부분 검증기이며 JSON Schema 2020-12 전체 구현이 아닙니다. 미지원 키워드는 실패 처리합니다. 실제 UTT 프로그램, 운영 데이터 접합, Windows 환경, 네트워크나 승인 기능을 검증하지 않습니다. 생성물의 UTF-16 좌표는 반열린 [start,end) 코드 단위이며 UTF-8 바이트나 Unicode 코드 포인트 인덱스가 아닙니다.
