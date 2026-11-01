# Windows PowerShell twin of grade.sh: run AutoGrader inside its Docker image.
#
#   .\grade.ps1 grade problems\add_two_integers\submissions\add.c --rules problems/add_two_integers/rules.json
#   .\grade.ps1 llm-check
#   .\grade.ps1 serve                    # web UI on http://localhost:8000 ($env:AUTOGRADER_PORT to change)
#
# The current folder is mounted at /work, so the SQLite database, LLM answers and
# generated assets (.autograder-cache\generated\<problem>\<generation_id>\) are
# written to .autograder-cache in this folder and survive between runs.
# Paths after the command are passed to Linux: use forward slashes.
$ErrorActionPreference = "Stop"
$Image = if ($env:AUTOGRADER_IMAGE) { $env:AUTOGRADER_IMAGE } else { "autograder:latest" }
docker image inspect $Image *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "building $Image ..."
    docker build -t $Image $PSScriptRoot
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$HostDir = (Get-Location).Path
$Ollama = if ($env:AUTOGRADER_OLLAMA_HOST) { $env:AUTOGRADER_OLLAMA_HOST } else { "http://host.docker.internal:11434" }
$Run = @("run", "--rm",
    "-v", "${HostDir}:/work", "-w", "/work",
    "-e", "AUTOGRADER_CACHE=/work/.autograder-cache",
    "-e", "AUTOGRADER_HOST_WORKDIR=$HostDir",
    "-e", "OLLAMA_HOST=$Ollama",
    "-e", "AUTOGRADER_LLM_PROVIDER", "-e", "AUTOGRADER_LLM_MODEL",
    "-e", "GROQ_API_KEY", "-e", "OPENAI_COMPAT_BASE_URL", "-e", "OPENAI_COMPAT_API_KEY",
    "--add-host=host.docker.internal:host-gateway",
    "--pids-limit", "512", "--memory", "4g")

$Rest = @($args)
if ($Rest.Count -gt 0 -and $Rest[0] -eq "serve") {
    $Port = if ($env:AUTOGRADER_PORT) { $env:AUTOGRADER_PORT } else { "8000" }
    $Run += @("-it", "-p", "127.0.0.1:${Port}:8000", "-e", "AUTOGRADER_UI_URL=http://localhost:$Port")
    $Extra = if ($Rest.Count -gt 1) { $Rest[1..($Rest.Count - 1)] } else { @() }
    $Rest = @("serve", "--host", "0.0.0.0", "--port", "8000") + $Extra
}
& docker @Run $Image @Rest
exit $LASTEXITCODE
