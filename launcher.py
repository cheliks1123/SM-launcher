import json
import os
import re
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import certifi
import minecraft_launcher_lib as mll
import webview
from version import VERSION

BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
ROOT = Path.home() / ".mylauncher"
MC = str(ROOT / "minecraft")
LIB = ROOT / "library"
INST = ROOT / "instances"
state = {"text": "Готов к запуску", "progress": 0, "busy": False}


API = "https://api.modrinth.com/v2"
CTX = ssl.create_default_context(cafile=certifi.where())
UA = {"User-Agent": "MyLauncher/1.0 (personal minecraft launcher)"}


def modrinth(path, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), context=CTX, timeout=20) as r:
        return json.load(r)


def search_mods(q, loaders, versions, offset):
    facets = [["project_type:mod"]]
    if loaders:
        facets.append([f"categories:{x}" for x in loaders])
    if versions:
        facets.append([f"versions:{x}" for x in versions])
    d = modrinth("/search", {"query": q, "facets": json.dumps(facets), "limit": 20,
                             "offset": offset, "index": "relevance" if q else "downloads"})
    keys = ("project_id", "title", "description", "icon_url", "downloads")
    return {"total_hits": d["total_hits"], "hits": [{k: h.get(k) for k in keys} for h in d["hits"]]}


def mod_versions(pid):
    keys = ("id", "version_number", "game_versions", "loaders", "version_type")
    return [{k: v[k] for k in keys} for v in modrinth(f"/project/{pid}/version")]


def install_mod(version_id):
    files = modrinth(f"/version/{version_id}")["files"]
    f = next((x for x in files if x.get("primary")), files[0])
    dst = LIB
    dst.mkdir(parents=True, exist_ok=True)
    name = Path(f["filename"]).name
    with urllib.request.urlopen(urllib.request.Request(f["url"], headers=UA), context=CTX, timeout=60) as r, \
            open(dst / name, "wb") as out:
        shutil.copyfileobj(r, out)
    return name


def safe_name(n):
    return re.sub(r'[\\/:*?"<>|]', "", n or "").strip(" .")


def inst_dir(name):
    d = INST / safe_name(name)
    if not safe_name(name) or not (d / "instance.json").exists():
        raise ValueError("Версия не найдена")
    return d


def safe_target(root, rel):
    p = (root / rel).resolve()
    if root.resolve() not in p.parents:
        raise ValueError("Небезопасный путь: " + rel)
    return p


def create_instance(name, loader, mc):
    n = safe_name(name)
    if not n or not mc:
        raise ValueError("Укажи название и версию")
    if loader not in ("", "fabric", "forge"):
        raise ValueError("Неизвестный загрузчик")
    d = INST / n
    if d.exists():
        raise ValueError("Версия с таким названием уже есть")
    (d / "mods").mkdir(parents=True)
    (d / "instance.json").write_text(json.dumps({"name": n, "loader": loader, "mc": mc}, ensure_ascii=False), "utf-8")
    return {"name": n}


def list_instances():
    ids, out = installed_ids(), []
    for d in sorted(INST.glob("*")) if INST.exists() else []:
        if not (d / "instance.json").exists():
            continue
        m = json.loads((d / "instance.json").read_text("utf-8"))
        out.append({**m, "mods": len(list((d / "mods").glob("*.jar"))),
                    "installed": is_installed(m["loader"], m["mc"], ids)})
    return out


def instance_info(name):
    d = inst_dir(name)
    meta = json.loads((d / "instance.json").read_text("utf-8"))
    files, mods = [], []
    for p in sorted(d.rglob("*")):
        if p.is_file() and p.name != "instance.json":
            rel, size = p.relative_to(d).as_posix(), p.stat().st_size
            files.append({"path": rel, "size": size})
            if rel.startswith("mods/") and rel.count("/") == 1 and rel.endswith(".jar"):
                mods.append({"name": p.name, "size": size})
    return {"meta": meta, "mods": mods, "files": files[:3000], "total": len(files),
            "installed": is_installed(meta["loader"], meta["mc"], installed_ids())}


def remove_file(name, rel):
    safe_target(inst_dir(name), rel).unlink()


def add_from_library(name, files):
    d = inst_dir(name)
    (d / "mods").mkdir(exist_ok=True)
    for f in files:
        shutil.copy2(LIB / Path(f).name, d / "mods" / Path(f).name)
    return {"added": len(files)}


def library_list():
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted(LIB.glob("*.jar"))] if LIB.exists() else []


def open_folder(what, name=None):
    p = inst_dir(name) if what == "instance" else LIB
    p.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(p))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


def import_archive(path, d):
    count = 0
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        mr = "modrinth.index.json" in names
        manifest = mr or "manifest.json" in names
        prefixes = ("overrides/", "client-overrides/") if manifest else ("",)
        strip = ""
        if not manifest:
            firsts = {n.split("/")[0] for n in names}
            if len(firsts) == 1 and all("/" in n for n in names):
                strip = firsts.pop() + "/"
        for n in names:
            if n.endswith("/"):
                continue
            rel = next((n[len(strip + p):] for p in prefixes if n.startswith(strip + p)), "")
            if not rel:
                continue
            t = safe_target(d, rel)
            t.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as src, open(t, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
        if mr:
            files = json.loads(z.read("modrinth.index.json")).get("files", [])
            jobs = [(f["downloads"][0], safe_target(d, f["path"])) for f in files
                    if f.get("downloads") and f.get("env", {}).get("client") != "unsupported"]

            def dl(job):
                url, t = job
                t.parent.mkdir(parents=True, exist_ok=True)
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), context=CTX, timeout=60) as r, \
                        open(t, "wb") as out:
                    shutil.copyfileobj(r, out)

            with ThreadPoolExecutor(8) as ex:
                list(ex.map(dl, jobs))
            count += len(jobs)
    return count


def process_upload(tp, fname, target, inst):
    low = fname.lower()
    try:
        if low.endswith(".jar"):
            dst = LIB if target == "library" else inst_dir(inst) / "mods"
            dst.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tp), dst / fname)
            return {"added": 1, "kind": "mod"}
        if target != "library" and low.endswith((".zip", ".mrpack")):
            return {"added": import_archive(tp, inst_dir(inst)), "kind": "pack"}
        raise ValueError("Поддерживаются .jar, .zip и .mrpack (RAR и 7z пока нет)")
    finally:
        tp.unlink(missing_ok=True)


SETTINGS = ROOT / "settings.json"


def get_settings():
    try:
        return json.loads(SETTINGS.read_text("utf-8"))
    except Exception:
        return {}


def save_settings(b):
    ROOT.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps({**get_settings(), **b}, ensure_ascii=False), "utf-8")


GITHUB_REPO = ""


def ram_gb():
    try:
        return max(1, min(32, int(get_settings().get("ram", 2))))
    except Exception:
        return 2


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def check_update():
    repo = get_settings().get("repo") or GITHUB_REPO
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
        raise ValueError("Укажи GitHub-репозиторий в настройках (имя/репозиторий)")
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), context=CTX, timeout=20) as r:
            rel = json.load(r)
    except urllib.error.HTTPError as e:
        raise ValueError("Релизов нет, или репозиторий не найден" if e.code == 404 else f"GitHub: {e}")
    asset = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".exe")), None)
    return {"current": VERSION, "latest": rel["tag_name"].lstrip("vV"),
            "newer": vtuple(rel["tag_name"]) > vtuple(VERSION), "url": rel["html_url"],
            "asset": asset and asset["name"], "download": asset and asset["browser_download_url"]}


def run_update(info):
    try:
        dst = ROOT / "tmp" / info["asset"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(urllib.request.Request(info["download"], headers=UA),
                                    context=CTX, timeout=60) as r, open(dst, "wb") as out:
            total, done = int(r.headers.get("Content-Length") or 0), 0
            while chunk := r.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if total:
                    state["progress"] = done / total * 100
        state["text"] = "Запуск установщика..."
        subprocess.Popen([str(dst), "/SILENT", "/CLOSEAPPLICATIONS"])
        time.sleep(1)
        os._exit(0)
    except Exception as e:
        state.update(text=f"Ошибка обновления: {e}", busy=False)


def start_update():
    info = check_update()
    if not info["newer"]:
        raise ValueError("Обновлений нет")
    if not (getattr(sys, "frozen", False) and sys.platform == "win32" and info["download"]):
        webbrowser.open(info["url"])
        return {"opened": True}
    if state["busy"]:
        raise ValueError("Подожди, идёт другая операция")
    state.update(busy=True, text="Загрузка обновления...", progress=0)
    threading.Thread(target=run_update, args=(info,), daemon=True).start()
    return {}


POST_ROUTES = {
    "/api/mods/install": lambda b: {"file": install_mod(b["version_id"])},
    "/api/instance/create": lambda b: create_instance(b["name"], b["loader"], b["mc"]),
    "/api/instance/delete": lambda b: shutil.rmtree(inst_dir(b["name"])),
    "/api/instance/add": lambda b: add_from_library(b["name"], b["files"]),
    "/api/instance/remove": lambda b: remove_file(b["name"], b["path"]),
    "/api/library/remove": lambda b: (LIB / Path(b["file"]).name).unlink(),
    "/api/open": lambda b: open_folder(b.get("what"), b.get("name")),
    "/api/settings": lambda b: save_settings(b),
    "/api/update/install": lambda b: start_update(),
}
GET_ROUTES = {
    "/api/instances": lambda q: list_instances(),
    "/api/instance": lambda q: instance_info(q["name"]),
    "/api/library": lambda q: library_list(),
    "/api/settings": lambda q: get_settings(),
    "/api/update": lambda q: check_update(),
    "/api/about": lambda q: {"version": VERSION},
}


def make_callback():
    mx = {"v": 1}

    def set_max(m):
        mx["v"] = max(m, 1)

    def set_progress(p):
        state["progress"] = p / mx["v"] * 100

    return {"setStatus": lambda t: state.update(text=t), "setMax": set_max, "setProgress": set_progress}


def installed_ids():
    try:
        return {v["id"] for v in mll.utils.get_installed_versions(MC)}
    except Exception:
        return set()


def is_installed(loader, mc, ids):
    if loader == "fabric":
        return any(i.startswith("fabric-loader-") and i.endswith("-" + mc) for i in ids)
    if loader == "forge":
        return any(i.startswith(mc + "-forge-") for i in ids)
    return mc in ids


def list_versions(loader):
    ids = installed_ids()
    if loader == "fabric":
        items = [(v["version"], "release" if v["stable"] else "snapshot")
                 for v in mll.fabric.get_all_minecraft_versions()]
    else:
        base = mll.utils.get_version_list()
        if loader == "forge":
            ok = {v.split("-")[0] for v in mll.forge.list_forge_versions()
                  if mll.forge.supports_automatic_install(v)}
            items = [(v["id"], "release") for v in base if v["type"] == "release" and v["id"] in ok]
        else:
            items = [(v["id"], v["type"]) for v in base]
    return [{"id": i, "type": t, "installed": is_installed(loader, i, ids)} for i, t in items]


def get_java(mc_version, cb):
    try:
        rt = mll.runtime.get_version_runtime_information(mc_version, MC)
        name = rt["name"] if rt else "jre-legacy"
        exe = mll.runtime.get_executable_path(name, MC)
        if not exe:
            state["text"] = f"Загрузка Java ({name})..."
            mll.runtime.install_jvm_runtime(name, MC, callback=cb)
            exe = mll.runtime.get_executable_path(name, MC)
        return exe
    except Exception:
        return None


def play(name, mc, loader, launch=True, game_dir=None):
    try:
        cb = make_callback()
        state.update(text="Установка версии...", progress=0)
        mll.install.install_minecraft_version(mc, MC, callback=cb)
        java = get_java(mc, cb)
        run_id = mc

        if loader == "fabric":
            lv = mll.fabric.get_latest_loader_version()
            run_id = f"fabric-loader-{lv}-{mc}"
            if not (Path(MC) / "versions" / run_id).exists():
                state["text"] = "Установка Fabric..."
                mll.fabric.install_fabric(mc, MC, loader_version=lv, callback=cb, java=java)
        elif loader == "forge":
            fv = mll.forge.find_forge_version(mc)
            if not fv:
                raise RuntimeError("Для этой версии нет Forge")
            run_id = mll.forge.forge_to_installed_version(fv)
            if not (Path(MC) / "versions" / run_id).exists():
                state["text"] = "Установка Forge (может занять пару минут)..."
                mll.forge.install_forge_version(fv, MC, callback=cb, java=java)

        if not launch:
            state.update(text="Версия загружена, можно играть!", progress=100)
            return
        opts = {
            "username": name,
            "uuid": str(uuid.uuid3(uuid.NAMESPACE_OID, "OfflinePlayer:" + name)),
            "token": "0",
            "jvmArguments": [f"-Xmx{ram_gb()}G"],
        }
        if java:
            opts["executablePath"] = java
        if game_dir:
            opts["gameDirectory"] = game_dir
        cmd = mll.command.get_minecraft_command(run_id, MC, opts)
        subprocess.Popen(cmd, cwd=game_dir or MC, creationflags=0x08000000 if sys.platform == "win32" else 0)
        state.update(text="Игра запущена!", progress=100)
    except Exception as e:
        state["text"] = f"Ошибка: {e}"
    finally:
        state["busy"] = False


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(BASE / "ui"), **k)

    def log_message(self, *a):
        pass

    def reply(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def upload(self, q):
        try:
            g = lambda k: q.get(k, [""])[0]
            fname = Path(g("filename")).name
            tmp = ROOT / "tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            tp = tmp / fname
            left = int(self.headers.get("Content-Length", 0))
            with open(tp, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            self.reply(200, process_upload(tp, fname, g("target"), g("name")))
        except Exception as e:
            self.reply(502, {"error": str(e)})

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/versions":
            try:
                self.reply(200, list_versions(parse_qs(u.query).get("loader", [""])[0]))
            except Exception as e:
                self.reply(502, {"error": str(e)})
        elif u.path == "/api/mods":
            q = {k: v[0] for k, v in parse_qs(u.query).items()}

            def split(key):
                return [x for x in q.get(key, "").split(",") if x]

            try:
                self.reply(200, search_mods(q.get("q", ""), split("loaders"), split("versions"),
                                            int(q.get("offset", 0))))
            except Exception as e:
                self.reply(502, {"error": str(e)})
        elif u.path == "/api/mod/versions":
            try:
                self.reply(200, mod_versions(parse_qs(u.query)["id"][0]))
            except Exception as e:
                self.reply(502, {"error": str(e)})
        elif u.path in GET_ROUTES:
            try:
                self.reply(200, GET_ROUTES[u.path]({k: v[0] for k, v in parse_qs(u.query).items()}))
            except Exception as e:
                self.reply(502, {"error": str(e)})
        elif u.path == "/api/status":
            self.reply(200, state)
        else:
            super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/upload":
            return self.upload(parse_qs(u.query))
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path in POST_ROUTES:
            try:
                return self.reply(200, POST_ROUTES[self.path](b) or {})
            except Exception as e:
                return self.reply(502, {"error": str(e)})
        if self.path != "/api/play":
            return self.reply(404, {})
        if not b.get("name") or not b.get("version"):
            return self.reply(400, {})
        if state["busy"]:
            return self.reply(409, {})
        gd = str(inst_dir(b["inst"])) if b.get("inst") else None
        state["busy"] = True
        threading.Thread(target=play, args=(b["name"], b["version"], b.get("loader", ""), b.get("launch", True), gd),
                         daemon=True).start()
        self.reply(200, {})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    webview.create_window("Minecraft Launcher", f"http://127.0.0.1:{srv.server_port}", width=1180, height=780, min_size=(900, 600))
    webview.start()