# 7. Folder_Organizer — Python/Rust 이중 구현 파일 정리기

**저장소:** [P-JSL/Folder_Organizer](https://github.com/P-JSL/Folder_Organizer)  
**소스 분석 기준:** `main` / [`f74aff3e3e3f56a95fde15cb24d35ba78fee5b59`](https://github.com/P-JSL/Folder_Organizer/commit/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59)  
**보고서 작성일:** 2026-11-11 (KST)  
**대상:** 개발자 · 운영자 · 릴리스 담당자  
**개선 후보:** 5개 (우선순위·수정 위치·구현·검증·원복 기준 포함)  
**상태:** 분석·구현 제안만 작성. 실제 소스 최적화·동적 성능 측정·프로그램 배포는 수행하지 않음.  
> 문서 내 장·절 번호는 원래 6개 저장소 통합 보고서의 번호를 보존한 것입니다. 코드 근거 링크는 작성 시점의 커밋으로 고정했습니다.

## 7.1 두 판의 구조와 구분

**Python판:** [folder_organizer/__main__.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/__main__.py) → [folder_organizer/ui.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/ui.py)(PySide6 GUI) → `scanner.py`/`multiroot.py`(파일 목록) → `matching.py`/`comparison.py`(중복/유사 판정) → `workspace_actions.py`/`operations.py`(변경 실행) → `recovery*.py`(복구) → `updater.py`(업데이트).

**Rust판:** [rust-app/src/main.rs](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/rust-app/src/main.rs) → [rust-app/src/gui.rs](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/rust-app/src/gui.rs)(egui GUI) → `core.rs`/`multiroot.rs` → `similarity.rs`/`workspace_actions.rs` → `operations.rs`/`recovery.rs` → `updater.rs`.

두 판은 **동일 파일 시스템을 대상으로 작업**하지만 복원 기록의 구현·저장 경로가 다릅니다. Rust GUI의 [rust-app/src/gui.rs](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/rust-app/src/gui.rs#L30)에는 Python과 Rust의 작업 기록을 서로 변환하거나 삭제하지 않는다는 정책이 명시되어 있습니다. 같은 폴더에서 두 프로그램을 번갈아 사용하는 작업은 각 판의 **별도 복원 경로**를 이해한 후 진행해야 합니다.

## 7.2 읽기·판정·변경의 데이터 계약

```text
폴더(한 개 또는 여러 개) 등록
 → 읽기 전용 스캔 (원본 파일 목록 / 스냅샷 / 제외·숨김 조건)
 → 완전 중복: 크기 후보 → SHA-256 → 바이트 일치 여부
 → 유사: 파일명 / 텍스트 / 이미지의 비교 후보(삭제 확정 아님)
 → 사용자 선택 또는 전용 '첫 파일 제외 삭제'
 → 원본/대상 미리보기·계획 생성 → 원본 스냅샷/락 검증
 → 원본 이동·복사·delete_files 보관 → 인증된 작업 저널 기록
 → 기록 조회·충돌 확인 후 복원
```

### 핵심 코드

| 코드 | 책임 | 반드시 지킬 제약 |
|---|---|---|
| [folder_organizer/scanner.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/scanner.py#L1) | 파일 시스템 목록·메타데이터, 링크/내부 기록 제외 | 스캔은 읽기 전용, 취소는 불완전 결과로 표시 |
| [folder_organizer/matching.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/matching.py#L436) | 해시와 최종 바이트 비교로 완전 중복 판정 | **이름/해시 유사만으로 삭제 확정 금지** |
| [folder_organizer/comparison.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/comparison.py#L161) | 두 파일의 읽기 전용 실제 비교 | 유사도는 조사가 필요한 후보일 뿐 |
| [folder_organizer/multiroot.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/multiroot.py#L109) | 볼륨/폴더별 작업자 풀과 여러 루트 스캔 | 부분 실패·취소를 전체 완료로 보고하지 않음 |
| [folder_organizer/workspace_actions.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/workspace_actions.py#L309) | 루트별 계획 잠금·실행, 공통 사전검사 | 정렬된 락 획득·부분 결과 및 재분석 요구 |
| [folder_organizer/operations.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/operations.py#L533) | 서명된 작업 저널·복사/이동의 무결성 보호 | 계획·실행 시점 사이 파일 바뀜 검출 |
| [folder_organizer/deletion.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/deletion.py#L24) | 삭제 보관 경로 `delete_files` 선택 | 휴지통·영구 삭제를 추측하지 않음 |
| [folder_organizer/recovery.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/recovery.py#L436) | 원본을 덮어쓰지 않는 별도 복구 사본 생성 | 손상 파일의 원본 전체 복구를 약속하지 않음 |
| [rust-app/src/operations.rs](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/rust-app/src/operations.rs#L3) | Rust판 인증된 이벤트 체인·복원 저널 | 복구 데이터와 실물 보관 파일을 함께 유지 |
| [rust-app/src/updater.rs](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/rust-app/src/updater.rs#L1) | 별도 도우미를 이용한 Rust Windows 업데이트 | 임시 패키지 검증 후 기존 앱 보존·교체 |

Python판 [folder_organizer/matching.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/matching.py#L436)은 완전 중복을 **같은 크기 → SHA-256 → 실제 바이트 비교**로 확정합니다. 유사 판정은 텍스트/이미지/이름 기반의 점수로 후보를 만들지만 삭제 근거가 아닙니다. 스캔한 뒤 파일이 교체되면, 계획에서 생성된 증빙은 더 이상 유효하지 않을 수 있으므로 실행 단계가 스냅샷을 다시 확인합니다.

## 7.3 삭제·복원 — 가장 중요한 운영 위험

- **실제 삭제 방식:** OS 휴지통을 추측하지 않고 원본 소속 폴더 아래 **`delete_files`**로 이동합니다. 폴더가 남아 있는 한 디스크 공간도 사용합니다.
- **복원 의존 데이터:** `delete_files` 안의 파일 본문 + Python판 **`.folder-organizer`** 또는 Rust판 **`.folder-organizer-rust`** 작업 기록·인증 키·보관 데이터. **메타데이터만 가지고 원본 파일을 다시 생성할 수 없습니다.**
- **첫 파일 제외 삭제:** GUI 문구와 [folder_organizer/ui.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/ui.py#L1051)에 따르면, 현재 확인된 중복 결과의 첫 파일과 보호 대상을 제외하고 나머지를 **추가 전체 검사·확인창 없이 즉시** `delete_files`로 보관합니다. 원본이 실행 전 변경된 경우 이를 거부하는 검사는 유지됩니다. 여기서 **첫 파일은 가장 최신이나 최상 품질이라는 뜻이 아닙니다.**
- **복원 도중 충돌:** 원래 경로에 다른 파일이 생겼다면 조용히 덮어쓰지 않습니다. 충돌 상태를 보존하고 관리자 판단 후 별도 경로로 복구해야 합니다.

### 운영자 안전 작업 절차 (데이터 변경 전)

1. **대상 승인:** 삭제·이동·분류하려는 원본 폴더 목록과 현재 생성된 중복/유사 결과를 파일·경로·크기로 확인합니다. NAS·공유 폴더는 다른 작업자가 동시에 파일을 변경할 수 있음을 고려합니다.
2. **백업:** 대량 변경 전 원본 파일 또는 파일 스냅샷과 **숨김 작업 기록 폴더**를 다른 디스크에 보존합니다. 단순 TXT/CSV 결과 보고서는 복원 수단이 아닙니다.
3. **분류 확인:** 완전 중복과 유사 후보를 구분합니다. 높은 유사도 점수나 동일 이름만으로 삭제하지 않습니다.
4. **미리보기:** 복사·이동·보관 대상과 결과 경로를 확인하고 파일 잠금·용량·쓰기 권한을 점검합니다.
5. **실행:** 작업 로그와 처리 건수, 건너뜀·실패를 확인합니다. 취소·일부 실패를 전체 성공으로 처리하지 않습니다.
6. **복원 시험:** 시험용 2~3개 파일에 대해 삭제 보관 → 기록 확인 → 원래 위치 복원 → SHA-256 일치를 확인한 후 대량 정리에 사용합니다.
7. **정리 후:** `delete_files`, `.folder-organizer`, `.folder-organizer-rust`를 검증 없이 삭제하거나 일괄 정리하지 않습니다. 복원 보관 정책이 있으면 별도 승인 후 폐기합니다.

### 자동 업데이트

Python [folder_organizer/updater.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/updater.py#L277)과 Rust [rust-app/src/updater.rs](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/rust-app/src/updater.rs#L381)는 각각 전용 ZIP과 체크섬, 파일 크기·버전을 검증해 업데이트 계획을 준비합니다. 실행 중인 앱 교체는 종료 후 전용 업데이트 도우미가 처리합니다. **Python판 ZIP과 Rust판 ZIP을 혼동하지 말 것**이 중요합니다. CI [.github/workflows/dual-release.yml](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/.github/workflows/dual-release.yml)에는 Windows 빌드·ZIP·SHA-256 생성 과정이 있습니다.

### 개발자 검증

```sh
# Python GUI/코어 테스트 (Windows 또는 적절한 Qt 런타임 필요)
python -m pytest tests -q

# Rust판 (저장소 루트)
cargo +1.99.1 test --manifest-path rust-app/Cargo.toml --locked
cargo +1.99.1 clippy --manifest-path rust-app/Cargo.toml --locked --all-targets -- -D warnings
```

**필수 보강 시나리오:** 스캔 후 파일 내용 변경(동일 크기), 심볼릭 링크/정션 치환, NAS 잠금, 하드링크 동일성, 중도 취소·부분 실패, 두 루트 동시 접근, 첫 파일 제외 삭제, 복원 중 충돌, 손상 저널 및 인증 키 불일치, Python↔Rust 간 복원 기록의 명확한 분리. 테스트 명령의 실제 실행 결과는 이번 보고서에 포함되지 않습니다.

---

## 7.4 파일 탐색·중복 비교·복원 속도 최적화 심층 검토 (추가)

**성능 성격:** 파일 목록 열거 + 디스크 읽기/쓰기 + SHA-256 + 파일시스템 메타데이터 + Qt/egui 결과 테이블. **LLM 추론 없음.** 파일 유사도·이미지 점수는 규칙·콘텐츠 비교이며 언어 모델 실행 속도와는 무관합니다.

### 7.4.1 이미 구현된 고효율·안전 경계

- Python [matching.py L481-L558](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/matching.py#L481)는 **파일 크기 → 대용량 파일 표본 SHA-256 → 전체 SHA-256 → 바이트 직접 비교**로 후보를 축소합니다. 파일 이름이 비슷하다는 이유만으로 삭제 가능한 중복이라고 판정하지 않습니다.
- Rust도 [core.rs L904-L945](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/rust-app/src/core.rs#L904)에 대용량 파일의 **3개 구간 표본 검사**를 구현하고, 전체 SHA-256과 실제 바이트 검증을 뒤따르게 합니다. 이중 검증을 '중복 작업'으로 제거하면 안전 계약이 깨집니다.
- Python [multiroot.py L197-L226](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/multiroot.py#L197)와 Rust [multiroot.rs L318-L327](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/rust-app/src/multiroot.rs#L318)는 다중 루트를 관리합니다. **볼륨별 탐색 작업을 묶어** 같은 디스크에 대한 중첩 탐색 부담을 제한하며 Rust판은 1~4개 동시 탐색을 허용합니다.
- Python [workspace_actions.py L309-L348](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/workspace_actions.py#L309)는 참여 루트를 정렬해 작업 잠금을 확보하고, 사전 검사를 통과한 실행만 수행합니다. Rust판 역시 실행 계획과 원본 스냅샷 검증/저널 잠금을 갖습니다.
- Python [operations.py L597-L637](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/operations.py#L597)는 복사 중 SHA-256, `fsync`, 원본·결과 재검증을 합니다. 단순 복사를 빨리 하려고 검증·동기화를 제거하는 변경은 허용할 수 없습니다.
- 유사 파일 비교는 [matching.py L885-L918](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/matching.py#L885)에서 기본 최대 25,000쌍으로 제한하고 큰 목록에는 근사 후보 인덱스를 사용합니다. 근사 모드에서 일부 유사 파일을 놓칠 수 있다는 **탐색 완전성 트레이드오프**가 존재합니다.

### 7.4.2 추가 후보와 보존 조건

| ID/우선 | 코드 병목 후보 | 개선 가능성 / 위험 |
|---|---|---|
| **F-P1** | Python [matching.py L524-L537](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/matching.py#L524)의 전체 해시 대상 처리는 후보 배치 순서대로 실행됩니다. 대용량 파일이 많으면 순차 I/O의 지연이 지배할 수 있습니다. | **디스크 종류·볼륨별 1~N개 제한된 해시 작업자** 실험. SSD/NVMe와 HDD/NAS를 구분하고, 같은 물리 볼륨 과병렬 탐색 금지. 파일 스냅샷·해시·중단/불완전 결과 처리를 기존과 동일하게 유지. |
| **F-P1** | Rust [core.rs L804-L850](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/rust-app/src/core.rs#L804)의 전체 파일 해시·내용 읽기와 [L858-L901](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/rust-app/src/core.rs#L858)의 바이트 재검증에서 같은 파일을 여러 번 읽을 수 있습니다. | 비교 후보의 비율과 다시 읽는 바이트 수를 측정. 최근 읽은 내용의 **작은 한정 캐시** 또는 한 작업 내 스트리밍 I/O 공유를 검토하되, 파일 변경 감지와 최종 검증을 생략하지 않음. 큰 파일 전부를 RAM에 저장하는 것은 금지. |
| **F-P2** | Python [matching.py L899-L918](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/matching.py#L899)의 유사 후보 검색은 pair budget와 후보 인덱스 정책을 가집니다. | 인덱스 구성·후보 생성·실제 점수 계산을 따로 계측하고 텍스트/이미지의 **실제 유사 쌍 recall**을 데이터셋으로 검증. 높은 속도 때문에 `complete` 모드와 `indexed` 모드의 의미를 혼동시키지 말 것. |
| **F-P2** | Python [ui.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/ui.py#L1)는 약 2,900줄, Rust [gui.rs](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/rust-app/src/gui.rs#L1)는 약 4,500줄의 화면/이벤트 코드를 담고 있습니다. | 기능별 프레젠터와 모델을 분리해 UI 변경 범위를 줄이는 유지보수 최적화 후보. 이것이 **사용자 실행 속도 향상**이라는 직접 증거는 없으므로 먼저 결과 목록 렌더링 비용을 측정해야 함. |
| **F-P2** | 다중 루트 겹침 검사는 Python [multiroot.py L188-L193](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/multiroot.py#L188)에서 각 경로의 쌍을 비교합니다. 최대 64개 루트라 이 단계의 O(R²)는 작습니다. | 최적화 우선 대상이 **아님**. 먼저 파일 I/O·해시 비용 개선을 증명해야 합니다. |
| **F-P3** | Python [recovery_metadata.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/recovery_metadata.py#L219)·Rust [operations.rs](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/rust-app/src/operations.rs#L1)은 안전 복원을 위해 압축 메타데이터/인증 저널을 사용합니다. | journal 압축 레벨·batch flush 주기 최적화는 가능하지만 **전원 차단 후 복원성**에 영향. 실패 주입·강제 종료 시험 없이 I/O 동기화 축소 금지. |

### 7.4.3 성능 검증 시나리오·측정 단위

| 시나리오 | 입력 | 핵심 KPI |
|---|---|---|
| 파일 열거 | 작은 파일 1만/10만개, 깊은 트리, 제외 폴더 혼합 | files/s, p95 응답, 파일시스템 메타데이터 호출 수 |
| 대용량 중복 | 1~5GiB 파일 다수, 서로 다른 파일/완전 복제/하드링크 혼합 | `sample_bytes`, `full_hash_bytes`, `byte_comparison_bytes`, 처리 MB/s, peak RSS |
| 유사 후보 | 이미지·텍스트와 25,000쌍 한계 조건 | 후보 비교 수·시간, true-pair recall, 제한 표시 정확성 |
| 다중 루트 | 동일 SSD, 다른 SSD, HDD, NAS 조합 | 볼륨별 MB/s, disk queue, 네트워크 왕복, 취소 지연 |
| 복사·보관·복원 | 파일 충돌, 작업 중 원본 변경, 실행 중단, 강제 종료 | 체크섬, journal 검증, 복원 성공/중복 보관 여부 |

```sh
# Python판 (저장소 개발 환경)
python -m pytest -q
# Rust판
cargo test --locked --manifest-path rust-app/Cargo.toml
cargo clippy --locked --manifest-path rust-app/Cargo.toml --all-targets -- -D warnings
```

**반드시 유지:** SHA-256+바이트 동일성, 파일 수정 감지, 심볼릭 링크/재분석 지점 차단, 동작 전 계획 인증, 완료/취소/부분 완료 구분, `delete_files` 기반 복원. **결과물:** Python/Rust 각각의 실제 프로파일 그래프, 파일 수·바이트·저장매체별 속도표, 데이터 손실이 없는 fault-injection 로그. 단순히 I/O 횟수를 줄였다고 성공으로 판정하지 않습니다.

## 7.5 파일 정리기: 데이터 안전을 유지하는 최적화 설계 (추가)

**절대 조건:** 해시나 실제 바이트 비교를 생략해서 속도를 얻는 방식, `delete_files`/저널/복원 기록을 지우는 방식, 검사 중 바뀐 파일을 정상으로 처리하는 방식은 **개선안에서 제외**합니다. Python과 Rust 구현은 같은 사용자 안전성을 독립적으로 보증해야 합니다.

### IMP-F01 · Python 전체 해시의 볼륨별 제한 병렬화 (P1, 구현 전 의존성 수정 필수)

- **위치:** `folder_organizer/matching.py`의 크기 → 표본 해시 → 전체 해시 → 바이트 비교. [근거 L475-L537](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/matching.py#L475). `multiroot.py`의 볼륨 그룹은 이미 존재합니다. [근거 L197-L226](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/multiroot.py#L197).
- **정합성 핵심:** 현재 `_DUPLICATE_READS`라는 **thread-local** 컨텍스트가 `hash_snapshots`와 `metrics`를 전달합니다([matching.py L475-L478](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/matching.py#L475)). `ThreadPoolExecutor.map(_digest, ...)`만 추가하면 새 작업자 스레드에 같은 thread-local이 자동 전달되지 않습니다. 그러면 **재검증용 스냅샷 누락·통계 부정확·증거 생성 오류**가 날 수 있습니다.
- **안전한 구현 순서:** ① 우선 파일 1천/1만/10만개의 전체 해시 `MB/s`, `disk busy%`, 파일별 read time을 계측합니다. ② `_digest()`의 내부 알고리즘을 그대로 유지하되 스냅샷·바이트 수·오류를 `HashTaskResult(path, snapshot, digest, bytes_read, error)` 형태의 **명시적 반환 데이터**로 전달하는 실험용 helper를 만듭니다. ③ 메인 스레드가 결과를 **원래 후보 순서**로 합쳐 `hash_snapshots`/metrics/보고서를 확정합니다. ④ **같은 물리 HDD/NAS 볼륨은 우선 작업자 1개**, 서로 다른 SSD/NVMe 볼륨은 상한 2~4개부터 실험합니다. ⑤ 중단 신호는 파일 read 경계마다 검사하고, 중간 결과는 정상 결과 목록으로 게시하지 않습니다.
- **검증:** 스레드 1/2/4, 동일/다른 SSD 및 HDD/NAS, 하드링크·교차 루트·권한 거부·파일 변경·시도 중 취소. **발견된 중복 그룹, 각 그룹 proof, 누락 사유, 측정 bytes**가 기준 구현과 같아야 합니다. 실제 이득 없으면 기존 순차 해시 유지.

### IMP-F02 · Rust 작은 파일 재읽기 감소와 캐시 범위 제한 (P1)

- **위치:** `rust-app/src/core.rs`의 `fingerprint_inner()`와 `bytes_equal()`. [근거 L804-L901](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/rust-app/src/core.rs#L804).
- **구현:** ① `full_hash_bytes`, `byte_comparison_bytes`, 파일 reopen 수와 cache hit 가능성을 측정합니다. ② 같은 실행에서 확인된 **작은 파일(예: 1MiB 이하)의 안전한 읽기 버퍼**만 `FileSnapshot` 키와 함께 bounded LRU에 보관하는 프로토타입을 만듭니다. ③ 캐시를 이용하더라도 최종 삭제·이동 직전에는 원본을 다시 안전하게 열고 기존 경로 스냅샷/저널 검증을 그대로 적용합니다. ④ 큰 파일은 기존 스트리밍을 유지합니다.
- **주의:** 캐시가 과거 내용의 일치만 확인하고 파일 변경 여부를 검사하지 않으면 stale proof가 됩니다. `snapshot(path)`가 바뀌면 즉시 캐시 버림. 실제 바이트 직접 비교를 캐시된 해시 비교로 **대체하지 않습니다**. 작은 파일 캐시의 총량 상한과 실행 종료 시 해제를 테스트합니다.

### IMP-F03 · 유사 파일 후보 탐색의 recall/속도 선택 (P2)

- **위치:** `folder_organizer/matching.py`의 `find_similar()`, `search_mode`, `max_pairs`, `max_candidates_per_file`. [근거 L885-L918](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/matching.py#L885).
- **방법:** ① 이름·UTF-8 텍스트·이미지별 정답이 알려진 fixture를 구축합니다. ② `complete` 모드는 기준 정답을 만들고, 인덱스 모드는 후보 생성 시간·점수 계산 시간·인덱스 메모리/정답 recall을 측정합니다. ③ 결과 수가 큰 UI에서 `max_candidates_per_file` 등 **품질을 바꾸는 값**은 기존 기본값으로 유지하고, 새 튜닝값은 명시적인 사용자 옵션으로만 검토합니다. ④ 완전 중복인 파일은 이미 사전 SHA/바이트 검증으로 제외하므로 유사 후보 최적화와 안전한 삭제 기능을 분리합니다.
- **승인 기준:** 동일 모드·동일 옵션에서 기존 결과가 동일할 것. 후보 탐색 방식을 바꿔 일부 유사쌍을 놓치게 된다면 **완전 탐색으로 표시 금지**, 품질 수치와 제한을 보고서에 노출.

### IMP-F04 · UI 결과가 많을 때만 화면 렌더 비용 감소 (P2)

- **위치:** Python `folder_organizer/ui.py`, Rust `rust-app/src/gui.rs`의 목록 갱신. 현재 대형 GUI 파일을 작은 모듈로 나누는 것 자체는 **실행속도 향상의 증거가 아닙니다**.
- **방법:** ① 1천/1만/10만 행 결과에서 스캔 완료 시 화면 준비 시간, 스크롤 프레임 p95, 선택/검색 응답을 계측합니다. ② 무의미한 전체 리스트 재생성을 줄이고 **변경된 행만 갱신**하거나 viewport 기반 가상화·페이지 단위 표시를 검토합니다. ③ 선택된 경로·비교 근거·중단/부분 결과 배지·접근성 라벨을 고정합니다.
- **테스트:** 동일한 그룹 순서, 숨김/필터/선택 상태, 경로 이름이 같은 파일, 삭제 대상 체크박스와 proof가 항상 같은 파일을 참조하는지 확인. 이 상태가 흔들리면 렌더 최적화는 반려.

### IMP-F05 · 저널·복사 비용은 안전성 우선 (R / 원칙적으로 유지)

- **위치:** Python `folder_organizer/operations.py`의 `_copy_verified()` [L597-L637](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/folder_organizer/operations.py#L597), Rust `rust-app/src/operations.rs`의 exclusive rename [L489-L550](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/rust-app/src/operations.rs#L489).
- **권고:** 먼저 source read MB/s, target write MB/s, fsync/rename 지연, 저널 flush 빈도를 분리 계측합니다. 디스크가 느리다는 이유로 `fsync`, 원본·대상 SHA, `RENAME_NOREPLACE`/무덮어쓰기 동작을 빼는 것은 허용하지 않습니다. 수정이 필요하면 **작업 단위 묶음 또는 메타데이터 압축 최적화**처럼 손실 위험이 낮은 경로부터 적용하되, 충돌·권한 부족·프로세스 강제 종료·전원 차단 시나리오를 통과해야 합니다.

### 파일 정리기 테스트·원복 기준

```sh
python -m pytest -q
cargo test --locked --manifest-path rust-app/Cargo.toml
cargo clippy --locked --manifest-path rust-app/Cargo.toml --all-targets -- -D warnings
```

**측정 로그:** `files/s`, `full_hash_bytes`, `byte_comparison_bytes`, sample/reopen 횟수, 볼륨별 `MB/s`·busy%, 작업자 수, Peak RSS, 취소 후 중단 지연, 저널 검증·복원 결과 SHA. **즉시 원복:** 정상 그룹 누락, 하드링크 중복 반영, 변경 중인 파일의 정상 판정, `delete_files`/작업 기록 손상, 강제 종료 후 복원 불가, 다른 루트의 파일 변경.


## 7.6 추가 보완 과제 — 재현 가능한 성능 근거·이중 구현 일치·복구 증빙 (2026-11-11)

기존 IMP-F01~F05는 해시/I/O와 사용자 화면 효율 중심입니다. 추가로 **이미 존재하는 벤치마크를 활용하는 방법**, Python/Rust 간 계약 동등성, 보관·복구 신뢰성을 변경 승인 조건으로 연결해야 합니다.

### IMP-F06 · 기존 스케일링 벤치마크의 기준선·재현성 체계화 (P1)

- **확인 근거:** [scripts/benchmark_analysis.py L88-L161, L416-L457](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/scripts/benchmark_analysis.py#L88-L161)는 일회용 synthetic fixture와 중복/스케일링, read byte 계측을 이미 지원합니다. 새 측정기를 만들기 전에 재사용합니다.
- **실행 예시:** 개발 의존성 설치 후 python scripts/benchmark_analysis.py --suite scaling --counts 1000 2000 --repeats 5 실행. 동일 입력을 사용하는 작업 트리/기준 커밋 비교는 --compare-revision 값에 **검증된 기존 커밋 SHA**만 지정합니다. recovery-payload, gui-keys, duplicate-preview는 서로 다른 단계 측정이므로 결과를 합산해 속도 개선율로 표시하지 않습니다.
- **개선 절차:** 변경 전·후 source SHA, Python 버전, 디스크 유형/캐시 상태, 입력 건수, 반복 중앙값·요청 read bytes·Peak RSS를 결과에 함께 저장합니다. 읽은 바이트 수는 OS의 실제 물리 I/O와 다르며, 페이지 캐시가 따뜻한 결과로 일반 NAS/대형 HDD 성능을 단정하지 않습니다.
- **합격/원복:** 결과 그룹/누락 경고/파일 원본 SHA가 같으면서 반복 실행의 중앙값이 개선되는 경우에만 후보 승인. 단일 시도 수치만 유리한 변경은 보류합니다.

### IMP-F07 · Python/Rust CLI 출력 동등성과 릴리스 증거 연계 (P1 운영)

- **확인 근거:** [scripts/benchmark_editions_v070.py L1-L11, L329-L405](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/scripts/benchmark_editions_v070.py#L329-L405)은 독립 프로세스로 두 판의 공통 보고서 계약·실행 시간을 비교하고 원본 파일 변경이 없음을 확인합니다. [.github/workflows/dual-release.yml](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/.github/workflows/dual-release.yml)과 [promote-verified.yml](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/.github/workflows/promote-verified.yml)은 배포 단계가 분리됩니다.
- **개선 절차:** 실제 Windows 패키지 기준 python scripts/benchmark_editions_v070.py --require-windows --python-command '["dist/FolderOrganizer/FolderOrganizer.exe","--cli"]' --rust-command '["dist/FolderOrganizerRust/FolderOrganizerRustCLI.exe"]' --output editions-benchmark.json 실행을 검증표에 포함합니다. Windows PowerShell에서 따옴표 전달이 달라지면 스크립트의 도움말에 맞춰 수정합니다. 산출 JSON에는 각판 실행 파일 SHA/커밋/공통 스키마/그룹/시각 정밀도 비교 결과를 묶습니다.
- **검증/원복:** 실측 속도가 우수해도 결과 그룹, 제외 패턴, 생성·수정일 정밀도, action_authority=false 등의 의미가 달라지면 두 판 동등성 미충족으로 반려합니다. 테스트가 사용한 임시 소스 트리를 고객 데이터 경로로 바꾸지 않습니다.

### IMP-F08 · 파일 삭제·압축 보관·복원을 릴리스 차단형 안전성 증거로 관리 (P1 데이터 보호)

- **확인 근거:** [tests/test_recovery_compaction.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/tests/test_recovery_compaction.py)에는 packed payload 복원, 원본과 충돌, 객체 손상, 저널 실패 및 구버전 기록 호환 시험이 있습니다. [tests/test_multiroot_extended.py](https://github.com/P-JSL/Folder_Organizer/blob/f74aff3e3e3f56a95fde15cb24d35ba78fee5b59/tests/test_multiroot_extended.py)는 부분 실패와 다중 루트의 읽기 전용 계약을 확인합니다.
- **개선 절차:** 최적화 PR마다 python -m pytest -q tests/test_recovery_compaction.py tests/test_multiroot_extended.py 를 독립 실행하고, Windows 실제 배포본에서 ① 안전한 삭제 ② 대상 파일 재생성 충돌 ③ 복원 ④ 강제 중단 후 저널 재시작 ⑤ Python/Rust 교차 버전 확인을 수행합니다. 자동화된 테스트의 수행 범위와 수동 리허설 범위를 구분합니다.
- **합격/원복:** 원본이 변경되었는데도 이전 분석 proof로 삭제되는 경로가 0건이어야 하며, 보관 대상의 SHA 불일치나 partial undo 은폐가 있으면 즉시 반려합니다. 속도 개선을 위해 파일 identity·동기화·저널 내구성 검사를 생략하지 않습니다.
