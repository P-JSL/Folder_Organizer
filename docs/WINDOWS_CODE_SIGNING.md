# Windows 코드 서명 — 인증서가 전혀 없는 경우부터

대상: `P-JSL/Folder_Organizer`의 Python/Rust Windows 5개 EXE. 기존 ZIP 파일명, SHA-256 체크섬, 자동 업데이트 규격을 유지합니다.

**중요:** 자체 서명 인증서는 무료로 생성할 수 있지만 외부 사용자의 Windows에서 게시자를 공인 인증하거나 SmartScreen 경고를 해제하지 못합니다. 개발 환경에서의 서명·변조 검사 연습용입니다. 공개 배포에서 검증된 게시자를 표시하려면 적합한 공인 코드 서명 공급자와 보안 키 보관/클라우드 서명 서비스를 이용하세요.

## 1. Windows에서 무료 개발용 인증서 생성

저장소를 클론하고 Windows PowerShell에서 다음을 실행하세요.

```powershell
.\scripts\setup_dev_signing.ps1
```

PFX 암호를 입력하면 `%USERPROFILE%\FolderOrganizer-Signing` 아래에 다음이 생성됩니다.

- `FolderOrganizer-DEV.pfx`: 개인키가 포함된 **비밀** 파일. 백업은 보안 저장소에만 보관.
- `FolderOrganizer-DEV.cer`: 개인키가 없는 공개 인증서.
- 실제 PFX 암호와 개인키 파일은 **GitHub 소스, 채팅, 이슈, Pull Request, 로그에 올리지 마세요.**

GitHub CLI(https://cli.github.com/) 설치 후 `gh auth login`까지 마친 상태라면, **처음 생성하는 경우에만** `-UploadGitHubSecrets` 옵션으로 자동 등록할 수 있습니다.

```powershell
.\scripts\setup_dev_signing.ps1 -UploadGitHubSecrets
```

기존 생성된 파일이 있는 경우에는 재생성하지 마시고 다음과 같이 업로드하세요.

```powershell
$pfx = Join-Path $env:USERPROFILE 'FolderOrganizer-Signing\FolderOrganizer-DEV.pfx'
[Convert]::ToBase64String([IO.File]::ReadAllBytes($pfx)) |
  gh secret set WINDOWS_SIGNING_PFX_BASE64 --repo P-JSL/Folder_Organizer

$pwd = Read-Host -AsSecureString 'PFX password'
$plain = [System.Net.NetworkCredential]::new('', $pwd).Password
try {
  $plain | gh secret set WINDOWS_SIGNING_PFX_PASSWORD --repo P-JSL/Folder_Organizer
} finally {
  $plain = $null
}
```

## 2. GitHub Actions Secrets와 Variables

GitHub 저장소 → **Settings → Secrets and variables → Actions**:

**Secrets** (2개 필수):
- `WINDOWS_SIGNING_PFX_BASE64`: 개발용 PFX 전체 파일을 Base64 인코딩한 값
- `WINDOWS_SIGNING_PFX_PASSWORD`: PFX 암호

**Variables** (설정하지 않으면 기본값 사용):
- `WINDOWS_SIGNING_TRUST_MODE=development`: 개발용 자체 서명 검증. 신뢰된 게시자로 표현하지 않습니다.
- `WINDOWS_SIGNING_REQUIRED=true`: 서명 인증서 미설정 시에도 워크플로를 성공시키지 않고 실패로 처리. Secrets가 없는 동안은 설정하지 마세요.
- `WINDOWS_SIGNING_TIMESTAMP_URL=http://timestamp.digicert.com`: RFC 3161 타임스탬프 URL. 설정하지 않아도 이 기본값 사용. 내부 Windows 서명 테스트에서만 `development` 모드와 함께 `none`을 사용하여 네트워크 타임스탬프 없이 검사할 수 있습니다. 실제 배포에는 권장하지 않습니다.

**자체 서명 인증서로 `WINDOWS_SIGNING_TRUST_MODE=public`을 설정하면 신뢰 검증 실패가 정상입니다.**

## 3. 현재 저장소 빌드 경로

| 워크플로 | EXE 처리 | 서명 적용 시점 |
| --- | --- | --- |
| `dual-release.yml` | Python 2 + Rust 3 | 각 실행 파일을 빌드·패키지 디렉터리에 배치한 뒤, ZIP 전 |
| `version-release.yml` | Python 2 + Rust 3 | `package_version_release.py`의 ZIP 전 |
| `rust-hotfix.yml` | Rust 3 | `windows_rust_hotfix_v6.py package` 전 |
| `release.yml` | Python 2 (구버전 태그만) | ZIP 전 |

서명 대상은 다음과 같습니다.
- `FolderOrganizer.exe`
- `FolderOrganizerUpdater.exe`
- `FolderOrganizerRust.exe`
- `FolderOrganizerRustCLI.exe`
- `FolderOrganizerRustUpdater.exe`

외부 공급자 DLL/PYD는 재서명하지 않습니다. 서명 후 EXE가 바뀌므로 **ZIP 및 SHA-256을 반드시 다시 생성**합니다. 설치 및 업데이트 디렉터리 구조는 바꾸지 않습니다.

별도 `signing-smoke.yml` 워크플로는 GitHub Windows Runner에서 임시 자체 서명 인증서와 테스트 EXE를 생성하여 서명 경로를 검증합니다. 기존 Secrets나 공개 Release 파일을 사용하지 않습니다.

Secrets 두 개가 **모두 없으면** 기존처럼 미서명 빌드를 생성하고 Actions Step Summary에 `NOT SIGNED`를 표시합니다. 한 개만 입력되면 설정 오류로 실패합니다. 서명 중 오류가 생겨도 **미서명 빌드로 대체하지 않습니다**.

## 4. 서명 검증

압축을 풀고 Windows SDK에 포함된 SignTool이 있다면:

```powershell
Get-AuthenticodeSignature .\FolderOrganizer.exe |
  Format-List Status,SignerCertificate
signtool verify /pa /v .\FolderOrganizer.exe
```

자체 서명 인증서는 일반 PC에서 `NotTrusted` 등이 나오는 것이 정상입니다. **공개 신뢰 서명일 때에만** `signtool verify /pa`의 성공과 PowerShell `Valid`를 요구합니다.

GitHub Secrets는 목록과 수정 시각을 조회할 수 있지만 저장한 *Secret 값*은 다시 조회할 수 없습니다. GitHub 연결 도구에 Secrets 메타데이터 조회 기능이 없더라도, 이 저장소에서 워크플로 코드를 수정·검토하는 데 Secret 원문은 필요하지 않습니다.

## 5. 실제 사용자 배포시

- 공인 코드 서명 인증서가 없는 경우 `Windows에서 확인되지 않은 게시자` 메시지가 발생할 수 있습니다. 자체 서명만으로는 해결되지 않습니다.
- 일반 사용자에게 자체 서명 인증서를 Windows 신뢰 루트에 설치하라고 요구하지 마세요.
- 신규 공개 신뢰 인증서 개인키는 보안 하드웨어나 클라우드 서비스에서 관리하는 것이 일반적이므로, 이 저장소의 **PFX 경로가 모든 CA 인증서를 지원하는 것은 아닙니다**. 키를 내보낼 수 없는 서비스는 해당 공급자의 서명 API를 사용하는 별도 연동이 필요합니다.
- Signing Secrets를 사용하는 워크플로를 수정할 수 있는 저장소 쓰기 권한을 제한하고, 필요한 경우 보호된 GitHub Environments와 승인 절차를 구성하세요. Actions Secrets에 저장된 PFX는 HSM 보안의 대체가 아닙니다.
- GitHub Release ZIP의 SHA-256 파일은 무결성 비교를 돕지만, 별도로 신뢰된 서명을 제공하는 것은 아닙니다.
