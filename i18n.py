"""Språk: svenska om Windows visningsspråk är svenska, annars engelska.

Texterna i koden skrivs på svenska och slås upp här:  t("Ny flik"),  t("{n} objekt", n=fmt_int(n)).
Sätt miljövariabeln SWPT_LANG=sv eller en för att tvinga ett språk.
"""
import ctypes
import os


def _system_lang():
    try:
        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "sv" if lang_id & 0x3FF == 0x1D else "en"   # LANG_SWEDISH
    except Exception:
        return "sv"


def _configured_lang():
    try:
        import store
        lang = store.load_config().get("settings", {}).get("language")
        return lang if lang in ("sv", "en") else None
    except Exception:
        return None


LANG = os.environ.get("SWPT_LANG") or _configured_lang() or _system_lang()

EN = {
    # bakgrundsjobb och status
    "Fel: {e}": "Error: {e}",
    "Mappen hittades inte – är den synkad/ansluten?": "Folder not found – is it synced/connected?",
    "Läser in mappen…": "Reading folder…",
    "Läser in…": "Loading…",
    "Indexerar innehåll {k}/{n}…": "Indexing contents {k}/{n}…",
    "Indexeringen avbröts – försöker igen vid nästa uppdatering": "Indexing was interrupted – will retry on next update",
    "Laddar index…": "Loading index…",
    "Uppdaterar indexet i bakgrunden…": "Updating the index in the background…",
    "Indexerar datorn…": "Indexing this PC…",
    "Indexerar datorn… {n} filer hittills": "Indexing this PC… {n} files so far",
    "Sorterar {n} filer…": "Sorting {n} files…",
    "Indexeringen misslyckades – högerklicka på fliken för att försöka igen":
        "Indexing failed – right-click the tab to try again",
    # resultatlista
    "Namn": "Name",
    "Mapp": "Folder",
    "Träff i innehåll": "Content match",
    "Ändrad": "Modified",
    "Storlek": "Size",
    "Finns bara i molnet – söks inte i innehållet": "Online-only file – contents are not searched",
    "Sök i filinnehåll": "Search file contents",
    "Kunde inte öppna": "Could not open",
    "Förhandsvisa\tMellanslag": "Preview\tSpace",
    "Öppna\tEnter": "Open\tEnter",
    "Visa i Utforskaren": "Show in Explorer",
    "Kopiera sökväg": "Copy path",
    "Kopiera filnamn": "Copy file name",
    "{n} objekt": "{n} items",
    "{n} av {total} objekt": "{n} of {total} items",
    " … söker": " … searching",
    "{n} filer finns bara i molnet och söks inte i innehållet":
        "{n} files are online-only and their contents are not searched",
    "innehållssökning kräver minst 3 tecken": "content search needs at least 3 characters",
    # flikar
    "Hela datorn": "This PC",
    "Söker bland alla filer på datorns lokala diskar": "Searches all files on this PC's local drives",
    "Sök i hela datorn…   (t.ex.  offert *.pdf   ·   ext:docx standard   ·   projekt\\ ritning)":
        "Search this PC…   (e.g.  quote *.pdf   ·   ext:docx standard   ·   project\\ drawing)",
    "Sök i {name}…   (flera ord = alla måste finnas)": "Search {name}…   (several words = all must match)",
    "Hela datorn är inte indexerad – klicka på ”Indexera datorn” för att kunna söka bland alla filer.":
        "This PC is not indexed – click “Index this PC” to search all files.",
    "Indexera datorn": "Index this PC",
    "Ny flik": "New tab",
    "Ny flik…": "New tab…",
    "Lägg till en mapp som flik (Ctrl+T)": "Add a folder as a tab (Ctrl+T)",
    "Välj mapp för den nya fliken": "Choose a folder for the new tab",
    "Namn på fliken:": "Tab name:",
    "Uppdatera indexet nu": "Update the index now",
    "Stäng av indexering av datorn…": "Turn off indexing of this PC…",
    "Byt namn…": "Rename…",
    "Byt mapp…": "Change folder…",
    "Öppna mappen i Utforskaren": "Open folder in Explorer",
    "Uppdatera nu (F5)": "Refresh now (F5)",
    "Sluta indexera innehåll (frigör utrymme)": "Stop indexing contents (frees space)",
    "Ta bort flik…": "Remove tab…",
    "Byt namn": "Rename",
    "Nytt namn på fliken:": "New tab name:",
    "Välj ny mapp": "Choose a new folder",
    "Ta bort flik": "Remove tab",
    "Ta bort fliken ”{name}”?\n\nFilerna i mappen påverkas inte – bara fliken och dess sökindex tas bort.":
        "Remove the tab “{name}”?\n\nThe files in the folder are not affected – only the tab and its search index are removed.",
    # första start
    "Välkommen till swpt": "Welcome to swpt",
    "<b>Vill du att swpt indexerar datorn?</b>": "<b>Do you want swpt to index this PC?</b>",
    "För att kunna söka blixtsnabbt bland alla filer på datorn går swpt först igenom filnamnen på dina lokala "
    "diskar. Det tar ungefär en minut och sker i bakgrunden – du kan använda swpt under tiden.\n\n"
    "Därefter håller swpt indexet uppdaterat automatiskt. Bara filnamn, mappar, storlek och datum sparas, "
    "och bara på den här datorn.":
        "To search all files on this PC instantly, swpt first goes through the file names on your local drives. "
        "It takes about a minute and runs in the background – you can use swpt in the meantime.\n\n"
        "After that, swpt keeps the index up to date automatically. Only file names, folders, sizes and dates "
        "are stored, and only on this PC.",
    "Indexera nu": "Index now",
    "Inte nu": "Not now",
    "Stäng av indexering": "Turn off indexing",
    "Stänga av indexeringen av datorn?\n\nIndexet raderas och fliken ”Hela datorn” kan inte söka förrän du "
    "indexerar igen. Mappflikarna påverkas inte.":
        "Turn off indexing of this PC?\n\nThe index is deleted and the “This PC” tab can't search until you index "
        "again. Folder tabs are not affected.",
    # förhandsvisning
    "Kopiera\tCtrl+C": "Copy\tCtrl+C",
    "Markera allt\tCtrl+A": "Select all\tCtrl+A",
    "Stäng": "Close",
    "Öppna": "Open",
    "Sök i dokumentet…   (Ctrl+F, Enter = nästa)": "Search in document…   (Ctrl+F, Enter = next)",
    "Sökning finns för PDF- och textfiler": "Search is available for PDF and text files",
    "Föregående träff (Shift+Enter)": "Previous match (Shift+Enter)",
    "Nästa träff (Enter)": "Next match (Enter)",
    "Inga träffar": "No matches",
    "Ingen förhandsvisning för {name}": "No preview for {name}",
    "Mapp: {name}": "Folder: {name}",
    # inställningar och systemfält
    "Inställningar": "Settings",
    "Inställningar (Ctrl+,)": "Settings (Ctrl+,)",
    "Inställningar…": "Settings…",
    "Öppna swpt": "Open swpt",
    "Avsluta": "Quit",
    "swpt körs vidare i systemfältet.": "swpt keeps running in the system tray.",
    "Öppna med {key}.": "Open it with {key}.",
    "Kortkommando": "Keyboard shortcut",
    "Öppna swpt med:": "Open swpt with:",
    "Fungerar var du än är i Windows, t.ex. Ctrl+Alt+S. Klicka i rutan och tryck tangenterna. "
    "Trycker du igen när swpt redan är framme döljs fönstret.":
        "Works wherever you are in Windows, e.g. Ctrl+Alt+S. Click the box and press the keys. "
        "Pressing it again while swpt is in front hides the window.",
    "Kortkommandot måste innehålla Ctrl, Alt eller Win (eller vara en F-tangent).":
        "The shortcut must include Ctrl, Alt or Win (or be a function key).",
    "Kortkommandot {key} används redan av ett annat program.": "The shortcut {key} is already used by another program.",
    "Start och bakgrund": "Startup and background",
    "Starta swpt när Windows startar": "Start swpt when Windows starts",
    "Starta dolt – bara som ikon i systemfältet": "Start hidden – only as an icon in the system tray",
    "Fortsätt köra i systemfältet när fönstret stängs": "Keep running in the system tray when the window is closed",
    "Behövs för att kortkommandot ska fungera hela tiden och för att indexet ska hållas uppdaterat. "
    "Avsluta helt via ikonen i systemfältet.":
        "Needed for the shortcut to work at all times and to keep the index up to date. "
        "Quit completely from the tray icon.",
    "Språk": "Language",
    "Språk:": "Language:",
    "Automatiskt (samma som Windows)": "Automatic (same as Windows)",
    "Det nya språket används nästa gång swpt startar.": "The new language is used the next time swpt starts.",
    "Index över hela datorn": "Index of this PC",
    "Indexera hela datorn (för fliken ”Hela datorn”)": "Index this whole PC (for the “This PC” tab)",
    "{n} filer · senast genomsökt {when}": "{n} files · last scanned {when}",
    "Inte indexerad": "Not indexed",
    "Spara": "Save",
    "Avbryt": "Cancel",
    # fästa flikar
    "Fäst flik": "Pin tab",
    "Lossa flik": "Unpin tab",
    "Fäst fliken – den finns kvar nästa gång swpt startar": "Pin the tab – it stays the next time swpt starts",
    "Lossa fliken – den stängs när swpt stängs": "Unpin the tab – it closes when swpt closes",
    "Stäng fliken (Ctrl+W)": "Close tab (Ctrl+W)",
    "Stäng flik\tCtrl+W": "Close tab\tCtrl+W",
    "Skapa flik": "Create tab",
    # innehållssök i hela datorn
    "Söker i texten i dina dokument (PDF, Word, Excel, PowerPoint, text) i användarmappen":
        "Searches the text in your documents (PDF, Word, Excel, PowerPoint, text) in your user folder",
    "<b>Indexera innehållet i dina dokument?</b>": "<b>Index the contents of your documents?</b>",
    "För att kunna söka i filinnehåll läser swpt texten i dina dokument – PDF, Word, Excel, "
    "PowerPoint och textfiler – i din användarmapp ({path}), inklusive OneDrive och SharePoint.\n\n"
    "Första gången kan det ta en stund beroende på hur många dokument du har. Det sker i bakgrunden, "
    "och indexet hålls sedan uppdaterat. Filer som bara finns i molnet läses inte.":
        "To search file contents, swpt reads the text in your documents – PDF, Word, Excel, PowerPoint and "
        "text files – in your user folder ({path}), including OneDrive and SharePoint.\n\n"
        "The first time can take a while depending on how many documents you have. It runs in the background, "
        "and the index is kept up to date afterwards. Online-only files are not read.",
    "Indexera innehåll": "Index contents",
    # OCR
    "Läser skannade dokument {k}/{n}…": "Reading scanned documents {k}/{n}…",
    "Läs in skannade dokument (OCR)…": "Read scanned documents (OCR)…",
    "Sluta läsa skannade dokument (OCR)": "Stop reading scanned documents (OCR)",
    "OCR på": "OCR on",
    "Skannade dokument (OCR)": "Scanned documents (OCR)",
    "<b>Läsa in skannade dokument i ”{name}”?</b>": "<b>Read scanned documents in “{name}”?</b>",
    "swpt tolkar texten i inskannade PDF:er och bilder (JPG, PNG, TIFF) i den här mappen med "
    "Windows inbyggda textigenkänning, så att du kan söka i dem. Allt sker på datorn och i "
    "bakgrunden – ungefär en halv sekund per sida.\n\n"
    "PDF:er som redan har text påverkas inte. Nya skannade filer i mappen läses in automatiskt.":
        "swpt recognizes the text in scanned PDFs and images (JPG, PNG, TIFF) in this folder using Windows' "
        "built-in text recognition, so you can search them. Everything happens on this PC and in the "
        "background – about half a second per page.\n\n"
        "PDFs that already contain text are not affected. New scanned files in the folder are read automatically.",
    "Starta OCR": "Start OCR",
    "Läser skannad text…": "Reading scanned text…",
    "Windows textigenkänning (OCR) är inte installerad. Lägg till den under Inställningar → "
    "Tid och språk → Språk → Svenska → Språkalternativ → Optisk teckenigenkänning.":
        "Windows text recognition (OCR) is not installed. Add it under Settings → Time & language → "
        "Language → (your language) → Language options → Optical character recognition.",
    "Sluta indexera innehåll i dokument (frigör utrymme)": "Stop indexing document contents (frees space)",
    # uppdateringar
    "Uppdateringar": "Updates",
    "Sök efter nya versioner automatiskt (en gång per dygn)": "Check for new versions automatically (once a day)",
    "Version {v}": "Version {v}",
    "Sök efter uppdateringar nu": "Check for updates now",
    "Du har den senaste versionen av swpt ({v}).": "You have the latest version of swpt ({v}).",
    "Uppdatera till {v}": "Update to {v}",
    "Det finns en ny version av swpt. Klicka för att ladda ner och installera den – swpt startar om av sig själv.":
        "A new version of swpt is available. Click to download and install it – swpt restarts by itself.",
    "swpt {v} finns. Vill du uppdatera nu? swpt startar om av sig själv.":
        "swpt {v} is available. Do you want to update now? swpt restarts by itself.",
    "swpt {v} finns. Öppna swpt och klicka på ”Uppdatera”.": "swpt {v} is available. Open swpt and click “Update”.",
    "Laddar ner… {p} %": "Downloading… {p} %",
    "Installerar…": "Installing…",
    "Uppdateringen kunde inte laddas ner.": "The update could not be downloaded.",
    "Kunde inte söka efter uppdateringar. Är datorn ansluten till internet?":
        "Could not check for updates. Is this PC connected to the internet?",
}


def t(text, **kw):
    s = EN.get(text, text) if LANG == "en" else text
    return s.format(**kw) if kw else s


def fmt_int(n):
    """Tusentalsavgränsare: mellanslag på svenska, komma på engelska."""
    s = f"{n:,}"
    return s.replace(",", " ") if LANG == "sv" else s


def fmt_dec(x):
    s = f"{x:.1f}"
    return s.replace(".", ",") if LANG == "sv" else s
