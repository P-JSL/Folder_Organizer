# Python·Rust 공통 보고서 v1

`--json-common 새파일.json` 또는 `--csv-common 새파일.csv`를 사용합니다. 기존 `--json`·`--csv` 형식과 기본 동작은 유지합니다. 출력 파일은 덮어쓰지 않습니다.

```text
FolderOrganizer.exe --cli <폴더> --duplicates --json-common <새파일.json>
FolderOrganizerRustCLI.exe <폴더> --duplicates --json-common <새파일.json>
```

JSON의 `schema`는 `folder-organizer.common.v1`입니다. `action_authority`는 항상 `false`이며, 보고서에는 실행 가능한 작업 계획이나 프로세스 내부 HMAC 증명을 포함하지 않습니다.

| 필드 | 의미 |
| --- | --- |
| `scope` | 하위 폴더·숨김 파일·제외 패턴·최소/최대 크기 |
| `inventory.files` | 상대 경로, 바이트, 수정/생성 시각, 종류, 점을 포함한 확장자 |
| `inventory.complete` | 취소·목록 경고 없이 완료했는지 |
| `duplicates.requested` | 사용자가 `--duplicates`를 요청했는지. 유사도 내부의 중복 제외 단계와 구분 |
| `duplicates.groups` | 전체 SHA-256과 실제 바이트가 검증된 그룹의 정렬된 상대 경로·크기·해시 |
| `duplicates.complete` | 해당 요청 범위의 중복 검사를 완료했는지. 미요청은 `null` |
| `duplicates.warnings` | 파일 경로·단계·실패 이유. 상세 개수 한도가 있으므로 총 건수·생략 여부도 확인 |
| `duplicates.metrics` | 실제 표본/해시/바이트 비교 읽기량, 성공 읽기 파일 수, 하드링크 별칭 및 건너뜀 수 |
| `similarity` | 요청 여부, 방법·임계값·비교 한도·직접 비교한 후보·부분/근사 범위·경고 |

시간은 정밀도를 잃지 않도록 부호 있는 십진 나노초 **문자열**로 기록합니다. 제공되지 않는 생성일은 `null`입니다. float 초에서 가짜 나노초 정밀도를 만들지 않습니다. 어떤 플랫폼에서 생성일을 한 엔진만 제공하면 가용성 차이로 기록하고 알려진 두 값만 대조합니다.

미요청 항목의 `groups`·`pairs`·`warnings`는 빈 배열, `metrics`·`thresholds`·`candidate_index`는 빈 객체입니다. 엔진별 보조 계측은 차이가 있을 수 있으나 같은 필드에 다른 컨테이너 종류를 쓰지 않습니다. 부분 결과는 완료된 전체 검사나 폴더 전체의 중복 없음 판정이 아닙니다. CLI의 파일 읽기 실패·취소 등 부분 결과는 2를 반환할 수 있으며 보고서에 이유를 보존합니다. 유사도 후보 한도에 따른 근사 범위만으로 실행 실패를 뜻하지는 않습니다.

유사도의 점수·후보 순서·문자열 정규화 알고리즘까지 두 엔진이 같다는 계약은 아닙니다. `byte_identical`은 확인되지 않았으면 `null`이며, 유사 점수로 실제 바이트 불일치를 만들어내지 않습니다. 그룹 경로의 공통 정렬은 보고서 비교를 위한 것으로 자동 보존할 원본 추천 순서가 아닙니다.

공통 CSV 컬럼은 두 엔진 모두 다음 순서입니다. 시간 값은 같은 십진 정수 문자열이며 모르는 생성일은 빈 칸입니다. 파일 이름 등 스프레드시트 수식으로 오인될 수 있는 값에는 표시용 작은따옴표를 붙입니다. JSON은 원래 문자열을 유지합니다.

```text
relative_path,size,modified_ns,created_ns,kind,extension
```

CSV 원문은 정밀한 십진 정수 문자열입니다. 스프레드시트가 자동으로 숫자로 변환하면 긴 정수의 정밀도가 줄 수 있으므로 시간 열을 텍스트로 가져오세요. JSON의 나노초 문자열은 이러한 자동 숫자 변환과 구분됩니다.
