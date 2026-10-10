# Pentax K-5 II -> N.I.N.A. camera bridge  (for TPPA, plate solving, imaging)

This small Python program makes the Pentax K-5 II appear in N.I.N.A.'s **Camera** drop-down
(as "Pentax K-5 II (pkTriggerCord bridge)"). It talks to the camera through the
`pktriggercord-cli.exe` which comes from the PkTriggerCord App, and gives N.I.N.A. the RAW Bayer image of every shot.
Nothing is installed inside N.I.N.A. - N.I.N.A. finds the program on its own (ASCOM Alpaca).

```
N.I.N.A. / TPPA  -- Alpaca -->  pentax_alpaca_camera.py  -- runs -->  pktriggercord-cli.exe  --USB-->  K-5 II
```

## 1. Installation

1. Unzip this folder somewhere simple, e.g. `C:\PentaxNINA` (not into "Program Files").
2. Install **Python 3.12 (64-bit)** from https://www.python.org/downloads/windows/
   - tick **"Add python.exe to PATH"** on the first page of the installer.
3. Double-click **`1_install.bat`** (creates a private environment, installs numpy + rawpy; needs internet once).
4. Open **`pentax_alpaca_config.json`** in Notepad and set `cli_path` to the full path of your
   `pktriggercord-cli.exe` (same folder as the pkTriggerCord program you use), using forward slashes:
   `"cli_path": "C:/pkTriggerCord/pktriggercord-cli.exe"`. Save.

## 2. Prepare the camera (K-5 II)

- Mode **M**. Focus **MF**
- Menu: **USB connection = MSC** (pkTriggerCord does not work in PTP mode).
- Close the pkTriggerCord GUI (only one program can use the camera).
- Heads-up: the bridge sets the camera to **RAW-only, DNG** (pkTriggerCord does this). Change it back in the camera menu afterwards if you want JPEGs again.

## 3. Test the camera (before going to N.I.N.A.)

1. Double-click **`2_check_camera.bat`** -> should print "OK - camera found" and your lens. (Accept the Windows "administrator" prompt.)
2. Double-click **`3_test_shot.bat`** -> takes a 3 s photo, prints statistics and writes `test_frame.fits`.
   Open that FITS in ASTAP and press *Solve* to check plate solving without N.I.N.A.

## 4. Use it in N.I.N.A.

1. Double-click **`4_start_server.bat`** and leave the window open. (Windows Firewall asks once: allow on private networks.)
2. Start N.I.N.A. -> **Equipment -> Camera** -> press the rescan button -> choose **Pentax K-5 II (pkTriggerCord bridge)** -> Connect.

No camera at hand (indoor test)? Double-click `4b_start_server_SIMULATION.bat`: N.I.N.A. then sees a fake camera that delivers synthetic star fields.

Uninstall: close the server, delete the folder.
