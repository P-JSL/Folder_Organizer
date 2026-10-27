# Folder Organizer

Windows에서 **내 컴퓨터의 폴더**를 선택해 파일을 살펴보고, 변경 내역을 미리 확인한 후 안전하게 정리하는 데스크톱 앱입니다. 파일은 외부 서버에 업로드하지 않습니다.

![파일 목록과 중복 검토 화면](docs/screenshot.png)

## Windows에서 사용

1. [Releases](https://github.com/P-JSL/Folder_Organizer/releases)에서 `FolderOrganizer-Windows-x64.zip`을 다운로드합니다. 해당 파일이 아직 없으면 아래 소스 실행 방법을 사용하세요.
2. ZIP 전체를 압축 해제한 뒤 `FolderOrganizer/FolderOrganizer.exe`를 실행합니다. `_internal` 폴더도 함께 보관해야 합니다.
3. **폴더 선택 → 분석**으로 시작합니다. 우선 작은 테스트 폴더에서 미리보기와 되돌리기를 확인하세요.

Windows 10/11 x64, Python 3.11 이상을 대상으로 합니다. 실행 파일은 코드 서명되지 않은 초기 버전이므로 Windows에서 게시자 확인 알림을 표시할 수 있습니다. Linux에서 자동화 검증하며 Windows 실행 파일은 GitHub Actions의 Windows 러너에서 빌드·검증합니다. macOS는 소스 실행을 지원하는 구조이며 이 버전에서 별도 패키지나 실기기 검증을 제공하지 않습니다.

## 기능

| 기능 | 동작 |
| --- | --- |
| 파일 목록 | 상대 경로, 파일별 용량, 수정일, 생성일, 파일/응용 프로그램 구분, 정렬·검색·필터 |
| 하위 폴더 | 재귀 분석, 숨김 파일 선택 포함, 진행 상태와 취소 |
| 중복 검사 | 같은 크기 → SHA-256 → 실제 내용 비교. 같은 이름만으로 중복 판정하지 않음 |
| 유사 파일 | 이름, 제한된 UTF-8 텍스트 내용, 이미지 지각 해시를 비교한 **검토 후보**와 근거 표시 |
| 날짜·확장자 정리 | 미리보기 후 선택 파일을 연월별 또는 확장자별 폴더로 이동 |
| 복사·이동 | 사용자가 선택한 대상 폴더, 이름 충돌 시 별도 이름, 복사 검증 후 이동 |
| 선택 항목을 중복으로 간주 | 중복 검증 없이 사용자가 선택한 파일을 격리하는 **별도 버튼**과 확인 절차 |
| 격리·되돌리기 | 영구 삭제 대신 선택 폴더 내부 격리 보관, 작업 기록, 원래 경로 복원 |
| 파일 건강 검사·복구 | 지원되는 ZIP·Office ZIP·이미지·PDF 검사, 가능한 내용만 **새 파일**로 복구 |
| 내보내기 | UTF-8 CSV, 폴더별 분석 JSON을 위한 읽기 전용 CLI |
| 자동 업데이트 | Windows 배포 앱에서 새 정식 GitHub 릴리스 확인·다운로드·검증, 작업 종료 후 교체 |

생성일은 운영체제가 제공하는 실제 생성 시각만 표시합니다. Windows는 생성 시각을, 지원되는 macOS는 birthtime을 사용하며, Linux 등에서 제공되지 않으면 `제공되지 않음`으로 표시합니다. 메타데이터 변경 시각을 생성일로 표시하지 않습니다. 용량은 논리적 파일 크기이며 디스크 실제 할당량이나 중복 제거 후 확보 공간과 다를 수 있습니다.

`.exe`, `.msi`, `.dmg`, `.pkg`, `.deb`, `.rpm`, `.appimage`, `.desktop` 등은 응용 프로그램 관련 파일로 분류합니다. 선택한 폴더 내 파일을 관리하며, Windows 설치 프로그램 목록이나 레지스트리를 읽거나 프로그램을 제거하지 않습니다. 서로 다른 버전의 실행 파일은 이름이 유사해도 대체 가능한 프로그램으로 간주하지 않습니다. macOS `.app`은 내부 파일 단위로 표시하며, 앱 묶음을 손상시킬 수 있는 내부 파일 개별 이동은 차단합니다.

## 자동 업데이트

Windows ZIP 배포판에는 업데이트 기능이 기본 활성화되어 있습니다. 새 정식 버전을 GitHub Releases에서 확인하고 Windows ZIP과 체크섬을 내려받아 SHA-256 및 패키지 구조·버전을 검증합니다. 정리 작업 중에 앱을 강제 종료하지 않으며, 다운로드된 업데이트는 **앱을 정상 종료한 뒤** 별도 도우미가 적용하고 앱을 다시 실행합니다. 교체 실패 시 이전 앱 디렉터리로 되돌립니다. 설정에서 자동 확인을 끄거나 수동으로 확인할 수 있습니다.

인터넷 연결과 앱 폴더에 대한 쓰기 권한이 필요합니다. 관리자 권한이 필요한 Program Files 대신 사용자 쓰기 가능한 폴더에 압축을 푸세요. 오프라인·GitHub API 제한·체크섬 오류·잠긴 파일이 있으면 기존 버전을 유지하고 오류를 표시합니다. 소스 실행과 Linux/macOS에서는 자동 교체를 하지 않습니다. 앱이 실행되지 않는 동안에는 업데이트를 확인하지 않으며, 다른 사람의 PC에 직접 접속해 업데이트하는 서비스가 아닙니다.

업데이트 확인은 GitHub에 버전 메타데이터와 배포 파일을 요청합니다. 개인 파일·선택 폴더 경로는 전송하지 않습니다. HTTPS와 릴리스 체크섬을 검증하며, 별도의 코드 서명 인증서는 포함하지 않습니다. 개발자는 버전을 올리고 `vX.Y.Z` 태그를 게시해야 합니다. 같은 태그의 파일을 덮어쓰는 방식으로는 새 버전을 배포하지 않습니다.

## 데이터 보호와 복구 범위

- 모든 정리는 미리보기와 명시적 확인 후 실행합니다. 유사도 결과는 자동 삭제 근거가 아닙니다.
- `.folder-organizer`는 격리 파일·작업 기록·보호 사본을 보관하는 전용 폴더입니다. 일반 분석에서 제외합니다. 되돌리기가 필요하면 원래 폴더와 함께 유지하고 지우거나 옮기지 마세요. 이동·격리·실행 취소도 마지막 원본이나 수정본을 잃지 않도록 보호 사본을 남깁니다. 따라서 디스크 공간을 자동 회수하지 않으며 추가 여유 공간이 필요합니다. 다른 드라이브로 복사하거나 오류가 발생한 경우 대상 폴더 쪽에도 `.folder-organizer`가 생성될 수 있습니다. 작업 결과에 표시된 보관 경로를 함께 유지하세요.
- 원본과 대상의 내용·변경 여부를 확인하며, 기존 파일을 덮어쓰지 않습니다. 원래 경로에 새 파일이 생기거나 정리 후 파일을 편집하면 되돌리기가 충돌을 보고하고 파일을 보존합니다.
- 복사 작업도 기록에서 되돌릴 수 있습니다. 파일 내용이 바뀌지 않았고 이 앱이 생성한 복사본임을 확인한 경우에만 대상 위치에서 보호 보관으로 옮깁니다. 변경된 파일은 충돌을 보고하고 보존합니다.
- 심볼릭 링크를 따라가지 않습니다. 관리 권한 상승을 요구하지 않습니다. 시스템 폴더·프로그램 설치 폴더 대신 문서·다운로드 등 개인 데이터 폴더에 사용하세요.
- 대용량 파일은 스트리밍 해시를 사용하며, 이미지·텍스트·유사 후보 수·압축 해제 크기는 제한합니다. 일부 파일을 건너뛴 경우 진행 상태와 경고를 확인하세요. 취소된 분석은 전체 결과가 아닙니다.
- ZIP 복구는 읽을 수 있는 항목을 새 ZIP에 보존합니다. 손실된 항목은 재생성하지 못하며, 일부 항목만 복구되면 `partial`로 표시합니다. DOCX/XLSX/PPTX는 내부 구조가 손실되면 앱에서 정상적으로 열리지 않을 수 있습니다.
- 이미지 복구는 디코딩 가능한 데이터를 다시 저장하는 방식, PDF 복구는 읽을 수 있는 구조를 다시 쓰는 방식입니다. 손실된 원본 바이트, 암호, 임의 실행 파일/동영상 손상, 디스크에서 영구 삭제된 파일을 복원하지 못합니다. 디스크 복구 도구를 대체하지 않습니다.
- 정리 작업의 복원과 손상 파일 복구는 별개입니다. 중요한 데이터는 별도 장치에 백업하세요. 앱을 처음 사용하는 동안에는 원본 복사본으로 검증하는 것을 권장합니다.

## 소스에서 실행

Windows PowerShell:

```powershell
git clone https://github.com/P-JSL/Folder_Organizer.git
cd Folder_Organizer
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m folder_organizer
```

Linux/macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m folder_organizer
```

GUI는 로컬 데스크톱 세션에서 실행해야 개인 컴퓨터의 폴더를 선택할 수 있습니다. 클라우드 실행에서 개인 컴퓨터의 드라이브에 접근하지 않습니다. Ubuntu/Debian은 기본 Qt 라이브러리로 `libegl1`, `libopengl0`가 필요하며, 데스크톱의 xcb 플러그인 오류에는 배포판의 Qt/X11 런타임도 설치하세요. 자동 테스트는 `QT_QPA_PLATFORM=offscreen`을 사용합니다.

읽기 전용 분석:

```sh
python -m folder_organizer --cli /path/to/folder --duplicates --similar
folder-organizer-cli /path/to/folder --csv new-inventory.csv
folder-organizer-cli /path/to/folder --duplicates --json new-analysis.json
```

CLI는 파일을 정리하지 않습니다. CSV는 이미 존재하는 파일을 덮어쓰지 않습니다.

## 테스트·배포

```sh
python -m pytest -q
python -m build
python -m pip install -e '.[bundle]'
python -m PyInstaller --noconfirm --clean --onedir --windowed --name FolderOrganizer --collect-data PySide6 desktop_entry.py
```

GUI 없는 환경에서는 테스트 앞에 `QT_QPA_PLATFORM=offscreen`을 설정하세요. Windows 실행 파일은 Windows에서 빌드해야 합니다. `v*` 태그를 push하면 `.github/workflows/release.yml`이 Windows 테스트, 실행 파일 패키징, 실제 실행 파일의 분석·내보내기·업데이트 도우미 교체 검증, ZIP·SHA-256 체크섬을 포함한 GitHub Release 게시를 수행합니다. 이 워크플로가 성공하기 전에는 Windows 배포가 완료된 것으로 간주하지 않습니다.

[상용 프로그램 비교와 설계 근거](docs/COMPETITIVE_ANALYSIS.md) · [변경 내역](CHANGELOG.md) · [제삼자 라이선스](docs/THIRD_PARTY.md)
