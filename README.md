# Folder Organizer

Windows에서 폴더의 파일 정보를 확인하고, 중복·유사 검사부터 복사·이동·분류·복원까지 진행하는 데스크톱 앱입니다. **폴더 한 개 또는 여러 개를 같은 화면에서 분석·정리**하며, 개인 파일을 외부 서버에 업로드하지 않습니다.

최신 정식 버전: **[v0.9.1 · Python / Rust](https://github.com/P-JSL/Folder_Organizer/releases/tag/v0.9.1)**

**이용은 권리자 P-JSL 본인과 명시적으로 서면 허락받은 사람에게만 허용됩니다.** 공개 저장소와 다운로드 제공은 이용·재배포 허락을 의미하지 않습니다. [LICENSE](LICENSE)를 확인하세요.

## 다운로드 및 실행

Windows 10/11 x64용입니다. 두 배포판 모두 별도 Python 설치 없이 실행할 수 있습니다.

| 배포판 | 다운로드 | 압축 해제 후 실행 파일 |
| --- | --- | --- |
| Python | [Windows ZIP](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.1/FolderOrganizer-Windows-x64.zip) | `FolderOrganizer/FolderOrganizer.exe` |
| Rust | [Windows ZIP](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.1/FolderOrganizer-Rust-Windows-x64.zip) | `FolderOrganizerRust/FolderOrganizerRust.exe` |

1. 원하는 배포판의 ZIP **전체를 새 폴더에 압축 해제**합니다.
2. 표에 안내된 실행 파일을 엽니다. 함께 들어 있는 도우미·라이선스 파일과 Python판의 `_internal` 폴더를 유지하세요.

체크섬은 [릴리즈의 `SHA256SUMS.txt`](https://github.com/P-JSL/Folder_Organizer/releases/download/v0.9.1/SHA256SUMS.txt), 빌드 정보와 확인 범위는 [실제 배포 결과](docs/RELEASE_RESULTS_0.9.1.md)에서 확인할 수 있습니다. 현재 실행 파일은 코드 서명되지 않아 Windows에서 게시자 확인 알림이 표시될 수 있습니다.

## 처음 사용하는 순서

1. **폴더 추가**로 위치를 등록하고, 분석할 폴더를 체크합니다. 하위 폴더·숨김 파일·제외 조건도 확인하세요.
2. **분석 시작**을 눌러 파일별 용량·날짜와 완전 중복 결과를 확인합니다. 폴더가 한 개여도 같은 화면을 사용합니다.
3. 필요하면 **유사 비교**를 실행하고, 비교 근거와 파일 정보를 살펴본 뒤 정리할 파일을 체크합니다.
4. 복사·이동·삭제 보관·규칙 분류를 선택해 **원본·대상 경로 미리보기**를 확인하고 실행합니다.
5. 변경 내역은 **기록·복원**에서 확인합니다. 다음 작업에 새 분석이 필요하면 화면의 안내를 따르세요.

상단 리본에서 작업을 고르고, 왼쪽에서 폴더·필터, 가운데에서 파일·검사 결과, 오른쪽에서 선택 파일 정보를 확인합니다. 아래에는 종류별 용량 요약과 진행 상태가 표시됩니다.

기본 테마는 다크 모드이며 **보기**에서 라이트 모드로 바꿀 수 있습니다. **도움말 또는 F1**에서 기능 설명을 찾을 수 있습니다. 자세한 순서는 [통합 작업 공간 안내](docs/WORKSPACE_ACTIONS.md)를 참고하세요.

## 주요 기능

| 기능 | 설명 |
| --- | --- |
| 단일·다중 폴더 분석 | 파일별 용량·수정일·생성일·종류를 조회하고 검색·필터로 결과를 좁힙니다. |
| 용량 요약 | 종류·폴더별 용량과 큰 파일·빈 파일 등 정리할 후보를 확인합니다. |
| 완전 중복 검사 | 같은 크기 → SHA-256 → 실제 바이트 비교로 내용이 같은 파일을 확인합니다. |
| 유사 파일 비교 | 이름·지원 텍스트·이미지의 유사 후보와 근거를 표시하고 두 파일을 나란히 비교합니다. |
| 중복 보존 기준 | 첫 파일·수정일·경로·선호 폴더 등의 기준과 보호 폴더를 사용합니다. |
| 복사·이동·삭제 보관 | 체크한 파일을 미리보기 후 처리하며, 기존 파일은 덮어쓰지 않습니다. |
| 규칙 자동 분류 | 종류·날짜·확장자와 이름·크기·경로 조건, 이름·날짜 토큰을 이용한 폴더 매핑을 지원합니다. |
| 내용 병합 | 지원 UTF-8 텍스트·Markdown·CSV·TSV의 겹치는 문단·행을 정리한 새 파일을 만듭니다. 원본 유지가 기본입니다. |
| 작업 기록·복원 | 실제 원본 폴더별로 처리 내역을 남기고, 충돌을 확인하며 되돌립니다. 복원용 메타데이터 압축과 보관 데이터 최적화도 지원합니다. |
| 파일 건강 검사·복구 | 지원 ZIP·Office ZIP·이미지·PDF를 검사하고, 읽을 수 있는 내용을 새 파일로 복구합니다. |
| 진행 상태·로그·내보내기 | 작업 진행·취소·부분 실패와 파일별 로그를 확인하고, CSV 목록·JSON 분석 보고서를 저장합니다. |
| 자동 업데이트 | 각 판에 맞는 새 정식 ZIP을 확인·검증하고, 작업 종료 후 앱 교체를 진행합니다. |

## 중복과 유사, 삭제·복원 방식

**완전 중복**은 실제 내용이 같은 파일이고, **유사 파일**은 사용자가 확인할 후보입니다. 이름이 같거나 유사도가 100%여도 완전 중복이나 삭제 가능 여부를 보장하지 않습니다. 자세한 기준은 [중복·유사 판정 안내](docs/DUPLICATES_AND_SIMILARITY.md)에 있습니다.

**첫 번째 파일만 남기고 나머지 삭제**는 현재 검증된 중복 그룹의 첫 파일과 보호 대상을 남기는 전용 동작입니다. 여러 폴더에 걸친 그룹도 전체에서 첫 파일을 보존하며, **추가 전체 재검사·재해시·확인 창 없이 즉시 실행**합니다. 파일 식별 정보와 변경 여부 확인은 유지합니다. 여기서 첫 파일은 최신 파일이나 가장 좋은 원본이라는 뜻이 아닙니다.

직접 체크한 파일을 **중복으로 간주해 삭제 보관**하는 동작은 별개입니다. 해당 파일을 완전 중복으로 새로 판정하는 것이 아니므로 일반 정리 미리보기에서 대상을 확인하세요.

삭제할 파일은 실제 소속 폴더 아래의 **`delete_files`**로 이동합니다. Windows·NAS 휴지통 이동을 보장하지 않으며, 안전 보관한 파일은 계속 디스크 공간을 사용합니다. 복원하려면 **`delete_files`와 `.folder-organizer`의 기록·키·보관 데이터를 함께 유지**하세요.

압축 메타데이터는 복원에 필요한 경로·해시 등의 정보입니다. **파일 내용이 없는 메타데이터만으로 원본을 재생성할 수는 없습니다.** 손상 파일 복구도 읽을 수 있는 내용을 새 파일로 보존하는 기능이며, 영구 삭제된 디스크 영역을 복원하는 도구는 아닙니다.

## 업데이트와 지원 범위

Windows ZIP 배포판은 자동 업데이트가 기본 활성화되어 있습니다. **도구**에서 자동 확인을 끄거나 수동으로 새 버전을 확인할 수 있습니다. 업데이트는 각 판의 ZIP·체크섬·버전을 확인한 뒤 **앱을 정상 종료한 후** 적용합니다. 인터넷 연결과 앱 폴더 쓰기 권한이 필요하며, 오류가 발생하면 기존 버전을 유지합니다.

생성일은 운영체제가 제공할 때만 표시하며, 제공되지 않으면 해당 상태를 알립니다. 파일 용량은 논리적 크기로 실제 디스크 할당량과 다를 수 있습니다. 유사 검색·병합·복구에는 형식과 용량 제한이 있으며, 취소·접근 실패·부분 결과를 전체 완료와 구분합니다.

Linux/macOS는 소스 실행 구조를 제공하지만 별도 정식 패키지나 실기기 검증을 제공하지 않습니다. 자동 앱 교체는 Windows 배포판에서 지원합니다.

## 소스에서 실행

개인 컴퓨터의 폴더를 선택하려면 해당 컴퓨터의 데스크톱 세션에서 실행하세요. 클라우드 환경에서 실행한 앱이 개인 PC의 드라이브에 직접 접근하지는 않습니다.

<details>
<summary>Python판 실행 방법 · Python 3.11 이상</summary>

저장소를 받은 뒤 해당 디렉터리에서 실행합니다.

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m folder_organizer
```

Linux/macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m folder_organizer
```

Ubuntu/Debian에서는 Qt 실행에 `libegl1`, `libopengl0`와 데스크톱에 맞는 Qt/X11 런타임이 필요할 수 있습니다.

</details>

<details>
<summary>Rust판 실행 방법 · Rust 1.99.1</summary>

저장소 디렉터리에서 실행합니다.

```sh
rustup toolchain install 1.99.1 --profile minimal
cargo +1.99.1 run --manifest-path rust-app/Cargo.toml --locked --release --features gui --bin FolderOrganizerRust
```

</details>

<details>
<summary>Python CLI로 읽기 전용 분석</summary>

위에서 만든 Python 가상 환경을 활성화한 터미널에서 사용합니다.

```sh
python -m folder_organizer --cli /path/to/folder --duplicates --similar
folder-organizer-cli /path/to/folder --csv new-inventory.csv
folder-organizer-cli /path/to/folder --duplicates --json new-analysis.json
```

CLI는 파일을 정리하지 않습니다. `--exclude`로 제외 패턴을 반복 지정할 수 있고, `--min-size`·`--max-size`는 바이트 단위입니다. 기존 보고서는 덮어쓰지 않습니다.

</details>

## 관련 문서

| 내용 | 문서 |
| --- | --- |
| 화면과 작업 순서 | [통합 작업 공간](docs/WORKSPACE_ACTIONS.md) · [화면별 기능 안내](docs/GUI_GUIDE.md) |
| 검사·분류 기준 | [중복과 유사](docs/DUPLICATES_AND_SIMILARITY.md) · [자동 분류 규칙](docs/AUTOMATIC_CLASSIFICATION.md) |
| 기술·진단 | [통합 분석](docs/MULTIROOT_ANALYSIS.md) · [성능과 한계](docs/PERFORMANCE.md) · [로그](docs/LOGGING.md) |
| 설계·개선 계획 | [상용 프로그램 비교](docs/COMPETITIVE_ANALYSIS.md) · [1.0.0 준비 기준](docs/V1_READINESS.md) |
| 버전·배포 기록 | [변경 내역](CHANGELOG.md) · [0.9.1 배포 결과](docs/RELEASE_RESULTS_0.9.1.md) · [이전 배포 파일](DOWNLOADS/README.md) |
| 개발·확인 원칙 | [배포·확인 정책](docs/RELEASE_POLICY.md) · [기존 45건 확인 결과](docs/WORKSPACE_ACTION_RESULTS.md) |

기존에 통과한 검사와 수정하지 않은 기능의 검사는 반복하지 않으며, 화면 캡처는 기본적으로 생략합니다. 이전 버전의 상세 변경 내역과 측정 결과는 위 문서에서 확인할 수 있습니다.

## 이용약관·라이선스

[앱 라이선스](LICENSE) · [이용약관](docs/TERMS_OF_USE.md) · [개인정보 안내](docs/PRIVACY.md) · [이용허락 요청](docs/PERMISSION_REQUEST.md) · [제3자 라이선스](docs/THIRD_PARTY.md)

배포 ZIP의 `LICENSE`와 `licenses/FolderOrganizer/`에 관련 문서를 포함합니다. 제3자 라이선스와 기존의 적법한 권리는 유지됩니다.
