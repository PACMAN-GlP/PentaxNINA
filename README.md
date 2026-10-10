# Pentax K-5 II -> N.I.N.A. camera bridge  (for TPPA, plate solving, imaging)

This small Python program makes your Pentax K-5 II appear in N.I.N.A.'s **Camera** drop-down
(as "Pentax K-5 II (pkTriggerCord bridge)"). It talks to the camera through the
`pktriggercord-cli.exe` you already have, and gives N.I.N.A. the RAW Bayer image of every shot.
Nothing is installed inside N.I.N.A. - N.I.N.A. finds the program on its own (ASCOM Alpaca).

```
N.I.N.A. / TPPA  --Alpaca-->  pentax_alpaca_camera.py  --runs-->  pktriggercord-cli.exe  --USB-->  K-5 II
```

## 1. One-time setup

1. Unzip this folder somewhere simple, e.g. `C:\PentaxNINA` (not into "Program Files").
2. Install **Python 3.12 (64-bit)** from https://www.python.org/downloads/windows/
   - tick **"Add python.exe to PATH"** on the first page of the installer.
3. Double-click **`1_install.bat`** (creates a private environment, installs numpy + rawpy; needs internet once).
4. Open **`pentax_alpaca_config.json`** in Notepad and set `cli_path` to the full path of your
   `pktriggercord-cli.exe` (same folder as the pkTriggerCord program you use), using forward slashes:
   `"cli_path": "C:/pkTriggerCord/pktriggercord-cli.exe"`. Save.

## 2. Prepare the camera (K-5 II)

- Mode dial **M**. Focus switch **MF**, lens focused on stars (infinity). Lens cap off.
- Menu: **USB connection = MSC** (pkTriggerCord does not work in PTP mode).
- **Shake Reduction OFF**. **Auto power off = OFF**. Slow-shutter-speed NR and high-ISO NR: OFF.
- Fresh battery (or AC adapter) and an SD card inserted. Close the pkTriggerCord GUI (only one program can use the camera).
- Heads-up: the bridge sets the camera to **RAW-only, DNG** (pkTriggerCord does this). Change it back in the camera menu afterwards if you want JPEGs again.

## 3. Test the camera (before going to N.I.N.A.)

1. Double-click **`2_check_camera.bat`** -> should print "OK - camera found" and your lens. (Accept the Windows "administrator" prompt.)
2. Double-click **`3_test_shot.bat`** -> takes a 3 s photo, prints statistics and writes `test_frame.fits`.
   Open that FITS in ASTAP and press *Solve* to check plate solving without N.I.N.A.

## 4. Use it in N.I.N.A.

1. Double-click **`4_start_server.bat`** and leave the window open. (Windows Firewall asks once: allow on private networks.)
2. Start N.I.N.A. -> **Equipment -> Camera** -> press the rescan button -> choose **Pentax K-5 II (pkTriggerCord bridge)** -> Connect.
3. In N.I.N.A.: **Options -> Equipment -> Telescope**: focal length **300**, focal ratio 4. The camera tells N.I.N.A. its pixel size (4.8 um) -> 3.3 arcsec/pixel, field about 4.5 x 3.0 degrees.
4. Options -> Plate Solving: configure ASTAP (any D-series star database: D05/D20/D50/D80 all work for fields >= 0.6 degrees).
5. **Gain in N.I.N.A. = ISO** (e.g. 3200). Binning is 1x1 only. There is no cooling/offset.
6. Install the plugin **Three Point Polar Alignment** (N.I.N.A. -> Plugins tab) and open it in the Imaging tab.
   Suggested start: exposure 3-5 s, gain 3200, binning 1x1. Raise the search radius if solving fails.

No camera at hand (indoor test)? Double-click `4b_start_server_SIMULATION.bat`: N.I.N.A. then sees a fake camera that delivers synthetic star fields.

## If something does not work

| Symptom | What to do |
|---|---|
| `2_check_camera.bat` says no camera | Camera on? USB mode MSC? GUI closed? Run via the .bat (needs admin). Still nothing: put the drive letter of the camera into `camera_device` (e.g. `"E"`). |
| N.I.N.A. does not list the camera | Is the server window open? Open http://localhost:11200/ in a browser. Allow Python in Windows Firewall. Press rescan. Plan B: ASCOM Diagnostics -> Choose Device -> Camera -> Chooser -> **Alpaca** menu -> Create an Alpaca driver, enter 127.0.0.1, port 11200, device 0; then pick that driver in N.I.N.A. |
| Exposure error / timeout after a minute | Mode dial M, focus MF, lens cap, battery, MSC mode. Details are in `pentax_alpaca.log` (start the server with `--verbose` for more). |
| Plate solve fails | Focus! 3-5 s exposure, ISO 3200-6400, focal length 300 in the telescope profile, bigger TPPA search radius, ASTAP database installed. |
| Star colours look odd after debayering | Set `bayer_pattern` in the config to RGGB / BGGR / GRBG / GBRG. (Plate solving is not affected.) |
| Each frame takes ~10 s longer than the exposure | Normal: pktriggercord reconnects and downloads the RAW over USB for every frame. |

## Good to know

- Exposure times are snapped to the camera's real shutter steps (1/3 EV: ..., 2, 2.5, 3, 4, 5, 6, 8, 10 ...). Maximum 30 s (no Bulb).
- Cancelling an exposure in N.I.N.A. returns control immediately, but a shot that already runs inside the camera finishes first (then it is discarded); the next exposure starts after it.
- The server is open to your local network (no password). Use it on your own network only.
- Log file: `pentax_alpaca.log` (next to the program). Status page: http://localhost:11200/
- Uninstall: close the server, delete the folder.
