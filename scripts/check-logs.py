#!/usr/bin/env python3
# pylint: disable=invalid-name

import glob
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

from utils import CI_ROOT, get_build, get_image_name, get_requested_llvm_version, print_red, get_cbl_name, show_builds, die, warn, info


def _fetch(title, url, dest):
    current_time = time.strftime("%H:%M:%S", time.localtime())
    info(f"{current_time}: fetching {title} from: {url}")
    retries = 0
    max_retries = 7
    retry_codes = [404, 500, 504]
    while retries < max_retries:
        try:
            if retries:
                time.sleep(2**retries)
            retries += 1
            urllib.request.urlretrieve(url, dest)
            break
        except ConnectionResetError as err:
            warn(f"{title} download error ('{err}'), retrying...")
        except urllib.error.HTTPError as err:
            if err.code in retry_codes:
                warn(f"{title} download error ({err.code}), retrying...")
            else:
                die(f"{err.code} error trying to download {title}")
        except urllib.error.URLError as err:
            warn(f"{title} download error ('{err}'), retrying...")

    if retries == max_retries:
        die(f"Unable to download {title} after {max_retries} tries")

    if dest.exists():
        info(f"Filesize: {dest.stat().st_size}")
    else:
        die(f"Unable to download {title}")


def verify_build():
    build = get_build()

    # If the build was neither fail nor pass, we need to fetch the status.json
    # of the particular build to try and get an updated result. We attempt this
    # up to 9 times.
    retries = 0
    max_retries = 9
    while retries < max_retries:
        # build never started
        if (build_status := build['tuxbuild_status']) == 'error':
            die(f"msg from tuxsuite: {build['status_message']}")
        # build is finished
        elif build_status == "complete":
            break

        if retries:
            time.sleep(2**retries)
        retries += 1

        status_json = Path(CI_ROOT, "status.json")
        url = build["download_url"] + status_json.name
        _fetch("status.json", url, status_json)
        build = json.loads(status_json.read_text(encoding='utf-8'))

    print(json.dumps(build, indent=4))

    if retries == max_retries:
        die("Build is not finished on TuxSuite's side!")

    if "Build Timed Out" in build["status_message"]:
        die(build["status_message"])

    if build["status_message"] == "Unable to apply kernel patch":
        print_red(
            "ERROR: Patch failed to apply to current kernel tree, does it need to be removed or updated?"
        )
        fetch_logs(build)
        sys.exit(1)

    return build


def fetch_logs(build):
    log = Path(CI_ROOT, "build.log")
    url = build["download_url"] + log.name
    _fetch("logs", url, log)
    print(log.read_text(encoding='utf-8'))


def check_log(build):
    warnings_count = build["warnings_count"]
    errors_count = build["errors_count"]
    if warnings_count + errors_count > 0:
        info(f"{warnings_count} warnings, {errors_count} errors")
        fetch_logs(build)


def fetch_dtb(build):
    config = os.environ["CONFIG"]
    if config not in ("multi_v5_defconfig", "aspeed_g5_defconfig"):
        return
    dtb = {
        "multi_v5_defconfig": "aspeed-bmc-opp-palmetto.dtb",
        "aspeed_g5_defconfig": "aspeed-bmc-opp-romulus.dtb",
    }[config]
    remote_dtb_path = f"dtbs/{dtb}"
    (dtb_path := Path(CI_ROOT, remote_dtb_path)).parent.mkdir(exist_ok=True)
    url = build["download_url"] + remote_dtb_path
    _fetch("DTB", url, dtb_path)


def fetch_kernel_image(build):
    image_name = Path(CI_ROOT, get_image_name())
    url = build["download_url"] + image_name.name
    _fetch("kernel image", url, image_name)


def fetch_built_config(build):
    url = build["download_url"] + "config"
    _fetch("built .config", url, Path(CI_ROOT, ".config"))


def check_built_config(build):
    # Only check built configs if we have specific CONFIGs requested.
    custom = False
    for config in build["kconfig"]:
        if 'CONFIG' in config:
            custom = True
    if not custom:
        return

    fetch_built_config(build)
    # Build dictionary of CONFIG_NAME: y/m/n ("is not set" translates to 'n').
    configs = {}
    with Path(CI_ROOT, '.config').open(encoding='utf-8') as file:
        for rawline in file:
            if not (line := rawline.strip()):
                continue

            name = None
            state = None
            if '=' in line:
                name, state = line.split('=', 1)
            elif line.startswith("# CONFIG_"):
                name, state = line.split(" ", 2)[1:]
                if state != "is not set":
                    warn(
                        f"Could not parse '{name}' from .config line '{line}'!?"
                    )
                state = 'n'
            elif not line.startswith("#"):
                warn(f"Could not parse .config line '{line}'!?")
            configs[name] = state

    # Compare requested configs against the loaded dictionary.
    fail = False
    for config in build["kconfig"]:
        if 'CONFIG' not in config:
            continue
        name, state = config.split('=')
        # If a config is missing from the dictionary, it is considered 'n'.
        if state != configs.get(name, 'n'):
            print_red(f"ERROR: {config} not found in .config!")
            fail = True
        else:
            print(f"ok: {name}={state}")
    if fail:
        sys.exit(1)


def print_clang_info(build):
    # There is no point in printing the clang version information for anything
    # other than clang-nightly because the stable branches are very unlikely to
    # have regressions that require triage based on build date and revision
    # information
    if get_requested_llvm_version() != "clang-nightly":
        return

    metadata_file = Path(CI_ROOT, "metadata.json")
    url = build["download_url"] + metadata_file.name
    _fetch(metadata_file, url, metadata_file)
    metadata_json = json.loads(metadata_file.read_text(encoding='utf-8'))
    info("Printing clang-nightly checkout date and hash")
    parse_cmd = [
        Path(CI_ROOT, "scripts/parse-debian-clang.py"), "--print-info",
        "--version-string", metadata_json["compiler"]["version_full"]
    ]
    subprocess.run(parse_cmd, check=True)


def fetch_boot_utils_file(file_path):
    url = f"https://github.com/ClangBuiltLinux/boot-utils/raw/main/{file_path.name}"
    _fetch(file_path.name, url, file_path)
    file_path.chmod(0o755)


def run_boot(build):
    cbl_arch = get_cbl_name()
    kernel_image = Path(CI_ROOT, get_image_name())

    (boot_utils := Path(CI_ROOT, 'boot-utils')).mkdir(exist_ok=True)

    if cbl_arch == "um":
        boot_cmd = [Path(boot_utils, 'boot-uml.py')]
        # The execute bit needs to be set to avoid "Permission denied" errors
        kernel_image.chmod(0o755)
    else:
        boot_cmd = [Path(boot_utils, 'boot-qemu.py'), "-a", cbl_arch]

    fetch_boot_utils_file(boot_cmd[0])
    fetch_boot_utils_file(Path(boot_utils, 'utils.py'))

    boot_cmd += [
        '--gh-json-file',
        Path(CI_ROOT, 'boot-utils.json'),
        "-k",
        kernel_image,
    ]
    # If we are running a sanitizer build, we should increase the number of
    # cores and timeout because booting is much slower
    if "CONFIG_KASAN=y" in build["kconfig"] or \
       "CONFIG_KCSAN=y" in build["kconfig"] or \
       "CONFIG_UBSAN=y" in build["kconfig"]:
        boot_cmd += ["-s", "4"]
        if "CONFIG_KASAN=y" in build["kconfig"]:
            boot_cmd += ["-t", "20m"]
        else:
            boot_cmd += ["-t", "10m"]
        if "CONFIG_KASAN_KUNIT_TEST=y" in build["kconfig"] or \
           "CONFIG_KCSAN_KUNIT_TEST=y" in build["kconfig"]:
            info("Disabling Oops problem matcher under Sanitizer KUnit build")
            print("::remove-matcher owner=linux-kernel-oopses::")

    # Before spawning a process with potentially different IO buffering,
    # flush the existing buffers so output is ordered correctly.
    sys.stdout.flush()
    sys.stderr.flush()

    try:
        subprocess.run(boot_cmd, check=True)
    except subprocess.CalledProcessError as err:
        if err.returncode == 124:
            print_red("ERROR: Image failed to boot")
        raise err


def boot_test(build):
    if build["result"] == "unknown":
        die("unknown build result, skipping boot")
    if build["result"] == "fail":
        die("fatal build errors encountered during build, skipping boot")
    if "BOOT" in os.environ and os.environ["BOOT"] == "0":
        info("boot test disabled via config, skipping boot")
        return
    fetch_kernel_image(build)
    fetch_dtb(build)
    run_boot(build)


if __name__ == "__main__":
    missing = []
    for var in ["ARCH", "CONFIG", "LLVM_VERSION"]:
        if var not in os.environ:
            missing.append(var)
    if missing:
        for var in missing:
            print_red(f"ERROR: ${var} must be specified")
        show_builds()
        sys.exit(1)
    verified_build = verify_build()
    info("Register clang error/warning problem matchers")
    for problem_matcher in glob.glob(".github/problem-matchers/*.json"):
        print(f"::add-matcher::{problem_matcher}")
    print_clang_info(verified_build)
    check_log(verified_build)
    check_built_config(verified_build)
    boot_test(verified_build)
