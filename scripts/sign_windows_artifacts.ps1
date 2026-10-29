# Optional Authenticode for first-party EXEs. Run after build and before packaging.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]] $Files
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-SigningSummary([string] $message) {
    Write-Host $message
    if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_STEP_SUMMARY)) {
        Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Encoding utf8 -Value $message
    }
}
function Find-SignTool {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) { return $command.Source }
    $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    if (-not (Test-Path -LiteralPath $kits -PathType Container)) {
        throw 'Windows SDK SignTool is not installed.'
    }
    $candidates = @(Get-ChildItem -LiteralPath $kits -Recurse -File -Filter signtool.exe |
        Where-Object { $_.DirectoryName -match '[\\/]x64$' } |
        Sort-Object FullName -Descending)
    if ($candidates.Count -eq 0) { throw 'Windows SDK x64 SignTool is not installed.' }
    return $candidates[0].FullName
}

$targets = @($Files | ForEach-Object {
    $candidate = (Resolve-Path -LiteralPath $_ -ErrorAction Stop).ProviderPath
    if (-not [IO.File]::Exists($candidate) -or [IO.Path]::GetExtension($candidate).ToLowerInvariant() -ne '.exe') {
        throw "Not an existing Windows executable: $_"
    }
    $candidate
} | Select-Object -Unique)

$required = $env:WINDOWS_SIGNING_REQUIRED -eq 'true'
$encodedPfx = $env:WINDOWS_SIGNING_PFX_BASE64
$password = $env:WINDOWS_SIGNING_PFX_PASSWORD
$hasPfx = -not [string]::IsNullOrWhiteSpace($encodedPfx)
$hasPassword = -not [string]::IsNullOrWhiteSpace($password)
if ($hasPfx -ne $hasPassword) {
    throw 'Incomplete signing configuration: both signing secrets are required.'
}
if (-not $hasPfx) {
    if ($required) { throw 'Signing is required, but the certificate secrets are missing.' }
    Write-SigningSummary '### Windows code signing: NOT SIGNED (certificate secrets not configured)'
    return
}

$trustMode = $env:WINDOWS_SIGNING_TRUST_MODE
if ([string]::IsNullOrWhiteSpace($trustMode)) { $trustMode = 'development' }
if ($trustMode -notin @('development', 'public')) {
    throw 'WINDOWS_SIGNING_TRUST_MODE must be development or public.'
}
$timestampUrl = $env:WINDOWS_SIGNING_TIMESTAMP_URL
if ([string]::IsNullOrWhiteSpace($timestampUrl)) { $timestampUrl = 'http://timestamp.digicert.com' }
$timestampOptions = @()
if ($timestampUrl -eq 'none') {
    if ($trustMode -ne 'development') { throw 'Public-trust signing requires an RFC 3161 timestamp.' }
    Write-Warning 'Development signing without a timestamp. The signature will not outlive certificate validity.'
} else {
    if ($timestampUrl -notmatch '^https?://') { throw 'Invalid timestamp server URL.' }
    $timestampOptions = @('/tr', $timestampUrl, '/td', 'SHA256')
}

$signTool = Find-SignTool
$pfx = Join-Path $env:RUNNER_TEMP ('signing-' + [guid]::NewGuid().ToString('N') + '.pfx')
try {
    [IO.File]::WriteAllBytes($pfx, [Convert]::FromBase64String($encodedPfx))
    $certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
        $pfx, $password, [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)
    try {
        if (-not $certificate.HasPrivateKey) { throw 'The PFX does not contain a private key.' }
        $hasCodeSigning = $false
        foreach ($extension in $certificate.Extensions) {
            if ($extension -is [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]) {
                foreach ($usage in $extension.EnhancedKeyUsages) {
                    if ($usage.Value -eq '1.3.6.1.5.5.7.3.3') { $hasCodeSigning = $true }
                }
            }
        }
        if (-not $hasCodeSigning) { throw 'The certificate lacks the Code Signing EKU.' }
        if ([datetime]::UtcNow -lt $certificate.NotBefore.ToUniversalTime() -or
            [datetime]::UtcNow -gt $certificate.NotAfter.ToUniversalTime()) {
            throw 'The certificate is not currently valid.'
        }
        foreach ($target in $targets) {
            & $signTool sign /f $pfx /p $password /fd SHA256 @timestampOptions $target
            if ($LASTEXITCODE -ne 0) { throw "SignTool signing failed: $target" }
            $signature = Get-AuthenticodeSignature -LiteralPath $target
            if ($null -eq $signature.SignerCertificate -or
                $signature.SignerCertificate.Thumbprint -ne $certificate.Thumbprint) {
                throw "Signing certificate mismatch: $target"
            }
            if ($trustMode -eq 'public') {
                & $signTool verify /pa /v $target
                if ($LASTEXITCODE -ne 0 -or $signature.Status -ne 'Valid') {
                    throw "Public trust verification failed: $target"
                }
            } else {
                # Windows does not trust self-signed CI certificates by default.
                # Validate with the public certificate trusted ONLY in this runner's
                # CurrentUser Root store, then remove it immediately after verification.
                # The signed binaries stay self-signed/untrusted on user machines.
                if ($certificate.Subject -ne $certificate.Issuer) {
                    throw "Development mode accepts only self-signed certificates: $target"
                }
                $trustStore = [System.Security.Cryptography.X509Certificates.X509Store]::new(
                    'Root', [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser)
                $trustedPublicCertificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
                    $certificate.RawData)
                $installedByStep = $false
                try {
                    $trustStore.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
                    $alreadyTrusted = @($trustStore.Certificates.Find(
                        [System.Security.Cryptography.X509Certificates.X509FindType]::FindByThumbprint,
                        $certificate.Thumbprint, $false)).Count -gt 0
                    if (-not $alreadyTrusted) {
                        $trustStore.Add($trustedPublicCertificate)
                        $installedByStep = $true
                    }
                    Write-Host "Validating self-signed signature using temporary CurrentUser Root trust"
                    $trustedSignature = Get-AuthenticodeSignature -LiteralPath $target
                    if ($trustedSignature.Status -ne 'Valid' -or
                        $null -eq $trustedSignature.SignerCertificate -or
                        $trustedSignature.SignerCertificate.Thumbprint -ne $certificate.Thumbprint) {
                        throw "Development signature integrity verification failed ($($trustedSignature.Status)): $target"
                    }
                    Write-Host "Verified development signature cryptographic integrity: $([IO.Path]::GetFileName($target))"
                    # Public-chain validation is performed separately in public mode.
                    # A development certificate is deliberately not trusted on users' PCs.
                } finally {
                    if ($installedByStep) { $trustStore.Remove($trustedPublicCertificate) }
                    $trustStore.Close()
                    $trustedPublicCertificate.Dispose()
                }
            }
            Write-Host "Signed: $([IO.Path]::GetFileName($target)); Status: $($signature.Status)"
        }
        if ($trustMode -eq 'public') {
            Write-SigningSummary "### Windows code signing: PUBLIC TRUST verified ($($targets.Count) executable(s))"
        } else {
            Write-SigningSummary "### Windows code signing: DEVELOPMENT signature ($($targets.Count) executable(s)); not publicly trusted"
        }
    } finally { $certificate.Dispose() }
} finally { Remove-Item -LiteralPath $pfx -Force -ErrorAction SilentlyContinue }
