# Windows 빌드와 릴리스

먼저 `main`의 Windows·Linux 검사를 통과한 커밋에서 **Windows release → Run workflow**로 검토용 빌드를 만듭니다. 수동 실행은 릴리스를 게시하지 않습니다. 완료된 작업의 `FolderOrganizer-Windows-x64` 아티팩트에는 portable ZIP과 `SHA256SUMS.txt`가 들어 있습니다.

검토용 빌드도 전체 테스트, 실제 패키지 GUI·CLI 실행, 업데이트 도우미 검사와 이전 안정 버전에서의 업데이트 호환성 검사를 통과해야 합니다. 다운로드한 ZIP은 체크섬과 버전 표식, 압축 무결성을 확인합니다.

검토용 파일을 먼저 제공하고, 사용자 지시에 따라 릴리스 게시를 진행합니다. 별도 게시 지시 없이 시험용 빌드 요청만 받은 경우 태그와 공개 릴리스는 만들지 않습니다. 게시 시에는 검토한 커밋 그대로 `vX.Y.Z` 태그를 만들고, 태그 빌드가 모든 검사를 통과한 뒤 공개 ZIP·체크섬을 게시합니다.

새 버전은 `folder_organizer/__init__.py`, `pyproject.toml`, `version.json`을 함께 변경합니다. `scripts/windows_upgrade_compatibility.py`의 이전 버전도 현재 공개 안정 버전으로 맞춥니다. 검토 중인 버전의 README에는 공개 안정 버전 다운로드와 검토용 빌드를 구분합니다.
