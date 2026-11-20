"""Layout-preserving Office conversion when a sandboxed LibreOffice is available."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


LEGACY = {"doc": "docx", "xls": "xlsx", "ppt": "pptx"}
MIMES = {"doc": "application/msword", "xls": "application/vnd.ms-excel", "ppt": "application/vnd.ms-powerpoint"}
FILTERS = {"doc": "MS Word 97", "xls": "MS Excel 97", "ppt": "MS PowerPoint 97",
           "docx": "Office Open XML Text", "xlsx": "Calc MS Excel 2007 XML", "pptx": "Impress MS PowerPoint 2007 XML",
           "odt": "writer8", "ods": "calc8", "odp": "impress8", "odg": "draw8", "rtf": "Rich Text Format"}
FAMILIES = ({"doc", "docx", "odt", "rtf"}, {"xls", "xlsx", "ods"}, {"ppt", "pptx", "odp"})


def convertible(source, target):
    return (source, target) in {("pdf", "odg"), ("odg", "pdf")} or any(
        source in family and (target in family or target == "pdf") for family in FAMILIES)


def convert(raw, source, target):
    # The conversion inputs and output live in an ephemeral mount. The helper
    # sees no user/config directories or network, including during file import.
    sandbox = shutil.which("bwrap")
    executable = os.environ.get("ALEPH_OFFICE_BIN") or shutil.which("soffice")
    if not sandbox or not executable or not convertible(source, target):
        return None
    binary = Path(executable).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return None
    mounts = []
    if binary.is_relative_to("/usr"):
        command = str(binary)
    elif binary.parent.name == "program":
        mounts = ["--ro-bind", str(binary.parent.parent), "/office"]
        command = "/office/program/" + binary.name
    else:
        return None
    with tempfile.TemporaryDirectory(prefix="convert-", dir=".") as sandbox_dir:
        root = Path(sandbox_dir).resolve()
        (root / "out").mkdir()
        (root / ("input." + source)).write_bytes(raw)
        args = [
            sandbox, "--unshare-all", "--die-with-parent", "--new-session",
            "--ro-bind", "/usr", "/usr", "--ro-bind-try", "/bin", "/bin",
            "--ro-bind-try", "/lib", "/lib", "--ro-bind-try", "/lib64", "/lib64",
            "--ro-bind-try", "/etc/fonts", "/etc/fonts", "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
            "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev", *mounts,
            "--bind", str(root), "/work", "--chdir", "/work", "--clearenv",
            "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/tmp",
            "--setenv", "TMPDIR", "/tmp", "--setenv", "LANG", "C.UTF-8", "--setenv", "SAL_USE_VCLPLUGIN", "svp",
            "--setenv", "MAX_CONCURRENCY", "2", "--setenv", "MALLOC_ARENA_MAX", "2", "--setenv", "SAL_DISABLE_OPENCL", "1",
            command, "-env:UserInstallation=file:///tmp/profile", "--headless", "--norestore", "--nodefault",
            "--nofirststartwizard", "--convert-to", target + (":" + FILTERS[target] if target in FILTERS else ""),
            "--outdir", "/work/out", "/work/input." + source,
        ]
        # Calc reserves a larger virtual address range for its workbook engine.
        if any(fmt in FAMILIES[1] for fmt in (source, target)) and (prlimit := shutil.which("prlimit")):
            args[:0] = [prlimit, "--as=4294967296:4294967296", "--"]
        try:
            if subprocess.run(args, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                return None
            output = root / "out" / ("input." + target)
            if output.parent.is_symlink() or output.is_symlink() or not output.is_file() or not 0 < output.stat().st_size <= 16 * 1024 * 1024:
                return None
            return output.read_bytes()
        except (OSError, subprocess.SubprocessError):
            return None
