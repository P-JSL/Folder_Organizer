**Folder Organizer v0.9.1 — 단일·다중 폴더 검사·정리 통합 · Python / Rust**

| 다운로드 | 실행 파일 |
| --- | --- |
| [Python판 ZIP](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.1/FolderOrganizer-Windows-x64.zip) | `FolderOrganizer/FolderOrganizer.exe` |
| [Rust판 ZIP](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.1/FolderOrganizer-Rust-Windows-x64.zip) | `FolderOrganizerRust/FolderOrganizerRust.exe` |

- **한 화면에서 분석부터 정리까지:** 폴더 한 개 또는 여러 개를 분석한 뒤 같은 결과에서 완전 중복·유사 검사, 복사·이동·삭제 보관·규칙 분류·내용 병합을 사용합니다. 정리하려고 단일 폴더 화면으로 전환할 필요가 없습니다.
- **기능별 리본 UI:** 상단 도구 모음, 왼쪽 검색 폴더·필터, 가운데 체크 가능한 결과, 오른쪽 파일 정보, 아래 종류별 요약과 진행 상태를 배치했습니다. 작업 중지와 실제 폴더별 기록·복원도 연결했습니다.
- **그룹 전체에서 첫 파일 보존:** 여러 폴더에 걸친 완전 중복도 그룹의 첫 파일과 보호 대상을 남깁니다. 현재 검증 근거를 사용해 추가 전체 재검사·재해시·확인 창 없이 나머지를 각 원래 폴더의 `delete_files`로 옮깁니다. 변경된 파일이나 근거가 없는 결과로는 실행하지 않습니다.
- **폴더별 안전한 작업:** 실제 원본·보존 폴더의 잠금과 서명된 계획을 사용하고, 원본·대상·보존 파일의 충돌과 공통 출력 이름을 확인합니다. 중간 실패·취소는 완료된 항목과 나머지를 구분해 기록합니다. 정리 후 이전 계획과 작업 권한은 무효화합니다.
- **여러 폴더의 내용 병합:** 지원 UTF-8 텍스트·Markdown·CSV·TSV의 겹치는 문단·행을 정리한 새 파일을 미리 확인합니다. 원본 유지가 기본이며, 원본 보관을 선택하면 출력 검증 후 폴더별 `delete_files`와 기록으로 이동합니다.
- **보고서와 실행 권한 구분:** 내보낸 JSON은 계속 `action_authority=false`입니다. 파일 변경은 현재 앱의 분석 세션에서만 실행하며, 보고서를 수정하거나 다시 읽어 실행 권한을 얻을 수 없습니다.

[사용 안내](https://github.com/P-JSL/Folder_Organizer/blob/main/docs/WORKSPACE_ACTIONS.md) · [이미 완료한 새 사례 45건의 확인 결과](https://github.com/P-JSL/Folder_Organizer/blob/main/docs/WORKSPACE_ACTION_RESULTS.md)

**이번 정식 릴리즈에서는 모든 통과 검사를 재실행하지 않고 기존 결과를 재사용합니다. 화면 캡처도 생략합니다.** 앞선 Windows 확인에서 Python 26건·Rust 17건이 통과했고, 실패했던 실제 Python 작업자 흐름 2건은 검사 대기의 GIL 점유 원인을 수정한 뒤 별도 실행에서 통과했습니다. 다른 43건은 반복하지 않았습니다. v0.9.1은 이 구현에 버전 정보를 반영해 두 판을 정식 Windows ZIP으로 각각 빌드합니다. 체크섬·빌드 소스·기존 확인 결과의 재사용 범위와 실제 서명 상태는 첨부 `VALIDATION_RESULTS.zip`에 기록합니다. 기존 모든 기능이나 실제 NAS 성능을 새로 검증했다는 뜻은 아닙니다.

원래 [v0.8.0](https://github.com/P-JSL/Folder_Organizer/tree/main/DOWNLOADS/v0.8.0)·[v0.9.0](https://github.com/P-JSL/Folder_Organizer/tree/main/DOWNLOADS/v0.9.0) 배포 파일은 `DOWNLOADS`에 보관했습니다. 준비 중 최신 릴리즈를 v0.7.0으로 롤백했으며 이전 태그·ZIP은 교체하지 않았습니다.

Windows 10/11 x64용 ZIP 전체를 압축 해제해 실행하세요. 각 판은 자기 판의 자동 업데이트 자산만 사용합니다. 실행 파일의 실제 코드 서명 상태는 배포 기록을 확인하세요.

사용은 권리자 P-JSL 본인과 명시적으로 서면 허락받은 사람에게만 허용합니다. ZIP의 `LICENSE`와 `licenses/FolderOrganizer/` 이용약관을 확인하세요. 제3자 권리와 기존 적법한 허락은 보존합니다.
