**Folder Organizer v0.6.0 — Python판과 Rust판을 별도 Windows 정식 빌드로 제공합니다.**

| 다운로드 | 실행 파일 |
| --- | --- |
| [Python판 ZIP](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.6.0/FolderOrganizer-Windows-x64.zip) | `FolderOrganizer/FolderOrganizer.exe` |
| [Rust판 ZIP](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.6.0/FolderOrganizer-Rust-Windows-x64.zip) | `FolderOrganizerRust/FolderOrganizerRust.exe` |

각 ZIP 전체를 별도 폴더에 압축 해제하세요. Python 설치는 필요 없습니다. 체크섬은 함께 제공하는 `SHA256SUMS.txt`에서 확인할 수 있습니다.

- **두 개의 독립 앱**: Rust판은 Python을 호출하지 않는 네이티브 구현입니다. 파일 조회, 중복·유사 검토, 규칙 정리, 병합, 기록·복원, 압축 보관, 손상 검사·복구본, 로그·보고서·업데이트를 제공합니다.
- **Python 내부 속도 개선**: 반복 경로·제외 패턴 계산, 목록 레코드와 직렬화 비용, 용량 요약, 이름 비교와 진행 이벤트 생성을 줄였습니다. 로컬 예시 측정의 범위와 수치는 성능 문서에서 구분합니다.
- **중복된 정리 화면 통합**: 날짜·생성일·확장자 분류는 공통 규칙 화면의 추천 규칙과 `{규칙}` 매핑에서 설정합니다. 기존 작업 기록의 복원은 유지합니다.
- **첫 파일만 남기기**: 현재 인증된 중복 결과를 재사용해 첫 파일과 보호 파일을 남기고, 재검사·추가 확인 없이 나머지를 `delete_files`로 옮깁니다. 새 삭제에서 `quarantine`·`retained`를 만들지 않습니다.
- **복원 데이터 보존**: Windows 추가 데이터 스트림·EFS가 있거나 이를 확인할 수 없으면 압축 최적화 대신 원시 보관을 유지합니다. 두 판이 같은 폴더를 동시에 변경하지 않도록 OS 잠금을 공유합니다.
- **기록과 업데이트 분리**: Python 작업은 Python판, Rust 작업은 Rust판에서 복원합니다. 두 판의 설정·인증된 기록·업데이트 자산을 구분해 서로 다른 앱으로 바뀌지 않게 합니다.

지원 경계도 구분합니다. 유사도 알고리즘의 점수·후보 순서는 두 판에서 같다고 보장하지 않습니다. Rust PDF 복구는 자원 제한 때문에 압축 객체·xref 스트림을 거부하며 이미지 복구는 첫 프레임 보존 범위입니다. 압축 복원은 내용·수정일 중심이고, ACL·확장 속성·원래 생성일의 완전한 재현은 1.0.0 준비 과제입니다. 실제 NAS 장비의 정책을 검증했다는 뜻은 아니며 새 삭제는 `delete_files`를 사용합니다.

이미 통과한 검사는 재사용했습니다. 새 코드·Windows 전용 경로와 이번 두 배포 파일에 필요한 확인만 수행합니다.

[두 판의 구성·기능 대응](https://github.com/P-JSL/Folder_Organizer/blob/main/docs/EDITIONS_0.6.0.md) · [1.0.0 조사와 완료 기준](https://github.com/P-JSL/Folder_Organizer/blob/main/docs/V1_READINESS.md) · [기능 정리 결정](https://github.com/P-JSL/Folder_Organizer/blob/main/docs/FEATURE_DECISIONS.md)
