# v0.9.0 실제 배포 결과

2026-11-02에 Python·Rust Windows x64 정식 ZIP을 게시했고 최신 릴리즈를 v0.9.0으로 지정했습니다.

| 구분 | 결과 |
| --- | --- |
| [릴리즈](https://github.com/P-JSL/Folder_Organizer/releases/tag/v0.9.0) | 정식 공개, 릴리즈 ID `431102398` |
| 빌드·새 UI 확인·게시 소스 | `d2878140953477f4a75cd38eb81583e7809db688` |
| [정식 빌드·새 메인 화면 확인](https://github.com/P-JSL/Folder_Organizer/actions/runs/41394047417) | 성공 |
| [게시 전용 실행](https://github.com/P-JSL/Folder_Organizer/actions/runs/41394477530) | 성공; 빌드·검사 작업은 모두 스킵 |
| 태그 | `v0.9.0`, 정식 빌드 소스에 고정 |

## 순서와 보관

먼저 원래 v0.8.0의 두 ZIP·체크섬·증거를 [DOWNLOADS/v0.8.0](../DOWNLOADS/v0.8.0/README.md)에 보관하고 `df5ec198a38c057c1f1f5e1c58cffaa446caebc1`로 커밋·푸시했습니다. 그다음 v0.8.0 릴리즈를 초안으로 전환하고 최신 릴리즈를 v0.7.0으로 되돌린 것을 API로 확인했습니다. 이전 태그·v0.7.0 배포 파일은 이동하거나 교체하지 않았습니다.

앱 소스는 v0.8.0을 유지한 채 새 메인 화면을 구현했습니다. v0.9.0 게시가 끝난 현재 최신 릴리즈는 v0.9.0입니다. 릴리즈 채널을 이전 버전으로 되돌리는 것만으로 이미 설치한 앱을 자동 다운그레이드하지 않습니다.

## 배포 파일

| 판 | ZIP 바이트 | 비압축 바이트 | 파일 수 | SHA-256 |
| --- | ---: | ---: | ---: | --- |
| [Python](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.0/FolderOrganizer-Windows-x64.zip) | 57,473,509 | 129,038,626 | 249 | `3f6ce8135d758f1ee42e21af6227ac31c99f9aeb5e11d2326acb380145812f8f` |
| [Rust](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.0/FolderOrganizer-Rust-Windows-x64.zip) | 8,732,215 | 21,536,656 | 426 | `9dc4a56f5c024d5519fe8166317b060e51ce800614587dc65c0b0b85daba10b4` |

공유 `SHA256SUMS.txt`와 각 실행 파일의 해시가 포함된 `VALIDATION_RESULTS.zip`을 함께 게시했습니다. GitHub 자산의 해시와 빌드 기록의 해시가 일치합니다. 서명 모드는 `unsigned`, 실행 파일 5개 모두 `NotSigned`, 공인 신뢰 검증은 `false`입니다. 정식 배포 여부와 코드 서명 여부는 별개입니다.

## 이번에 확인한 범위

- 새 Python UI 사례 8개, 새 Rust UI 사례 4개, 새 배포 도구 사례 9개가 통과했습니다. 새 UI는 합성 스냅샷으로 화면 연결·범위 전환·체크 목록·파일 정보·작업 수명 주기를 확인합니다.
- 두 정식 Windows 배포본의 **새 메인 화면**을 실행해 체크한 폴더 2개, 파일 6개, 완전 중복 그룹 1개와 선택 파일 정보를 확인했습니다. 두 프로세스 모두 종료 코드 0이며 임시 원본 바이트는 유지되었습니다.
- 통합 모드는 `main_workspace=true`, `scope_mode=global`, `action_authority=false`입니다. 통합 결과를 기존 단일 폴더의 파일 변경 권한으로 사용하지 않습니다.
- 이전 전체 검사, 기존 CLI 계약, 기존 Windows 네이티브 검사, v0.8.0 GUI 드라이버, 미수정 분석·정리·복원 검사는 **전부 스킵**했습니다. 게시 전용 실행에서는 새 UI 확인도 반복하지 않았습니다.

실제 실행 기록과 게시 전에 이미 생성한 화면은 릴리즈의 `VALIDATION_RESULTS.zip`에 보존합니다. 이후 사용자 요청으로 추가 캡처는 진행하지 않았으며, 향후 캡처는 기본적으로 생략합니다. Rust의 해당 실행은 Microsoft Basic Render Driver의 소프트웨어 렌더링을 사용했습니다. 이 확인은 실제 NAS·대량 파일·분석 엔진 성능 측정이나 기존 모든 기능의 재검증이 아닙니다.
