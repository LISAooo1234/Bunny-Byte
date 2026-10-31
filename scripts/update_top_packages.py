"""刷新依赖守卫内置的热门包名单（bunnybyte/data/top_pypi.txt、top_npm.txt）。

来源：
- PyPI：https://github.com/hugovk/top-pypi-packages（最近 30 天下载量排序）
- npm：https://github.com/wooorm/npm-high-impact（MIT，按下载量 / 依赖数排序）

用法：python scripts/update_top_packages.py [--count 10000]
"""

import argparse
import json
import re
import urllib.request
from datetime import date
from pathlib import Path

PYPI_URL = "https://hugovk.github.io/top-pypi-packages/top-pypi-packages.min.json"
NPM_URL = "https://raw.githubusercontent.com/wooorm/npm-high-impact/main/lib/top.js"
DATA_DIR = Path(__file__).resolve().parents[1] / "bunnybyte" / "data"


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=10000)
    args = parser.parse_args()
    pypi = json.loads(fetch(PYPI_URL))
    pypi_names = [row["project"] for row in pypi["rows"]][: args.count]
    npm_names = re.findall(r"^\s*'([^']+)',?$", fetch(NPM_URL), re.M)[: args.count]
    today = date.today().isoformat()
    write(DATA_DIR / "top_pypi.txt", pypi_names, f"hugovk/top-pypi-packages {pypi.get('last_update', '')}, fetched {today}")
    write(DATA_DIR / "top_npm.txt", npm_names, f"wooorm/npm-high-impact (MIT), fetched {today}")


def write(path, names, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {source}; ordered by popularity\n" + "\n".join(names) + "\n", encoding="utf-8")
    print(f"{path.name}: {len(names)} names")


if __name__ == "__main__":
    main()
