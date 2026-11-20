"""Brings verknode's package index (index.json) up to date with the packages' GitHub releases: each release
vX.Y.Z of a listed repository becomes a version, with the node API range of its pyproject.toml ([tool.verknode]
api) and how to install it (the release's wheel, else the tag from git). Run by packages-index.yml every day.

    python packages/update_index.py [index.json]        # GITHUB_TOKEN: for the API's rate limit
"""
import json
import os
import re
import sys
import tomllib
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def get(url: str, raw: bool = False):
    headers = {"User-Agent": "verknode-index", "Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN") and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        data = r.read()
    return data if raw else json.loads(data)


def version_key(v: str):
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.+-]", v))


def update(entry: dict) -> list[str]:
    m = re.match(r"https://github.com/([^/]+)/([^/]+?)(?:\.git)?/?$", entry.get("repository", ""))
    if not m:
        return []
    owner, repo = m.groups()
    have = {v["version"] for v in entry.get("versions", [])}
    added = []
    for rel in get(f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=100"):
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = rel["tag_name"]
        try:
            meta = tomllib.loads(get(f"https://raw.githubusercontent.com/{owner}/{repo}/{tag}/pyproject.toml", raw=True)
                                 .decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"{entry['name']} {tag}: no pyproject.toml ({e})")
            continue
        version = meta["project"]["version"]
        api = meta.get("tool", {}).get("verknode", {}).get("api")
        if version in have or not api or tag.lstrip("v") != version:
            continue
        dist = meta["project"]["name"]
        wheel = next((a["browser_download_url"] for a in rel.get("assets", []) if a["name"].endswith(".whl")), None)
        install = f"{dist} @ {wheel}" if wheel else f"{dist} @ git+https://github.com/{owner}/{repo}@{tag}"
        entry.setdefault("versions", []).append({"version": version, "api": api, "install": install,
                                                  "released": rel.get("published_at", "")[:10]})
        entry["dist"] = dist
        added.append(f"{entry['name']} {version}")
    entry["versions"].sort(key=lambda v: version_key(v["version"]))
    return added


def main(path: str):
    with open(path, encoding="utf-8") as fh:
        index = json.load(fh)
    added = []
    for entry in index["packages"]:
        try:
            added += update(entry)
        except Exception as e:  # noqa: BLE001 - one unreachable repository: the others are still brought up to date
            print(f"{entry.get('name')}: {e}")
    if added:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(index, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    print("added:", ", ".join(added) or "nothing")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "index.json"))
