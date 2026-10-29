# 1.0.0 준비 조사와 완료 기준

조사일: **2026-10-28**. 공개 v0.5.0의 기능, 현재 Python 코드와 새 `rust-app` 소스, 아래 공식 자료를 기준으로 작성했습니다. 이 문서는 **1.0.0 출시 완료 선언이 아닙니다**. Rust 소스 모듈이 있다는 사실과 Windows 배포판의 기능·안정성이 확인됐다는 사실을 구분합니다.

1.0.0의 기준은 기능 개수를 늘리는 것이 아니라 **지원하는 동작과 데이터 형식을 약속하고, 그 약속을 두 구현에서 지킬 수 있는가**입니다. SemVer 공식 명세도 “Version 1.0.0 defines the public API”라고 설명합니다. 여기서는 CLI·보고서·규칙·작업 기록·업데이트 계약까지 공개 동작에 포함합니다.

## 우선순위와 필요한 기술

| 우선순위 | 필요한 일 | 채택할 기술·설계 | 1.0.0 완료 증거 |
| --- | --- | --- | --- |
| 필수 | Python판·Rust판의 동작 범위 일치 | 두 구현의 기능 표, 공통 예제 데이터와 결과 규격, 지원하지 않는 형식의 명시적 안내 | 파일 조회부터 중복 정리·복원·자동 업데이트까지 두 Windows 패키지의 지원 표와 변경된 경로의 검증 기록 |
| 필수 | 파일 변경 시 원본 보존 | 파일 식별자·크기·변경 시각 확인, 완전 중복의 전체 SHA-256·바이트 확인, 덮어쓰지 않는 이동, 실행 전 기록과 중단 후 재개 | 변경·잠금·권한·이름 충돌·작업 중단 시에도 마지막 원본을 잃지 않는 새 코드 경로의 증거 |
| 필수 | 두 버전의 기록 충돌 방지 | 명시적 기록 스키마·버전·구현 구분, 루트 단위 잠금, 인증된 기록, 호환되지 않는 기록은 읽기 전용 또는 명시적 거부 | 한 구현이 다른 구현의 기록을 추측해 수정·복원하지 않음; 호환을 제공한다면 형식·인증·복원 결과까지 확인 |
| 필수 | 복원 데이터의 독립성 | 원본 바이트의 독립 보관, 청크 SHA-256·무손실 압축, 검증한 설명과 데이터의 원자적 확정, 부적합 시 원시 보관 유지 | 살아 있는 다른 원본이 없어도 복원 가능; 손상·변경된 보관본은 조용히 성공 처리하지 않음 |
| 필수 | 복원 범위와 운영체제 메타데이터 | Windows ADS·EFS 여부를 확인해 보존하지 못하는 자료는 원시 보관 유지; 권한·확장 속성·생성 시각의 명시적 계약 | 압축으로 추가 스트림을 버리지 않음; 내용·수정일 복원과 전체 파일 시스템 메타데이터 복원을 구분하고, 전체 재현을 지원하려면 형식·실제 복원을 추가 검증 |
| 필수 | Windows·NAS 경계의 일관성 | Unicode·긴 경로·UNC·매핑 드라이브, 링크·재분석 지점 차단, 공유 위반과 연결 끊김 처리 | 실제 지원 환경과 새 장애 경로의 결과를 기록; `#recycle` 존재만으로 휴지통 보장을 표시하지 않음 |
| 필수 | 배포와 업데이트 계약 분리 | Python판·Rust판의 서로 다른 패키지 식별자·파일명·버전 표식, HTTPS·크기 제한·해시·구조 검증, 작업 종료 후 교체·롤백 | Python 업데이트가 Rust판을 설치하거나 그 반대가 되지 않음; 두 패키지의 이전 지원 버전에서 갱신 가능 |
| 필수 | 정식 배포의 출처 확인 | 고정된 의존성·잠금 파일, 라이선스 고지, 변경 불가능한 버전 태그, 검증된 빌드의 출처 기록 | 어떤 소스·의존성으로 각 실행 파일이 만들어졌는지 추적 가능; 같은 버전 자산을 덮어써 새 버전처럼 배포하지 않음 |
| 필수 | 명확한 사용자 경험 | 헤더 메뉴, 적은 주요 버튼, 검토와 즉시 실행의 구분, 키보드 탐색·고해상도·다크/라이트 표시 | 전용 “첫 파일만 남기기”의 대상·보존 파일·보관 위치를 실행 전에 이해 가능; 오류가 다음 행동을 설명함 |
| 중요 | Windows 게시자와 업데이트 진위 강화 | Authenticode 서명·타임스탬프, 신뢰할 게시자 검증 또는 별도 서명된 릴리스 명세 | 실제 발급·보관된 서명 수단과 검증 결과. 현재의 HTTPS·SHA-256만으로 독립적인 게시자 서명을 제공한다고 표시하지 않음 |
| 중요 | 큰 폴더에서 예측 가능한 속도 | 순차 탐색, 불필요한 메타데이터 호출·객체 할당 제거, 크기별 중복 인덱스, 재사용 가능한 화면 모델, 제한된 병렬 I/O | 동일 장치·데이터·캐시 조건에서 변경 전후 시간·읽은 바이트·메모리·화면 응답을 측정 |
| 중요 | 지원 비용과 진단 | 사용자 경로·파일 내용을 보내지 않는 로컬 로그, 제한된 크기·순환 보관, 동작 이력과 진단의 구분 | 로그로 실패 위치를 찾을 수 있고, 복원 권한은 로그가 아니라 인증된 기록으로 판단 |
| 선택 | 더 높은 공간 효율 | CDC·다른 압축 코덱·저장소 정리 정책 | 실제 자료에서 이득과 복원 비용이 입증될 때만 도입; 현 고정 청크·원시 보관 대체 경로 유지 |
| 보류 | 저수준 전체 디스크 탐색·상시 감시 | NTFS MFT·USN Journal·파일 시스템 감시 | 권한·누락·네트워크 공유·무효화 정책을 정의하기 전에는 기본 기능으로 넣지 않음 |

게시자 서명은 상용 배포 수준을 높이는 권장 과제입니다. 인증서나 서명 서비스를 아직 구성하지 않았다면 완료로 표시할 수 없습니다. SHA-256은 다운로드가 기대한 바이트인지 확인하지만, 동일 배포처의 실행 파일과 체크섬이 함께 바뀌는 상황에 대한 독립적인 게시자 인증은 아닙니다.

## Python과 Rust의 공통 기능 계약

아래의 “소스 경로”는 구현 위치를 찾는 근거입니다. **Rust 모듈의 존재를 Windows 실행 검증이나 기능 동등성 통과로 해석하지 않습니다.** 최종 지원 여부는 각 배포판의 기능 표와 이번 변경에 대한 빌드·검증 기록에 표시해야 합니다.

| 기능 | Python 기준 구현 | Rust의 대응 소스·필요 계약 |
| --- | --- | --- |
| 파일별 크기·수정일·생성일, 제외 폴더, 진행·취소 | `scanner.py`, `models.py`, `ui.py` | `rust-app/src/core.rs`; 생성 시각이 없으면 없다고 표시, 탐색 누락·취소를 숨기지 않음 |
| 완전 중복과 첫 파일 보존 | `matching.py`, `rules.py`, `operations.py` | `core.rs`, `operations.rs`; 이름이나 유사도만으로 삭제하지 않음; 검사 결과가 바뀌면 기존 삭제 계획 거부 |
| 유사 후보와 근거 | `matching.py`, `comparison.py` | `similarity.rs`; 이름·텍스트·이미지의 지원 범위와 비교 한도를 Python과 함께 설명 |
| 규칙 분류·선택 복사·이동·수동 안전 보관 | `rules.py`, `operations.py` | `rules.rs`, `operations.rs`; `{규칙}` 매핑·날짜 기준·충돌 회피·미리보기 계약 일치 |
| 겹치는 내용 병합 | `merging.py`, `merge_view.py` | `merging.rs`; 지원 인코딩·문단/행 중복·입력 한도·원본 유지 기본값 일치 |
| 작업 기록·복원·압축 데이터 | `operations.py`, `recovery_metadata.py`, `recovery_payload.py` | `operations.rs`, `recovery.rs`; 기록 인증·독립 데이터·압축 대체 경로와 형식 호환 정책 명시 |
| 손상 검사와 새 복구본 생성 | `recovery.py` | `recovery.rs`; 읽을 수 있는 내용을 새 파일로 보존하는 범위이며 삭제 섹터나 유실된 원문을 생성하는 기능으로 표시하지 않음 |
| 보고서·실행 로그 | `reporting.py`, `execution_log.py` | `reporting.rs`와 실제 로그 구현; 생성 파일 덮어쓰기 금지, 제어문자·HTML/CSV 내보내기 정책 확인 |
| 데스크톱 UI·설정·도움말 | PySide6 UI 모듈 | Rust GUI 구현; Python 런타임을 호출하는 포장판을 독립 Rust판으로 표시하지 않음 |
| 자동 업데이트 | `updater.py`, `updater_helper.py` | Rust 전용 구현·패키지 표식·다운로드 자산 계약 필요; 실제 지원 여부를 별도 표시 |

## 속도 개선에서 적용할 기준

파일 목록을 완전히 읽는 작업은 최소한 각 항목을 방문해야 하므로 파일 수 `n`과 경로 길이에 비례하는 비용이 듭니다. 내용 `B` 바이트의 완전한 확인에는 최소 `Ω(B)` 읽기가 필요합니다. Rust로 바꾸는 것만으로 모든 작업이 `O(n)`보다 빨라지거나 NAS 지연이 사라지지 않습니다.

| 처리 | 개선 방향 | 유지해야 할 조건 |
| --- | --- | --- |
| 목록 탐색 | 필요하지 않은 정렬·상태 조회·패턴 재계산을 줄이고 진행 알림을 묶음 처리 | 숨김·제외·취소·생성일·링크 방지 결과가 바뀌지 않음 |
| 중복 후보 | 크기 인덱스와 이미 계산한 특징의 작업 내 재사용, 필요할 때만 내용 읽기 | 전체 해시·바이트 확인과 변경 감지가 삭제 근거로 남음 |
| 유사 비교 | 전체 목록에서 제한된 후보 인덱스를 만들고 특징을 한 번 계산 | 제한된 후보가 완전 탐색과 같다고 표시하지 않음 |
| 화면 표시 | 가상 목록, 표시하는 행의 형식화, 경로→행 인덱스, 선택 합계의 증분 계산 | 화면에서 숨긴 파일과 실제 검사 범위를 구분 |
| 파일 변경 | 불필요한 원본 재탐색을 줄이고 기존 검증 결과와 영수증을 사용 | 인증된 계획·파일 변경 확인·덮어쓰기 금지·내구성 기록 생략 금지 |
| Rust 최적화 | release 프로필·LTO·codegen 설정을 실제 자료로 비교 | 컴파일 옵션의 이름만으로 향상률을 약속하지 않음; 안전 검사를 제거해 수치를 만들지 않음 |

측정은 로컬 SSD·실제 사용 NAS를 구분하고, 파일 개수·전체 바이트·같은 크기 파일 비율·정확 중복 비율·캐시 상태를 함께 기록합니다. 같은 데이터에 대한 Python/Rust 결과의 일치와 처리 비용을 함께 봅니다. 기존 [성능 안내](PERFORMANCE.md)의 v0.5.0 수치는 새 Rust판의 성능 수치가 아닙니다.

## 이미 통과한 검사의 처리

사용자 지시에 따라 **기존에 통과한 동일 코드의 검사는 재실행하지 않습니다**. 검증 기록에는 이전 통과 실행·커밋, 이번 변경 파일과 재사용 근거를 남깁니다. 새 Rust 구현, 변경한 알고리즘·화면·파일 변경 경로는 이전 Python 코드의 통과 기록으로 대신할 수 없으므로 변경에 필요한 새 검증만 수행합니다.

Rust판에 대한 결과가 없으면 “미확인”으로 표시합니다. 기존 검사를 건너뛰었다는 이유로 “통과”를 새로 만들거나, 아직 구성되지 않은 서명·NAS 실기기 동작·기능 동등성을 완료로 표시하지 않습니다.

## 실제로 읽은 공식 자료

아래 자료는 모두 **2026-10-28**에 읽었습니다. 공개 제품 설명·기술 문서를 조사했으며 제품 구매·경쟁 제품 속도 실측·비공개 알고리즘 분석을 했다는 뜻은 아닙니다.

- [Semantic Versioning 2.0.0 공식 원문](https://github.com/semver/semver/blob/master/semver.md): 공개 API의 명시와 1.0.0 이후의 버전 계약. 핵심 원문: “Version 1.0.0 defines the public API.”
- [Cargo 공식 SemVer 호환성 문서](https://github.com/rust-lang/cargo/blob/master/doc/book/src/reference/semver.md): 호환·비호환 변경의 구분. 앱의 기록·보고서 형식 변경에도 명시적인 호환 정책이 필요하다는 설계 근거로 참고했습니다.
- [Cargo 공식 프로필 문서](https://github.com/rust-lang/cargo/blob/master/doc/book/src/reference/profiles.md): 최적화·LTO·codegen 단위의 비용과 절충. 핵심 원문: “It is recommended to experiment with different levels to find the right balance for your project.”
- [Cargo.toml과 Cargo.lock 공식 안내](https://github.com/rust-lang/cargo/blob/master/doc/book/src/guide/cargo-toml-vs-cargo-lock.md): 잠금 파일을 버전 관리해 선택된 의존성을 재현. 핵심 원문: “When in doubt, check Cargo.lock into the version control system.”
- [Rust 공식 Unsafe Rust 장](https://github.com/rust-lang/book/blob/main/src/ch20-01-unsafe-rust.md): FFI 등 unsafe 영역은 별도 불변 조건을 지켜야 하며 Rust 사용 자체가 파일 작업의 정확성을 보장하지 않음.
- [Microsoft SignTool 공식 문서](https://github.com/MicrosoftDocs/win32/blob/docs/desktop-src/SecCrypto/signtool.md): 디지털 서명·타임스탬프·검증과 SHA-256 지정. 핵심 원문: “Digital signatures protect files from tampering and enable users to verify the signer based on a signing certificate.”
- [Microsoft WinVerifyTrust 공식 API 문서](https://github.com/MicrosoftDocs/sdk-api/blob/docs/sdk-api-src/content/wintrust/nf-wintrust-winverifytrust.md): 다운로드 객체의 신뢰 검증과 실제 반환값·정책 처리의 필요성.
- [Microsoft MoveFileExW 공식 API 문서](https://github.com/MicrosoftDocs/sdk-api/blob/docs/sdk-api-src/content/winbase/nf-winbase-movefileexw.md): 볼륨 간 이동은 복사·삭제로 모사될 수 있으며 복사 성공 뒤 원본 삭제가 실패할 수 있음. 동일 볼륨의 원자적 이동과 같은 성질로 취급하지 않습니다.
- [Microsoft 긴 경로 공식 안내](https://github.com/MicrosoftDocs/win32/blob/docs/desktop-src/FileIO/maximum-file-path-limitation.md): Win32 긴 경로 지원은 앱 manifest와 시스템 조건을 함께 고려해야 함.
- [Duplicate Cleaner 공식 기능 안내](https://www.duplicatecleaner.com/): Selection Assistant와 근거를 검토하는 선택 흐름. 파일 유형·보존 조건과 사용자가 이해하는 정리 절차에 우선순위를 둡니다.
- [TreeSize 공식 기능 목록](https://www.jam-software.com/treesize/features.shtml): 폴더·종류·큰 파일·시기별 용량 분석과 변경 비교. 전용 클라우드 연결·저수준 디스크 기능 전체를 복제할 필요는 없습니다.
- [Beyond Compare 공식 기능 비교](https://www.scootersoftware.com/kb/feature_compare): 저장한 세션·비교 필터·내용 없는 파일 시스템 스냅샷. 내용 없는 스냅샷을 원본 바이트 복구 기술로 해석하지 않습니다.

기능을 줄일 범위와 보존할 호환 계층은 [기능 정리 결정](FEATURE_DECISIONS.md)에 따릅니다.
