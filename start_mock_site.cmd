@echo off
setlocal
set "ROOT=%~dp0"
pushd "%ROOT%"
if not exist ".runtime\python312-full\python.exe" (
  echo Missing .runtime\python312-full\python.exe. Run the project setup first.
  popd
  exit /b 1
)
".runtime\python312-full\python.exe" -m app mock-site --open
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
