# Folder Organizer v0.9.3 · Python / Rust

Python판의 Windows 파일 복사·이동에서 NTFS 추가 데이터 스트림(ADS)과
EFS 암호화 특성의 보존을 확인하지 않고 성공 처리할 수 있던 경로를 수정했습니다.

- 주 데이터만으로 보존할 수 없는 ADS·EFS 파일은 복사·이동을 거부하고 원본을 유지합니다. 스트림 조회가 불완전한 제공자도 안전하게 중단합니다.
- 미리보기 이후·복사 도중·원본 보관 순간에 속성이 바뀌는 경우를 재확인합니다. 불완전한 복사본을 성공으로 표시하지 않으며 원본 전체 또는 저널의 복구 경로를 보존합니다.
- 파일 전체를 같은 볼륨에서 이동하는 delete_files 보관·복원은 유지합니다. Rust판은 해당 보호가 이미 있어 기능 변경 없이 버전 표식을 맞췄습니다.

기존에 통과한 새 회귀 사례 28건(Windows PE·실제 NTFS 11건, 메타데이터 응답 모사
17건)을 재사용합니다. 릴리즈 빌드 전에 실제 소스 SHA-256과 보존된 Windows
통과 기록의 일치를 확인하며, 통과 검사·전체 검사·CLI/GUI 실행·화면 캡처는
반복하지 않습니다. 전체 Windows GUI·실제 EFS 파일·실제 NAS 검증은 미수행입니다.

[상세 분석](https://github.com/P-JSL/Folder_Organizer/blob/v0.9.3/docs/WINDOWS_TRANSFER_METADATA_REVIEW.md) ·
[수정 전후 검증 기록](https://github.com/P-JSL/Folder_Organizer/blob/v0.9.3/docs/WINDOWS_TRANSFER_METADATA_RESULTS.json)

Python판과 Rust판은 별도 Windows x64 ZIP으로 제공합니다. 두 ZIP의 체크섬은
`SHA256SUMS.txt`에서 확인하세요. 이 릴리즈는 클라우드 VM의 바탕화면·평가판
활성화·공유 뷰어 문제가 해결됐다는 뜻이 아니며, 환경 게시는 수행하지 않습니다.
