"""Backport ORT's upstream ARM CPU-ID bounds fix; leave model/operator code intact.

Upstream: https://github.com/microsoft/onnxruntime/pull/28344
The 1.20.1 source can index vectors outside their bounds during ArmLinuxInit,
corrupting the heap and crashing in CPUIDInfo's destructor at process exit.
"""

import argparse
from pathlib import Path

FIX_ID = "ort-arm-cpuid-bounds-v1"
ORIGINAL = """      auto coreid = proc->linux_id;
      auto uarch = corep->uarch;
      core_uarchs_[coreid] = uarch;
"""
FIXED = """      auto coreid = proc->linux_id;
      if (coreid < 0 || static_cast<size_t>(coreid) >= core_uarchs_.size()) {
        continue;
      }

      auto uarch = corep->uarch;
      core_uarchs_[coreid] = uarch;
"""


def prepare(path):
    original = path.read_text()
    if original.count(FIXED) == 1 and ORIGINAL not in original:
        return
    if original.count(ORIGINAL) != 1 or FIXED in original:
        raise ValueError("Unexpected ORT CPUIDInfo source; inspect before applying upstream fix")
    backup = path.with_name(path.name + ".before-arm-cpuid-bounds")
    if not backup.exists():
        backup.write_text(original)
    path.write_text(original.replace(ORIGINAL, FIXED, 1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    prepare(parser.parse_args().source)
    print(FIX_ID)
