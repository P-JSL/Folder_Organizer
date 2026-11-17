# Folder Organizer

Windows 10/11 x64에서 폴더의 파일을 분석하고 중복 검사·분류·복사·이동·복원을 진행하는 데스크톱 앱입니다. 폴더 한 개와 여러 개를 같은 화면에서 다루며, 개인 파일을 외부 서버로 업로드하지 않습니다.

최신 정식 버전: **[v0.9.3 · Python / Rust](https://github.com/P-JSL/Folder_Organizer/releases/tag/v0.9.3)**

이용은 권리자 P-JSL 본인 또는 명시적으로 서면 허락받은 사람에게만 허용됩니다. 공개 저장소와 다운로드 제공은 이용·재배포 허락을 의미하지 않습니다. [LICENSE](LICENSE)를 확인하세요.

## 다운로드

두 배포판 모두 별도 Python 설치 없이 실행할 수 있습니다.

| 배포판 | Windows x64 ZIP | 압축 해제 후 실행 |
| --- | --- | --- |
| Python | [다운로드](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.3/FolderOrganizer-Windows-x64.zip) | `FolderOrganizer/FolderOrganizer.exe` |
| Rust | [다운로드](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.3/FolderOrganizer-Rust-Windows-x64.zip) | `FolderOrganizerRust/FolderOrganizerRust.exe` |

ZIP 전체를 새 폴더에 압축 해제한 뒤 실행하세요. 도우미·라이선스 파일과 Python판의 `_internal` 폴더를 유지해야 합니다.

[SHA-256 체크섬](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.3/SHA256SUMS.txt) · [이번 버전 변경 사항](docs/RELEASE_NOTES_0.9.3.md) · [빌드·검증 기록](docs/RELEASE_RESULTS_0.9.3.md)

실행 파일은 코드 서명되지 않아 Windows에서 게시자 확인 알림이 표시될 수 있습니다.

## 사용 순서

1. **폴더 추가**로 위치를 등록하고 분석할 폴더를 체크합니다.
2. 하위 폴더·숨김 파일·제외 조건을 확인하고 **분석 시작**을 누릅니다.
3. 완전 중복이나 유사 후보를 검토하고 정리할 파일을 체크합니다.
4. 복사·이동·삭제 보관·규칙 분류를 선택해 미리보기를 확인하고 실행합니다.
5. 결과를 확인하고 필요한 경우 **작업 기록·복원**에서 되돌립니다.

자세한 조작은 [통합 작업 공간 안내](docs/WORKSPACE_ACTIONS.md)를 참고하세요.

## 주요 기능

| 기능 | 지원 범위 |
| --- | --- |
| 분석·용량 요약 | 파일 정보·종류별 용량·큰 파일·빈 파일, 검색과 필터 |
| 중복·유사 검사 | 크기·SHA-256·바이트 비교, 이름·지원 텍스트·이미지의 유사 후보 검토 |
| 파일 정리 | 복사·이동·삭제 보관, 날짜·확장자·사용자 규칙 분류 |
| 내용 병합 | UTF-8 텍스트·Markdown·CSV·TSV를 새 파일로 병합, 원본 유지가 기본 |
| 기록·복원·복구 | 실제 폴더별 작업 기록과 되돌리기, 지원 ZIP·Office·이미지·PDF의 새 복구본 생성 |
| 보고·업데이트 | CSV·JSON 내보내기, 로컬 로그, 각 배포판에 맞는 검증된 업데이트 |

## 파일 보존과 지원 한계

- 유사도는 검토용 점수입니다. 유사도 100%나 같은 이름만으로 완전 중복 또는 삭제 안전성을 보장하지 않습니다.
- 삭제 보관은 실제 소속 폴더의 `delete_files`로 이동합니다. 복원하려면 이 폴더와 앱의 기록·키·보관 데이터를 함께 유지하세요. Windows·NAS 휴지통 이동을 보장하지 않습니다.
- v0.9.3은 ADS·EFS 특성을 보존할 수 없거나 스트림 조회가 불완전한 복사·이동을 중단하고 원본을 유지합니다. [원인과 검증 범위](docs/WINDOWS_TRANSFER_METADATA_REVIEW.md)를 확인하세요.
- 압축 메타데이터만으로 원본 내용을 복원할 수는 없습니다. 손상 파일 복구는 읽을 수 있는 내용을 새 파일로 보존하며, 영구 삭제된 디스크 영역을 복원하지 않습니다.
- 자동 업데이트는 각 판의 ZIP·체크섬·버전을 확인한 뒤 정상 종료 후 적용합니다. 인터넷 연결과 앱 폴더 쓰기 권한이 필요합니다.

## 소스에서 실행

개인 컴퓨터의 폴더는 해당 컴퓨터에서 앱을 실행해 선택하세요. 클라우드에서 실행한 앱이 개인 PC의 드라이브에 직접 접근하지는 않습니다.

<details>
<summary>Python · Python 3.11 이상, 아래 예시는 3.12</summary>

Windows PowerShell에서 저장소 디렉터리로 이동해 실행합니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m folder_organizer
```

Linux/macOS에서는 가상 환경의 `bin/python`을 사용합니다. Qt에 필요한 그래픽 런타임과 데스크톱 세션이 필요하며, 별도 정식 패키지·실기기 검증은 제공하지 않습니다.

</details>

<details>
<summary>Rust · Rust 1.99.1</summary>

```sh
rustup toolchain install 1.99.1 --profile minimal
cargo +1.99.1 run --manifest-path rust-app/Cargo.toml --locked --release --features gui --bin FolderOrganizerRust
```

</details>

## 문서와 이용 조건

[작업 공간](docs/WORKSPACE_ACTIONS.md) · [중복·유사 판정](docs/DUPLICATES_AND_SIMILARITY.md) · [자동 분류](docs/AUTOMATIC_CLASSIFICATION.md) · [로그](docs/LOGGING.md) · [변경 내역](CHANGELOG.md) · [배포 원칙](docs/RELEASE_POLICY.md)

[이용약관](docs/TERMS_OF_USE.md) · [개인정보 안내](docs/PRIVACY.md) · [이용허락 요청](docs/PERMISSION_REQUEST.md) · [제3자 라이선스](docs/THIRD_PARTY.md)
