#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["tidalapi>=0.8.11", "mutagen>=1.47", "requests", "rich>=13"]
# ///
"""Download Tidal playlists/albums as AAC m4a and push them to a Garmin watch over USB (MTP/gvfs).

  tidal2garmin.py                             # status: login, library, watches on USB
  tidal2garmin.py list [-s]                   # music on the watch, per playlist
  tidal2garmin.py login
  tidal2garmin.py playlists                   # list your playlists with IDs
  tidal2garmin.py pull <name|id|url> [...]    # download into the local library
  tidal2garmin.py pull --all                  # re-pull every known playlist (refresh)
  tidal2garmin.py drop <name>                 # forget a playlist locally
  tidal2garmin.py push [--dry-run] [--eject]  # mirror library to the watch's Music/ folder
  tidal2garmin.py eject                       # unmount the watch before unplugging
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
import tidalapi
from mutagen.mp4 import MP4, MP4Cover
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn


def progress() -> Progress:
    return Progress(
        TextColumn("  {task.description}"), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn(),
        TextColumn("[dim]{task.fields[item]}"),
        console=out,
    )


out = Console(highlight=False, soft_wrap=True)


def head(title: str) -> None:
    out.print()
    out.print(f"[bold]{title}[/]")


def kv(label: str, value: str) -> None:
    out.print(f"  [dim]{label:<9}[/] {value}")


def short(path: Path) -> str:
    """~ for home; for the gvfs mount just the part inside the watch."""
    try:
        return "watch:/" + str(path.relative_to(GVFS)).split("/", 1)[1]
    except (ValueError, IndexError):
        return str(path).replace(str(Path.home()), "~", 1)


def usage_bar(used: int, total: int, width: int = 24) -> str:
    frac = used / total if total else 0
    n = round(frac * width)
    color = "red" if frac > 0.9 else "yellow" if frac > 0.75 else "green"
    return f"[{color}]{'━' * n}[/][dim]{'━' * (width - n)}[/] {frac:.0%} used"

LIB = Path(os.environ.get("TIDAL2GARMIN_LIB", Path.home() / "Music" / "tidal-garmin"))
TRACKS = LIB / "tracks"
STATE = LIB / "state.json"
SESSION = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tidal2garmin" / "session.json"
OURS = re.compile(r"^T\d+\.m4a$")  # only files matching this are ever deleted on the watch


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"playlists": {}, "pushed_m3u": []}


def save_state(st: dict) -> None:
    LIB.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False))
    tmp.replace(STATE)


def session(interactive: bool = False) -> tidalapi.Session:
    s = tidalapi.Session()
    s.audio_quality = tidalapi.Quality.low_320k  # AAC 320k m4a: plays natively on Garmin
    SESSION.parent.mkdir(parents=True, exist_ok=True)
    if SESSION.exists():
        s.load_session_from_file(SESSION)
    if not s.check_login():
        if not interactive:
            sys.exit("Not logged in. Run: tidal2garmin.py login")
        s.login_oauth_simple()
        if not s.check_login():
            sys.exit("Login failed.")
    s.save_session_to_file(SESSION)
    SESSION.chmod(0o600)
    return s


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', " ", name).strip()
    return re.sub(r"\s+", " ", name)[:60] or "playlist"


UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def my_playlists(s: tidalapi.Session) -> list:
    """Playlists the user created or favorited (the endpoint pages by 50)."""
    out, offset = [], 0
    while True:
        page = s.user.playlist_and_favorite_playlists(offset=offset)
        out += page
        if len(page) < 50:
            return out
        offset += 50


def lookup(s: tidalapi.Session, ref: str) -> tuple[str, str]:
    """Map a URL, UUID, album ID or playlist name to (kind, id)."""
    if m := re.search(r"(playlist|album|mix)/([0-9a-zA-Z-]+)", ref):
        return m.group(1), m.group(2)
    if UUID.match(ref):
        return "playlist", ref
    if ref.isdigit():
        return "album", ref
    # A name we pulled before: reuse its stored source (works for albums/mixes too).
    for name, v in load_state()["playlists"].items():
        if name.casefold() == ref.casefold():
            kind, _, oid = v["ref"].partition("/")
            return kind, oid
    # Otherwise search your Tidal playlists: exact name first, then substring.
    pls = my_playlists(s)
    hits = [p for p in pls if p.name.casefold() == ref.casefold()] or \
           [p for p in pls if ref.casefold() in p.name.casefold()]
    if len(hits) == 1:
        return "playlist", hits[0].id
    if not hits:
        sys.exit(f"No playlist matching '{ref}'. See: tidal2garmin.py playlists")
    sys.exit(f"'{ref}' is ambiguous:\n" + "\n".join(f"  {p.id}  {p.name}" for p in hits))


def resolve(s: tidalapi.Session, ref: str):
    """Return (kind, id, display_name, tracks) for a URL, ID or playlist name."""
    kind, oid = lookup(s, ref)
    if kind == "playlist":
        p = s.playlist(oid)
        return kind, oid, p.name, p.tracks_paginated()
    if kind == "album":
        a = s.album(oid)
        return kind, oid, f"{a.artist.name} - {a.name}", a.tracks()
    mx = s.mix(oid)
    return kind, oid, mx.title, [t for t in mx.items() if isinstance(t, tidalapi.Track)]


def download_track(s: tidalapi.Session, t: tidalapi.Track, dest: Path) -> str | None:
    """Download one track to dest. Returns None on success, else a reason string."""
    try:
        stream = t.get_stream()
        man = stream.get_stream_manifest()
    except Exception as e:  # unavailable in region, removed, etc.
        return f"no stream ({e.__class__.__name__})"
    if man.is_encrypted:
        return "encrypted stream, skipped"
    if man.get_codecs() not in ("MP4A", "AAC") and "mp4a" not in str(man.get_codecs()).lower():
        return f"unexpected codec {man.get_codecs()}"

    # BTS (what HIGH delivers today) is a single, plain m4a: no remux needed.
    # DASH would be fragmented MP4 segments; only then do we need ffmpeg to remux.
    dash = len(man.get_urls()) > 1
    if dash and not shutil.which("ffmpeg"):
        return "DASH stream needs ffmpeg (not installed)"

    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "raw.m4a"
        with requests.Session() as http, raw.open("wb") as f:
            for url in man.get_urls():
                r = http.get(url, timeout=60)
                r.raise_for_status()
                f.write(r.content)
        out = raw
        if dash:
            out = Path(td) / "out.m4a"
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-i", str(raw), "-map", "0:a",
                 "-c", "copy", "-movflags", "+faststart", str(out)],
                check=True,
            )
        tag(t, out)
        shutil.move(out, dest)
    return None


def tag(t: tidalapi.Track, path: Path) -> None:
    a = MP4(path)
    a["\xa9nam"] = t.full_name if hasattr(t, "full_name") and t.full_name else t.name
    a["\xa9ART"] = ", ".join(x.name for x in (t.artists or [t.artist]))
    if t.album:
        a["\xa9alb"] = t.album.name
        a["aART"] = t.album.artist.name if t.album.artist else a["\xa9ART"][0]
        if t.album.release_date:
            a["\xa9day"] = str(t.album.release_date.year)
        try:
            img = requests.get(t.album.image(640), timeout=30)
            if img.ok:
                a["covr"] = [MP4Cover(img.content, MP4Cover.FORMAT_JPEG)]
        except Exception:
            pass
    if t.track_num:
        a["trkn"] = [(t.track_num, 0)]
    if t.volume_num:
        a["disk"] = [(t.volume_num, 0)]
    a["\xa9gen"] = "Tidal"
    a.save()


def cmd_login(_):
    s = session(interactive=True)
    head("Tidal")
    kv("user", str(s.user.id))
    kv("session", short(SESSION))
    out.print()


def cmd_playlists(_):
    s = session()
    pls = my_playlists(s)
    head(f"Your playlists ({len(pls)})")
    for p in pls:
        out.print(f"  [dim]{p.id}[/]  {p.num_tracks:5d}  {p.name}")
    out.print()


def cmd_pull(args):
    s = session()
    st = load_state()
    refs = args.refs or []
    if args.all:
        refs += [v["ref"] for v in st["playlists"].values()]
    if not refs:
        sys.exit("Nothing to pull. Give a URL/ID or --all.")
    TRACKS.mkdir(parents=True, exist_ok=True)
    out.print()
    for ref in refs:
        kind, oid, name, tracks = resolve(s, ref)
        name = safe_name(name)
        ids, new, skipped = [], 0, 0
        with progress() as bar:
            task = bar.add_task(f"{name} ({kind})", total=len(tracks), item="")
            for t in tracks:
                dest = TRACKS / f"T{t.id}.m4a"
                label = f"{t.artist.name if t.artist else '?'} - {t.name}"
                bar.update(task, item=label)
                if dest.exists():
                    ids.append(t.id)
                elif err := download_track(s, t, dest):
                    bar.console.print(f"  [yellow]skip[/] {label} [dim]({err})[/]")
                    skipped += 1
                else:
                    ids.append(t.id)
                    new += 1
                bar.advance(task)
            bar.update(task, item="")
        out.print(f"  [green]✓[/] [bold]{name}[/]: {len(ids)} tracks "
                  f"[dim]({new} new, {skipped} skipped)[/]")
        # Drop any other entry that pointed at the same source (e.g. playlist renamed).
        for k in [k for k, v in st["playlists"].items() if v["ref"] == f"{kind}/{oid}" and k != name]:
            del st["playlists"][k]
        st["playlists"][name] = {"ref": f"{kind}/{oid}", "tracks": ids}
        save_state(st)
    out.print()
    kv("library", short(LIB))
    out.print()


def cmd_drop(args):
    st = load_state()
    if st["playlists"].pop(args.name, None) is None:
        sys.exit(f"Unknown playlist. Known: {', '.join(st['playlists'])}")
    save_state(st)
    out.print(f"\n  Dropped [bold]{args.name}[/]. Run [bold]push[/] to remove it from the watch.\n")


GVFS = Path(f"/run/user/{os.getuid()}/gvfs")
TRACK_REF = re.compile(r"T(\d+)\.m4a", re.I)  # matches our bare names and the watch's "0:/MUSIC/T1.M4A"


def usb_garmins() -> list[dict]:
    """Garmin devices on the USB bus, with the local process holding each one (if any)."""
    out = subprocess.run(["lsusb"], capture_output=True, text=True).stdout
    devs = []
    for m in re.finditer(r"Bus (\d+) Device (\d+): ID 091e:([0-9a-f]{4}) ?(.*)", out):
        node = f"/dev/bus/usb/{m.group(1)}/{m.group(2)}"
        devs.append({"node": node, "pid": m.group(3), "name": m.group(4).strip(), "holder": holder(node)})
    return devs


def holder(node: str) -> str | None:
    """Name of one of our own processes that has the USB device node open."""
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                if os.readlink(fd) == node:
                    return (proc / "comm").read_text().strip()
        except OSError:
            continue
    return None


def mounted_music() -> Path | None:
    for host in sorted(GVFS.glob("mtp:host=091e_*")) if GVFS.exists() else []:
        try:
            for storage in host.iterdir():
                if (storage / "Music").is_dir():
                    return storage / "Music"
        except OSError:  # stale mount after the watch re-enumerated
            continue
    return None


def find_watch_music(release_kde: bool = False) -> Path:
    """Locate (mounting if needed) the Garmin's Music folder via gvfs, or exit with a hint."""
    import time

    if (p := mounted_music()):
        return p
    devs = usb_garmins()
    if not devs:
        sys.exit("No Garmin on USB. Check the cable/clip (lsusb should show 091e:...).")
    kde = any(d["holder"] == "kiod6" for d in devs)
    if kde and release_kde:
        out.print("  [yellow]Stopping KDE kiod6 so gvfs can claim the watch (KDE restarts it on demand)...[/]")
        subprocess.run(["pkill", "-x", "kiod6"], check=False)
        time.sleep(1.5)
    out = subprocess.run(["gio", "mount", "-li"], capture_output=True, text=True).stdout
    for uri in sorted(set(re.findall(r"mtp://091e[^\s]*", out))):
        subprocess.run(["gio", "mount", uri], check=False)
    if (p := mounted_music()):
        return p
    if kde and not release_kde:
        sys.exit("Watch is held by KDE (kiod6). Retry with --release-kde")
    if not any(d["pid"] != "0003" for d in devs):
        sys.exit("Watch is in a transitional USB mode (091e:0003). Wait a few seconds or replug.")
    sys.exit("Garmin on USB but not mountable via gvfs (gvfs-backends / gvfs-fuse installed?).")


def track_label(tid: str) -> str:
    """'Artist - Title' from the local library copy, else the bare filename."""
    f = TRACKS / f"T{tid}.m4a"
    try:
        tags = MP4(f).tags or {}
        return f"{tags.get('©ART', ['?'])[0]} - {tags.get('©nam', ['?'])[0]}"
    except Exception:
        return f.name


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1000 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000


def space(music: Path) -> tuple[int, int]:
    """(free, total) bytes on the watch storage holding Music/."""
    v = os.statvfs(music)
    return v.f_bavail * v.f_frsize, v.f_blocks * v.f_frsize


SPACE_MARGIN = 200_000_000  # keep some room for activities, maps and firmware updates


def plan(st: dict, music: Path) -> dict:
    """What a push would do, and how many bytes it needs."""
    want = {f"T{i}.m4a" for v in st["playlists"].values() for i in v["tracks"]}
    have = {p.name for p in music.iterdir() if OURS.match(p.name)}
    missing = sorted(f for f in want - have if (TRACKS / f).exists())
    stale = sorted(have - want)
    add = sum((TRACKS / f).stat().st_size for f in missing)
    freed = sum((music / f).stat().st_size for f in stale)
    free, total = space(music)
    return {"missing": missing, "stale": stale, "add": add, "freed": freed,
            "free": free, "total": total, "after": free + freed - add}


def cmd_status(_):
    st = load_state()
    n = len({i for v in st["playlists"].values() for i in v["tracks"]})
    head("Tidal")
    # Offline check only (session file present), no network call.
    kv("login", "[green]session stored[/]" if SESSION.exists() else "[red]not logged in[/] (run: login)")
    kv("library", f"{short(LIB)} [dim]·[/] {len(st['playlists'])} playlists [dim]·[/] {n} tracks")
    if st["playlists"]:
        kv("lists", ", ".join(st["playlists"]))

    devs = usb_garmins()
    head("Watch")
    if not devs:
        kv("usb", "[yellow]no Garmin found[/] (check the cable, lsusb | grep 091e)")
        out.print()
        return
    for d in devs:
        mode = "[yellow]transitional, wait or replug[/]" if d["pid"] == "0003" else "MTP"
        held = {None: "free", "gvfsd-mtp": "[green]ok[/] (gvfs)"}.get(d["holder"], f"[yellow]held by {d['holder']}[/]")
        if d["holder"] == "kiod6":
            held = "[yellow]held by KDE kiod6[/] (use --release-kde)"
        kv("usb", f"{d['name'] or 'Garmin'} [dim]091e:{d['pid']} {d['node']}[/]")
        kv("state", f"{mode} [dim]·[/] {held}")
    music = mounted_music()
    if not music:
        kv("mount", "[dim]not mounted (push/list will try)[/]")
        out.print()
        return
    on = [p.name for p in music.iterdir()]
    ours = [x for x in on if OURS.match(x)]
    m3u = [x[:-4] for x in on if x.lower().endswith(".m3u")]
    pl = plan(st, music)
    used = pl["total"] - pl["free"]
    kv("music", f"{len(ours)} synced tracks [dim]·[/] {len(on) - len(ours) - len(m3u)} other files "
                f"[dim]·[/] playlists: {', '.join(m3u) or '-'}")
    kv("space", f"{human(pl['free'])} free of {human(pl['total'])}  {usage_bar(used, pl['total'])}")

    head("Next push")
    if not (pl["missing"] or pl["stale"]):
        kv("sync", "[green]in sync[/]")
    else:
        kv("copy", f"{len(pl['missing'])} tracks [dim]({human(pl['add'])})[/]")
        kv("delete", f"{len(pl['stale'])} tracks [dim]({human(pl['freed'])})[/]")
        kv("after", f"{human(pl['after'])} free")
        if pl["after"] < SPACE_MARGIN:
            kv("", "[red]not enough space on the watch, drop a playlist first[/]")
    out.print()


def cmd_list(args):
    music = find_watch_music(args.release_kde)
    files = sorted(p.name for p in music.iterdir())
    listed = set()
    if args.short:
        head("Playlists on the watch")
    for m3u in [f for f in files if f.lower().endswith(".m3u")]:
        ids = TRACK_REF.findall((music / m3u).read_text(errors="replace"))
        listed.update(f"T{tid}.m4a" for tid in ids)
        if args.short:
            out.print(f"  {len(ids):5d}  {m3u[:-4]}")
            continue
        head(f"{m3u[:-4]} [dim]({len(ids)} tracks)[/]")
        for n, tid in enumerate(ids, 1):
            out.print(f"  [dim]{n:3d}[/]  {track_label(tid)}")
    loose = [f for f in files if not f.lower().endswith(".m3u") and f not in listed]
    if loose:
        head(f"Not in any playlist [dim]({len(loose)})[/]")
        for f in loose:
            m = OURS.match(f) and TRACK_REF.match(f)
            out.print(f"       {track_label(m.group(1)) if m else f}")
    out.print()


def cmd_push(args):
    st = load_state()
    music = find_watch_music(args.release_kde)
    pl = plan(st, music)
    missing, stale = pl["missing"], pl["stale"]
    m3us = {f"{name}.m3u": [f"T{i}.m4a" for i in v["tracks"]] for name, v in st["playlists"].items()}
    old_m3u = [m for m in st.get("pushed_m3u", []) if m not in m3us]

    head("Push" + (" [dim](dry run)[/]" if args.dry_run else ""))
    kv("watch", short(music))
    kv("copy", f"{len(missing)} tracks [dim]({human(pl['add'])})[/]")
    kv("delete", f"{len(stale)} tracks [dim]({human(pl['freed'])})[/]")
    kv("lists", f"write {len(m3us)}, delete {len(old_m3u)}")
    kv("space", f"{human(pl['free'])} free now, {human(pl['after'])} after [dim](of {human(pl['total'])})[/]")
    out.print()
    if pl["after"] < SPACE_MARGIN:
        sys.exit(f"Not enough space: need {human(pl['add'] - pl['freed'] + SPACE_MARGIN - pl['free'])} more "
                 f"(keeping {human(SPACE_MARGIN)} reserve). Drop a playlist or delete other music.")
    if args.dry_run:
        return

    def gio(*a):
        subprocess.run(["gio", *a], check=True)

    for f in stale:
        gio("remove", str(music / f))
    for m in old_m3u:
        if (music / m).exists():
            gio("remove", str(music / m))
    if missing:
        with progress() as bar:
            task = bar.add_task("copy to watch", total=len(missing), item="")
            for f in missing:
                bar.update(task, item=track_label(TRACK_REF.match(f).group(1)))
                gio("copy", str(TRACKS / f), str(music / f))
                bar.advance(task)
            bar.update(task, item="")
    with tempfile.TemporaryDirectory() as td:
        for m, files in m3us.items():
            src = Path(td) / m
            # Garmin wants a bare m3u: no #EXTM3U header, paths relative to the playlist.
            src.write_text("\n".join(files) + "\n")
            # gvfs-mtp can't reliably overwrite in place: delete, then copy.
            if (music / m).exists():
                gio("remove", str(music / m))
            gio("copy", str(src), str(music / m))
    st["pushed_m3u"] = sorted(m3us)
    save_state(st)
    if args.eject:
        out.print("  [green]✓ Done.[/]")
        eject()
    else:
        out.print("  [green]✓ Done.[/] Run [bold]eject[/] before unplugging the watch.\n")


def mounted_uris() -> list[str]:
    """gvfs MTP URIs of mounted Garmin devices (mtp:host=091e_5117_SERIAL -> mtp://091e_5117_SERIAL/)."""
    if not GVFS.exists():
        return []
    return [f"mtp://{h.name.split('=', 1)[1]}/" for h in sorted(GVFS.glob("mtp:host=091e_*"))]


def eject() -> None:
    uris = mounted_uris()
    if not uris:
        out.print("  [dim]No mounted watch, nothing to eject.[/]\n")
        return
    for uri in uris:
        r = subprocess.run(["gio", "mount", "-u", uri], capture_output=True, text=True)
        if r.returncode:
            out.print(f"  [red]Eject failed[/] {uri}: {r.stderr.strip()} [dim](still copying? close file managers)[/]")
        else:
            out.print("  [green]✓ Ejected.[/] Safe to unplug.")
    out.print()


def cmd_eject(_):
    out.print()
    eject()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.set_defaults(fn=cmd_status)
    sub = ap.add_subparsers()
    sub.add_parser("status", help="default: login, library and watch state").set_defaults(fn=cmd_status)
    p = sub.add_parser("list", help="music currently on the watch")
    p.add_argument("-s", "--short", action="store_true", help="playlists only")
    p.add_argument("--release-kde", action="store_true")
    p.set_defaults(fn=cmd_list)
    sub.add_parser("login").set_defaults(fn=cmd_login)
    sub.add_parser("playlists").set_defaults(fn=cmd_playlists)
    p = sub.add_parser("pull")
    p.add_argument("refs", nargs="*")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_pull)
    p = sub.add_parser("drop")
    p.add_argument("name")
    p.set_defaults(fn=cmd_drop)
    p = sub.add_parser("push")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--release-kde", action="store_true",
                   help="stop KDE's kiod6 if it holds the MTP device")
    p.add_argument("--eject", action="store_true", help="unmount the watch when done")
    p.set_defaults(fn=cmd_push)
    sub.add_parser("eject", help="unmount the watch so it can be unplugged").set_defaults(fn=cmd_eject)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
