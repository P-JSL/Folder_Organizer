# 유사 제품 분석과 설계 반영

조사일: 2026-10-27. 대상: Windows 개인 폴더의 파일 확인·정리·복원.

## 조사 방법과 확인 범위

Microsoft의 공개 `winget-pkgs` 저장소에 등록된 실제 배포 제품의 설명, 게시자, 제품 링크와 버전 정보를 읽고 비교했다. 프로그램을 구매하거나 설치해 성능을 측정한 비교는 아니다. 공급업체 상세 판매 페이지 접근은 환경의 네트워크 정책으로 제한되었으므로 현재 가격·구독 조건·결제 가능 여부·모든 유료 기능은 확인하지 않았다. 특히 WinGet의 `License: Freeware`는 해당 배포 패키지의 메타데이터이므로 제품의 모든 에디션이 무료라는 뜻으로 해석하지 않았다.

아래에는 상용 제품군에서 얻은 설계 참고와 무료 도구에서 얻은 기술 참고를 구분했다. 확인하지 않은 기능이나 경쟁사의 성능 우위를 주장하지 않는다.

## 상용 제품군의 공개 배포 설명에서 확인한 패턴

| 제품 / 확인 버전 | 읽은 자료가 명시하는 기능 | Folder Organizer에 반영한 설계 | 범위 차이 |
| --- | --- | --- | --- |
| Duplicate Cleaner Pro 5 / 5.28.0.0 | 다양한 파일 형식의 중복 검색, 삭제·이동할 사본 선택 보조, 휴지통 또는 다른 위치로 이동 | 정확한 중복을 찾은 뒤 사본을 선택하고, 원본·대상 경로를 미리 본 다음 격리하거나 이동 | 정확한 중복과 유사 후보를 구분한다. 파일 이름이 비슷하다는 이유만으로 삭제하지 않는다. 앱의 격리함은 Windows 휴지통과 별개다. |
| TreeSize / 9.9.1 | 저장소 분석과 파일 직접 관리·정리 | 파일별 크기, 날짜, 종류와 전체 용량 표시, 검색·정렬·CSV 내보내기 | 선택 폴더의 논리적 파일 크기를 보여준다. 디스크 할당 크기, NTFS 압축·중복 제거 효과, 전체 볼륨 사용량과 동일하지 않다. |
| Beyond Compare 5 / 5.2.6.32774 | 폴더·텍스트·이미지·표 비교, 차이 검토와 병합. 매니페스트에 구매 URL 제공 | 파일 이름·텍스트·이미지의 유사 후보와 판별 근거를 보여주고 사람이 결정 | 전문적인 양방향 동기화, 표 비교, 3-way 병합은 구현 범위에 포함하지 않는다. |
| Disk Drill / 6.3.1398.0 | 삭제·유실·손상 데이터와 다양한 저장장치의 데이터 복구 | 정리 작업의 되돌리기와 손상 파일의 별도 결과물 복구를 구분하고 복구 범위를 명시 | 원시 디스크·삭제 섹터·RAID·휴대전화 복구를 제공하지 않는다. 임의의 파일을 완전히 복원한다고 보장하지 않는다. |

Duplicate Cleaner Pro의 매니페스트는 제품명을 `Pro`로 기록하면서 `License: Freeware`로 표기한다. 이 자료만으로 실제 판매 라이선스를 확정할 수 없으므로 구매 또는 재배포 판단의 근거로 사용하면 안 된다. TreeSize도 무료판용 매니페스트와 일반 제품용 매니페스트가 별도로 존재한다. 본 비교에서는 일반 제품의 파일 분석·관리 설명만 채택했다.

## 추가한 사용성·안전 기능

- **작업 미리보기와 명시적 확인:** 변경할 파일과 대상 경로를 먼저 검토한다. 유사 후보에는 자동 삭제 정책을 적용하지 않는다.
- **별도의 ‘선택 항목을 중복으로 간주’ 기능:** 사용자의 가정을 정확한 중복 판정과 구별하고, 검증되지 않았다는 안내 뒤 격리 계획을 검토한다.
- **격리함·작업 이력·되돌리기:** 영구 삭제 대신 복구 가능한 이동을 제공한다. 원래 경로에 파일이 생겼거나 결과 파일이 수정되었다면 덮어쓰지 않고 충돌을 알린다.
- **이름 충돌 방지와 복사 검증:** 기존 파일을 덮어쓰지 않는다. 이동도 복사 결과를 확인한 뒤 원본을 제거하도록 설계한다.
- **범위가 보이는 유사 검색:** 이름, 텍스트, 이미지의 유사도는 검토를 돕는 후보 신호다. 의미가 같거나 불필요한 파일이라는 보증이 아니다.
- **읽기 오류와 취소 표시:** 접근 권한·심볼릭 링크·변경된 파일·취소를 숨기지 않는다. 스캔 중 바뀐 파일은 작업 직전에 다시 확인한다.
- **날짜의 정확한 의미:** 수정일과 생성일을 구분한다. 운영체제가 실제 생성일을 제공하지 않으면 ‘확인 불가’로 표시한다.
- **로컬 처리:** 개인 폴더의 내용이나 파일을 분석 서버로 업로드하는 기능은 두지 않는다.

## 복구 기능의 명확한 경계

정리 작업의 **복원**은 이 앱이 기록한 이동·복사·격리 작업을 되돌리는 기능이다. 손상 파일의 **복구**는 ZIP/Office ZIP, 이미지, PDF에서 읽을 수 있는 내용을 새 결과 파일로 저장하는 제한적인 기능이다. 원본은 유지하며 부분 복구를 완전 복구로 표시하지 않는다.

Recuva의 공개 설명은 Windows 컴퓨터·휴지통·메모리 카드 등에서 삭제된 파일을 되살리는 기능을 명시한다. 이는 폴더 수준의 파일 읽기와 다른 기술 범위다. Folder Organizer는 Recuva나 Disk Drill의 디스크 복구 기능을 대체하지 않는다. 손상된 실행 파일을 재구성하거나 모든 영상·문서 형식의 손상을 고치는 기능도 제공하지 않는다.

## 무료 도구의 기술 참고

Czkawka/Krokiet의 공식 README에는 해시 기반 중복, 유사 이미지, 손상 파일 탐지, 로컬 처리와 GUI/CLI가 명시되어 있다. 이를 참고해 정확한 중복 판정과 유사 후보 판정을 분리하고, UI와 파일 처리 코어를 분리했다. 해당 도구의 코드나 그래픽 자산을 복사하지 않았다. 문서의 기능 목록은 공급자가 주장하는 기능이며 이번 작업에서 경쟁 도구의 정확도·속도를 실측하지 않았다.

## 실제로 읽은 출처

1. [Duplicate Cleaner Pro 5, WinGet 5.28.0.0](https://github.com/microsoft/winget-pkgs/blob/master/manifests/d/DigitalVolcanoSoftware/DuplicateCleaner/5/5.28.0.0/DigitalVolcanoSoftware.DuplicateCleaner.5.locale.en-US.yaml) — 중복 스캔, 사본 선택, 휴지통·이동 설명.
2. [TreeSize, WinGet 9.9.1](https://github.com/microsoft/winget-pkgs/blob/master/manifests/j/JAMSoftware/TreeSize/9.9.1/JAMSoftware.TreeSize.locale.en-US.yaml) — 저장소 분석과 파일 관리·정리 설명.
3. [TreeSize Free, WinGet 4.7.3](https://github.com/microsoft/winget-pkgs/blob/master/manifests/j/JAMSoftware/TreeSize/Free/4.7.3/JAMSoftware.TreeSize.Free.locale.en-US.yaml) — 일반 제품과 무료판이 별도로 등록된 사실.
4. [Beyond Compare 5, WinGet 5.2.6.32774](https://github.com/microsoft/winget-pkgs/blob/master/manifests/s/ScooterSoftware/BeyondCompare/5/5.2.6.32774/ScooterSoftware.BeyondCompare.5.locale.en-US.yaml) — 비교 기능과 [공급업체 구매 링크](https://www.scootersoftware.com/shop).
5. [Disk Drill, WinGet 6.3.1398.0](https://github.com/microsoft/winget-pkgs/blob/master/manifests/c/CleverFiles/DiskDrill/6.3.1398.0/CleverFiles.DiskDrill.locale.en-US.yaml) — 삭제·유실·손상 데이터와 저장장치 복구 설명.
6. [Recuva, WinGet 1.55.0.133](https://github.com/microsoft/winget-pkgs/blob/master/manifests/p/Piriform/Recuva/1.55.0.133/Piriform.Recuva.locale.en-US.yaml) — 삭제 파일 복구 범위 설명.
7. [Czkawka/Krokiet 공식 README](https://github.com/qarmin/czkawka/blob/master/README.md) — 해시 중복, 유사 이미지, 손상 파일 탐지, 로컬 처리와 GUI/CLI 설명.

제품 링크는 위 매니페스트에서 확인할 수 있다. 공급업체 판매 페이지를 직접 읽지 못한 제한 때문에 현재 가격표나 유료판별 상세 기능 표는 제공하지 않는다.
