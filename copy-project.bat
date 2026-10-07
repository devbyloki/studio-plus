@echo off
echo Copying project to C:\dev\gps (this may take a while)...
xcopy "G:\My Drive\Projects\devbyloki\sites\gps-painting" C:\dev\gps /E /I /H /Y /Q /EXCLUDE:C:\dev\excludes.txt > C:\dev\xcopy.log 2>&1
echo Done. Exit code: %errorlevel%
dir C:\dev\gps | findstr /R "File\(s\) Dir\(s\)"
