# Folder_Organizer — Codex 실행용 개선 작업서

> 기준 코드: [`30e20507e07d`](https://github.com/P-JSL/Folder_Organizer/tree/30e20507e07d9f66b18fea86860d16885b1e29c4) · 보고서 정리: **2026-11-12(KST)**  
> 상태: **개선 설계/미구현**. 이 보고서 갱신은 실제 애플리케이션 수정이나 테스트 통과를 뜻하지 않습니다.  
> 범위: Python/PySide6·Rust 이중 구현 파일 정리 프로그램

## 1. Codex 작업 운영 규칙

1. 우선순위 표에서 ID 한 개를 선택합니다. **항상 현재 브랜치의 최신 소스와 테스트를 먼저 열어** 실행 방식·기존 방어장치를 확인합니다. 아래 기준 커밋은 분석 기준이지 수정 대상 버전을 고정하는 지시가 아닙니다.
2. 현재 공개 API·파일 형식·관리자 동작·권한 모델·데이터 정합성을 유지합니다. 새로운 메서드·라이브러리·옵션이 필요하면 실제 의존성을 확인한 후 작성합니다.
3. **이미 구현**, **제안**, **확인된 불일치**를 엄격히 구분합니다. 확인된 불일치도 코드 수정 후 테스트하지 않았다면 계속 미해결입니다.
4. 변경 전 실패를 재현하는 테스트 또는 프로파일을 먼저 만들고, 최소 수정→회귀 검증→관련 소스만 커밋합니다. 네트워크/인증/데이터 삭제를 수반하는 실험은 승인된 테스트 환경에서만 수행합니다.
5. Codex 작업 완료 보고는 변경 파일, 기존/수정 동작, 수행한 명령과 결과, 미실행 테스트, 잔존 위험, 커밋 SHA를 포함해야 합니다. 요청하지 않은 릴리스/배포는 하지 않습니다.

## 2. 실제 구조 및 절대 유지할 계약

Python판과 Rust판은 파일 탐색·중복 판정·정리 계획·삭제·복구·업데이트 기능을 구현합니다. 동일 파일이라는 판단과 실제 파일 변경 사이에는 강한 검증 경계가 있으며, 원본 데이터의 안전성이 속도보다 우선합니다.

- 완전 중복은 크기/해시 검증과 실제 바이트 비교가 필요하며, 유사 파일 점수만으로 삭제하지 않음.
- 원본을 변경할 때는 스캔 시점의 정보가 아닌 실행 시점 파일 identity/서명된 계획/저널을 다시 확인.
- 삭제·복구 저널은 인증·내구성·충돌 방지가 필요하며 중단 후 부분 복원 결과를 성공으로 숨기지 않음.
- Python·Rust를 동일 구현이라고 가정하지 않고 공통 결과·삭제·복구 계약을 별도로 비교.
- 성능 측정 시 HDD/SSD/NAS/OS 파일시스템 캐시·원본 변화·다중 루트 교차를 구분하고 디스크 요청 바이트와 물리 읽기를 구분.

## 3. 코드·다른 저장소 교차 확인 결과

- 기존 구현: Python `matching.py`의 같은 크기·SHA-256·바이트 비교, 스냅샷 증빙, 운영 저널과 복구 코드.
- 기존 UI: 중복 결과에서 첫 파일을 제외한 바로 삭제 동작은 별도의 추가 확인창 없이 진행할 수 있습니다. 그러나 실행 안전 검증은 별도로 존재합니다.
- 미측정: Windows/NAS 실제 정리 속도와 Python/Rust 패키지 간 전체 기능 동등성. 일부 과거 Linux warm fixture 속도 결과는 미래 개선치가 아닙니다.

## 4. 우선순위 및 작업 목록

| ID | 우선 | 작업명 | 확인 상태 |
|---|---|---|---|
| F-01 | P0 | 정확 중복 판정·변경 직전 재검증 보장 | 기존 보호의 회귀 고정 |
| F-02 | P0 | 삭제 UI의 보호·복구 가능 범위를 정확하게 설명 | 현재 UX와 실제 동작 일치 |
| F-03 | P0 | 인증 저널·압축 보관·중단 복원 회귀 강화 | 현재 안전 계약 필수 |
| F-04 | P1 | 다중 루트 변경 실패의 격리 | 경합·락 순서 검증 |
| F-05 | P1 | Python 해시의 볼륨별 제한 병렬화 검토 | 측정 후에만 수정 |
| F-06 | P2 | Rust의 재열기/재해시 비용 검토 | 기존 증명 구조 우선 |
| F-07 | P2 | 유사 파일 후보 탐색의 결과 완전성 판단 | 유사/완전 중복 분리 |
| F-08 | P2 | 다량 결과 UI 렌더 최적화 | Qt/egui 구현별 독립 측정 |
| F-09 | P1 | Python/Rust 실행 결과 및 릴리스 동등성 | 서로 다른 바이너리 실제 비교 |
| F-10 | P1 | 업데이트 패키지 검증과 실패 복구 | 기존 업데이터 보호 유지 |
| F-11 | P2 | 유니코드·장경로 및 경로 경합 검증 | Windows 파일시스템 사례 필수 |

**P0**는 우선 정합성을 바로잡을 영역, **P1**은 회귀·운영 품질, **P2**는 실측 후 채택할 최적화/사용성입니다. 모든 P0가 실제 보안 사고를 의미하지는 않습니다.

## 5. Codex 상세 실행 지시

### F-01 · 정확 중복 판정·변경 직전 재검증 보장 (P0)

- **판정:** 기존 보호의 회귀 고정
- **확인·수정 대상:** [`folder_organizer/matching.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/folder_organizer/matching.py), [`folder_organizer/workspace_actions.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/folder_organizer/workspace_actions.py), [`tests/test_matching.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/tests/test_matching.py)
- **작업 내용:** 파일 스캔→원본 파일 변경→계획 실행, 같은 길이/다른 내용, hardlink/symlink, 경로 치환 시나리오를 테스트합니다. 이미 구현된 digest·스냅샷 검증을 약화하지 않으며 추가 검사의 필요성은 실패 재현으로 판단합니다.
- **검증:** `python -m pytest -q tests/test_matching.py tests/test_operations.py`
- **완료 기준:** 서로 다른 바이트가 같은 파일로 삭제 처리되지 않고 변경된 원본을 거부
- **중단·원복 조건:** 유사 파일/이름 점수만으로 파일 삭제 금지
- **이전 보고서 연결:** Q-F01, Q-F02

### F-02 · 삭제 UI의 보호·복구 가능 범위를 정확하게 설명 (P0)

- **판정:** 현재 UX와 실제 동작 일치
- **확인·수정 대상:** [`folder_organizer/ui.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/folder_organizer/ui.py), [`folder_organizer/deletion.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/folder_organizer/deletion.py)
- **작업 내용:** 현재 '첫 파일 유지 후 삭제' 흐름이 별도 확인창 없이 실행되는 경로를 점검하고 문구·보호 대상·복구 위치를 명시합니다. 새 확인창 추가 여부는 사용자 동작 변경이므로 별도 제품 결정을 거칩니다.
- **검증:** `python -m pytest -q tests/test_deletion_destination.py tests/test_safety_review.py`
- **완료 기준:** 버튼이 실제 수행하는 변경과 안내 문구 일치, 보호된 파일을 삭제 대상으로 오인하지 않음
- **중단·원복 조건:** 문서만 보고 현재 이미 확인창이 있다고 주장하지 않음
- **이전 보고서 연결:** Q-F06

### F-03 · 인증 저널·압축 보관·중단 복원 회귀 강화 (P0)

- **판정:** 현재 안전 계약 필수
- **확인·수정 대상:** [`folder_organizer/operations.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/folder_organizer/operations.py), [`folder_organizer/recovery.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/folder_organizer/recovery.py), [`tests/test_recovery_compaction.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/tests/test_recovery_compaction.py), [`rust-app/src/operations.rs`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/rust-app/src/operations.rs)
- **작업 내용:** 중단 시점별 durable intent→실제 move→객체 packing→undo 재시작, corrupted payload, 사용자 동일 파일명 충돌을 Python/Rust에서 재현합니다. 복구 실패가 partial_undo로 명확하게 남도록 유지합니다.
- **검증:** `python -m pytest -q tests/test_recovery_compaction.py tests/test_recovery.py`
- **완료 기준:** 원본 덮어쓰기·조용한 손실 0건, 저널 변조 거부, 재시작 후 정확한 상태 확인
- **중단·원복 조건:** flush·동기화·인증 저널을 단순 성능 목적으로 생략하지 않음
- **이전 보고서 연결:** IMP-F05, IMP-F08, Q-F04, Q-F09

### F-04 · 다중 루트 변경 실패의 격리 (P1)

- **판정:** 경합·락 순서 검증
- **확인·수정 대상:** [`folder_organizer/workspace_actions.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/folder_organizer/workspace_actions.py), [`folder_organizer/multiroot.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/folder_organizer/multiroot.py), [`tests/test_multiroot_extended.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/tests/test_multiroot_extended.py)
- **작업 내용:** 루트 A에서 변경 가능/루트 B에서 권한 거부, 공유 보호 파일, 루트 순서 반대, 스캔 중 취소를 테스트합니다. 기존 정렬된 루트 락·global preflight 순서를 유지합니다.
- **검증:** `python -m pytest -q tests/test_multiroot_extended.py`
- **완료 기준:** 어느 루트가 실패했는지 식별되며 관련 없는 루트에 비의도 변경이 발생하지 않음
- **중단·원복 조건:** 전역 락 제거로 서로 다른 루트가 공유 원본을 동시에 수정하지 않도록
- **이전 보고서 연결:** Q-F05

### F-05 · Python 해시의 볼륨별 제한 병렬화 검토 (P1)

- **판정:** 측정 후에만 수정
- **확인·수정 대상:** [`folder_organizer/matching.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/folder_organizer/matching.py), [`scripts/benchmark_analysis.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/scripts/benchmark_analysis.py)
- **작업 내용:** 기존 thread-local `_DUPLICATE_READS` 때문에 단순 ThreadPoolExecutor 도입은 메트릭/증빙을 끊을 수 있습니다. 파일별 (snapshot,digest,bytes,error)을 반환하고 메인 스레드가 원래 순서로 병합하는 실험을 만듭니다. 동일 HDD 1-worker, 분리 SSD 2/4-worker를 따로 비교합니다.
- **검증:** `python scripts/benchmark_analysis.py --suite scaling --counts 1000 2000 --repeats 5`
- **완료 기준:** 동일 중복 그룹·메트릭·정렬 순서, 디스크 조건별 실제 wall-time 이득과 RSS 상한
- **중단·원복 조건:** I/O 병목에서 worker 증설만으로 개선이라 주장하지 않음
- **이전 보고서 연결:** IMP-F01, IMP-F06

### F-06 · Rust의 재열기/재해시 비용 검토 (P2)

- **판정:** 기존 증명 구조 우선
- **확인·수정 대상:** [`rust-app/src/core.rs`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/rust-app/src/core.rs)
- **작업 내용:** 작은 파일의 metadata/read/open 호출과 최종 바이트 비교 횟수를 측정합니다. 동일 파일 스냅샷으로 증명할 수 있는 경우에 한해서만 재사용 가능한 정보를 제한적 캐시로 관리합니다.
- **검증:** `cargo test --locked --manifest-path rust-app/Cargo.toml`
- **완료 기준:** 실측 중복 I/O 감소 및 최신 파일 identity 검증 유지
- **중단·원복 조건:** 이전 스캔의 해시를 변화 확인 없이 재사용 금지
- **이전 보고서 연결:** IMP-F02

### F-07 · 유사 파일 후보 탐색의 결과 완전성 판단 (P2)

- **판정:** 유사/완전 중복 분리
- **확인·수정 대상:** [`folder_organizer/matching.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/folder_organizer/matching.py), [`tests/test_matching_extended.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/tests/test_matching_extended.py)
- **작업 내용:** 현재 후보 인덱스의 recall 기준을 고정 fixture로 먼저 산출합니다. Bloom/근사 필터는 부재 판정용 보조 수단인지 구분하고, 사용자가 동일 결과를 기대하는 경로에서는 후보 누락을 허용하지 않습니다.
- **검증:** `python -m pytest -q tests/test_matching_extended.py tests/test_matching_scaling.py`
- **완료 기준:** 정확 중복 판정 불변, 유사 후보 누락·오탐 지표가 문서화됨
- **중단·원복 조건:** 근사 알고리즘을 정밀 판정과 혼용하지 않음
- **이전 보고서 연결:** IMP-F03

### F-08 · 다량 결과 UI 렌더 최적화 (P2)

- **판정:** Qt/egui 구현별 독립 측정
- **확인·수정 대상:** [`folder_organizer/ui.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/folder_organizer/ui.py), [`rust-app/src/gui.rs`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/rust-app/src/gui.rs)
- **작업 내용:** 수만 건의 결과에서 visible row 중심 렌더·정렬 키 캐시를 검사하고 이미 제공된 키 캐시를 중복 생성하지 않습니다. 선택 상태·삭제 후보 표시·검색 결과를 검증합니다.
- **검증:** `python -m pytest -q tests/test_ui.py tests/test_consolidated_ui_v1.py`
- **완료 기준:** UI 응답 시간이 단축되고 기존 선택·정렬·결과 수가 유지
- **중단·원복 조건:** 대량 데이터에서만 유리한 경우 작은 결과 경로 악화 금지
- **이전 보고서 연결:** IMP-F04

### F-09 · Python/Rust 실행 결과 및 릴리스 동등성 (P1)

- **판정:** 서로 다른 바이너리 실제 비교
- **확인·수정 대상:** [`scripts/benchmark_editions_v070.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/scripts/benchmark_editions_v070.py), [`.github/workflows/ci.yml`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/.github/workflows/ci.yml), [`rust-app/src/bin/cli.rs`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/rust-app/src/bin/cli.rs)
- **작업 내용:** 두 판의 파일·중복 그룹·제외 패턴·일시각 정밀도·undo 결과·JSON 응답 스키마를 같은 임시 fixture에서 비교합니다. Linux 가상 CLI와 Windows 정식 EXE 검증을 구분합니다.
- **검증:** `python -m pytest -q`
- **완료 기준:** 계약 불일치가 CI에서 확인되며 실제 배포 EXE 비교 여부가 별도로 표기됨
- **중단·원복 조건:** 한 판 테스트 성공을 양쪽 기능 통과로 간주하지 않음
- **이전 보고서 연결:** IMP-F07, Q-F07

### F-10 · 업데이트 패키지 검증과 실패 복구 (P1)

- **판정:** 기존 업데이터 보호 유지
- **확인·수정 대상:** [`folder_organizer/updater.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/folder_organizer/updater.py), [`rust-app/src/updater.rs`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/rust-app/src/updater.rs), [`tests/test_updater.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/tests/test_updater.py)
- **작업 내용:** 변조 ZIP, 체크섬 부재, 중단 다운로드, 구버전에서 최신 버전 갱신, 복구 가능한 버전 백업을 시험합니다. SHA는 전송 무결성이고 배포 주체 인증은 별도의 신뢰 경계로 분리합니다.
- **검증:** `python -m pytest -q tests/test_updater.py`
- **완료 기준:** 검증되지 않은 바이너리로 기존 실행 파일을 교체하지 않음
- **중단·원복 조건:** 체크섬만으로 신뢰할 수 없는 게시자를 인증했다고 주장 금지
- **이전 보고서 연결:** Q-F08

### F-11 · 유니코드·장경로 및 경로 경합 검증 (P2)

- **판정:** Windows 파일시스템 사례 필수
- **확인·수정 대상:** [`folder_organizer/scanner.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/folder_organizer/scanner.py), [`folder_organizer/operations.py`](https://github.com/P-JSL/Folder_Organizer/blob/30e20507e07d9f66b18fea86860d16885b1e29c4/folder_organizer/operations.py)
- **작업 내용:** Windows long path, Unicode 정규화, 동일 이름/다른 파일, symbolic/hard link, 파일명 대소문자 충돌을 별도 fixture로 테스트합니다. 변경 중 원본 identity/소유 경로를 다시 확인합니다.
- **검증:** `python -m pytest -q tests/test_scanner.py tests/test_operations.py`
- **완료 기준:** 허용되지 않은 루트 외 파일 변경 0건, 파일명 정규화로 다른 파일이 섞이지 않음
- **중단·원복 조건:** 경로 문자열만 동일하면 파일 동일이라고 단정하지 않음
- **이전 보고서 연결:** Q-F03, Q-F10

## 6. 검증 명령(실행 순서)

```bash
python -m pip install -e ".[dev]"
python -m pytest -q tests/test_matching.py tests/test_operations.py tests/test_recovery_compaction.py tests/test_multiroot_extended.py
python -m pytest -q
cargo test --locked --manifest-path rust-app/Cargo.toml
python scripts/benchmark_analysis.py --suite scaling --counts 1000 2000 --repeats 5
```

검증은 코드·테스트가 준비된 실행 환경에서 수행합니다. **파일을 변경하는 스크립트·실제 다운로드·외부 API 검증은 테스트 환경과 권한을 확인한 후에만 실행**합니다. Linux/Windows와 Docker/CI 등 환경 차이가 있는 테스트는 통과 여부를 분리해 기록합니다.

## 7. 기존 보고서의 불일치·중복 정리

- 이전 **IMP-*** 성능 과제와 **Q-*** 보안·운영 통제는 위 ID별로 병합했습니다. 기존 구현을 새로 만들라는 중복 지시는 제거했고, 필요한 회귀 보강만 남겼습니다.
- 100/1,000회 검토 이력은 실제 프로그램 반복 실행이 아니라 합성 부하·수학·규정 모델 검산입니다. 상세 시나리오와 학술 참고문헌은 과거 별도 증거 ZIP으로 보존하고, 이 문서에는 바로 코드 작업에 도움이 되는 근거와 검증 조건만 남겼습니다.
- 위 문서에서 '확인됨'은 **소스에서 동작 또는 설정을 확인**한 것이며, 전체 기능 테스트와 취약점 재현을 완료했다는 뜻이 아닙니다. 성능 속도 개선 배수는 현행 코드에 대한 추가 실측 없이는 예측하지 않습니다.
- 이력 항목을 의도적으로 누락하지 않도록 작업 지시 아래에 **기존 IMP 번호 연결**을 보존했습니다. 실험 과제는 기준선·결과 무결성·원복 경로를 먼저 확보합니다.
