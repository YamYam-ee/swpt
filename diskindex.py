"""Eget index över alla filnamn på datorns lokala diskar – swpt behöver inget annat program.

Indexet byggs i en egen process med låg prioritet (build_index) och sparas som en fil som
minnesmappas (DiskIndex.load): Windows läser bara in de delar som används och kan släppa dem
när som helst, så indexet tar nästan inget av appens eget minne. Alla filnamn (gemener, UTF-8)
ligger i en enda lång bytesträng sorterad på namn, så en sökning är en snabb find i C.
Ändringar fångas live med ReadDirectoryChangesW (Watcher) och läggs i ett litet tilläggslager
ovanpå (DiskIndex.apply).
"""
import ctypes
import glob
import json
import mmap
import os
import pickle
import queue
import string
import threading
import time
from array import array
from bisect import bisect_left, bisect_right
from ctypes import wintypes

import query as Q

MAGIC = b"SWPTIX2\0"
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)


def fixed_drives():
    mask = _k32.GetLogicalDrives()
    return [f"{d}:" for i, d in enumerate(string.ascii_uppercase)
            if mask >> i & 1 and _k32.GetDriveTypeW(f"{d}:\\") == 3]  # DRIVE_FIXED


def _enc(s):
    return s.encode("utf-8", "surrogatepass")     # filnamn kan innehålla ogiltig UTF-16


def _dec(b):
    return b.decode("utf-8", "surrogatepass")


# ---------------------------------------------------------------- indexfiler

def latest(folder):
    """Den senaste indexfilen i mappen, eller None."""
    files = sorted(glob.glob(os.path.join(folder, "diskindex-*.idx")))
    return files[-1] if files else None


def new_path(folder):
    return os.path.join(folder, f"diskindex-{int(time.time() * 1000)}.idx")


def cleanup(folder, keep=None):
    """Tar bort gamla indexfiler. En fil som fortfarande är mappad går inte att ta bort – den tas
    bort en annan gång."""
    for p in glob.glob(os.path.join(folder, "diskindex*")):
        if keep and os.path.normcase(p) == os.path.normcase(keep):
            continue
        try:
            os.remove(p)
        except OSError:
            pass


def _offsets(blobs, sep):
    """Startposition för varje post i sep.join(blobs) (med sep först om sep), plus en slutpost."""
    ofs = array("I")
    pos = len(sep)
    for b in blobs:
        ofs.append(pos)
        pos += len(b) + len(sep)
    ofs.append(pos)
    return ofs


def _pack(out_path, created, drives, dirs, lower, orig, parent, flags, size, mtime):
    """Skriver indexfilen: MAGIC, sektionerna (8-byte-justerade), JSON-huvud, huvudets position och längd.
    lower/orig ger bytes per post (items[k]), redan i ordning sorterad på lower. Sektionerna skrivs
    en i taget, så att bygget inte behöver ha allt i minnet samtidigt."""
    n, nd = len(lower), len(dirs)

    def kids_and_kofs():
        kofs = array("I", bytes(4 * (nd + 1)))     # posterna per mapp (för att snabbt ta bort en mapp)
        for d in parent:
            kofs[d + 1] += 1
        for d in range(nd):
            kofs[d + 1] += kofs[d]
        pos, kids = array("I", kofs), array("I", bytes(4 * n))
        for i, d in enumerate(parent):
            kids[pos[d]] = i
            pos[d] += 1
        return kids, kofs

    def names(items, name):
        blob, ofs = bytearray(b"\n"), array("I")
        for k in range(n):
            ofs.append(len(blob))
            blob += items[k]
            blob += b"\n"
        ofs.append(len(blob))
        yield name, "B", blob
        del blob
        yield name[0] + "ofs", "I", ofs

    def dir_sections():
        dirs_b = [_enc(d) for d in dirs]
        yield "dirs", "B", b"".join(dirs_b)
        yield "dofs", "I", _offsets(dirs_b, b"")
        del dirs_b
        dlow_b = [_enc(d.lower()) for d in dirs]
        yield "dlower", "B", b"".join(dlow_b)
        yield "dlofs", "I", _offsets(dlow_b, b"")
        yield "dsort", "I", array("I", sorted(range(nd), key=dlow_b.__getitem__))

    def sections():
        yield from names(lower, "lower")
        yield from names(orig, "orig")
        yield from dir_sections()
        kids, kofs = kids_and_kofs()
        yield "kids", "I", kids
        yield "kofs", "I", kofs
        yield from (("parent", "I", parent), ("flags", "B", flags), ("size", "q", size), ("mtime", "d", mtime))

    layout = {}
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(MAGIC)
        for name, code, data in sections():
            f.write(b"\0" * (-f.tell() % 8))
            layout[name] = [f.tell(), len(data) * (1 if code == "B" else data.itemsize), code]
            f.write(data)
            del data
        head = json.dumps({"created": created, "drives": drives, "n": n, "nd": nd, "sections": layout}).encode()
        pos = f.tell()
        f.write(head + pos.to_bytes(8, "little") + len(head).to_bytes(8, "little"))
    os.replace(tmp, out_path)


def convert_legacy(folder):
    """Gör om ett index från swpt 1.1 och äldre (diskindex.bin) till det nya formatet."""
    old = os.path.join(folder, "diskindex.bin")
    if not os.path.exists(old):
        return None
    with open(old, "rb") as f:
        data = pickle.load(f)
    if data.get("format") != 1:
        return None
    lower = [_enc(s) for s in data.pop("lower")[1:-1].split("\n")]
    orig = [_enc(s) for s in data.pop("orig")[1:-1].split("\n")]
    if len(lower) != len(data["parent"]) or len(orig) != len(lower):
        return None
    out = new_path(folder)
    _pack(out, data["created"], data["drives"], data["dirs"], lower, orig,
          data["parent"], data["flags"], data["size"], data["mtime"])
    return out


class _Names:
    """Namn som bytes i en enda buffert (i stället för miljontals små objekt under bygget)."""

    def __init__(self):
        self.blob, self.ofs = bytearray(), array("I", [0])

    def append(self, b):
        self.blob += b
        self.ofs.append(len(self.blob))

    def get(self, i):
        return bytes(self.blob[self.ofs[i]:self.ofs[i + 1]])

    def __len__(self):
        return len(self.ofs) - 1


class _Ordered:
    """items[k] = src.get(order[k])."""

    def __init__(self, src, order):
        self.src, self.order = src, order

    def __len__(self):
        return len(self.order)

    def __getitem__(self, k):
        return self.src.get(self.order[k])


# ---------------------------------------------------------------- bygga (egen process)

def _low_priority():
    try:
        h = _k32.GetCurrentProcess()
        _k32.SetPriorityClass(h, 0x00004000)   # BELOW_NORMAL_PRIORITY_CLASS
        _k32.SetPriorityClass(h, 0x00100000)   # PROCESS_MODE_BACKGROUND_BEGIN (låg disk-prioritet)
    except Exception:
        pass


def build_index(out_path, progress, skip_prefixes=()):
    """Går igenom alla fasta diskar och skriver indexfilen. Körs i en egen process."""
    _low_priority()
    started = time.time()
    drives = fixed_drives()
    dirs = list(drives)                       # mapp-id -> sökväg ("C:", "C:\\Users", …)
    orig, lower, parent, flags = _Names(), _Names(), array("I"), bytearray()
    size, mtime = array("q"), array("d")
    stack = list(range(len(drives)))
    skip = tuple(p.lower() for p in skip_prefixes)
    n = 0
    while stack:
        did = stack.pop()
        base = dirs[did]
        try:
            it = os.scandir(base + "\\")
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                orig.append(_enc(e.name))
                lower.append(_enc(e.name.lower()))
                parent.append(did)
                if is_dir:
                    flags.append(1)
                    size.append(-1)
                    path = base + "\\" + e.name
                    if not e.is_junction() and not e.is_symlink() and not path.lower().startswith(skip):
                        stack.append(len(dirs))
                    dirs.append(path)
                else:
                    flags.append(0)
                    size.append(st.st_size)
                mtime.append(st.st_mtime)
                n += 1
                if n % 25000 == 0:
                    progress.put(("progress", n))
    progress.put(("sorting", n))

    order = array("I", sorted(range(n), key=lower.get))
    _pack(out_path, started, drives, dirs, _Ordered(lower, order), _Ordered(orig, order),
          array("I", (parent[i] for i in order)), bytearray(flags[i] for i in order),
          array("q", (size[i] for i in order)), array("d", (mtime[i] for i in order)))
    progress.put(("done", n))


# ---------------------------------------------------------------- indexet (minnesmappat)

class DiskIndex:
    def __init__(self, path):
        with open(path, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        if mm[:len(MAGIC)] != MAGIC:
            raise ValueError("okänt indexformat")
        hpos, hlen = int.from_bytes(mm[-16:-8], "little"), int.from_bytes(mm[-8:], "little")
        head = json.loads(mm[hpos:hpos + hlen])
        start = 0
        self.file, self._mm = path, mm
        self.created, self.drives, self.n = head["created"], head["drives"], head["n"]
        # Byte-strängarna söks med mmap.find (C); tabellerna är memoryviews direkt ur filen.
        view = memoryview(mm)
        for name, (off, nbytes, code) in head["sections"].items():
            if name in ("lower", "orig", "dirs", "dlower"):
                setattr(self, name, _Blob(mm, start + off, nbytes))
            else:
                setattr(self, name, view[start + off:start + off + nbytes].cast(code))
        self.nd = head["nd"]
        self.dead = bytearray(self.n)       # borttagna sedan indexet byggdes
        self.changed = {}                   # id -> (storlek, ändrad) för ändrade filer
        self.extra = {}                     # gemen sökväg -> [sökväg, namn, är_mapp, storlek, ändrad]
        self.version = 0                    # ökar vid varje ändring (för att uppdatera sökningar)

    @classmethod
    def load(cls, path):
        return cls(path)

    # --- uppslag
    def lname(self, i):
        """Filnamnet i gemener som UTF-8-bytes."""
        return self.lower[self.lofs[i]:self.lofs[i + 1] - 1]

    def name(self, i):
        return _dec(self.orig[self.oofs[i]:self.oofs[i + 1] - 1])

    def dir_path(self, d):
        return _dec(self.dirs[self.dofs[d]:self.dofs[d + 1]])

    def dir_lower(self, d):
        return self.dlower[self.dlofs[d]:self.dlofs[d + 1]]

    def folder(self, i):
        return self.dir_path(self.parent[i])

    def path(self, i):
        return self.folder(i) + "\\" + self.name(i)

    def size_of(self, i):
        c = self.changed.get(i)
        return c[0] if c else self.size[i]

    def mtime_of(self, i):
        c = self.changed.get(i)
        return c[1] if c else self.mtime[i]

    def count(self):
        return self.n - self.dead.count(1) + len(self.extra)

    def find_dir(self, folder):
        """Mapp-id för en sökväg, eller None."""
        key = _enc(folder.lower())
        ds = self.dsort
        k = bisect_left(ds, key, key=self.dir_lower)
        return ds[k] if k < len(ds) and self.dir_lower(ds[k]) == key else None

    def find(self, path):
        """Index för en sökväg i grundindexet, eller -1."""
        folder, _, name = path.rpartition("\\")
        did = self.find_dir(folder)
        if did is None:
            return -1
        key = _enc(name.lower())
        lo = bisect_left(range(self.n), key, key=self.lname)
        while lo < self.n and self.lname(lo) == key:
            if self.parent[lo] == did:
                return lo
            lo += 1
        return -1

    # --- ändringar från Watcher
    def apply(self, action, path):
        """action: 'add', 'remove' eller 'modify'. Returnerar mappar som behöver läsas in."""
        lp = path.lower()
        rescan = []
        if action == "remove":
            if lp in self.extra:
                del self.extra[lp]
            i = self.find(path)
            if i >= 0:
                self.dead[i] = 1
                if self.flags[i]:
                    self._remove_tree(path)
            self._remove_extra_tree(lp)
        else:
            try:
                st = os.stat(path, follow_symlinks=False)
            except OSError:
                return rescan
            is_dir = os.path.isdir(path) and not os.path.islink(path)
            sz = -1 if is_dir else st.st_size
            i = self.find(path)
            if i >= 0 and not self.dead[i]:
                self.changed[i] = (sz, st.st_mtime)
            elif lp in self.extra:
                self.extra[lp][3:5] = [sz, st.st_mtime]
            elif action == "add":
                if i >= 0:
                    self.dead[i] = 0
                    self.changed[i] = (sz, st.st_mtime)
                else:
                    self.extra[lp] = [path, os.path.basename(path), is_dir, sz, st.st_mtime]
                if is_dir:
                    rescan.append(path)
        self.version += 1
        return rescan

    def add_tree(self, root):
        """Lägger till allt under en mapp (t.ex. en mapp som flyttats hit)."""
        stack = [root]
        while stack:
            try:
                it = os.scandir(stack.pop())
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    lp = e.path.lower()
                    if self.find(e.path) < 0 and lp not in self.extra:
                        self.extra[lp] = [e.path, e.name, is_dir, -1 if is_dir else st.st_size, st.st_mtime]
                    if is_dir and not e.is_junction():
                        stack.append(e.path)
        self.version += 1

    def _remove_tree(self, path):
        """Markerar allt under en borttagen mapp. Mapparna ligger sorterade, så alla under
        mappen ligger i en följd i dsort."""
        key = _enc(path.lower())
        prefix = key + b"\\"
        ds, dl = self.dsort, self.dir_lower
        k = bisect_left(ds, key, key=dl)
        gone = []
        if k < len(ds) and dl(ds[k]) == key:
            gone.append(ds[k])
        k = bisect_left(ds, prefix, key=dl)
        while k < len(ds) and dl(ds[k]).startswith(prefix):
            gone.append(ds[k])
            k += 1
        dead, kids, kofs = self.dead, self.kids, self.kofs
        for d in gone:
            for i in kids[kofs[d]:kofs[d + 1]]:
                dead[i] = 1

    def _remove_extra_tree(self, lp):
        prefix = lp + "\\"
        for k in [k for k in self.extra if k.startswith(prefix)]:
            del self.extra[k]

    # --- sökning
    def query(self, text):
        return Query(self, text)


class _Blob:
    """En bytesträng i den mappade filen: find och slicing direkt på filen, utan kopia i minnet."""
    __slots__ = ("mm", "base", "end")

    def __init__(self, mm, base, nbytes):
        self.mm, self.base, self.end = mm, base, base + nbytes

    def __getitem__(self, s):
        return self.mm[self.base + s.start:self.base + s.stop]

    def find(self, sub, start=0):
        p = self.mm.find(sub, self.base + start, self.end)
        return p - self.base if p >= 0 else -1


SORT_NAME, SORT_PATH, SORT_SIZE, SORT_MODIFIED = "name", "path", "size", "mtime"


class Query:
    """En pågående sökning. step() arbetar i korta tidsluckor så att gränssnittet aldrig fryser;
    matches fylls på i namnordning tills done är sant."""

    def __init__(self, idx, text):
        self.idx = idx
        self.matches = array("i")  # index i grundindexet (>= 0) eller -(k+1) för tillägg
        self.done = False
        self.name_terms, self.path_terms, self.exts = [], [], []
        self.exact_terms, self.exact_res = [], []
        for t, exact in Q.parse(text):
            if exact:                  # "135" = hela ordet 135, inte 135a
                if "\\" in t or "/" in t:
                    self.path_terms.append(t.replace("/", "\\"))
                else:
                    self.name_terms.append(t)
                    self.exact_terms.append(t)
                    self.exact_res.append(Q.word_re(t))
            elif t.startswith("ext:"):
                self.exts += ["." + e.strip(".") for e in t[4:].replace(",", ";").split(";") if e.strip(".")]
            elif "\\" in t or "/" in t:
                self.path_terms.append(t.replace("/", "\\").strip("*"))
            elif t.startswith("*.") and "*" not in t[2:]:
                self.exts.append(t[1:])
            else:
                self.name_terms += [p for p in t.split("*") if p]
        # samma villkor som bytes, för sökningen direkt i indexfilen
        self._bname = [_enc(t) for t in self.name_terms]
        self._bpath = [_enc(t) for t in self.path_terms]
        self._bexts = tuple(_enc(e) for e in self.exts)
        self._ex = list(idx.extra.values())      # ögonblicksbild, så att id:n står sig
        self._extras = list(self._extra_matches())
        self._gen = self._base_matches()

    def _ok(self, lname, folder_lower):
        """lname och folder_lower i gemener, som UTF-8-bytes."""
        if any(t not in lname for t in self._bname):
            return False
        if self.exact_res and not all(r.search(_dec(lname)) for r in self.exact_res):
            return False
        if self._bexts and not lname.endswith(self._bexts):
            return False
        if self._bpath:
            full = folder_lower + b"\\" + lname
            if any(t not in full for t in self._bpath):
                return False
        return True

    def ok_str(self, name_lower, folder_lower):
        return self._ok(_enc(name_lower), _enc(folder_lower))

    def _base_matches(self):
        ix = self.idx
        big, lofs, dead, n = ix.lower, ix.lofs, ix.dead, ix.n
        dl = ix.dir_lower if self._bpath else None
        par = ix.parent
        needle = max(self._bname, key=len) if self._bname else None
        if needle is None and not (self._bexts or self._bpath):   # allt som inte är borttaget
            i = 0
            while i < n:
                j = dead.find(1, i)
                j = n if j < 0 else j
                yield from range(i, j)
                i = j + 1
            return
        if needle is None and len(self._bexts) == 1:
            needle = self._bexts[0] + b"\n"
        if needle is None:
            for i in range(n):
                if not dead[i] and self._ok(big[lofs[i]:lofs[i + 1] - 1], dl(par[i]) if dl else b""):
                    yield i
            return
        pos = big.find(needle)
        last = -1
        while pos >= 0:
            i = bisect_right(lofs, pos) - 1
            if i != last:
                last = i
                if not dead[i]:
                    ln = big[lofs[i]:lofs[i + 1] - 1]
                    if self._ok(ln, dl(par[i]) if dl else b""):
                        yield i
            pos = big.find(needle, lofs[i + 1])

    def _extra_matches(self):
        for k, (path, name, *_rest) in enumerate(self._ex):
            ln = _enc(name.lower())
            if self._ok(ln, _enc(path[:len(path) - len(name) - 1].lower())):
                yield -(k + 1), ln

    def step(self, budget_s=0.03):
        """Hämtar fler träffar under högst budget_s sekunder. Returnerar antal nya."""
        if self.done:
            return 0
        end = time.perf_counter() + budget_s
        got, append, gen = 0, self.matches.append, self._gen
        for i in gen:
            append(i)
            got += 1
            if got % 2000 == 0 and time.perf_counter() > end:
                return got
        self.done = True
        if self._extras:   # sortera in tilläggen på namn (matches är redan i namnordning)
            m, lname = self.matches, self.idx.lname
            places = sorted((bisect_left(m, ln, key=lname), ln, k) for k, ln in self._extras)
            out, prev = array("i"), 0
            for p, _, k in places:
                out += m[prev:p]
                out.append(k)
                prev = p
            out += m[prev:]
            self.matches = out
            got += len(self._extras)
        return got

    def sort(self, by, descending=False):
        ix = self.idx
        ex = self._ex
        if by == SORT_NAME:
            if descending:
                self.matches.reverse()
            return
        if by == SORT_SIZE:
            key = lambda i: ix.size_of(i) if i >= 0 else ex[-i - 1][3]
        elif by == SORT_MODIFIED:
            key = lambda i: ix.mtime_of(i) if i >= 0 else ex[-i - 1][4]
        else:
            key = lambda i: ix.dir_lower(ix.parent[i]) if i >= 0 else _enc(os.path.dirname(ex[-i - 1][0]).lower())
        self.matches = array("i", sorted(self.matches, key=key, reverse=descending))

    def row(self, i):
        """Raden för resultatlistan: (id, sökväg, namn, är_mapp, storlek, ändrad, moln, mapp, "", "")."""
        ix = self.idx
        if i >= 0:
            folder = ix.folder(i)
            name = ix.name(i)
            return (i, folder + "\\" + name, name, bool(ix.flags[i]), ix.size_of(i), ix.mtime_of(i), 0, folder, "", "")
        path, name, is_dir, sz, mt = self._ex[-i - 1]
        return (i, path, name, is_dir, sz, mt, 0, os.path.dirname(path), "", "")


# ---------------------------------------------------------------- live-ändringar

FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_ALL = 0x7
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
NOTIFY_FILTER = 0x1 | 0x2 | 0x8 | 0x10     # filnamn, mappnamn, storlek, ändringstid
ACTIONS = {1: "add", 2: "remove", 3: "modify", 4: "remove", 5: "add"}

_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             wintypes.DWORD, wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
                                       ctypes.c_void_p]
_k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
INVALID_HANDLE = wintypes.HANDLE(-1).value


class Watcher:
    """Lyssnar på ändringar på alla fasta diskar och lägger dem i en kö: (åtgärd, sökväg) eller ("overflow", enhet)."""

    def __init__(self, drives, ignore_prefixes=()):
        self.events = queue.Queue()
        self.ignore = tuple(p.lower() for p in ignore_prefixes)
        self.handles = []
        self.stopped = False
        for d in drives:
            h = _k32.CreateFileW(d + "\\", FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None, OPEN_EXISTING,
                                 FILE_FLAG_BACKUP_SEMANTICS, None)
            if h and h != INVALID_HANDLE:
                self.handles.append(h)
                threading.Thread(target=self._run, args=(d, h), daemon=True).start()

    def _run(self, drive, handle):
        buf = ctypes.create_string_buffer(1 << 20)
        got = wintypes.DWORD()
        while not self.stopped:
            ok = _k32.ReadDirectoryChangesW(handle, buf, len(buf), True, NOTIFY_FILTER, ctypes.byref(got), None, None)
            if self.stopped:
                return
            if not ok:
                time.sleep(1)
                continue
            if got.value == 0:          # bufferten räckte inte – indexet behöver byggas om
                self.events.put(("overflow", drive))
                continue
            data = buf.raw[:got.value]
            pos = 0
            while True:
                nxt, action, ln = int.from_bytes(data[pos:pos + 4], "little"), \
                    int.from_bytes(data[pos + 4:pos + 8], "little"), int.from_bytes(data[pos + 8:pos + 12], "little")
                rel = data[pos + 12:pos + 12 + ln].decode("utf-16-le", "replace")
                path = drive + "\\" + rel
                if action in ACTIONS and not path.lower().startswith(self.ignore):
                    self.events.put((ACTIONS[action], path))
                if not nxt:
                    break
                pos += nxt

    def stop(self):
        self.stopped = True
        for h in self.handles:
            _k32.CancelIoEx(h, None)
            _k32.CloseHandle(h)
