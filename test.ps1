param([switch]$Native)
$ErrorActionPreference = 'Stop'
$python = (Get-Command python.exe).Source
Push-Location $PSScriptRoot
try {
    if ($Native) {
        & "$PSScriptRoot\native\build.ps1"
        New-Item -ItemType Directory -Path "$PSScriptRoot\tests\_build" -Force | Out-Null
        $compiler = (Get-Command g++.exe).Source
        foreach ($test in @('native_queue', 'native_shutdown')) {
            & $compiler -std=c++17 -O2 -Wall -Wextra -Werror -static -static-libgcc -static-libstdc++ "tests/$test.cpp" -lwinusb -o "tests/_build/$test.exe"
            if ($LASTEXITCODE -ne 0) { throw "Build failed: $test" }
            & "$PSScriptRoot\tests\_build\$test.exe"
            if ($LASTEXITCODE -ne 0) { throw "Test failed: $test" }
        }
    }
    & $python -B -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Python offline tests failed' }
}
finally {
    Pop-Location
}
