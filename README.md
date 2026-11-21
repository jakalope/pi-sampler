# PiSampler

Turns a Raspberry Pi 5 into a simple sound module for a digital piano: pick an
instrument on the touchscreen, play it from the piano, hear it through the DAC.

**Hardware this is set up for:** Raspberry Pi 5 · InnoMaker HiFi DAC HAT (PCM5122) ·
Waveshare 4.3" DSI touchscreen (800×480) · a piano connected by USB.

---

## 1. Flash the SD card

1. On your Mac or PC, install **Raspberry Pi Imager** (raspberrypi.com/software).
2. Choose **Raspberry Pi 5** → **Raspberry Pi OS (other)** → **Raspberry Pi OS Lite (64-bit)** → your SD card.
3. When it asks to customise settings, choose **Edit settings** and set:
   - Hostname: `pisampler`
   - A username and password (remember them)
   - Your Wi-Fi name and password (skip if using a network cable)
   - On the *Services* tab: **Enable SSH**, with password authentication
4. Write the card, put it in the Pi, and switch the Pi on. Wait about two minutes.

## 2. Run the installer

On your Mac, open **Terminal** in the folder that contains `pi-sampler`
(on Windows use PowerShell). Replace `USER` with the username you chose.

```bash
scp -r pi-sampler USER@pisampler.local:
```

```bash
ssh USER@pisampler.local
```

Then, on the Pi:

```bash
cd pi-sampler && sudo ./install.sh
```

This takes 15–20 minutes (it builds the SFZ engine). When it finishes:

```bash
sudo reboot
```

After the restart the instrument list appears on the touchscreen by itself.

## 3. Add instruments

**Over the network (easiest).** No password is needed.

- Mac: Finder → Go → Connect to Server → `smb://pisampler.local/instruments` → connect as **Guest**
- Windows: type `\\pisampler\instruments` into the File Explorer address bar

Drag files in, wait for the copy to finish, then tap **Rescan** on the touchscreen.

- **SFZ library:** copy the *whole folder* (the `.sfz` file together with its samples).
  Each library gets its own folder. If the download was a `.zip`, unzip it first.
- **SF2 / SF3 soundfont:** copy the single file straight into `instruments`.

**From a USB stick.** Make a folder called `instruments` on the stick, put libraries in it
the same way, and plug it into the Pi. They appear in the list marked **USB** (the list
refreshes itself). Instruments play straight from the stick, so leave it plugged in while
you use them. Use a blue USB 3 port for big pianos.

### Good free libraries to start with

| Library | What it is | Get it from | Put it in |
|---|---|---|---|
| **Salamander Grand Piano** | Yamaha C5 grand, 16 velocity layers, under 1 GB. The best free piano. | github.com/sfzinstruments/SalamanderGrandPiano (green **Code** button → Download ZIP) | `instruments/Salamander/` (whole folder) |
| **VSCO 2 Community Edition** | Orchestra: strings, brass, woodwinds, percussion, harp, organ. About 2.5 GB. | github.com/sgossner/VSCO-2-CE (green **Code** button → Download ZIP) | `instruments/VSCO/` (whole folder) |
| **GeneralUser GS** | One 30 MB SF2 file with a bit of everything: electric pianos, organs, strings, guitars. | schristiancollins.com/generaluser.php | `instruments/GeneralUser-GS.sf2` |

A file or library that holds several sounds shows as a **folder** (for example
the VSCO orchestra, or a General MIDI soundfont). Tap it to see the sounds inside;
the top line takes you back.

## 4. Using it

- **Tap an instrument** to load it (rows ending in **›** are folders: tap to open). It turns grey while loading and blue when it's ready to play.
- **▲ ▼** page through the list; you can also drag it with a finger.
- **Rescan** looks for new instruments. **Panic** silences stuck notes. **Volume** sets the output level.
- Top right shows **● piano name** when the piano is connected, **○ No piano** when not.
- **Loudness is matched automatically.** The first time an instrument is loaded it shows "Matching loudness…" for a few seconds while it is measured (silently); after that every instrument plays at the same level.
- The screen dims after 20 seconds without a touch. The first touch only brightens it again; it doesn't press anything.
- The last instrument is loaded again automatically when the Pi starts.
- To switch off, simply unplug it (instrument files are only read, never written, while playing).

## 5. Troubleshooting

First step for any problem: connect with `ssh USER@pisampler.local` and run

```bash
pisampler-doctor
```

It checks the DAC, screen, touch, piano and software in order and marks what is wrong.

**No sound**
- Is an instrument blue in the list, and the Volume slider up? Is the amplifier/headphone lead in the DAC HAT, not the Pi?
- Run `pisampler-doctor tone`. You should hear a 5-second test tone.
- If the doctor says *no DAC card*, the DAC driver setting doesn't suit this board. Try the alternatives one at a time, rebooting after each:
  ```bash
  sudo DAC_OVERLAY=allo-boss-dac-pcm512x-audio,slave ./install.sh
  ```
  ```bash
  sudo DAC_OVERLAY=hifiberry-dacplus-std ./install.sh
  ```
- Sound plays at the wrong pitch or speed: same fix, use `hifiberry-dacplus-std`.

**Crackles or dropouts**
- Edit the settings file with `sudo nano /etc/default/pisampler`, change `PISAMPLER_NPERIODS=2` to `3`, save, reboot. If still crackling, set `PISAMPLER_PERIOD=256`.
- Distortion only on big, loud chords: lower the loudness target in the same file, e.g. `PISAMPLER_NORMALIZE_DB=-15`, and turn your amplifier up instead.
- Use the official 27 W Pi 5 power supply and a fast SD card (A2 class).

**Piano not detected ("○ No piano")**
- Turn the piano on; use the piano's **USB TO HOST** socket (the square one), not "USB to Device".
- Unplug and replug the cable; it reconnects within two seconds, in any order of switching on.
- Some Yamahas have a setting that must be on: *Function → MIDI → Local/USB* or similar.

**Sustain pedal does nothing** – the pedal must be plugged into the piano; it is sent over USB with the notes. A few SF2 files ignore the pedal; try another instrument to check.

**Touch not working / screen black**
- Check the flat cable is fully seated at both ends.
- If it is plugged into the Pi's other display socket, try `sudo DSI_OVERLAY=vc4-kms-dsi-7inch,dsi0 ./install.sh` and reboot.
- Picture but no touch: reseat the cable; touch travels over the same cable.

**Network share is slow, or the Pi drops off the network** – the Wi-Fi signal at the Pi is weak. Move it or the router closer, or use a network cable; a cable is also much faster for multi-GB libraries.

**An instrument shows an error** – the SFZ file probably can't find its samples: copy the whole library folder again, keeping its internal layout.

## How it's put together (for the curious)

- `pisampler-jack` – JACK2 audio server on the DAC at 48 kHz, 128-frame buffer × 2 (about 5 ms).
- `pisampler-midi` – `a2jmidid`, which makes the USB piano appear in JACK and copes with hot-plugging.
- `pisampler-engine` – the sound engine: **sfizz** for SFZ or **FluidSynth** for SF2/SF3. The app starts and stops it.
- `pisampler-gain` – a tiny gain stage (`src/pisampler-gain.c`) between engine and DAC. Each instrument's loudest single note is scaled to −12 dB below full scale, leaving room for chords.
- `pisampler-ui` – the touchscreen app (`/opt/pisampler`, Python + GTK) inside the `cage` kiosk compositor.
  It wires piano → engine → DAC and re-checks the wiring every half second.
- **CPU split.** The Pi 5 has four cores. Cores 2–3 are a kernel cpuset partition (`pisampler.slice`)
  used only by the first three services above. The operating system, network, file sharing,
  the touchscreen app and all hardware interrupts are kept on cores 0–1, so nothing else can
  interrupt the sound. `pisampler-doctor` shows the split in section 5.
- Settings (buffer size, voices, loudness target, screen dimming) are in `/etc/default/pisampler`.
- Everything restarts automatically if it crashes. Logs: `journalctl -u pisampler-ui -u pisampler-jack -u pisampler-engine -b`.
- To undo the boot changes: restore `/boot/firmware/config.txt.pisampler-backup` and `cmdline.txt.pisampler-backup`.
