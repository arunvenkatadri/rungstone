"""Compile OpenPLC translations with MatIEC (iec2c) and gcc in Docker, the way the OpenPLC runtime builds them.

Development check only: it needs Docker and an image built from tools/matiec/Dockerfile, which takes
OpenPLC's own copy of MatIEC (utils/matiec_src). Nothing is uploaded to a runtime or run.

  docker build -t rungstone-matiec tools/matiec
  python3 tools/compile_openplc.py out/Line3_openplc.st [more.st ...]

Prints one JSON result per file: iec2c ok/errors, then gcc ok/errors on the generated C.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

IMAGE = os.environ.get("RUNGSTONE_MATIEC_IMAGE", "rungstone-matiec")
SCRIPT = r"""
cd /tmp && mkdir out && cd out
iec2c -f -l -p -r -R -a -I /matiec_lib -T /tmp/out /work/in.st > /tmp/iec2c.log 2>&1; rc=$?
# iec2c can exit 0 after "Bailing out!" on a syntax error
grep -qi error /tmp/iec2c.log && rc=1
echo "IEC2C_RC=$rc"; grep -i error /tmp/iec2c.log | head -20
if [ $rc -eq 0 ]; then
  gcc -c -w -I /matiec_lib/C Config0.c -o Config0.o > /tmp/gcc.log 2>&1 && gcc -c -w -I /matiec_lib/C Res0.c -o Res0.o >> /tmp/gcc.log 2>&1
  echo "GCC_RC=$?"; head -20 /tmp/gcc.log
fi
"""


def image_available():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


def compile_st(path):
    d = tempfile.mkdtemp(prefix="rungstone-oplc-")
    try:
        shutil.copy(path, os.path.join(d, "in.st"))
        p = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", "%s:/work:ro" % d, IMAGE, "bash", "-c", SCRIPT],
                           capture_output=True, text=True, timeout=300)
        out = p.stdout
        res = {"file": path, "iec2c_ok": "IEC2C_RC=0" in out, "gcc_ok": "GCC_RC=0" in out,
               "log": [l for l in out.splitlines() if l and not l.startswith(("IEC2C_RC", "GCC_RC"))]}
        res["ok"] = res["iec2c_ok"] and res["gcc_ok"]
        return res
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main(argv):
    if not argv:
        sys.exit(__doc__)
    if not image_available():
        sys.exit("Docker image %s not found. Build it: docker build -t %s tools/matiec" % (IMAGE, IMAGE))
    results = [compile_st(f) for f in argv]
    print(json.dumps(results, indent=1))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
