"""Eget index över alla filnamn på datorns lokala diskar – swpt behöver inget annat program.

Indexet byggs i en egen process med låg prioritet (build_index) och sparas som en fil.
I minnet ligger alla filnamn (gemener) i en enda lång sträng sorterad på namn, så en
sökning är en snabb str.find i C. Ändringar fångas live med ReadDirectoryChangesW
(Watcher) och läggs i ett litet tilläggslager ovanpå (DiskIndex.apply).
"""
import ctypes
import os
import pickle
import queue
import string
import threading
import time
from array import array
from bisect import bisect_right
from ctypes import wintypes

import query as Q

FORMAT = 1
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)


def fixed_drives():
    mask = _k32.GetLogicalDrives()
    return [f"{d}:" for i, d in enumerate(string.ascii_uppercase)
            if mask >> i & 1 and _k32.GetDriveTypeW(f"{d}:\\") == 3]  # DRIVE_FIXED


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
    names, parent, flags = [], array("I"), bytearray()
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
                names.append(e.name)
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

    lower = [s.lower() for s in names]
    order = sorted(range(n), key=lower.__getitem__)
    lower_sorted = [lower[i] for i in order]
    names_sorted = [names[i] for i in order]
    del lower, names
    data = {
        "format": FORMAT,
        "created": started,
        "drives": drives,
        "dirs": dirs,
        "lower": "\n" + "\n".join(lower_sorted) + "\n",
        "lofs": _offsets(lower_sorted),
        "orig": "\n" + "\n".join(names_sorted) + "\n",
        "oofs": _offsets(names_sorted),
        "parent": array("I", (parent[i] for i in order)),
        "flags": bytearray(flags[i] for i in order),
        "size": array("q", (size[i] for i in order)),
        "mtime": array("d", (mtime[i] for i in order)),
    }
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(data, f, protocol=5)
    os.replace(tmp, out_path)
    progress.put(("done", n))


def _offsets(strings):
    """Startposition för varje namn i "\\n" + "\\n".join(strings) + "\\n" (plus en slutpost)."""
    ofs = array("I")
    pos = 1
    for s in strings:
        ofs.append(pos)
        pos += len(s) + 1
    ofs.append(pos)
    return ofs


# ---------------------------------------------------------------- indexet i minnet

class DiskIndex:
    def __init__(self, data):
        self.__dict__.update(data)
        self.n = len(self.parent)
        self.dead = bytearray(self.n)       # borttagna sedan indexet byggdes
        self.extra = {}                     # gemen sökväg -> [sökväg, namn, är_mapp, storlek, ändrad]
        self._dir_ids = None
        self._dirs_lower = None
        self.version = 0                    # ökar vid varje ändring (för att uppdatera sökningar)

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            data = pickle.load(f)
        if data.get("format") != FORMAT:
            raise ValueError("gammalt indexformat")
        return cls(data)

    # --- uppslag
    def lname(self, i):
        return self.lower[self.lofs[i]:self.lofs[i + 1] - 1]

    def name(self, i):
        return self.orig[self.oofs[i]:self.oofs[i + 1] - 1]

    def folder(self, i):
        return self.dirs[self.parent[i]]

    def path(self, i):
        return self.dirs[self.parent[i]] + "\\" + self.name(i)

    def count(self):
        return self.n - self.dead.count(1) + len(self.extra)

    def dir_ids(self):
        if self._dir_ids is None:
            self._dir_ids = {d.lower(): i for i, d in enumerate(self.dirs)}
        return self._dir_ids

    def dirs_lower(self):
        if self._dirs_lower is None:
            self._dirs_lower = [d.lower() for d in self.dirs]
        return self._dirs_lower

    def find(self, path):
        """Index för en sökväg i grundindexet, eller -1."""
        folder, _, name = path.rpartition("\\")
        did = self.dir_ids().get(folder.lower())
        if did is None:
            return -1
        key = name.lower()
        lo, hi = 0, self.n
        while lo < hi:
            mid = (lo + hi) // 2
            if self.lname(mid) < key:
                lo = mid + 1
            else:
                hi = mid
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
                self.size[i], self.mtime[i] = sz, st.st_mtime
            elif lp in self.extra:
                self.extra[lp][3:5] = [sz, st.st_mtime]
            elif action == "add":
                if i >= 0:
                    self.dead[i] = 0
                    self.size[i], self.mtime[i] = sz, st.st_mtime
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
        prefix = path.lower() + "\\"
        gone = {i for i, d in enumerate(self.dirs_lower()) if d.startswith(prefix) or d == prefix[:-1]}
        if gone:
            par, dead = self.parent, self.dead
            for i in range(self.n):
                if par[i] in gone:
                    dead[i] = 1

    def _remove_extra_tree(self, lp):
        prefix = lp + "\\"
        for k in [k for k in self.extra if k.startswith(prefix)]:
            del self.extra[k]

    # --- sökning
    def query(self, text):
        return Query(self, text)


SORT_NAME, SORT_PATH, SORT_SIZE, SORT_MODIFIED = "name", "path", "size", "mtime"


class Query:
    """En pågående sökning. step() arbetar i korta tidsluckor så att gränssnittet aldrig fryser;
    matches fylls på i namnordning tills done är sant."""

    def __init__(self, idx, text):
        self.idx = idx
        self.matches = []          # index i grundindexet (>= 0) eller -(k+1) för tillägg
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
        self._ex = list(idx.extra.values())      # ögonblicksbild, så att id:n står sig
        self._extras = list(self._extra_matches())
        self._gen = self._base_matches()

    def _ok(self, lname, folder_lower):
        if any(t not in lname for t in self.name_terms):
            return False
        if self.exact_res and not all(r.search(lname) for r in self.exact_res):
            return False
        if self.exts and not lname.endswith(tuple(self.exts)):
            return False
        if self.path_terms:
            full = folder_lower + "\\" + lname
            if any(t not in full for t in self.path_terms):
                return False
        return True

    def _base_matches(self):
        ix = self.idx
        big, lofs, dead, n = ix.lower, ix.lofs, ix.dead, ix.n
        dl = ix.dirs_lower() if self.path_terms else None
        par = ix.parent
        needle = max(self.name_terms, key=len) if self.name_terms else None
        if needle is None and self.exts and len(self.exts) == 1:
            needle = self.exts[0] + "\n"
        if needle is None:
            for i in range(n):
                if not dead[i] and (not (self.exts or self.path_terms)
                                    or self._ok(big[lofs[i]:lofs[i + 1] - 1], dl[par[i]] if dl else "")):
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
                    if self._ok(ln, dl[par[i]] if dl else ""):
                        yield i
            pos = big.find(needle, lofs[i + 1])

    def _extra_matches(self):
        for k, (path, name, *_rest) in enumerate(self._ex):
            folder = path[:len(path) - len(name) - 1].lower()
            if self._ok(name.lower(), folder):
                yield -(k + 1), name.lower()

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
        if self._extras:   # sortera in tilläggen på namn
            from bisect import insort
            keyed = [(self.idx.lname(i), i) for i in self.matches]
            for k, ln in self._extras:
                insort(keyed, (ln, k))
            self.matches = [i for _, i in keyed]
            got += len(self._extras)
        return got

    def sort(self, by, descending=False):
        ix = self.idx
        ex = self._ex

        def get(i, field):
            if i >= 0:
                return {"size": ix.size, "mtime": ix.mtime}[field][i] if field != "path" else ix.folder(i).lower()
            rec = ex[-i - 1]
            return {"size": rec[3], "mtime": rec[4]}[field] if field != "path" else os.path.dirname(rec[0]).lower()
        if by == SORT_NAME:
            if descending:
                self.matches.reverse()
            return
        if by == SORT_SIZE:
            key = (lambda i: ix.size[i]) if not ex else (lambda i: get(i, "size"))
        elif by == SORT_MODIFIED:
            key = (lambda i: ix.mtime[i]) if not ex else (lambda i: get(i, "mtime"))
        else:
            dl = ix.dirs_lower()
            key = (lambda i: dl[ix.parent[i]]) if not ex else (lambda i: get(i, "path"))
        self.matches.sort(key=key, reverse=descending)

    def row(self, i):
        """Raden för resultatlistan: (id, sökväg, namn, är_mapp, storlek, ändrad, moln, mapp, "", "")."""
        ix = self.idx
        if i >= 0:
            folder = ix.folder(i)
            name = ix.name(i)
            return (i, folder + "\\" + name, name, bool(ix.flags[i]), ix.size[i], ix.mtime[i], 0, folder, "", "")
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
