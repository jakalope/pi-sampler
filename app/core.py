"""PiSampler core: instrument scanning, engine control, JACK wiring.

Nothing in here touches the GUI, so it can be tested without a screen.
The Controller runs one background thread; the UI sends it commands and
receives immutable Snapshot objects through a callback.
"""

from __future__ import annotations

import json
import os
import queue
import re
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

HOME = Path.home()
INSTRUMENT_DIR = Path(os.environ.get("PISAMPLER_INSTRUMENTS", HOME / "instruments"))
USB_ROOT = Path(os.environ.get("PISAMPLER_USB_ROOT", "/media/usb"))
STATE_FILE = Path(os.environ.get("PISAMPLER_STATE", HOME / ".config/pisampler/state.json"))
RUN_DIR = Path(os.environ.get("PISAMPLER_RUN_DIR", "/run/pisampler"))   # tmpfs, owned by our user
ENGINE_LOG = RUN_DIR / "engine.log"
ENGINE_CMD = RUN_DIR / "engine.cmd"
ENGINE_UNIT = "pisampler-engine.service"
GAIN_CLIENT = "pisampler-out"        # the gain stage (src/pisampler-gain.c)
GAIN_FIFO = RUN_DIR / "gain"
LOUDNESS_FILE = STATE_FILE.parent / "loudness.json"
# Loudness matching: every instrument is scaled so that its loudest single note
# peaks at this level (dB below digital full scale). The gap is the room left
# for many notes sounding together. "off" disables it.
NORMALIZE_DB = os.environ.get("PISAMPLER_NORMALIZE_DB", "-12").strip().lower()

ENGINE_CLIENT = "pisampler-engine"   # JACK client name used by whichever engine runs
SFZ_VOICES = os.environ.get("PISAMPLER_SFZ_VOICES", "128")
SFZ_PRELOAD = os.environ.get("PISAMPLER_SFZ_PRELOAD", "8192")
SF2_POLYPHONY = os.environ.get("PISAMPLER_SF2_POLYPHONY", "256")
SF2_GAIN = os.environ.get("PISAMPLER_SF2_GAIN", "0.7")
SAMPLE_RATE = "48000"

SOUNDFONT_EXT = {".sf2", ".sf3"}
MAX_DEPTH = 4            # how deep to look for .sfz files inside a library folder
MAX_RETRIES = 3          # automatic engine restarts before giving up


# ----------------------------------------------------------------- scanning

@dataclass(frozen=True)
class Instrument:
    name: str        # what the row says
    path: str
    kind: str        # "sfz" or "sf2"
    source: str      # "" for the Pi's own folder, "USB" for a stick
    group: str = ""      # folder title when the file/library holds several sounds
    group_id: str = ""   # what identifies that folder (a path)
    bank: int = -1       # SF2 preset to select (-1: engine default)
    prog: int = -1

    @property
    def key(self) -> str:
        """Unique id of one playable sound (remembered as the last instrument)."""
        return self.path if not self.group or self.bank < 0 else f"{self.path}#{self.bank}:{self.prog}"

    @property
    def title(self) -> str:
        return f"{self.group} · {self.name}" if self.group else self.name


def _pretty(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("_", " ")).strip()


def _visible(name: str) -> bool:
    # Skips dotfiles and the "._name" files macOS leaves on network/USB drives.
    return not name.startswith(".") and name != "__MACOSX"


def usb_instrument_dirs() -> list[Path]:
    found = []
    try:
        mounts = sorted(p for p in USB_ROOT.iterdir() if p.is_dir())
    except OSError:
        return found
    for mount in mounts:
        try:
            for child in sorted(mount.iterdir()):
                if child.is_dir() and child.name.lower() == "instruments":
                    found.append(child)
        except OSError:
            continue
    return found


def _files_below(folder: Path) -> list[Path]:
    out = []
    base_depth = len(folder.parts)
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if _visible(d))
        if len(Path(dirpath).parts) - base_depth >= MAX_DEPTH:
            dirnames[:] = []
        for fn in sorted(filenames):
            if _visible(fn):
                out.append(Path(dirpath) / fn)
    return out


_sf2_cache: dict = {}


def sf2_presets(path: Path) -> list[tuple[int, int, str]]:
    """(bank, program, name) of every sound inside an SF2/SF3 file, read from
    its preset table without touching the sample data."""
    try:
        st = path.stat()
        tag = (str(path), st.st_mtime_ns, st.st_size)
        if tag in _sf2_cache:
            return _sf2_cache[tag]
        presets: list[tuple[int, int, str]] = []
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:4] == b"RIFF" and head[8:12] == b"sfbk":
                end = 8 + struct.unpack("<I", head[4:8])[0]
                while f.tell() < end and not presets:
                    h = f.read(8)
                    if len(h) < 8:
                        break
                    size = struct.unpack("<I", h[4:])[0]
                    if h[:4] != b"LIST" or f.read(4) != b"pdta":
                        f.seek(size - (4 if h[:4] == b"LIST" else 0) + (size & 1), 1)
                        continue
                    stop = f.tell() + size - 4
                    while f.tell() < stop:
                        h = f.read(8)
                        if len(h) < 8:
                            break
                        size = struct.unpack("<I", h[4:])[0]
                        if h[:4] != b"phdr":
                            f.seek(size + (size & 1), 1)
                            continue
                        data = f.read(size)
                        for i in range(0, len(data) - 38, 38):      # last record is a terminator
                            name = data[i:i + 20].split(b"\0")[0].decode("latin-1").strip()
                            prog, bank = struct.unpack("<HH", data[i + 20:i + 24])
                            presets.append((bank, prog, _pretty(name) or f"Preset {bank}-{prog}"))
                        break
        presets.sort()
        _sf2_cache[tag] = presets
        return presets
    except (OSError, struct.error):
        return []


def _sf2_items(f: Path, title: str, source: str, group: str = "", group_id: str = "") -> list[Instrument]:
    """One soundfont file -> one row, or one row per sound inside a folder."""
    presets = sf2_presets(f)
    if len(presets) <= 1:
        bank, prog = presets[0][:2] if presets else (-1, -1)
        return [Instrument(title, str(f), "sf2", source, group, group_id, bank, prog)]
    if group:                                   # already inside a library folder: stay flat
        return [Instrument(f"{title} · {n}", str(f), "sf2", source, group, group_id, b, p)
                for b, p, n in presets]
    return [Instrument(n, str(f), "sf2", source, title, str(f), b, p) for b, p, n in presets]


def _scan_root(root: Path, source: str) -> list[Instrument]:
    items: list[Instrument] = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return items
    for entry in entries:
        if not _visible(entry.name):
            continue
        if entry.is_file():
            if entry.suffix.lower() in SOUNDFONT_EXT:
                items += _sf2_items(entry, _pretty(entry.stem), source)
            elif entry.suffix.lower() == ".sfz":
                items.append(Instrument(_pretty(entry.stem), str(entry), "sfz", source))
        elif entry.is_dir():
            playable = [f for f in _files_below(entry)
                        if f.suffix.lower() == ".sfz" or f.suffix.lower() in SOUNDFONT_EXT]
            lib = _pretty(entry.name)
            if len(playable) == 1:              # the usual case: one library, one sound
                f = playable[0]
                if f.suffix.lower() == ".sfz":
                    items.append(Instrument(lib, str(f), "sfz", source))
                else:
                    items += _sf2_items(f, lib, source)
                continue
            for f in playable:                  # several sounds: shown as a folder
                if f.suffix.lower() == ".sfz":
                    items.append(Instrument(_pretty(f.stem), str(f), "sfz", source, lib, str(entry)))
                else:
                    items += _sf2_items(f, _pretty(f.stem), source, lib, str(entry))
    return items


def scan() -> list[Instrument]:
    items = _scan_root(INSTRUMENT_DIR, "")
    for d in usb_instrument_dirs():
        items += _scan_root(d, "USB")
    items.sort(key=lambda i: (i.group or i.name).lower())    # stable: order inside a folder is kept
    return items


# -------------------------------------------------------------------- state

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


# ------------------------------------------------------------------- volume

def _dac_card() -> str | None:
    """Index of the DAC's ALSA card: has playback, is not HDMI, is not USB
    (the piano itself appears as a USB sound card)."""
    for d in sorted(Path("/proc/asound").glob("card[0-9]*")):
        try:
            if (d / "usbid").exists() or not (d / "pcm0p").is_dir():
                continue
            if "hdmi" in (d / "id").read_text().lower():
                continue
            return d.name[4:]
        except OSError:
            continue
    return None


def set_volume(percent: int) -> None:
    """Sets the PCM5122's own 'Digital' volume, so it works for every engine."""
    card = _dac_card()
    if card is None:
        return
    try:
        subprocess.run(["amixer", "-q", "-M", "-c", card, "sset", "Digital", f"{percent}%"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.SubprocessError):
        pass


# --------------------------------------------------------------------- JACK

class JackDown(Exception):
    pass


class JackLink:
    """Keeps piano -> engine -> DAC connected. Safe to call repeatedly."""

    def __init__(self):
        self._client = None

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _get(self):
        if self._client is None:
            try:
                import jack
                self._client = jack.Client("pisampler-ctl", no_start_server=True)
            except Exception as e:
                raise JackDown(str(e))
        return self._client

    def sync(self, open_engine: bool = True) -> tuple[bool, list[str], bool]:
        """Make all wanted connections.

        The engine is only wired to the piano and the output when open_engine is
        true (it is kept apart while its loudness is being measured).
        Returns (engine_ready, midi_device_names, gain_stage_present)."""
        c = self._get()
        try:
            playback = c.get_ports(is_audio=True, is_input=True, is_physical=True)
            if not playback:
                raise JackDown("no playback ports")
            prefix = "^" + re.escape(ENGINE_CLIENT) + ":"
            eng_out = c.get_ports(prefix, is_audio=True, is_output=True)
            eng_midi = c.get_ports(prefix, is_midi=True, is_input=True)
            gprefix = "^" + re.escape(GAIN_CLIENT) + ":"
            gain_in = c.get_ports(gprefix, is_audio=True, is_input=True)
            gain_out = c.get_ports(gprefix, is_audio=True, is_output=True)
            has_gain = len(gain_in) >= 2 and len(gain_out) >= 2
            sources = [p for p in c.get_ports(is_midi=True, is_output=True)
                       if not p.name.startswith(("pisampler", ENGINE_CLIENT))
                       and "through" not in p.name.lower()]
            if has_gain:
                for src, dst in zip(gain_out, playback):
                    self._connect(c, src, dst)
            if open_engine:
                dest = gain_in if has_gain else playback
                for src, dst in zip(eng_out, dest):
                    self._connect(c, src, dst)
                if len(eng_out) == 1 and len(dest) > 1:      # mono engine -> both sides
                    self._connect(c, eng_out[0], dest[1])
                for src in sources:
                    for dst in eng_midi:
                        self._connect(c, src, dst)
            return (bool(eng_out and eng_midi),
                    sorted({_device_name(p.name) for p in sources}), has_gain)
        except JackDown:
            self.close()
            raise
        except Exception as e:
            self.close()
            raise JackDown(str(e))

    def measure_peak(self, aborted=lambda: False) -> float | None:
        """Play the loudest note (velocity 127) on keys across the whole range,
        with the engine heard only by us, and return the highest sample peak."""
        try:
            import jack
            c = jack.Client("pisampler-measure", no_start_server=True)
        except Exception:
            return None
        try:
            out = c.midi_outports.register("out")
            ins = [c.inports.register("in_1"), c.inports.register("in_2")]
            pending: list[bytes] = []
            peak = [0.0]

            @c.set_process_callback
            def _process(frames):
                out.clear_buffer()
                while pending:
                    try:
                        out.write_midi_event(0, pending[0])
                    except Exception:
                        break
                    pending.pop(0)
                for port in ins:
                    buf = memoryview(port.get_buffer()).cast("f")
                    if len(buf):
                        peak[0] = max(peak[0], max(buf), -min(buf))

            prefix = "^" + re.escape(ENGINE_CLIENT) + ":"
            eng_midi = c.get_ports(prefix, is_midi=True, is_input=True)
            eng_out = c.get_ports(prefix, is_audio=True, is_output=True)
            if not eng_midi or not eng_out:
                return None
            c.activate()
            c.connect(out, eng_midi[0])
            for src, dst in zip(eng_out, ins):
                c.connect(src, dst)
            time.sleep(0.1)
            for note in range(24, 109, 4):                   # C1 .. C8, every major third
                if aborted():
                    return None
                pending.append(bytes((0x90, note, 127)))
                time.sleep(0.25)
                pending.append(bytes((0x80, note, 0)))
                pending.append(bytes((0xB0, 120, 0)))         # all sound off: no overlapping tails
                time.sleep(0.04)
            pending.append(bytes((0xB0, 123, 0)))
            time.sleep(0.15)
            c.deactivate()
            return peak[0]
        except Exception:
            return None
        finally:
            try:
                c.close()
            except Exception:
                pass

    @staticmethod
    def _connect(c, src, dst):
        try:
            if any(p.name == dst.name for p in c.get_all_connections(src)):
                return
            c.connect(src, dst)
        except Exception:
            pass    # port vanished or already connected; retried on next sync

    def panic(self) -> None:
        """Sustain off, all sound off, all notes off on every channel."""
        try:
            import jack
            c = jack.Client("pisampler-panic", no_start_server=True)
        except Exception:
            return
        try:
            out = c.midi_outports.register("out")
            pending = [bytes((0xB0 | ch, cc, 0)) for ch in range(16) for cc in (64, 120, 123)]
            done = threading.Event()

            @c.set_process_callback
            def _process(frames):
                out.clear_buffer()
                for _ in range(6):               # a few per cycle: MIDI buffers are small
                    if not pending:
                        done.set()
                        return
                    try:
                        out.write_midi_event(0, pending[0])
                    except Exception:
                        return
                    pending.pop(0)

            targets = c.get_ports("^" + re.escape(ENGINE_CLIENT) + ":", is_midi=True, is_input=True)
            if not targets:
                return
            c.activate()
            for t in targets:
                try:
                    c.connect(out, t)
                except Exception:
                    pass
            done.wait(1.0)
            time.sleep(0.05)
            c.deactivate()
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass


def _device_name(port: str) -> str:
    # a2jmidid names ports like "a2j:Digital Piano [20] (capture): Digital Piano MIDI 1"
    m = re.match(r"a2j:(.+?) (?:\[\d+\] )?\((?:capture|playback)\)", port)
    return m.group(1) if m else port.split(":", 1)[-1]


# ----------------------------------------------------------------- loudness

def normalize_target() -> float | None:
    """Wanted peak of the loudest single note as a linear level, or None if off."""
    try:
        return 10 ** (float(NORMALIZE_DB) / 20)
    except ValueError:
        return None


def set_gain(gain: float) -> bool:
    try:
        fd = os.open(GAIN_FIFO, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, f"{gain:.5f}\n".encode())
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def _file_tag(inst: "Instrument") -> list:
    try:
        st = os.stat(inst.path)
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return []


def cached_peak(inst: "Instrument") -> float | None:
    try:
        entry = json.loads(LOUDNESS_FILE.read_text()).get(inst.key)
    except (OSError, ValueError):
        return None
    if entry and entry.get("file") == _file_tag(inst):
        return float(entry["peak"])
    return None


def remember_peak(inst: "Instrument", peak: float) -> None:
    try:
        data = json.loads(LOUDNESS_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    data[inst.key] = {"peak": round(peak, 6), "file": _file_tag(inst)}
    try:
        LOUDNESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LOUDNESS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, LOUDNESS_FILE)
    except OSError:
        pass


# ------------------------------------------------------------------- engine

def engine_command(inst: Instrument) -> list[str]:
    if inst.kind == "sfz":
        # stdbuf: sfizz_jack block-buffers its output when piped, and we watch
        # the log for "Instrument loaded:" to know when loading has finished.
        return ["stdbuf", "-oL", "sfizz_jack", f"--client_name={ENGINE_CLIENT}",
                f"--num_voices={SFZ_VOICES}", f"--preload_size={SFZ_PRELOAD}", inst.path]
    return ["fluidsynth", "-a", "jack", "-m", "jack", "-r", SAMPLE_RATE, "-g", SF2_GAIN,
            "-o", f"audio.jack.id={ENGINE_CLIENT}",
            "-o", f"midi.jack.id={ENGINE_CLIENT}",
            "-o", "audio.jack.autoconnect=0",
            "-o", "midi.autoconnect=0",
            "-o", f"synth.polyphony={SF2_POLYPHONY}",
            "-o", "synth.reverb.active=0",
            "-o", "synth.chorus.active=0",
            inst.path]


class Engine:
    """The synth runs as its own systemd service (pisampler-engine) so that it
    lives in the realtime CPU partition, away from this UI and the OS. We hand
    it a command line through a file and start/stop the unit."""

    def __init__(self):
        self.started = False

    @staticmethod
    def _ctl(verb: str) -> None:
        subprocess.run(["sudo", "-n", "/usr/bin/systemctl", verb, ENGINE_UNIT],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)

    @staticmethod
    def _state() -> str:
        try:
            return subprocess.run(["systemctl", "is-active", ENGINE_UNIT], text=True,
                                  capture_output=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    def start(self, inst: Instrument) -> None:
        tmp = ENGINE_CMD.with_suffix(".tmp")
        cmd = {"argv": engine_command(inst)}
        if inst.kind == "sf2" and inst.bank >= 0:
            cmd["preset"] = [inst.bank, inst.prog]
        tmp.write_text(json.dumps(cmd))
        os.replace(tmp, ENGINE_CMD)
        self._ctl("restart")
        self.started = True

    def alive(self) -> bool:
        return self.started and self._state() in ("active", "activating", "unknown")

    def stop(self) -> None:
        was, self.started = self.started, False
        if was or self._state() in ("active", "activating", "deactivating"):
            try:
                self._ctl("stop")
            except (OSError, subprocess.SubprocessError):
                pass

    @staticmethod
    def log_text() -> str:
        try:
            return ENGINE_LOG.read_text(errors="replace")
        except OSError:
            return ""

    @staticmethod
    def last_log_line() -> str:
        lines = [l.strip() for l in Engine.log_text().splitlines() if l.strip()]
        return lines[-1][:120] if lines else ""


# --------------------------------------------------------------- controller

@dataclass(frozen=True)
class Snapshot:
    instruments: tuple = ()
    active: str = ""            # key of the selected instrument
    active_name: str = ""
    status: str = "idle"        # idle | loading | ready | error | noaudio
    message: str = ""
    midi: tuple = ()            # names of connected MIDI devices
    volume: int = 85


class Controller:
    def __init__(self, on_change=lambda snap: None):
        self.on_change = on_change
        self.snap = Snapshot()
        self._q: queue.Queue = queue.Queue()
        self._engine = Engine()
        self._jack = JackLink()
        self._wanted: Instrument | None = None
        self._failures = 0
        self._ready_since = 0.0
        self._ports_since = 0.0
        self._open = True            # engine wired to piano and output?
        self._usb_seen: tuple = ()
        self._thread = threading.Thread(target=self._run, daemon=True)

    # ---- called from the UI thread
    def start(self):
        self._thread.start()

    def select(self, key: str):
        self._q.put(("select", key))

    def rescan(self):
        self._q.put(("rescan", None))

    def panic(self):
        self._q.put(("panic", None))

    def set_volume(self, percent: int):
        self._q.put(("volume", int(percent)))

    def shutdown(self):
        self._q.put(("quit", None))
        self._thread.join(5)

    # ---- worker thread
    def _set(self, **changes):
        new = replace(self.snap, **changes)
        if new != self.snap:
            self.snap = new
            self.on_change(new)

    def _rescan(self):
        self._usb_seen = tuple(str(p) for p in usb_instrument_dirs())
        self._set(instruments=tuple(scan()))

    def _find(self, key: str) -> Instrument | None:
        return next((i for i in self.snap.instruments if i.key == key), None)

    def _launch(self):
        inst = self._wanted
        if inst is None:
            return
        self._ready_since = 0.0
        self._ports_since = 0.0
        self._open = normalize_target() is None      # stay closed until loudness is matched
        self._set(active=inst.key, active_name=inst.title, status="loading", message="")
        try:
            self._engine.start(inst)
        except (OSError, subprocess.SubprocessError) as e:
            self._failures = MAX_RETRIES
            self._set(status="error", message=f"Cannot start engine: {e}")

    def _run(self):
        state = load_state()
        volume = int(state.get("volume", 85))
        set_volume(volume)
        self._set(volume=volume)
        self._rescan()
        last = self._find(state.get("last", ""))
        if last:
            self._wanted = last
            self._set(active=last.key, active_name=last.title, status="loading")
        pending_volume_save = 0.0
        if not last:
            self._engine.stop()          # nothing to play: clear any engine left running

        while True:
            try:
                cmd, arg = self._q.get(timeout=0.5)
            except queue.Empty:
                cmd, arg = None, None

            if cmd == "quit":
                self._engine.stop()
                self._jack.close()
                return
            if cmd == "select":
                inst = self._find(arg)
                if inst:
                    self._wanted = inst
                    self._failures = 0
                    save_state({"last": inst.key, "volume": self.snap.volume})
                    self._launch()
            elif cmd == "rescan":
                self._rescan()
            elif cmd == "panic":
                self._jack.panic()
            elif cmd == "volume":
                while True:                      # only the newest slider position matters
                    try:
                        nxt = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt[0] == "volume":
                        arg = nxt[1]
                    else:
                        self._q.put(nxt)
                        break
                set_volume(arg)
                self._set(volume=arg)
                pending_volume_save = time.monotonic() + 2

            if pending_volume_save and time.monotonic() > pending_volume_save:
                pending_volume_save = 0.0
                save_state({"last": self._wanted.key if self._wanted else "",
                            "volume": self.snap.volume})

            self._tick()

    def _match_loudness(self) -> bool:
        """Set the output gain for the loaded instrument. False if interrupted."""
        inst, target = self._wanted, normalize_target()
        peak = cached_peak(inst)
        if peak is None:
            self._set(status="loading", message="Matching loudness…")
            peak = self._jack.measure_peak(aborted=lambda: not self._q.empty())
            if peak is None:
                if not self._q.empty():
                    return False
                peak = 0.0                   # could not measure: leave the level alone
            else:
                remember_peak(inst, peak)
        gain = min(32.0, target / peak) if peak > 0.001 else 1.0
        set_gain(gain)
        return True

    def _tick(self):
        # USB stick plugged in or removed -> refresh the list by itself.
        usb_now = tuple(str(p) for p in usb_instrument_dirs())
        if usb_now != self._usb_seen:
            self._rescan()

        try:
            ready, midi, has_gain = self._jack.sync(self._open)
        except JackDown:
            # An engine cannot survive without its audio server; drop it now and
            # it is started again as soon as JACK is back.
            self._engine.stop()
            self._set(status="noaudio", message="Waiting for the audio system…", midi=())
            return
        self._set(midi=tuple(midi))

        if self._wanted is None:
            if self.snap.status == "noaudio":
                self._set(status="idle", message="")
            return

        if not self._engine.alive():
            if self.snap.status == "error" and self._failures >= MAX_RETRIES:
                return
            if self._engine.started:                     # it died on us
                self._failures += 1
                self._engine.stop()
                if self._failures >= MAX_RETRIES:
                    detail = Engine.last_log_line()
                    self._set(status="error",
                              message="Could not load this instrument"
                                      + (f": {detail}" if detail else ""))
                    return
            if not os.path.exists(self._wanted.path):
                self._set(status="error", message="Instrument file is missing")
                self._failures = MAX_RETRIES
                return
            self._launch()
            return

        if ready and self._wanted.kind == "sfz" and self.snap.status == "loading":
            # sfizz_jack opens its ports before the samples are in; wait for its
            # own "loaded" line (or give up waiting after 30 s and assume it is).
            log = Engine.log_text()
            if "Could not load" in log or "Can't load" in log:
                self._engine.stop()
                self._failures = MAX_RETRIES
                self._set(status="error", message="sfizz could not read this SFZ file")
                return
            self._ports_since = self._ports_since or time.monotonic()
            if "Instrument loaded" not in log and time.monotonic() - self._ports_since < 30:
                return

        if ready and not self._open:
            if has_gain and not self._match_loudness():
                return                       # interrupted by a new command; it takes over
            self._open = True
            try:
                self._jack.sync(True)
            except JackDown:
                return

        if ready:
            if self.snap.status != "ready":
                self._ready_since = time.monotonic()
                self._set(status="ready", message="")
            elif self._failures and time.monotonic() - self._ready_since > 20:
                self._failures = 0
        elif self.snap.status != "loading":
            self._set(status="loading", message="")
