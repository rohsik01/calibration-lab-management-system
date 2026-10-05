# DHM Calibration QR Reader for Android

Standalone Android reader for DHM calibration certificates.

Features:
- Camera QR scanning.
- Manual QR payload paste fallback.
- Full DHM-CAL-OFFLINE JSON record displayed locally.
- No Flask server, tunnel, or Internet connection required after installation.
- No network permission is declared.
- Read-only; it never changes calibration data.
- The displayed fingerprint is an integrity identifier; the offline app does not claim cryptographic signature verification.

Build:
Open android/ in Android Studio and build app > assembleDebug.
APK output: app/build/outputs/apk/debug/app-debug.apk

GitHub Actions also builds the debug APK for changes under android/ and uploads it as a workflow artifact.
