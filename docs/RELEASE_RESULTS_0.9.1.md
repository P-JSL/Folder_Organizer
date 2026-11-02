# v0.9.1 정식 배포 결과

2026-11-03(KST)에 Python·Rust Windows x64 정식 ZIP을 게시하고 최신 릴리즈를 v0.9.1로 지정했습니다.

| 항목 | 기록 |
| --- | --- |
| [공개 릴리즈](https://github.com/P-JSL/Folder_Organizer/releases/tag/v0.9.1) | 정식 공개 · ID `431999458` |
| 빌드·게시·태그 소스 | `cbb1377416939d6a34ba56ecba043d7de64eb19b` |
| [기존 결과 재사용·정식 빌드](https://github.com/P-JSL/Folder_Organizer/actions/runs/41529870616) | 성공 |
| [게시 전용 실행](https://github.com/P-JSL/Folder_Organizer/actions/runs/41530650197) | 성공 · 빌드·재사용 확인 작업은 스킵 |
| 검사·캡처 | 모두 재실행하지 않음 · 기존 45건 통과 결과 재사용 |
| 서명 | `unsigned` · 실행 파일 5개 모두 `NotSigned` · 공인 신뢰 검증 `false` |

| 판 | ZIP 바이트 | 비압축 바이트 | 파일 수 | SHA-256 |
| --- | ---: | ---: | ---: | --- |
| [Python](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.1/FolderOrganizer-Windows-x64.zip) | 57,459,824 | 128,869,186 | 249 | `59477cb482e64f1d4685bf44d8b8591b22513cf36ea121f60b5486cf1f88635d` |
| [Rust](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.1/FolderOrganizer-Rust-Windows-x64.zip) | 8,855,375 | 21,943,694 | 426 | `299a2f88620c2a40c31c6d6215b60c85017378d4162a2979dd12784374c29fc1` |

공유 `SHA256SUMS.txt`와 `VALIDATION_RESULTS.zip`을 함께 게시했습니다. GitHub 자산의 해시·크기는 빌드 기록과 일치합니다. ZIP은 게시 단계에서 재빌드하지 않았으며 기존 공개 태그나 이전 ZIP을 교체하지 않았습니다.

이번 빌드는 앱 구현을 검증한 소스에서 버전 표식 6곳만 v0.9.1로 바꾼 것을 Git 바이트 비교로 확인했습니다. 이미 완료한 Windows Python 28건·Rust 17건과 컴파일 기록을 읽어 재사용했으며, 이번 배포에서는 앱·CLI·GUI 검사를 실행하지 않았습니다. 상세 범위와 두 Python 흐름의 검사 대기 수정은 [작업 공간 확인 결과](WORKSPACE_ACTION_RESULTS.md)에 있습니다.

`VALIDATION_RESULTS.zip`에는 빌드 정보·패키지 구성·원래 통과 로그·재사용 근거·빌드와 게시의 소스 연결 기록을 포함합니다. 화면 캡처는 포함하지 않습니다.

사용자 요청에 따라 먼저 v0.8.0·v0.9.0 원본을 [DOWNLOADS](../DOWNLOADS/README.md)에 보관하고 v0.7.0으로 롤백했습니다. 이후 명시적인 v0.9.1 릴리즈 요청에 따라 이번 버전을 최신 정식 릴리즈로 게시했습니다. 각 판은 자기 판의 업데이트 자산만 사용합니다.
