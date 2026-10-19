$ErrorActionPreference = 'Stop'
$compiler = (Get-Command g++.exe).Source
& $compiler -std=c++17 -O2 -Wall -Wextra -Werror -Wno-misleading-indentation -shared -static -static-libgcc -static-libstdc++ "$PSScriptRoot\engine.cpp" -lwinusb -o "$PSScriptRoot\..\canable\_native.dll"
if ($LASTEXITCODE -ne 0) { throw 'Native build failed' }
