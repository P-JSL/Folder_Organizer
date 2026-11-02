**Folder Organizer v0.9.0 — 메인 화면에서 여러 폴더 분석 · Python / Rust**

| 다운로드 | 실행 파일 |
| --- | --- |
| [Python판 ZIP](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.0/FolderOrganizer-Windows-x64.zip) | `FolderOrganizer/FolderOrganizer.exe` |
| [Rust판 ZIP](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.0/FolderOrganizer-Rust-Windows-x64.zip) | `FolderOrganizerRust/FolderOrganizerRust.exe` |

- **메인 화면에 복수 폴더:** 왼쪽에서 폴더를 추가·체크하고 검사합니다. 가운데 파일 목록·완전 중복 결과와 오른쪽 파일 정보를 함께 확인합니다.
- **짧은 동선:** 상단에서 폴더 추가·검사 시작·취소를 실행합니다. 정리·기록·복구 등 세부 기능은 헤더 메뉴에서 찾습니다.
- **범위를 분명하게:** 통합 결과는 읽기 전용입니다. 폴더 하나를 선택해 **이 폴더에서 정리**로 전환하면 기존 단일 폴더 기능을 사용합니다. 통합 결과만으로 삭제·이동하지 않습니다.
- **기존 분석 재사용:** v0.8.0 소스를 기반으로 화면과 연결을 변경했습니다. 분석 엔진, 인증된 중복 정리, `delete_files`와 복원 구조를 유지합니다. 선택한 파일 정보는 분석 스냅샷이며 화면 표시를 위해 내용을 다시 읽지 않습니다.
- **v0.8.0 보관:** 원래 두 배포 ZIP·체크섬·검사 자료를 [DOWNLOADS/v0.8.0](https://github.com/P-JSL/Folder_Organizer/tree/main/DOWNLOADS/v0.8.0)에 보관했습니다. v0.9.0 준비 전에 최신 릴리즈를 v0.7.0으로 되돌렸으며 이전 태그를 이동하지 않았습니다.

[새 메인 화면 사용 안내](https://github.com/P-JSL/Folder_Organizer/blob/main/docs/WORKSPACE_0.9.0.md)

**기존 검사는 모두 스킵했습니다.** 정식 빌드에서는 새 v0.9.0 메인 화면과 연결에 대한 확인만 수행합니다. 실제 명령·실행 파일 해시·새 화면·서명 상태는 첨부 `VALIDATION_RESULTS.zip`에 기록합니다. 이번 UI 변경을 분석 엔진의 새로운 속도 측정이나 기존 기능 전체 재검증으로 표시하지 않습니다.

Windows 10/11 x64용 두 ZIP을 각각 전체 압축 해제해서 실행하세요. 설치된 v0.8.0이 릴리즈 롤백만으로 자동 다운그레이드되지는 않습니다. 각 판은 자기 판의 업데이트 자산을 사용합니다.

사용은 권리자 P-JSL 본인과 명시적으로 서면 허락받은 사람에게만 허용합니다. ZIP의 `LICENSE`와 `licenses/FolderOrganizer/` 이용약관을 확인하세요. 제3자 권리와 기존 적법한 허락은 보존합니다.
