# v0.9.2 배포 기록

2026-11-16 23:05 UTC에 Python·Rust Windows x64 정식 ZIP을 게시하고 최신 릴리즈로 지정했습니다.

| 항목 | 기록 |
| --- | --- |
| [공개 릴리즈](https://github.com/P-JSL/Folder_Organizer/releases/tag/v0.9.2) | 정식 공개 · ID `447724998` |
| 빌드·태그 소스 | `4755a8a9846860213c0ffdfe9f9a0cc5397a77d2` |
| [정식 빌드·게시 실행](https://github.com/P-JSL/Folder_Organizer/actions/runs/43975293751) | 성공 · Windows에서 두 판 각각 빌드 |
| 앱 검사·실행·화면 캡처 | 기존 통과 범위를 반복하지 않음 |
| 실행 파일 형식 | 5개 모두 PE32+ x86_64 확인 |
| 서명 | 5개 모두 Authenticode 인증서 테이블 없음 · 서명되지 않음 |

| 판 | ZIP 바이트 | 파일 수 | SHA-256 |
| --- | ---: | ---: | --- |
| [Python](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.2/FolderOrganizer-Windows-x64.zip) | 57,452,948 | 249 | `52b530f71a837f060393a8e6410675707528018e2a16a67ba27e612e6338e25b` |
| [Rust](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.2/FolderOrganizer-Rust-Windows-x64.zip) | 8,849,700 | 426 | `c29d497ab1ddeb506fbed1ef5b2926f5be449541ea89c8ba94c735304194c0a9` |

게시된 ZIP을 실제로 내려받아 공통 `SHA256SUMS.txt`, GitHub 자산 크기, ZIP CRC,
각 판의 `version.json`과 실행 파일 형식을 확인했습니다. 태그가 정확한 빌드
소스를 가리키며 빌드 실행이 성공했고, 정식 최신 릴리즈인 것도 확인했습니다.
각 실행 파일 해시는 [RELEASE_MANIFEST_0.9.2.json](RELEASE_MANIFEST_0.9.2.json)에 보존합니다.

변경 범위는 개발 환경 스크립트·문서·버전 표식입니다. Python·Rust 앱 구현과
의존성은 기존 공개 v0.9.1을 유지합니다. 업그레이드 확인 도구의 이전 버전은
v0.9.1로 맞췄지만 해당 앱 실행 검사는 재수행하지 않았습니다. 이번 검증은
배포 파일의 전송·버전·형식 확인이며 새 기능·성능 검사 결과를 주장하지 않습니다.

실제 VM 디스크·개인 계정 비밀번호·무인 설치 XML·인증서·로그는 Git이나 릴리즈
첨부 파일에 넣지 않았습니다. 환경의 Python 편집 가능 설치 메타데이터도
0.9.2로 갱신했습니다. Windows VM은 환경 게시 준비를 위해 중지했습니다.

클라우드 환경은 설치·시작 설정을 초안으로 저장했습니다. 현재 채팅에 환경
게시 도구가 없어 새 환경 스냅샷 게시 완료는 확인할 수 없습니다. Windows
바탕화면·평가판 활성화와 공유 뷰어 도구 문제는 미해결로 유지합니다.
