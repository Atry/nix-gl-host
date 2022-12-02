#!/usr/bin/env python3

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

IN_NIX_STORE = False

if IN_NIX_STORE:
    # The following paths are meant to be substituted by Nix at build
    # time.
    PATCHELF_PATH = "@patchelf-bin@"
else:
    PATCHELF_PATH = "patchelf"


# The following regexes list has been figured out by looking at the
# output of nix-build -A linuxPackages.nvidia_x11 before running
# ls ./result/lib | grep -E ".so$".
#
# TODO: find a more systematic way to figure out these names *not
# requiring to build/fetch the nvidia driver at runtime*.
NVIDIA_DSO_PATTERNS = [
    "libEGL_nvidia\.so.*$",
    "libGLESv1_CM_nvidia\.so.*$",
    "libGLESv2_nvidia\.so.*$",
    "libGLX_nvidia\.so.*$",
    "libglxserver_nvidia\.so.*$",
    "libnvcuvid\.so.*$",
    "libnvidia-allocator\.so.*$",
    "libnvidia-cfg\.so.*$",
    "libnvidia-compiler\.so.*$",
    "libnvidia-eglcore\.so.*$",
    "libnvidia-egl-gbm\.so.*$",
    "libnvidia-egl-wayland\.so.*$",
    "libnvidia-encode\.so.*$",
    "libnvidia-fbc\.so.*$",
    "libnvidia-glcore\.so.*$",
    "libnvidia-glsi\.so.*$",
    "libnvidia-glvkspirv\.so.*$",
    "libnvidia-ml\.so.*$",
    "libnvidia-ngx\.so.*$",
    "libnvidia-nvvm\.so.*$",
    "libnvidia-opencl\.so.*$",
    "libnvidia-opticalflow\.so.*$",
    "libnvidia-ptxjitcompiler\.so.*$",
    "libnvidia-rtcore\.so.*$",
    "libnvidia-tls\.so.*$",
    "libnvidia-vulkan-producer\.so.*$",
    "libnvidia-wayland-client\.so.*$",
    "libnvoptix\.so.*$",

    # Host dependencies required by the nvidia DSOs to properly
    # operate

    # libdrm
    "libdrm\.so.*$",
    # libffi
    "libffi\.so.*$",
    # libgbm
    "libgbm\.so.*$",
    # Cannot find that one :(
    "libnvtegrahv\.so.*$",
    # libexpat
    "libexpat\.so.*$",
    # libxcb
    "libxcb-glx\.so.*$",
    # Coming from libx11
    "libX11-xcb\.so.*$",
    "libX11\.so.*$",
    "libXext\.so.*$",
    # libwayland
    "libwayland-server\.so.*$",
    "libwayland-client\.so.*$",

]

CUDA_DSO_PATTERNS = [
    "libcudadebugger\.so.*$",
    "libcuda\.so.*$"
]

def find_files(path, files_patterns):
    """Scans the PATH directory looking for the files complying with
    the FILES_PATTERNS regexes list.

    Returns the list of the DSOs absolute paths."""
    files = []

    def is_dso_matching_pattern(filename):
        for pattern in files_patterns:
            if re.search(pattern, filename):
                return True
        return False

    for f in os.listdir(path):
        abs_file_path = os.path.abspath(os.path.join(path, f))
        if os.path.isfile(abs_file_path) and is_dso_matching_pattern(abs_file_path):
            files.append(abs_file_path)

    return files

def find_nvidia_dsos(path):
    """Scans the PATH directory looking for the Nvidia driver shared
    libraries and their dependencies. A shared library is considered
    as a Nvidia one if its name maches a pattern contained in
    CUDA_DSO_PATTERNS.

    Returns the list of the DSOs absolute paths."""
    return find_files(path, NVIDIA_DSO_PATTERNS)

def find_cuda_dsos(path):
    """Scans the PATH directory looking for the cuda driver shared
    libraries. A shared library is considered
    as a cuda one if its name maches a pattern contained in
    CUDA_DSO_PATTERNS.

    Returns the list of the DSOs absolute paths."""
    return find_files(path, CUDA_DSO_PATTERNS)

def copy_and_patch_dsos_to_libs_dir(dsos, libs_dir):
    """Copies the graphic vendor DSOs to the cache directory before
    patchelf-ing them.

    The DSOs can dlopen each other. Sadly, we don't want any host
    libraries to the LD_LIBRARY_PATH to prevent polluting the nix
    binary env. We won't be able to find them on runtime. We don't
    want to alter LD_LIBRARY_PATH, the only option left is to patch
    their ELFs runpath.

    We also don't want to directly modify the host DSOs, we first copy
    them to the user's personal cache directory. We then alter their
    runpath to point to the cache directory."""
    for dso in dsos:
        basename = os.path.basename(dso)
        newpath = os.path.join(libs_dir, basename)
        log_info(f"Copying {basename} to {newpath}")
        shutil.copyfile(dso, newpath)
        shutil.copymode(dso, newpath)
        patch_dso(newpath, libs_dir)


def log_info(string):
    """Prints STR to STDERR if the DEBUG environment variable is
    set."""
    if "DEBUG" in os.environ:
        print(f"[+] {string}", file=sys.stderr)


def patch_dso(dsoPath, rpath):
    """Call patchelf to change the DSOPATH runpath with RPATH."""
    log_info(f"Patching {dsoPath}")
    log_info(f"Exec: {PATCHELF_PATH} --set-rpath {rpath} {dsoPath}")
    res = subprocess.run([PATCHELF_PATH, "--set-rpath", rpath, dsoPath])
    if res.returncode != 0:
        raise (f"Cannot patch {dsoPath}. Patchelf exited with {res.returncode}")

def generate_nvidia_egl_config_files(cache_dir, libs_dir):
    """Generates a set of JSON files describing the EGL exec
    envirnoment to libglvnd.

    These configuration files will point to the EGL, wayland and GBM
    Nvidia DSOs."""

    def generate_egl_conf_json(dso):
        return json.dumps({
            "file_format_version": "1.0.0",
            "ICD": {
                "library_path": dso
            }})

    egl_conf_dir = os.path.join(cache_dir, "egl-confs")
    os.makedirs(egl_conf_dir, exist_ok=True)
    dso_paths = [ ("10_nvidia.json", f"{libs_dir}/libEGL_nvidia.so.0"),
                  ("10_nvidia_wayland.json", f"{libs_dir}/libnvidia-egl-wayland.so.1"),
                  ("15_nvidia_gbm.json", f"{libs_dir}/libnvidia-egl-gbm.so.1") ]

    for (conf_file_name, dso_path) in dso_paths:
        with open(os.path.join(egl_conf_dir, conf_file_name), "w", encoding = "utf-8") as f:
            log_info(f"Writing {dso_path} conf to {egl_conf_dir}")
            f.write(generate_egl_conf_json(dso_path))

    return egl_conf_dir

def exec_binary(bin_path, args, cache_dir, libs_dir):
    """Replace the current python program with the program pointed by
    BIN_PATH.

    Sets the relevant libGLvnd env variables."""
    log_info(f"Execv-ing {bin_path}")
    log_info(f"Goodbye now.")
    # The following two env variables are required by our patched libglvnd
    # implementation to figure out what kind of driver the host
    # machine is using.
    os.environ["NIX_GLVND_GLX_PATH"] = libs_dir
    os.environ["__GLX_VENDOR_LIBRARY_NAME"] = "nvidia"
    # The following env variable is pointing to the directory
    # containing the EGL configuration.
    os.environ["__EGL_VENDOR_LIBRARY_DIRS"] = generate_nvidia_egl_config_files(cache_dir, libs_dir)
    os.execv(bin_path, [bin_path] + args)


def main(args):
    # 1. Scan NIX_GLVND_GLX_PATH for nvidia DSOs
    # 2. Copy DSOs
    # 3. Patchelf DSOs
    # 4. Execv program
    home = os.path.expanduser("~")
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME", os.path.join(home, ".cache"))
    cache_dir = os.path.join(xdg_cache_home, "nix-gl-host")
    libs_dir = os.path.join(cache_dir, "lib")
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(libs_dir, exist_ok=True)
    log_info(f'Using "{cache_dir}" as cache dir.')
    log_info(f'Scanning "{args.GL_VENDOR_PATH}" for DSOs.')
    dsos = find_nvidia_dsos(args.GL_VENDOR_PATH)
    log_info(f"Found the following DSOs:")
    [log_info(dso) for dso in dsos]
    log_info("Patching the DSOs.")
    copy_and_patch_dsos_to_libs_dir(dsos, libs_dir)
    exec_binary(args.NIX_BINARY, args.ARGS, cache_dir, libs_dir)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="nixglhost-wrapper",
        description="Wrapper used to massage the host GL drivers to work with your nix-built binary.",
    )
    parser.add_argument(
        "GL_VENDOR_PATH",
        type=str,
        help="a path pointing to the directory containing your GL driver shared libraries",
    )
    parser.add_argument(
        "NIX_BINARY",
        type=str,
        help="Nix-built binary you'd like to wrap.",
    )
    parser.add_argument(
        "ARGS",
        type=str,
        nargs="*",
        help="The args passed to the wrapped binary.",
    )
    args = parser.parse_args()
    ret = main(args)
    os.exit(ret)
