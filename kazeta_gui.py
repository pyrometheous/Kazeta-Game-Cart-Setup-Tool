#!/usr/bin/env python3
# kazeta_gui.py — Kazeta Cart Maker GUI for Linux (Bazzite-friendly)
# - Search Steam by game name to get appid
# - Format/mount SD card (ext4), with safety prompt
# - Download Kazeta runtime (Linux or Windows) with SHA-256 verification
# - Fetch artwork (SteamGridDB optional; else Steam header.jpg; Pillow optional for PNG)
# - Write cart.kzi
# - Optional: copy your game files into content/
#
# Run:  python3 kazeta_gui.py
# Requires: Python 3 with Tkinter. Optional: Pillow (for image conversion).
#
# NOTE: Formatting/mounting requires admin rights. We attempt to run privileged
# commands via `pkexec` (GUI polkit auth). If not available, run the whole app with:
#   sudo -E python3 kazeta_gui.py
# (Use -E if you need STEAMGRIDDB_API_KEY in the environment.)

import os
import sys
import re
import json
import shutil
import hashlib
import subprocess
import threading
import queue
from tkinter import *
from tkinter import ttk, filedialog, messagebox
from urllib.request import urlopen, Request
from urllib.parse import quote

# ---------- Config Defaults ----------
DEFAULTS = {
    "linux": {
        "runtime_url": "https://runtimes.kazeta.org/linux-1.0.kzr",
        "runtime_sha256": "9edbd30da69a04770b2780f30e86b74d162279f0cfb1650faf383dd86301a1dc",
        "exec_hint": "cd content && ./<binary>"
    },
    "windows": {
        "runtime_url": "https://runtimes.kazeta.org/windows-1.0.kzr",
        "runtime_sha256": "9e9baca24ee10c042fb26d344ea9fd78d99321d2ec950624522431f032245cab",
        "exec_hint": "content/<Game.exe>"
    }
}
DEFAULT_MOUNT_BASE = "/mnt"
DEFAULT_ICON_SIZE = 64

STEAMGRIDDB_API_KEY = os.environ.get("STEAMGRIDDB_API_KEY", "").strip()

# Try to import Pillow for image conversion
try:
    from PIL import Image
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

# ---------- Helpers ----------

def run_cmd(cmd, need_root=False, log=None):
    """Run a command, optionally via pkexec if not root."""
    if need_root and os.geteuid() != 0:
        cmd = ["pkexec"] + cmd
    if log:
        log(f"$ {' '.join(cmd)}")
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True)
        if log and res.stdout:
            log(res.stdout.strip())
        return res.stdout
    except subprocess.CalledProcessError as e:
        if log:
            log(e.stdout or "")
            log(f"ERROR: command failed: {' '.join(cmd)} (code {e.returncode})")
        raise

def lsblk_devices():
    """Return list of disk devices via lsblk JSON."""
    try:
        out = subprocess.check_output(["lsblk", "-J", "-o", "NAME,RM,SIZE,TYPE,MOUNTPOINT,MODEL"], text=True)
        data = json.loads(out)
    except Exception:
        return []
    devs = []
    def walk(block):
        name = block.get("name")
        rm = block.get("rm")
        size = block.get("size")
        typ = block.get("type")
        mpt = block.get("mountpoint")
        model = block.get("model") or ""
        if typ == "disk":
            devs.append({
                "name": name,
                "path": f"/dev/{name}",
                "rm": rm,
                "size": size,
                "mountpoint": mpt,
                "model": model
            })
        for child in block.get("children", []) or []:
            walk(child)
    for blk in data.get("blockdevices", []):
        walk(blk)
    return devs

def slugify(s):
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "game"

def labelify(s):
    base = re.sub(r"[^A-Za-z0-9]+", "", s.upper())
    return (base[:11] or "GAME")  # ext4 label limit is 16; keep short

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def http_json(url, headers=None):
    req = Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))

def http_bytes(url, headers=None):
    req = Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=60) as r:
        return r.read()

def search_steam(term):
    # Steam community quick search API
    url = f"https://steamcommunity.com/actions/SearchApps/{quote(term)}"
    try:
        data = http_json(url)
        return data  # list of {"appid":..., "name":...}
    except Exception:
        # Fallback: store search
        url2 = f"https://store.steampowered.com/api/storesearch/?term={quote(term)}&cc=us&l=en"
        try:
            data2 = http_json(url2)
            items = data2.get("items", [])
            return [{"appid": it.get("id"), "name": it.get("name")} for it in items if it.get("id")]
        except Exception:
            return []

def find_mounts_for_device(devpath):
    out = subprocess.check_output(["lsblk", "-o", "NAME,MOUNTPOINT", "-nr", devpath], text=True)
    mps = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            mps.append(parts[1])
    return mps

def unmount_device(devpath, log):
    mps = find_mounts_for_device(devpath)
    for mp in mps:
        run_cmd(["umount", "-l", mp], need_root=True, log=log)
    part = devpath + "1"
    try:
        run_cmd(["umount", "-l", part], need_root=True, log=log)
    except Exception:
        pass

def partition_and_format(devpath, label, log):
    run_cmd(["wipefs", "-a", devpath], need_root=True, log=log)
    run_cmd(["parted", "-s", devpath, "mklabel", "gpt", "mkpart", "primary", "ext4", "1MiB", "100%"], need_root=True, log=log)
    try:
        run_cmd(["partprobe", devpath], need_root=True, log=log)
    except Exception:
        pass
    run_cmd(["udevadm", "settle"], need_root=True, log=log)
    run_cmd(["mkfs.ext4", "-F", "-L", label, devpath + "1"], need_root=True, log=log)

def mount_partition(devpath, label, mount_base, log):
    mp = os.path.join(mount_base, label)
    run_cmd(["mkdir", "-p", mp], need_root=True, log=log)
    unmount_device(devpath, log)
    run_cmd(["mount", devpath + "1", mp], need_root=True, log=log)
    run_cmd(["chown", "-R", f"{os.getuid()}:{os.getgid()}", mp], need_root=True, log=log)
    return mp

def download_runtime(dest_path, url, expected_sha, verify, log):
    log(f"Downloading runtime from {url} ...")
    data = http_bytes(url)
    with open(dest_path, "wb") as f:
        f.write(data)
    if verify and expected_sha:
        actual = sha256_file(dest_path)
        if actual.lower() != (expected_sha or "").lower():
            raise RuntimeError(f"Runtime SHA-256 mismatch! expected {expected_sha}, got {actual}")

def fetch_artwork(appid, icon_png_path, header_jpg_path, icon_size, log):
    # Try SteamGridDB first (if API key present)
    if STEAMGRIDDB_API_KEY and appid:
        try:
            hdrs = {"Authorization": f"Bearer {STEAMGRIDDB_API_KEY}"}
            url_game = f"https://www.steamgriddb.com/api/v2/games/steam/{appid}"
            j = http_json(url_game, headers=hdrs)
            gid = j["data"][0]["id"]
            url_icon = f"https://www.steamgriddb.com/api/v2/icons/game/{gid}"
            ji = http_json(url_icon, headers=hdrs)
            if ji.get("data"):
                icon_url = ji["data"][0]["url"]
                log(f"Downloading icon from SteamGridDB: {icon_url}")
                data = http_bytes(icon_url)
                with open(icon_png_path, "wb") as f:
                    f.write(data)
                return True
        except Exception as e:
            log(f"SteamGridDB fetch failed: {e}")
    # Fallback to Steam header
    if appid:
        try:
            header_url = f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg"
            log(f"Downloading Steam header: {header_url}")
            data = http_bytes(header_url)
            with open(header_jpg_path, "wb") as f:
                f.write(data)
            if PIL_AVAILABLE:
                try:
                    im = Image.open(header_jpg_path).convert("RGBA")
                    im = im.resize((icon_size, icon_size), Image.BICUBIC)
                    im.save(icon_png_path, format="PNG")
                    return True
                except Exception as e:
                    log(f"Pillow conversion failed: {e}")
        except Exception as e:
            log(f"Steam header download failed: {e}")
    # Placeholder PNG if nothing else worked
    tiny_png = b'iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAIElEQVR4nGNgYGBg+P//PwMDA8N/GBgYGEAGBgaGgQEAAPk8B2LhB6oWAAAAAElFTkSuQmCC'
    with open(icon_png_path, "wb") as f:
        f.write(__import__("base64").b64decode(tiny_png))
    return True

def find_exe_under(path, runtime):
    candidates = []
    if runtime == "windows":
        for root, dirs, files in os.walk(path):
            for fn in files:
                if fn.lower().endswith(".exe"):
                    candidates.append(os.path.relpath(os.path.join(root, fn), path))
    else:
        # Linux: heuristics — executable files without extension (prefer top-level)
        for root, dirs, files in os.walk(path):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), path)
                full = os.path.join(path, rel)
                if "." not in fn and os.access(full, os.X_OK):
                    candidates.append(rel)
        # As fallback, include files in top-level even if not executable, no extension
        if not candidates:
            for fn in os.listdir(path):
                full = os.path.join(path, fn)
                if os.path.isfile(full) and "." not in fn:
                    candidates.append(fn)
    # prefer shallow paths and shorter names
    candidates.sort(key=lambda p: (p.count(os.sep), len(os.path.basename(p))))
    return candidates

def copy_tree(src, dst, log, progress=None):
    if not os.path.isdir(src):
        raise RuntimeError(f"Source dir not found: {src}")
    os.makedirs(dst, exist_ok=True)
    total = 0
    for root, dirs, files in os.walk(src):
        for f in files:
            total += 1
    copied = 0
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_dir = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(target_dir, exist_ok=True)
        for f in files:
            s = os.path.join(root, f)
            d = os.path.join(target_dir, f)
            shutil.copy2(s, d)
            copied += 1
            if progress:
                progress(min(100, int(copied * 100 / max(1, total))))

# ---------- GUI ----------

class App(Tk):
    def __init__(self):
        super().__init__()
        self.title("Kazeta Cart Maker")
        self.geometry("920x720")

        self.queue = queue.Queue()
        self.create_widgets()
        self.after(100, self.process_queue)

        self.refresh_devices()

    def log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.update_idletasks()

    def process_queue(self):
        try:
            while True:
                item = self.queue.get_nowait()
                if isinstance(item, str):
                    self.log(item)
                elif isinstance(item, tuple) and item[0] == "progress":
                    self.progress["value"] = item[1]
        except queue.Empty:
            pass
        self.after(100, self.process_queue)

    def qlog(self, msg):
        self.queue.put(msg)

    def set_progress(self, pct):
        self.queue.put(("progress", pct))

    def create_widgets(self):
        pad = 6

        # --- Search ---
        frm_search = ttk.LabelFrame(self, text="Search Steam")
        frm_search.pack(fill="x", padx=pad, pady=pad)

        self.search_var = StringVar()
        ttk.Label(frm_search, text="Title:").pack(side="left", padx=(pad,2), pady=pad)
        ttk.Entry(frm_search, textvariable=self.search_var, width=40).pack(side="left", padx=(0, pad), pady=pad)
        ttk.Button(frm_search, text="Search", command=self.on_search).pack(side="left", padx=(0,pad), pady=pad)
        self.results = ttk.Treeview(frm_search, columns=("appid","name"), show="headings", height=5)
        self.results.heading("appid", text="AppID")
        self.results.heading("name", text="Name")
        self.results.column("appid", width=100)
        self.results.column("name", width=560)
        self.results.pack(fill="x", padx=pad, pady=(0,pad))
        ttk.Button(frm_search, text="Use Selected", command=self.on_use_selected).pack(side="right", padx=pad, pady=(0,pad))

        # --- Game meta ---
        frm_meta = ttk.LabelFrame(self, text="Game Details & Card Settings")
        frm_meta.pack(fill="x", padx=pad, pady=pad)

        self.appid_var = StringVar()
        self.name_var = StringVar()
        self.id_var = StringVar()
        self.exe_var = StringVar()
        self.label_var = StringVar()
        self.source_var = StringVar()
        self.mount_base_var = StringVar(value=DEFAULT_MOUNT_BASE)
        self.device_var = StringVar()
        self.no_format_var = BooleanVar(value=False)
        self.verify_runtime_var = BooleanVar(value=True)
        self.eject_var = BooleanVar(value=False)

        # Runtime selection
        self.runtime_var = StringVar(value="windows")
        self.runtime_url_var = StringVar(value=DEFAULTS["windows"]["runtime_url"])
        self.runtime_sha_var = StringVar(value=DEFAULTS["windows"]["runtime_sha256"])

        row = 0
        grid = ttk.Frame(frm_meta); grid.pack(fill="x", padx=pad, pady=pad)

        def add_row(label, widget):
            nonlocal row
            ttk.Label(grid, text=label).grid(row=row, column=0, sticky="e", padx=(0,8), pady=3)
            widget.grid(row=row, column=1, sticky="we", pady=3)
            row += 1

        grid.columnconfigure(1, weight=1)

        add_row("Steam AppID:", ttk.Entry(grid, textvariable=self.appid_var))
        add_row("Name (KZI):", ttk.Entry(grid, textvariable=self.name_var))
        add_row("ID slug (KZI):", ttk.Entry(grid, textvariable=self.id_var))
        add_row("Executable (inside content/):", ttk.Entry(grid, textvariable=self.exe_var))
        add_row("SD Label:", ttk.Entry(grid, textvariable=self.label_var))

        # Source dir chooser
        frm_src = ttk.Frame(grid)
        ttk.Entry(frm_src, textvariable=self.source_var).pack(side="left", fill="x", expand=True)
        ttk.Button(frm_src, text="Browse…", command=self.on_browse_source).pack(side="left", padx=(6,0))
        add_row("Windows/Linux game source dir:", frm_src)

        # Runtime radios
        frm_rt = ttk.Frame(grid)
        def on_rt_change(*args):
            rt = self.runtime_var.get()
            self.runtime_url_var.set(DEFAULTS[rt]["runtime_url"])
            self.runtime_sha_var.set(DEFAULTS[rt]["runtime_sha256"])
        self.runtime_var.trace_add("write", lambda *a: on_rt_change())
        ttk.Radiobutton(frm_rt, text="Windows runtime", variable=self.runtime_var, value="windows").pack(side="left", padx=(0,10))
        ttk.Radiobutton(frm_rt, text="Linux runtime", variable=self.runtime_var, value="linux").pack(side="left")
        add_row("Runtime:", frm_rt)

        add_row("Runtime URL:", ttk.Entry(grid, textvariable=self.runtime_url_var))
        add_row("Runtime SHA-256:", ttk.Entry(grid, textvariable=self.runtime_sha_var))

        # Device dropdown
        frm_dev = ttk.Frame(grid)
        self.device_combo = ttk.Combobox(frm_dev, textvariable=self.device_var, width=34, state="readonly")
        self.device_combo.pack(side="left")
        ttk.Button(frm_dev, text="Refresh", command=self.refresh_devices).pack(side="left", padx=(6,0))
        add_row("Target device:", frm_dev)

        add_row("Mount base:", ttk.Entry(grid, textvariable=self.mount_base_var))

        # Flags
        frm_flags = ttk.Frame(frm_meta); frm_flags.pack(fill="x", padx=pad, pady=(0,pad))
        ttk.Checkbutton(frm_flags, text="Skip formatting (use existing ext4)", variable=self.no_format_var).pack(side="left", padx=(0,20))
        ttk.Checkbutton(frm_flags, text="Verify runtime hash", variable=self.verify_runtime_var).pack(side="left", padx=(0,20))
        ttk.Checkbutton(frm_flags, text="Eject (unmount) when done", variable=self.eject_var).pack(side="left")

        # Actions
        frm_act = ttk.Frame(self); frm_act.pack(fill="x", padx=pad, pady=pad)
        ttk.Button(frm_act, text="Build Kazeta Cart", command=self.on_build).pack(side="left")
        self.progress = ttk.Progressbar(frm_act, mode="determinate", length=260)
        self.progress.pack(side="left", padx=(10,0))

        # Log
        frm_log = ttk.LabelFrame(self, text="Log"); frm_log.pack(fill="both", expand=True, padx=pad, pady=pad)
        self.log_text = Text(frm_log, height=16, state="disabled")
        self.log_text.pack(fill="both", expand=True)

        # Hint row
        self.hint = StringVar(value=f"Exec hint: {DEFAULTS[self.runtime_var.get()]['exec_hint']}")
        lbl_hint = ttk.Label(self, textvariable=self.hint, foreground="#666")
        lbl_hint.pack(fill="x", padx=pad, pady=(0,pad))
        self.runtime_var.trace_add("write", lambda *a: self.hint.set(f"Exec hint: {DEFAULTS[self.runtime_var.get()]['exec_hint']}"))

    def refresh_devices(self):
        devs = lsblk_devices()
        items = []
        for d in devs:
            label = f"{d['path']}  [{d['size']}]  RM={d['rm']}  {d['model']}"
            items.append(label)
        self.device_combo["values"] = items
        if items:
            self.device_combo.current(0)
            self.device_var.set(items[0])

    def on_search(self):
        term = self.search_var.get().strip()
        if not term:
            messagebox.showerror("Error", "Enter a game title to search.")
            return
        self.results.delete(*self.results.get_children())
        try:
            res = search_steam(term)
        except Exception as e:
            messagebox.showerror("Error", f"Search failed: {e}")
            return
        for r in res[:100]:
            appid = r.get("appid")
            name = r.get("name")
            if appid and name:
                self.results.insert("", "end", values=(appid, name))

    def on_use_selected(self):
        sel = self.results.selection()
        if not sel:
            return
        appid, name = self.results.item(sel[0], "values")
        self.appid_var.set(str(appid))
        self.name_var.set(name)
        self.id_var.set(slugify(name))
        self.label_var.set(labelify(name))

    def on_browse_source(self):
        d = filedialog.askdirectory(title="Select game source directory")
        if d:
            self.source_var.set(d)

    def on_build(self):
        device_label = self.device_var.get().strip()
        if not device_label:
            messagebox.showerror("Error", "Pick a target device from the dropdown.")
            return
        devpath = device_label.split()[0]
        appid = self.appid_var.get().strip()
        name = self.name_var.get().strip()
        slug = self.id_var.get().strip()
        exe = self.exe_var.get().strip()
        label = self.label_var.get().strip()
        src = self.source_var.get().strip()
        mount_base = self.mount_base_var.get().strip() or DEFAULT_MOUNT_BASE

        runtime = self.runtime_var.get()
        runtime_url = self.runtime_url_var.get().strip() or DEFAULTS[runtime]["runtime_url"]
        runtime_sha = self.runtime_sha_var.get().strip() or DEFAULTS[runtime]["runtime_sha256"]

        verify_runtime = self.verify_runtime_var.get()
        no_format = self.no_format_var.get()
        eject = self.eject_var.get()

        if not (appid and name and slug and exe and label):
            messagebox.showerror("Error", "AppID, Name, ID slug, EXE, and SD Label are required.")
            return

        if not no_format:
            resp = messagebox.askquestion("FORMAT WARNING",
                f"This will ERASE all data on {devpath} and create an ext4 partition labeled {label}.\n\nContinue?",
                icon="warning")
            if resp != "yes":
                return

        threading.Thread(target=self.worker, args=(devpath, appid, name, slug, exe, label, src, mount_base, runtime, runtime_url, runtime_sha, verify_runtime, no_format, eject), daemon=True).start()

    def worker(self, devpath, appid, name, slug, exe, label, src, mount_base, runtime, runtime_url, runtime_sha, verify_runtime, no_format, eject):
        log = self.qlog
        setp = self.set_progress
        setp(0)
        udisks_was_active = False

        try:
            if not no_format:
                try:
                    out = subprocess.run(["systemctl", "is-active", "--quiet", "udisks2"])
                    if out.returncode == 0:
                        udisks_was_active = True
                        log("Stopping udisks2 …")
                        run_cmd(["systemctl", "stop", "udisks2"], need_root=True, log=log)
                except Exception:
                    pass

            log("Unmounting any existing mounts …")
            unmount_device(devpath, log)

            if not no_format:
                log("Wiping & partitioning device …")
                partition_and_format(devpath, label, log)

            log("Mounting partition …")
            mount_point = mount_partition(devpath, label, mount_base, log)
            log(f"Mounted at: {mount_point}")
            setp(10)

            content_dir = os.path.join(mount_point, "content")
            kazeta_dir = os.path.join(mount_point, "kazeta")
            os.makedirs(content_dir, exist_ok=True)
            os.makedirs(kazeta_dir, exist_ok=True)

            # Download runtime (Linux or Windows) and verify
            runtime_out = os.path.join(mount_point, os.path.basename(runtime_url))
            download_runtime(runtime_out, runtime_url, runtime_sha, verify_runtime, log)
            log("Runtime downloaded and verified." if verify_runtime else "Runtime downloaded (verification skipped).")
            setp(20)

            # Artwork
            icon_png = os.path.join(kazeta_dir, "icon.png")
            header_jpg = os.path.join(kazeta_dir, "header.jpg")
            fetch_artwork(appid, icon_png, header_jpg, DEFAULT_ICON_SIZE, log)
            setp(30)

            # Copy game files (optional)
            if src:
                log(f"Copying game files from {src} …")
                copy_tree(src, content_dir, log, progress=lambda p: setp(30 + int(p*0.5)))
                setp(80)
            else:
                log("No source dir provided; you can copy files later to content/.")

            # Verify / guess executable
            if runtime == "windows":
                exe_path = os.path.join(content_dir, exe)
                if not os.path.exists(exe_path):
                    log(f"Executable not found at content/{exe}. Searching for .exe files …")
                    candidates = find_exe_under(content_dir, runtime)
                    if candidates:
                        exe = candidates[0].replace("\\", "/")
                        log(f"Guessed executable: content/{exe}")
                    else:
                        log("WARNING: No .exe found under content/. You'll need to set Exec manually later.")
                exec_line = f"content/{exe}"
            else:
                # Linux: ensure executable bit if file exists
                exe_path = os.path.join(content_dir, exe)
                if os.path.exists(exe_path):
                    try:
                        st = os.stat(exe_path)
                        os.chmod(exe_path, st.st_mode | stat.S_IXUSR)
                    except Exception as e:
                        log(f"Could not chmod +x on {exe}: {e}")
                else:
                    log(f"Executable not found at content/{exe}. Searching for Linux binaries …")
                    candidates = find_exe_under(content_dir, runtime)
                    if candidates:
                        exe = candidates[0].replace("\\", "/")
                        log(f"Guessed executable: content/{exe}")
                        # try to make it executable
                        try:
                            st = os.stat(os.path.join(content_dir, exe))
                            os.chmod(os.path.join(content_dir, exe), st.st_mode | stat.S_IXUSR)
                        except Exception as e:
                            log(f"Could not chmod +x on {exe}: {e}")
                    else:
                        log("WARNING: No likely Linux binary found under content/. You may need to adjust later.")
                # KZI Linux Exec per docs: cd content && ./Binary
                exec_line = f"cd content && ./{exe}"

            # Write cart.kzi
            kzi_path = os.path.join(mount_point, "cart.kzi")
            with open(kzi_path, "w", encoding="utf-8") as f:
                f.write(f"Name={name}\nId={slug}\nExec={exec_line}\nIcon=kazeta/icon.png\nRuntime={runtime}\n")
            log(f"Wrote {kzi_path}")
            setp(90)

            try:
                os.sync()
            except Exception:
                pass

            if eject:
                log("Unmounting card …")
                run_cmd(["umount", "-l", mount_point], need_root=True, log=log)
                log("Card unmounted.")

            log("Done. Kazeta cart ready.")
            setp(100)

        except Exception as e:
            log(f"ERROR: {e}")
        finally:
            if udisks_was_active:
                try:
                    run_cmd(["systemctl", "start", "udisks2"], need_root=True, log=log)
                except Exception:
                    pass

if __name__ == "__main__":
    App().mainloop()
