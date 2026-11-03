# Writes the access token of YOUR current LegalDesk beta session to a file, for the T9 checks and the routing eval
# (LEGALDESK_EVAL_ACCESS_TOKEN). Read-only against AWS: one Lambda configuration read and one DynamoDB point read.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase15_get_eval_token.ps1 -OutFile "$HOME\legaldesk-t9\eval-token.txt"
#
# * The cookie value is read without echo (it is a secret) and is only used to compute the SHA-256 key of YOUR session record
#   (point read of LEGALDESK#P14#STATE#SESSION#<digest>; nothing is scanned or listed). It is never printed or written.
# * The token is never printed. It goes to -OutFile, which must be OUTSIDE this repository, and that file is restricted to your user.
# * Delete the file when the run is over (the runbook says when) and log out of the beta to end the session.
param(
    [Parameter(Mandatory = $true)][string]$OutFile,
    [int]$MinimumSecondsLeft = 1800
)
$ErrorActionPreference = "Stop"
$env:AWS_REGION = "eu-west-1"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path.TrimEnd("\")
$target = [IO.Path]::GetFullPath($OutFile)
if ($target.StartsWith($repo + "\", [StringComparison]::OrdinalIgnoreCase)) { throw "OutFile must be outside the repository ($repo): a token must never be able to reach a commit" }

$secure = Read-Host "Value of the legaldesk_session cookie (it is not shown)" -AsSecureString
$cookie = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
if (-not $cookie) { throw "empty cookie" }
$sha = [Security.Cryptography.SHA256]::Create()
$digest = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($cookie))) -replace "-", "").ToLower()
$cookie = $null

$table = aws lambda get-function-configuration --function-name LegalDeskPhase14PublicEdge-application --query "Environment.Variables.LEGALDESK_METADATA_TABLE_NAME" --output text
if ($LASTEXITCODE -ne 0 -or -not $table) { throw "could not read the table name (is the aws login still valid?)" }

$keyFile = Join-Path ([IO.Path]::GetTempPath()) ("legaldesk-session-key-" + [Guid]::NewGuid().ToString("N") + ".json")
try {
    $key = @{ pk = @{ S = "LEGALDESK#P14#STATE#SESSION#$digest" }; sk = @{ S = "RECORD" } } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($keyFile, $key, (New-Object Text.UTF8Encoding($false)))
    $raw = aws dynamodb get-item --table-name $table --key "file://$keyFile" --consistent-read --projection-expression "accessToken, expiresAt" --output json
    if ($LASTEXITCODE -ne 0) { throw "get-item failed" }
} finally {
    Remove-Item $keyFile -Force -ErrorAction SilentlyContinue
}
$item = $raw | ConvertFrom-Json
if (-not $item.Item) { throw "no session found for that cookie: log in again and copy the value of legaldesk_session" }

$left = [int]([double]$item.Item.expiresAt.N - [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
if ($left -lt $MinimumSecondsLeft) { throw "the session has only $left s left; log out and in again so the checks and the eval fit (they need about 30-40 minutes)" }

New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
[IO.File]::WriteAllText($target, $item.Item.accessToken.S, (New-Object Text.UTF8Encoding($false)))
icacls $target /inheritance:r /grant:r "$($env:USERNAME):(R,W,D)" | Out-Null
"OK: token written to $target (restricted to $($env:USERNAME); the session is valid for another $([int]($left / 60)) min)."
