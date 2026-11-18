# Download Aitrade

The setup file for Windows is in this folder: `Aitrade-Setup-<version>.exe`.
Take the file with the highest version number.

1. Click the file name.
2. Click the download button (the arrow pointing down, at the top right of the
   file page).
3. Double-click the downloaded file. It installs Aitrade for you, or updates
   the Aitrade that is already on the PC. Settings, saved passwords, logs and
   the MetaTrader copies are kept.

Windows may say "Windows protected your PC", because the setup file is not
signed. Choose "More info", then "Run anyway".

Test a new version with demo accounts before you use it with real money.

The files here are built by GitHub from the code in this repository
(`.github/workflows/build-installer.yml`): all tests run on Windows, the setup
file is built, installed and checked with its own self-test, and only then is
it put in this folder. A new file appears when the version number in
`aitrade/__init__.py` changes.
