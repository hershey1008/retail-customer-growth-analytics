"""Download and extract the official UCI Online Retail workbook."""

from __future__ import annotations

import argparse
import shutil
import ssl
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import certifi


DATA_URL = "https://archive.ics.uci.edu/static/public/352/online+retail.zip"
ARCHIVE_MEMBER = "Online Retail.xlsx"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "raw" / "online_retail.xlsx"


def download_dataset(output: Path = DEFAULT_OUTPUT, force: bool = False) -> Path:
    """Download the UCI archive and extract the workbook to *output*."""
    output = output.resolve()
    if output.exists() and not force:
        print(f"Dataset already exists: {output}")
        return output

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="online-retail-") as temp_dir:
        archive_path = Path(temp_dir) / "online-retail.zip"
        print(f"Downloading {DATA_URL}")
        request = urllib.request.Request(
            DATA_URL,
            headers={"User-Agent": "DATA_ANALYSIS portfolio project"},
        )
        # Use certifi explicitly so the download is reproducible even when a
        # local Windows certificate store contains a malformed certificate.
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=120, context=ssl_context) as response:
            with archive_path.open("wb") as destination:
                shutil.copyfileobj(response, destination)

        if not zipfile.is_zipfile(archive_path):
            raise RuntimeError("Downloaded file is not a valid ZIP archive")
        with zipfile.ZipFile(archive_path) as archive:
            if ARCHIVE_MEMBER not in archive.namelist():
                raise RuntimeError(
                    f"Expected {ARCHIVE_MEMBER!r}; archive contains {archive.namelist()}"
                )
            with archive.open(ARCHIVE_MEMBER) as source, output.open("wb") as destination:
                shutil.copyfileobj(source, destination)

    if output.stat().st_size < 1_000_000:
        raise RuntimeError("Extracted workbook is unexpectedly small")
    print(f"Saved dataset: {output} ({output.stat().st_size / 1_000_000:.1f} MB)")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    download_dataset(args.output, force=args.force)


if __name__ == "__main__":
    main()
