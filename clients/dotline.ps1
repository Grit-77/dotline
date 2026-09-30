# PowerShell 5.1+. This file is distributed as UTF-8 with BOM.
[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet('send', 'wait', 'replies', 'health')]
    [string]$Command,
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Text,
    [string]$Topic = '',
    [string]$File,
    [int]$Id = 0,
    [double]$Minutes = 10,
    [int]$After = 0
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

function Invoke-Dotline([string]$Path, [string]$Body = $null) {
    $request = [Net.HttpWebRequest]::Create($script:baseUrl + $Path)
    $request.AllowAutoRedirect = $false
    $request.Proxy = $null
    $request.Timeout = 15000
    $request.ReadWriteTimeout = 15000
    $request.Accept = 'application/json'
    if ($Path -ne '/v1/health') {
        $secret = [IO.File]::ReadAllText($script:tokenFile, $utf8).Trim()
        if (!$secret -or $secret -match '[^\x21-\x7e]') { throw 'Invalid token file' }
        $request.Headers['Authorization'] = 'Bearer ' + $secret
        $secret = $null
    }
    if ($null -ne $Body -and $Body.Length -gt 0) {
        $request.Method = 'POST'
        $request.ContentType = 'application/json; charset=utf-8'
        $bytes = $utf8.GetBytes($Body)
        if ($bytes.Length -gt 16384) { throw 'Body exceeds 16384 bytes' }
        $request.ContentLength = $bytes.Length
        $stream = $request.GetRequestStream()
        try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    }
    $response = $request.GetResponse()
    try {
        $reader = New-Object IO.StreamReader($response.GetResponseStream(), $utf8)
        try { return ($reader.ReadToEnd() | ConvertFrom-Json) }
        finally { $reader.Dispose() }
    } finally { $response.Dispose() }
}

try {
    $configHome = $env:DOTLINE_HOME
    if (!$configHome) {
        if ($env:APPDATA) { $configHome = Join-Path $env:APPDATA 'dotline' }
        elseif ($env:XDG_CONFIG_HOME) { $configHome = Join-Path $env:XDG_CONFIG_HOME 'dotline' }
        else { $configHome = Join-Path $HOME '.config/dotline' }
    }
    $script:baseUrl = $env:DOTLINE_URL
    $configFile = Join-Path $configHome 'config.toml'
    if (!$script:baseUrl -and (Test-Path -LiteralPath $configFile)) {
        foreach ($line in [IO.File]::ReadAllLines($configFile, $utf8)) {
            if ($line -match '^\s*url\s*=\s*"([^"\\]*)"') { $script:baseUrl = $Matches[1] }
            elseif ($line -match "^\s*url\s*=\s*'([^']*)'") { $script:baseUrl = $Matches[1] }
        }
    }
    if (!$script:baseUrl) { $script:baseUrl = 'http://127.0.0.1:8790' }
    $script:baseUrl = $script:baseUrl.TrimEnd('/')
    $origin = [Uri]$script:baseUrl
    if (!$origin.IsAbsoluteUri -or $origin.UserInfo -or $origin.Query -or $origin.Fragment -or $origin.AbsolutePath -ne '/') {
        throw 'URL must be an origin without credentials or query'
    }
    if ($origin.Scheme -ne 'https' -and !($origin.Scheme -eq 'http' -and $origin.Host -in @('127.0.0.1', 'localhost', '::1'))) {
        throw 'Remote URLs must use HTTPS'
    }
    $script:tokenFile = $env:DOTLINE_TOKEN_FILE
    if (!$script:tokenFile) { $script:tokenFile = Join-Path $configHome 'token' }
    switch ($Command) {
        send {
            if ($File) {
                if ($Text.Count -gt 0) { throw 'Use either text or -File' }
                $message = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $File), $utf8)
            } else { $message = $Text -join ' ' }
            if (!$message -or $message.Length -gt 8000 -or $Topic.Length -gt 120) { throw 'Message or topic exceeds limits' }
            $body = @{text = $message; topic = $Topic} | ConvertTo-Json -Compress
            (Invoke-Dotline '/v1/messages' $body).id
        }
        wait {
            if ($Id -eq 0 -and $Text.Count -gt 0) { $Id = [int]$Text[0] }
            if ($Id -lt 1 -or $Minutes -le 0 -or $Minutes -gt 10080) { throw 'Invalid id or minutes' }
            $timer = [Diagnostics.Stopwatch]::StartNew()
            $cursor = 0
            while ($true) {
                $result = Invoke-Dotline ("/v1/replies?after=" + $cursor)
                $found = $false
                foreach ($reply in $result.replies) {
                    if ($reply.to -eq $Id) { $reply.text; $found = $true; break }
                    $cursor = [Math]::Max($cursor, [int]$reply.id)
                }
                if ($found) { break }
                $remaining = $Minutes * 60 - $timer.Elapsed.TotalSeconds
                if ($remaining -le 0) { throw 'Timed out waiting for a reply' }
                Start-Sleep -Milliseconds ([int]([Math]::Min(20, $remaining) * 1000))
            }
        }
        replies {
            if ($After -lt 0) { throw 'After must be nonnegative' }
            Invoke-Dotline ("/v1/replies?after=" + $After) | ConvertTo-Json -Depth 5
        }
        health { Invoke-Dotline '/v1/health' | ConvertTo-Json -Compress }
    }
} catch {
    # Exception details may include headers or submitted text. Never echo them.
    [Console]::Error.WriteLine('dotline: request failed; check the URL, token file, limits and server availability')
    exit 1
}
