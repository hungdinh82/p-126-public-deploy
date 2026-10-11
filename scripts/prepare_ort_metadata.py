"""Update only the verified Eigen archive checksum in ORT build metadata."""

import argparse
from pathlib import Path

EIGEN_URL = "https://gitlab.com/libeigen/eigen/-/archive/e7248b26a1ed53fa030c5c459f7ea095dfd276ac/eigen-e7248b26a1ed53fa030c5c459f7ea095dfd276ac.zip"
OLD_SHA1 = "be8be39fdbc6e60e94fa7870b280707069b5b81a"
VERIFIED_SHA1 = "32b145f525a8308d7ab1c09388b2e288312d8eba"


def prepare(path):
    original = path.read_text()
    lines = original.splitlines(keepends=True)
    indexes = [i for i, line in enumerate(lines) if line.startswith("eigen;")]
    if len(indexes) != 1:
        raise ValueError("Expected exactly one pinned Eigen dependency")
    index = indexes[0]
    fields = lines[index].strip().split(";")
    if fields[1] != EIGEN_URL or fields[2] not in (OLD_SHA1, VERIFIED_SHA1):
        raise ValueError("Unexpected Eigen revision/checksum; inspect before building")
    if fields[2] == VERIFIED_SHA1:
        return
    backup = path.with_name(path.name + ".before-eigen-checksum")
    if not backup.exists():
        backup.write_text(original)
    lines[index] = f"eigen;{EIGEN_URL};{VERIFIED_SHA1}\n"
    path.write_text("".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deps", type=Path)
    prepare(parser.parse_args().deps)
