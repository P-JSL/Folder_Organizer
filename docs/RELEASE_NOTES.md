**Folder Organizer v0.7.0 — 보고서 기반 개선 · Python / Rust**

| 다운로드 | 실행 파일 |
| --- | --- |
| [Python판 ZIP](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.7.0/FolderOrganizer-Windows-x64.zip) | `FolderOrganizer/FolderOrganizer.exe` |
| [Rust판 ZIP](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.7.0/FolderOrganizer-Rust-Windows-x64.zip) | `FolderOrganizerRust/FolderOrganizerRust.exe` |

- **대형 중복 검사:** Rust에도 1 MiB 이상 앞·중간·끝 64 KiB 샘플 선별을 추가했습니다. 같은 샘플만으로 삭제 근거를 만들지 않고 전체 SHA-256·실제 바이트·스냅샷 검증을 유지합니다. 실제 읽기량을 기록합니다.
- **부분 오류를 명확히:** 변경·읽기 실패 파일과 검증된 정상 그룹을 구분합니다. 부분 결과를 전체 폴더의 중복 없음으로 표시하지 않습니다. Python CLI·GUI·저장 보고서에도 상태와 경고를 보존합니다.
- **내용 기반 유사 후보:** Rust의 큰 목록에서 선택한 텍스트·이미지 특징을 직접 인덱싱하고 이름 비교를 끈 경우 이름 후보가 한도를 소모하지 않게 했습니다. 후보·메모리 한도와 누락 가능성 표시는 유지합니다.
- **CLI와 정합성:** Rust에 개별 비교 방법·임계값·비교 한도를 추가했습니다. 두 판에 공통 JSON/CSV를 추가하고 기존 형식은 보존합니다. Python의 중복·유사 동시 요청은 인증된 결과를 재사용합니다.
- **복원 안전성:** 취소된 복사·이동 이후 사용자가 만든 하드링크를 앱의 복사본으로 오인하던 경로를 수정했습니다. 원래 경로의 다른 파일을 덮어쓰지 않으며 keep-first의 재검사 없는 실행은 유지합니다.
- **배포 구성:** Python의 전체 PySide6 데이터 수집을 의존성 기반 Widgets 구성으로 바꿨습니다. 런타임 플랫폼·이미지·소프트웨어 렌더링·Pillow 코덱·라이선스는 유지합니다.

수정하지 않은 기능과 기존에 통과한 검사는 반복하지 않습니다. 새 검사와 Windows 실행·합성 측정은 변경한 경로에 한정합니다. [보고서 항목별 판단](https://github.com/P-JSL/Folder_Organizer/blob/main/docs/REPORT_REVIEW_0.7.0.md)과 [공통 형식](https://github.com/P-JSL/Folder_Organizer/blob/main/docs/COMMON_REPORT_V1.md)을 참고하세요.

새 내용 후보는 일부 특징 읽기를 추가하므로 모든 입력에서 더 빠르거나 모든 유사 파일을 찾는다고 보장하지 않습니다. 고주파 노이즈·균일 패턴이 유사하게 나타나는 사례도 공개하고 유사 결과의 자동 삭제를 허용하지 않습니다. Windows 합성 CLI 측정을 실제 NAS·콜드 캐시·모든 GUI 작업의 배수로 해석하지 않습니다. Authenticode 인증서는 이 작업에 제공되지 않아 메인 EXE는 코드 서명했다고 표시하지 않습니다.

Python·Rust의 설정·업데이트 자산·작업 기록은 별도로 유지합니다. 알려진 구형 Rust 0.6.0 수정본의 앱 루트 빌드 문서는 새 업데이터에서 엄격하게 인식하지만, 구형 실행 파일 자체의 제한 때문에 최초 한 번은 수동 업그레이드가 필요할 수 있습니다. 기존 데이터 폴더와 복원 기록은 유지하세요.

사용은 권리자 P-JSL 본인과 명시적으로 서면 허락받은 사람에게만 허용합니다. 각 ZIP의 `LICENSE`와 `licenses/FolderOrganizer/`의 [이용약관](https://github.com/P-JSL/Folder_Organizer/blob/main/docs/TERMS_OF_USE.md)을 확인하세요. 제3자 권리와 기존 적법한 허락은 보존합니다.
