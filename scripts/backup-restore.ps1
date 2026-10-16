$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
Push-Location $repoRoot
$runId = [guid]::NewGuid().ToString('N')
$database = "restore_$runId"
$folder = "results/backup-$runId"
$dumpPath = "/tmp/backup-$runId.dump"
$restorePath = "/tmp/restore-$runId.dump"
$created = $false
$report = [ordered]@{ status = 'failed'; folder = $folder; source = 'settlement_lab'; restored = $database }
New-Item -ItemType Directory -Path $folder | Out-Null

function Invoke-RecordedDocker {
    param([string[]]$DockerArgs, [string]$Log)
    & docker @DockerArgs *> "$folder/$Log"
    if ($LASTEXITCODE -ne 0) { throw "Docker 작업 실패: $Log" }
}

try {
    Invoke-RecordedDocker -DockerArgs @('compose','run','--rm','runner','backup-check','--folder',$folder) -Log 'before.log'
    Invoke-RecordedDocker -DockerArgs @('compose','exec','-T','db','pg_dump','--version') -Log 'pg-dump-version.log'
    Invoke-RecordedDocker -DockerArgs @('compose','exec','-T','db','pg_restore','--version') -Log 'pg-restore-version.log'
    $timer = [Diagnostics.Stopwatch]::StartNew()
    Invoke-RecordedDocker -DockerArgs @('compose','exec','-T','db','pg_dump','-U','lab_admin','-d','settlement_lab','-Fc','-f',$dumpPath) -Log 'dump.log'
    $report.dump_seconds = $timer.Elapsed.TotalSeconds
    Invoke-RecordedDocker -DockerArgs @('compose','cp',"db:$dumpPath","$folder/settlement.dump") -Log 'copy-out.log'
    $archive = Get-Item -LiteralPath "$folder/settlement.dump"
    $report.archive_bytes = $archive.Length
    $report.archive_sha256 = (Get-FileHash -LiteralPath $archive.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    # PC에 저장한 파일을 다시 복사해 백업 원본 임시 파일과 분리해서 복원.
    Invoke-RecordedDocker -DockerArgs @('compose','cp',"$folder/settlement.dump","db:$restorePath") -Log 'copy-in.log'
    Invoke-RecordedDocker -DockerArgs @('compose','exec','-T','db','pg_restore','--list',$restorePath) -Log 'archive-list.log'
    Invoke-RecordedDocker -DockerArgs @('compose','exec','-T','db','createdb','-U','lab_admin','--template=template0',$database) -Log 'create.log'
    $created = $true
    $timer.Restart()
    Invoke-RecordedDocker -DockerArgs @('compose','exec','-T','db','pg_restore','-U','lab_admin','-d',$database,'--exit-on-error','--single-transaction',$restorePath) -Log 'restore.log'
    $report.restore_seconds = $timer.Elapsed.TotalSeconds
    $timer.Restart()
    Invoke-RecordedDocker -DockerArgs @('compose','run','--rm','runner','backup-check','--folder',$folder,'--database',$database) -Log 'after.log'
    $report.verification_seconds = $timer.Elapsed.TotalSeconds
    $report.verification = Get-Content -LiteralPath "$folder/verification.json" -Raw | ConvertFrom-Json
    $report.status = 'passed'
} catch {
    $report.error = $_.Exception.Message
    throw
} finally {
    try {
        # 대상은 이 실행에서 생성한 UUID DB 하나. 기존 DB는 제거하지 않음.
        if ($created) {
            Invoke-RecordedDocker -DockerArgs @('compose','exec','-T','db','dropdb','-U','lab_admin','--force',$database) -Log 'cleanup-db.log'
        }
        Invoke-RecordedDocker -DockerArgs @('compose','exec','-T','db','rm','-f',$dumpPath,$restorePath) -Log 'cleanup-files.log'
    } catch {
        $report.status = 'failed'
        $report.cleanup_error = $_.Exception.Message
        Write-Warning $report.cleanup_error
    }
    $report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath "$folder/summary.json" -Encoding utf8
    Pop-Location
}
if ($report.status -ne 'passed') { throw "복구 실험 실패: $folder/summary.json" }
$report | ConvertTo-Json -Depth 12
