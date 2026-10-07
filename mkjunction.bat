@echo off
if exist C:\dev\gps rmdir C:\dev\gps
mklink /J C:\dev\gps "G:\My Drive\Projects\devbyloki\sites\gps-painting"
dir C:\dev\gps
