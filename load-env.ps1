param(
    [string]$EnvFile = ".env"
)

$envPath = Join-Path $PSScriptRoot $EnvFile
if (-not (Test-Path $envPath)) {
    throw "Env file not found: $envPath"
}

Get-Content $envPath | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
        return
    }

    $name, $value = $line -split "=", 2
    $name = $name.Trim()
    $value = $value.Trim()
    Set-Item -Path "Env:$name" -Value $value
}

Write-Host "Environment loaded from $envPath"
