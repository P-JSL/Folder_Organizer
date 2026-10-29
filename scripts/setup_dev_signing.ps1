# Create a DEVELOPMENT-only code signing certificate on Windows.
# Never commit or share the exported private-key PFX.
[CmdletBinding()]
param(
    [string] $OutputDirectory = (Join-Path $env:USERPROFILE 'FolderOrganizer-Signing'),
    [switch] $UploadGitHubSecrets,
    [string] $Repository = 'P-JSL/Folder_Organizer'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'Run this script in PowerShell on Windows.' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$pfxPath = Join-Path $OutputDirectory 'FolderOrganizer-DEV.pfx'
$cerPath = Join-Path $OutputDirectory 'FolderOrganizer-DEV.cer'
if ((Test-Path -LiteralPath $pfxPath) -or (Test-Path -LiteralPath $cerPath)) {
    throw 'Certificate files already exist; refusing to overwrite an existing signing identity.'
}
$password = Read-Host 'Enter a strong PFX password (save it securely)' -AsSecureString
$certificate = New-SelfSignedCertificate -Type CodeSigningCert `
    -Subject 'CN=Folder Organizer Development - NOT PUBLICLY TRUSTED' `
    -CertStoreLocation 'Cert:\CurrentUser\My' `
    -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256 `
    -KeyExportPolicy Exportable -NotAfter (Get-Date).AddMonths(12)
Export-PfxCertificate -Cert $certificate -FilePath $pfxPath -Password $password | Out-Null
Export-Certificate -Cert $certificate -FilePath $cerPath | Out-Null
Write-Host "Created development certificate thumbprint: $($certificate.Thumbprint)"
Write-Host "Private-key PFX: $pfxPath"
Write-Host "Public certificate: $cerPath"
Write-Warning 'Self-signed certificates cannot establish public publisher trust or eliminate SmartScreen warnings.'
if ($UploadGitHubSecrets) {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw 'Install GitHub CLI, run gh auth login, then upload the two secrets manually (see docs).'
    }
    & gh auth status
    if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI is not authenticated.' }
    $encoded = [Convert]::ToBase64String([IO.File]::ReadAllBytes($pfxPath))
    $encoded | & gh secret set WINDOWS_SIGNING_PFX_BASE64 --repo $Repository
    if ($LASTEXITCODE -ne 0) { throw 'Could not upload the PFX secret.' }
    $plain = [System.Net.NetworkCredential]::new('', $password).Password
    try {
        $plain | & gh secret set WINDOWS_SIGNING_PFX_PASSWORD --repo $Repository
        if ($LASTEXITCODE -ne 0) { throw 'Could not upload the password secret.' }
    } finally {
        $plain = $null
        $encoded = $null
    }
    Write-Host 'Development signing secrets registered with GitHub.'
}
