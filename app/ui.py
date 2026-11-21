#!/usr/bin/env python3
"""PiSampler touchscreen UI (GTK 3, 800x480, runs full-screen inside `cage`)."""

import signal
import socket
import sys
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

CSS = b"""
* { font-family: "Inter", "DejaVu Sans", sans-serif; }
window { background: #111418; color: #e8eaed; }
#header { background: #1a1f26; padding: 6px 14px; border-bottom: 1px solid #2a313b; }
#title { font-size: 24px; font-weight: bold; }
#status { font-size: 15px; color: #9aa4b1; }
#status.error { color: #ff7b72; }
#midi { font-size: 15px; color: #6b7480; }
#midi.on { color: #56d364; }
list { background: #111418; }
row { padding: 0 16px; min-height: 68px; border-bottom: 1px solid #1f252d; }
row:selected, row:active { background: #111418; }
row .name { font-size: 22px; color: #e8eaed; }
row .tag { font-size: 13px; color: #6b7480; }
row.active { background: #1f6feb; }
row.active .name { color: #ffffff; font-weight: bold; }
row.active .tag { color: #cfe0ff; }
row.loading { background: #3d4b61; }
row.back { background: #171b21; }
row.back .name { color: #9aa4b1; font-size: 20px; }
#empty { font-size: 19px; color: #9aa4b1; padding: 30px; }
#footer { background: #1a1f26; padding: 6px 8px; border-top: 1px solid #2a313b; }
button { background: #2a313b; color: #e8eaed; border: none; border-radius: 10px;
         font-size: 18px; font-weight: bold; min-height: 52px; min-width: 64px;
         padding: 0 16px; box-shadow: none; text-shadow: none; background-image: none; }
button:active { background: #3d4b61; }
button#panic { background: #6e2b2b; }
button#panic:active { background: #a83a3a; }
#pager button { min-width: 72px; border-radius: 0; font-size: 26px; background: #171b21; }
#pager button:active { background: #2a313b; }
#pager button:disabled { color: #2a313b; }
#vol-label { font-size: 15px; color: #9aa4b1; }
scale trough { min-height: 14px; border-radius: 7px; background: #2a313b; border: none; }
scale highlight { background: #1f6feb; border-radius: 7px; border: none; }
scale slider { min-width: 40px; min-height: 40px; border-radius: 20px; background: #e8eaed;
               border: none; box-shadow: none; background-image: none; }
scrollbar { opacity: 0; }
"""

EMPTY_TEXT = ("No instruments yet.\n\nCopy SFZ folders or SF2 files into the “instruments” "
              "share on  {host}  from your computer,\nor plug in a USB stick that has an "
              "“instruments” folder, then tap Rescan.")


class Row(Gtk.ListBoxRow):
    """A list line. Exactly one of these is set: inst (a playable sound),
    folder (group_id of a folder to open), or neither (the Back line)."""

    def __init__(self, text, tag="", inst=None, folder=None, keys=()):
        super().__init__()
        self.inst, self.folder = inst, folder
        self.keys = frozenset(keys or ([inst.key] if inst else ()))    # sounds this row stands for
        box = Gtk.Box(spacing=12)
        name = Gtk.Label(label=text, xalign=0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.get_style_context().add_class("name")
        tag = Gtk.Label(label=tag)
        tag.get_style_context().add_class("tag")
        box.pack_start(name, True, True, 0)
        box.pack_end(tag, False, False, 0)
        self.add(box)
        if inst is None and folder is None:
            self.get_style_context().add_class("back")


def _tag(*parts):
    return " · ".join(p for p in parts if p)


class App(Gtk.Window):
    def __init__(self):
        super().__init__(title="PiSampler")
        self.set_default_size(800, 480)
        self._rows_for = None
        self._folder = None          # group_id of the folder being shown, None = top level
        self._snap = None
        self._volume_guard = False

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)

        # ---- header: active instrument + status
        header = Gtk.Box(spacing=12, name="header")
        self.spinner = Gtk.Spinner()
        self.title = Gtk.Label(label="PiSampler", xalign=0, name="title")
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.status = Gtk.Label(xalign=1, name="status")
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.set_max_width_chars(34)
        self.midi = Gtk.Label(name="midi")
        header.pack_start(self.spinner, False, False, 0)
        header.pack_start(self.title, True, True, 0)
        header.pack_end(self.midi, False, False, 0)
        header.pack_end(self.status, False, False, 0)
        root.pack_start(header, False, False, 0)

        # ---- middle: list + page buttons
        middle = Gtk.Box()
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.set_activate_on_single_click(True)
        self.listbox.connect("row-activated", self.on_row)
        empty = Gtk.Label(label=EMPTY_TEXT.format(host=socket.gethostname()), name="empty")
        empty.set_line_wrap(True)
        empty.set_justify(Gtk.Justification.CENTER)
        empty.show()
        self.listbox.set_placeholder(empty)
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_kinetic_scrolling(True)
        self.scroller.set_overlay_scrolling(True)
        self.scroller.add(self.listbox)
        middle.pack_start(self.scroller, True, True, 0)

        pager = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, name="pager")
        self.up, self.down = up, down = Gtk.Button(label="▲"), Gtk.Button(label="▼")
        up.connect("clicked", lambda *_: self.page(-1))
        down.connect("clicked", lambda *_: self.page(1))
        adj = self.scroller.get_vadjustment()
        adj.connect("changed", self.update_pager)
        adj.connect("value-changed", self.update_pager)
        pager.pack_start(up, True, True, 0)
        pager.pack_start(down, True, True, 0)
        middle.pack_end(pager, False, False, 0)
        root.pack_start(middle, True, True, 0)

        # ---- footer: rescan, panic, volume
        footer = Gtk.Box(spacing=10, name="footer")
        rescan = Gtk.Button(label="Rescan")
        rescan.connect("clicked", lambda *_: self.ctl.rescan())
        panic = Gtk.Button(label="Panic", name="panic")
        panic.connect("clicked", lambda *_: self.ctl.panic())
        vol_label = Gtk.Label(label="Volume", name="vol-label")
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume.set_draw_value(False)
        self.volume.connect("value-changed", self.on_volume)
        footer.pack_start(rescan, False, False, 0)
        footer.pack_start(panic, False, False, 0)
        footer.pack_start(vol_label, False, False, 8)
        footer.pack_start(self.volume, True, True, 8)
        root.pack_end(footer, False, False, 0)

        self.connect("realize", self.on_realize)
        self.connect("destroy", self.on_destroy)

        self.ctl = core.Controller(on_change=lambda snap: GLib.idle_add(self.render, snap))
        self.ctl.start()

    # ---- events
    def on_realize(self, *_):
        cursor = Gdk.Cursor.new_for_display(self.get_display(), Gdk.CursorType.BLANK_CURSOR)
        self.get_window().set_cursor(cursor)

    def on_destroy(self, *_):
        self.ctl.shutdown()
        Gtk.main_quit()

    def on_row(self, _listbox, row):
        if row.inst is not None:
            self.ctl.select(row.inst.key)
            return
        self._folder = row.folder            # open a folder, or None for Back
        if self._snap:
            self.render(self._snap)
        self.scroller.get_vadjustment().set_value(0)

    def build_rows(self, snap):
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        if self._folder and not any(i.group_id == self._folder for i in snap.instruments):
            self._folder = None              # the folder disappeared (USB stick pulled)
        if self._folder:
            inside = [i for i in snap.instruments if i.group_id == self._folder]
            self.listbox.add(Row("‹  " + inside[0].group, "Back"))
            for inst in inside:
                self.listbox.add(Row(inst.name, _tag(inst.kind.upper(), inst.source), inst=inst))
        else:
            seen = set()
            for inst in snap.instruments:
                if not inst.group_id:
                    self.listbox.add(Row(inst.name, _tag(inst.kind.upper(), inst.source), inst=inst))
                elif inst.group_id not in seen:
                    seen.add(inst.group_id)
                    inside = [i for i in snap.instruments if i.group_id == inst.group_id]
                    self.listbox.add(Row(inst.group, _tag(f"{len(inside)} sounds", inst.source) + "  ›",
                                         folder=inst.group_id, keys=[i.key for i in inside]))
        self.listbox.show_all()

    def on_volume(self, scale):
        if not self._volume_guard:
            self.ctl.set_volume(int(scale.get_value()))

    def update_pager(self, adj):
        # Arrows light up only when there is more list in that direction.
        self.up.set_sensitive(adj.get_value() > adj.get_lower() + 1)
        self.down.set_sensitive(adj.get_value() < adj.get_upper() - adj.get_page_size() - 1)

    def page(self, direction):
        adj = self.scroller.get_vadjustment()
        step = adj.get_page_size() * 0.85
        target = adj.get_value() + direction * step
        adj.set_value(max(adj.get_lower(), min(target, adj.get_upper() - adj.get_page_size())))

    # ---- drawing (always on the GTK thread)
    def render(self, snap):
        self._snap = snap
        shown = (snap.instruments, self._folder)
        if shown != self._rows_for:
            self.build_rows(snap)
            self._rows_for = (snap.instruments, self._folder)

        for row in self.listbox.get_children():
            ctx = row.get_style_context()
            is_active = snap.active in row.keys
            for cls, on in (("active", is_active and snap.status == "ready"),
                            ("loading", is_active and snap.status != "ready")):
                (ctx.add_class if on else ctx.remove_class)(cls)

        self.title.set_text(snap.active_name or "Choose an instrument")

        loading = snap.status == "loading"
        self.spinner.set_visible(loading)
        (self.spinner.start if loading else self.spinner.stop)()

        text = {"loading": "Loading…", "ready": "", "idle": ""}.get(snap.status, snap.message)
        self.status.set_text(text)
        sctx = self.status.get_style_context()
        (sctx.add_class if snap.status in ("error", "noaudio") else sctx.remove_class)("error")

        mctx = self.midi.get_style_context()
        if snap.midi:
            self.midi.set_text("● " + snap.midi[0][:18])
            mctx.add_class("on")
        else:
            self.midi.set_text("○ No piano")
            mctx.remove_class("on")

        if int(self.volume.get_value()) != snap.volume:
            self._volume_guard = True
            self.volume.set_value(snap.volume)
            self._volume_guard = False
        return False


def main():
    win = App()
    win.show_all()
    win.spinner.hide()
    win.update_pager(win.scroller.get_vadjustment())
    win.fullscreen()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, win.destroy)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, win.destroy)
    Gtk.main()


if __name__ == "__main__":
    main()
