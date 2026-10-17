"""Download the public dataset when needed, then rebuild every project artifact."""

from src.download_data import DEFAULT_OUTPUT, download_dataset
from src.pipeline import run


if __name__ == "__main__":
    download_dataset(DEFAULT_OUTPUT)
    run(DEFAULT_OUTPUT)
