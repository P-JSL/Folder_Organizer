**Folder Organizer v0.8.0 — 여러 폴더 통합 분석 · Python / Rust**

| 다운로드 | 실행 파일 |
| --- | --- |
| [Python판 ZIP](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.8.0/FolderOrganizer-Windows-x64.zip) | `FolderOrganizer/FolderOrganizer.exe` |
| [Rust판 ZIP](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.8.0/FolderOrganizer-Rust-Windows-x64.zip) | `FolderOrganizerRust/FolderOrganizerRust.exe` |

- **여러 폴더를 함께 분석:** 파일 → 여러 폴더 통합 분석에서 폴더를 추가하고 선택·전체 검사, 취소, 폴더별 상태와 통합 목록·완전 중복을 확인합니다. 다른 폴더에 있는 같은 내용도 SHA-256·실제 바이트·스냅샷으로 확인합니다.
- **읽기 전용 통합 결과:** 새 화면에는 삭제·이동 기능이 없습니다. 중복·중첩 루트의 소속과 하드링크를 구분하고, 실패·취소를 전체 완료와 구분합니다. 기존 단일 폴더 정리와 복원 기록은 그대로 유지합니다.
- **반복 읽기 감소:** Python 분석 세션의 서로 다른 해시를 재사용해 유사 후보의 불필요한 전체 바이트 재읽기를 줄입니다. 같은 해시의 실제 바이트 비교, 변경 감지와 중복 작업의 인증 증명은 유지합니다.
- **반복 계산·할당 감소:** Rust의 Unicode 이름 특징을 제한된 캐시로 공유하고 미지원 파일이 텍스트 캐시를 차지하지 않게 했습니다. 단일·다중 폴더가 같은 검증 코어를 재사용하며 대량 경고의 상세 복제는 1,000건으로 제한하고 전체 수는 보존합니다.
- **새 CLI·보고서:** `--add-folder`, `--workers`, `--json-multiroot`를 추가했습니다. 다중 폴더 보고서는 `folder-organizer.multiroot.v1`과 `action_authority=false`로 구분하며 기존 단일 폴더 JSON/CSV는 유지합니다.
- **검사 증거 보존:** 실제 명령·종료 코드·실행 파일과 출력의 해시·화면을 콘솔 출력 전에 저장하고, 후속 실패에도 생성된 자료를 보관합니다.

[사용 안내·새 화면·계측](https://github.com/P-JSL/Folder_Organizer/blob/main/docs/MULTIROOT_ANALYSIS.md) · [확장 보고서 대조](https://github.com/P-JSL/Folder_Organizer/blob/main/docs/EXTENDED_REPORT_REVIEW.md)

이미 통과한 새 로컬 검사 52건과 기존 미수정 기능의 검사는 반복하지 않습니다. 이번 정식 배포의 새 확인은 Windows에 특유한 경로·식별·변경 시각과 실제 배포 파일의 새 다중 폴더 기능에 한정합니다. 새 화면의 Esc·취소·종료 경계 등 추가 수정은 해당 새 사례만 확인합니다.

성능 수치는 각각의 합성 입력 범위에만 해당합니다. Python의 Linux 매칭 측정 42.43→21.49ms와 Rust의 이름 특징 생성 1,560→40회는 Windows·NAS 전체 처리 속도 배수가 아닙니다. 실제 NAS·HDD·콜드 캐시·10만 행 GUI는 별도 장비 검증이 필요합니다. 볼륨 그룹은 물리 디스크를 확정하는 자동 튜닝이 아니며, 자동 감시·예약 갱신·자막 의미/싱크 비교는 이번 기능에 포함되지 않습니다.

실제 실행 파일의 서명 상태와 빌드·검사·게시 소스는 릴리즈 첨부 `VALIDATION_RESULTS.zip`에 기록합니다. 개발용 자체 서명은 공인 게시자 인증이 아닙니다. 각 판은 자기 판의 업데이트 자산을 사용하며 구형 0.6.0 Rust 수정본은 최초 수동 업그레이드가 필요할 수 있습니다.

사용은 권리자 P-JSL 본인과 명시적으로 서면 허락받은 사람에게만 허용합니다. ZIP의 `LICENSE`와 `licenses/FolderOrganizer/` 이용약관을 확인하세요. 제3자 권리와 기존 적법한 허락은 보존합니다.
