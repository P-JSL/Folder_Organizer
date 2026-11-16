# 클라우드 개발 환경

이 디렉터리는 채팅에서 구성한 Python·Rust 개발 도구, Linux 데스크톱과
클라우드 내부 Windows 가상머신의 재사용 가능한 설치·시작 안내를 보관합니다.
앱의 파일 분석·정리 기능을 변경하는 코드가 아닙니다.

## 설치와 시작

Codex 관리형 Debian 환경에서 저장소가 `/workspace/Folder_Organizer`에 있을 때
필요한 설치 또는 갱신 작업으로 실행합니다.

```sh
bash /workspace/Folder_Organizer/.codex/environment/install.sh
. /workspace/.folder-organizer-tools/activate.sh
```

설치 스크립트는 개발 의존성·도구를 준비하고 기존 VM 디스크를 유지합니다.
실행 중인 서비스와 `/tmp` 소켓은 환경 복원 후 다시 시작해야 합니다.
상세 명령은 [START.md](START.md)에 있습니다. 앱 검사·빌드와 Windows 부팅을
환경 시작만으로 자동 실행하지 않습니다.

## 현재 확인 범위

- Python 도구와 Rust 1.99.1, 인증된 Linux Xorg/XFCE 세션을 준비했습니다.
- Windows는 공식 Microsoft 한국어 Enterprise 평가판을 확인된 SHA-256으로
  설치했습니다. QEMU TCG의 2 vCPU·8 GiB 메모리를 사용하며 하드웨어 가속이 없습니다.
- Windows 초기 설정과 Bootstrap, 게스트 프록시·인증서 적용을 확인했습니다.
  이전 원격 인증은 성공했지만 탐색기 오류와 원격 화면 준비 지연이 남아 있습니다.
- 평가판 활성화는 관리형 프록시의 상위 서버 연결 오류(HTTP 503)로 실패했습니다.
  활성화 우회·평가 기간 연장·시계 변경은 하지 않았습니다.
- 현재 환경 설정 채팅에는 공유 데스크톱을 여는 도구가 없습니다.
  로컬 Xorg/RDP 창 실행만으로 사용자가 보는 공유 화면이 생기지는 않습니다.

Windows 바탕화면 사용, 평가판 활성화, 새 작업에서의 환경 복원은 완료된 것으로
표시하지 않습니다. 앱 릴리즈 성공은 이 환경의 Windows 사용 가능 여부를 대신하지 않습니다.

## 개인 상태와 디스크 보존

실제 VM·자격 증명·인증서·설치 미디어·로그는 저장소 밖의
`/workspace/.folder-organizer-tools`에만 보관합니다. 이 디렉터리에는 생성된
비밀번호, 개인 키, 설치 XML 또는 디스크 이미지가 포함되지 않습니다.
설치 시 비밀번호를 새로 생성하는 코드와 공개 Microsoft 다운로드 주소는 포함됩니다.

기존 VM을 유지할 때 `windows.qcow2`와 그 필수 원본
`windows.pre-logon-repair.qcow2`, TPM·정상 UEFI 상태를 함께 보존합니다.
기존 디스크에 `prepare.py`를 다시 실행하거나 무인 설치를 반복하지 않습니다.
환경 게시 전에 Windows를 정상 종료하고, 저장된 설정과 실제 게시 결과를 구분합니다.
