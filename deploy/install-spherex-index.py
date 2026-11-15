"""Download and verify the exact SPHEREx snapshot selected for this build."""

import gzip
from hashlib import file_digest
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
from urllib.request import urlopen


def install(lock, destination):
    metadata = json.loads(lock.read_text())
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not metadata:
        return
    tag = metadata["tag"]
    if not re.fullmatch(r"spherex-index-v1-[0-9]+-[0-9]+", tag):
        raise ValueError("Invalid snapshot tag")
    url = f"https://github.com/azizbekphd/sac26/releases/download/{tag}/spherex.sqlite3.gz"
    with TemporaryDirectory(dir=destination.parent) as temp:
        archive = Path(temp) / "spherex.sqlite3.gz"
        database = Path(temp) / "spherex.sqlite3"
        with urlopen(url, timeout=120) as source, archive.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        with archive.open("rb") as source:
            if file_digest(source, "sha256").hexdigest() != metadata["sha256"]:
                raise ValueError("SPHEREx snapshot checksum mismatch")
        with gzip.open(archive, "rb") as source, database.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        with sqlite3.connect(database) as db:
            built_at, dr1, qr3 = db.execute("SELECT built_at, dr1, qr3 FROM snapshot").fetchone()
        if (built_at != metadata["builtAt"] or {"dr1": dr1, "qr3": qr3} != metadata["counts"]
                or min(dr1, qr3) <= 0):
            raise ValueError("SPHEREx snapshot counts or timestamp mismatch")
        database.replace(destination)
        print(f"Bundled snapshot {tag} ({destination.stat().st_size:,} bytes, DR1 {dr1:,}, QR3 {qr3:,})")


if __name__ == "__main__":
    install(Path(sys.argv[1]), Path(sys.argv[2]))
