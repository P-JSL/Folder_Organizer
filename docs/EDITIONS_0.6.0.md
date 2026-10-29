# Python판과 Rust판 · 0.6.0 정식 빌드

기존 Python/PySide6 앱과 별도로 Rust/egui 앱을 구현했습니다. 두 앱 모두 로컬 파일을 처리하며 Rust판은 Python 실행 파일을 호출하지 않습니다. v0.6.0은 두 판의 정식 빌드를 별도 ZIP으로 제공하며, 1.0.0 완료 조건은 별도로 추적합니다.

## 구성

| 구분 | Python | Rust |
| --- | --- | --- |
| Windows 패키지 | `FolderOrganizer-Windows-x64.zip` | `FolderOrganizer-Rust-Windows-x64.zip` |
| 앱 | `FolderOrganizer/FolderOrganizer.exe` | `FolderOrganizerRust/FolderOrganizerRust.exe` |
| 콘솔 분석 | 앱의 `--cli` 인자 | `FolderOrganizerRustCLI.exe` |
| 업데이트 도우미 | `FolderOrganizerUpdater.exe` | `FolderOrganizerRustUpdater.exe` |
| 앱 식별자 | `P-JSL/Folder_Organizer` | `P-JSL/Folder_Organizer.Rust` |
| 작업 기록 | `.folder-organizer` | `.folder-organizer-rust` |
| 새 삭제 보관 | 선택 폴더의 `delete_files` | 선택 폴더의 `delete_files` |

ZIP 전체를 서로 다른 앱 폴더에 풀어 사용하세요. 두 판은 업데이트 파일과 설정·복원 기록을 구분합니다. **Python에서 만든 기록은 Python판에서, Rust에서 만든 기록은 Rust판에서 복원**합니다. 기록 형식의 상호 변환을 제공한다고 주장하지 않습니다. `.folder-organizer`의 작업 잠금은 두 판이 같은 폴더를 동시에 변경하지 않도록 공유하며, 이 잠금 파일은 복원 데이터가 아닙니다.

## 기능 대응과 지원 경계

| 기능 | 두 판의 목표 동작 | 경계 |
| --- | --- | --- |
| 파일 조회 | 용량·종류·수정일·제공되는 생성일, 재귀·숨김·제외 조건, 검색·정렬·진행·취소 | 논리적 크기 합계이며 실제 할당 공간과 다를 수 있음 |
| 완전 중복 | 크기·전체 SHA-256·실제 바이트, 현재 검증 결과로 첫 파일·보호 파일 보존 | 이름만 같거나 유사한 파일을 삭제 근거로 사용하지 않음 |
| 유사 비교 | 이름·지원 텍스트·이미지의 근거와 제한된 후보 검색 | 점수 알고리즘·후보 순서가 언어 간 동일하다고 보장하지 않으며 완전 탐색 여부를 구분 |
| 정리 | 선택 복사·이동·안전 보관, 명시적 미리보기와 충돌 회피 | 전용 첫 파일 보존 버튼은 별도 전체 검사·확인 없이 현재 증명 사용 |
| 규칙 | 순서·조건·날짜·한글/영문 `{규칙}` 매핑, 추천 규칙 | 생성일이 없으면 수정일로 추측하지 않음; 폴더 밖 경로 차단 |
| 병합 | UTF-8 TXT·Markdown 문단, 같은 형식·열 구조의 CSV·TSV 행 | 새 병합본 기본값; 의미 충돌·Office·임의 바이너리를 합치는 기능은 아님 |
| 작업 기록·복원 | 인증된 기록, 덮어쓰지 않는 원래 위치 복원, 변경·손상 거부 | 두 판의 기록은 별도 형식 |
| 보관 데이터 최적화 | 인증된 압축 메타데이터, 독립적인 실제 바이트 청크 공유·무손실 압축 | 이득·시간·크기 한도에 맞지 않으면 원시 보관 유지; 살아 있는 원본에 의존하지 않음 |
| 손상 검사·복구본 | 지원 ZIP/Office ZIP·이미지·PDF에서 읽을 수 있는 내용을 새 파일로 보존 | Rust PDF는 자원 한도를 보장할 수 없는 압축 객체/xref 스트림을 명시적으로 거부; 애니메이션 복구는 첫 프레임 |
| 실행 로그·보고서 | 로컬 실행·진단 기록, 새 CSV·JSON·HTML, HTML 이스케이프 | 보고서에 파일 본문·이미지 미리보기를 넣지 않음 |
| GUI·설정 | 헤더 메뉴, 기본 다크·라이트, 최근 폴더, 도움말, 비교·용량 요약 | UI 구현과 배치가 완전히 동일한 것은 아님 |
| 업데이트 | 각 판의 안정 릴리즈 자산·버전·체크섬 확인, 종료 후 도우미 교체·백업 | Rust 자산 없는 릴리즈는 Rust에 제공하지 않음; 시험 빌드를 공개 자동 업데이트로 가장하지 않음 |

Rust 코드는 같은 기능 영역을 구현하지만, **언어를 바꿨다는 사실이나 모듈의 존재만으로 모든 파일의 결과·Windows/NAS 동작이 동등하다고 표시하지 않습니다.** 실제 빌드·검증 기록과 남은 1.0 과제를 함께 확인하세요. NAS의 `#recycle` 존재만으로 안전 삭제를 추정하지 않으며 두 판의 새 삭제는 `delete_files`를 사용합니다.

보관 파일을 그대로 이동해 복원하면 운영체제가 보존하는 속성도 유지됩니다. 별도 압축 최적화의 복원 계약은 파일 내용과 수정일 중심이며 ACL·확장 속성·원래 생성일의 완전한 재현은 1.0 준비 과제입니다. Windows의 추가 데이터 스트림이나 EFS 암호화가 있거나 이를 확인할 수 없으면 원본 보관본을 유지합니다. 복원에 필요한 추가 바이트를 압축 이득을 위해 버리지 않습니다.

## 소스에서 실행

Python은 저장소의 설치 안내대로 의존성을 설치한 뒤 `python -m folder_organizer`로 실행합니다. Rust는 `rust-app/rust-toolchain.toml`의 도구 체인과 `Cargo.lock`을 사용합니다.

```sh
cargo run --manifest-path rust-app/Cargo.toml --locked --features gui --bin FolderOrganizerRust
cargo run --manifest-path rust-app/Cargo.toml --locked --bin FolderOrganizerRustCLI -- /path/to/folder --duplicates --json /path/to/new-report.json
cargo build --manifest-path rust-app/Cargo.toml --locked --release --features gui --bins
```

Windows 정식 패키지는 `--target x86_64-pc-windows-msvc`를 명시해 빌드합니다. 저장소의 `.cargo/config.toml`은 이 대상의 C 런타임을 정적으로 연결하며, 패키징할 때 별도 VC++ 런타임 DLL이 필요한지도 확인합니다.

콘솔 분석은 읽기 전용이고 기존 보고서 파일을 덮어쓰지 않습니다. 파일 변경은 데스크톱 앱에서 수행합니다. Windows 실행 파일은 Windows에서 빌드하며 Linux의 빌드 성공을 Windows 실행 성공으로 대신하지 않습니다.

## 기존에 통과한 검사는 재사용

v0.5.0의 Windows·Ubuntu 검사와 실제 패키지/업데이트 검증은 해당 코드의 기존 기록으로 유지합니다. 이번 작업에서는 새 Python 최적화·통합 UI와 새 Rust 구현에 필요한 검사만 수행합니다. 이미 통과한 검사를 반복하지 않으며, 검증하지 않은 새 경로를 통과했다고 적지 않습니다. 빌드·의존성·런타임을 바꾸는 경우에는 바뀐 결과에 대한 확인이 필요합니다. 정식 빌드 워크플로는 Python ZIP과 Rust ZIP을 각각 업로드하고, 두 파일과 체크섬이 준비된 뒤 정식 릴리스를 게시합니다.

측정 근거: [Python 탐색·요약 벤치마크](INVENTORY_BENCHMARK_0.6.0.json). 해당 Linux 임시 폴더의 실측이며 Windows/NAS·모든 자료의 향상률을 뜻하지 않습니다.
