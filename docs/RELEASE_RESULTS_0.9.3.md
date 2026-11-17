# v0.9.3 배포 기록

2026-11-17 13:20 KST에 Python·Rust Windows x64 ZIP을 정식 최신 릴리즈로 공개했습니다.

| 항목 | 기록 |
| --- | --- |
| [공개 릴리즈](https://github.com/P-JSL/Folder_Organizer/releases/tag/v0.9.3) | 정식 공개 · ID `448120393` |
| 실제 빌드·태그 소스 | `3c73b0d6430bb30048302c6ffe541338bc2f1bd7` |
| 워크플로 정의 소스 | `dcfcc12370a7175a5ae6b14acb2c83e515c1f4c9` |
| [빌드·초안 업로드](https://github.com/P-JSL/Folder_Organizer/actions/runs/44015881068) | 성공 · 기존 태그를 정확히 체크아웃 |
| 기존 통과 결과 | Windows PE·NTFS 11건, 속성 응답 모사 17건 재사용 |
| 앱 검사·실행·화면 캡처 | 이번 릴리즈에서 재수행하지 않음 |
| 실행 파일 | 5개 모두 PE32+ x86_64 · Authenticode 인증서 테이블 없음 |

| 판 | ZIP 바이트 | 파일 수 | SHA-256 |
| --- | ---: | ---: | --- |
| [Python](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.3/FolderOrganizer-Windows-x64.zip) | 57,452,242 | 249 | `05bb61ebcce70655edf1475c6d63695e5c1da97764bb2a7e70b8ed6257fe2909` |
| [Rust](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.3/FolderOrganizer-Rust-Windows-x64.zip) | 8,850,469 | 426 | `32681287b0bbe298f2ace7147005723ded4a9d5c022c67eb60264866d8362c2d` |

공개 전 초안의 ZIP을 내려받아 공통 체크섬·압축 CRC·각 판의 버전 표식·실행 파일 형식을 확인했습니다. Python EXE를 실행하지 않고 내장 모듈을 읽어 새 보호 함수와 미리보기·복사·원본 보관 호출점이 실제 배포 파일에 포함된 것도 확인했습니다. 상세 자산·실행 파일 해시는 [RELEASE_MANIFEST_0.9.3.json](RELEASE_MANIFEST_0.9.3.json)에 있습니다.

릴리즈 소스의 operations.py는 [기존 Windows 검증 기록](WINDOWS_TRANSFER_METADATA_RESULTS.json)의 SHA-256과 일치합니다. 실제 EFS 파일·전체 Windows GUI·NAS 검증은 하지 않았으며, 이전 통과 결과를 이번 실행의 새 기능 검사로 표시하지 않습니다.

빌드에서는 Git의 줄바꿈 자동 변환을 끄고 체크아웃·태그·검증 기록의 동일성을 확인합니다. Actions 계정의 릴리즈 초안 생성 권한 제한 때문에 기존 연결 권한으로 빈 초안을 준비하고, 성공한 작업은 자산을 그 초안에 업로드했습니다. 자산 검증 후 같은 연결 권한으로 정식 공개했습니다. 소스 태그와 이미 공개된 자산은 이동·교체하지 않았습니다.

빌드 패키지와 RELEASE_BUILD_INFO.json은 게시 API 호출 전에 Actions 아티팩트로도 보존했습니다. 초기 소스 해시 검사 및 초안 생성 실패 실행은 앱 검사 실패가 아니며, 성공 실행의 소스·자산 기록과 구분합니다.

릴리즈 후 별도 문서 정리로 구버전 Markdown 27개를 제거하고 README를 86줄로 정리했습니다. 현재 가이드·배포 절차·문서 링크도 v0.9.3에 맞췄습니다. 기존 Git 태그, 과거 배포 ZIP·체크섬·JSON 원시 기록, 라이선스·약관·개인정보·제3자 고지는 유지합니다. 정리된 문서는 main의 후속 커밋이며, 이미 공개한 v0.9.3 태그·ZIP의 내용을 덮어쓰지 않았습니다.

이 작업은 GitHub 앱 릴리즈와 문서 정리입니다. 클라우드 환경은 게시하지 않았습니다.
