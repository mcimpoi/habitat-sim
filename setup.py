#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""
Adapted from: http://www.benjack.io/2017/06/12/python-cpp-tests.html
"""

import argparse
import builtins
import glob
import json
import os
import os.path as osp
import re
import shlex
import subprocess
import sys
from distutils.version import StrictVersion

from setuptools import Extension, find_packages, setup
from setuptools.command.build_ext import build_ext

ARG_CACHE_BLACKLIST = {"force_cmake"}


def build_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        help="""Build in headless mode.
Use "HEADLESS=True pip install ." to build in headless mode with pip""",
    )
    parser.add_argument(
        "--force-cmake",
        "--cmake",
        dest="force_cmake",
        action="store_true",
        help="Forces cmake to be rerun",
    )
    parser.add_argument(
        "--build-tests", dest="build_tests", action="store_true", help="Build tests"
    )
    parser.add_argument(
        "--cmake-args",
        type=str,
        default="",
        help="""Additional arguements to be passed to cmake.
Note that you will need to do `--cmake-args="..."` as `--cmake-args "..."`
will generally not be parsed correctly
You may need to use --force-cmake to ensure cmake is rerun with new args.
Use "CMAKE_ARGS="..." pip install ." to set cmake args with pip""",
    )

    parser.add_argument(
        "--no-update-submodules",
        dest="no_update_submodules",
        action="store_true",
        help="Don't update git submodules",
    )

    parser.add_argument(
        "--no-cached-args",
        dest="no_cached_args",
        action="store_true",
        help="Does not reload cached args for setup.py",
    )

    return parser


parseable_args = []
unparseable_args = []
for i, arg in enumerate(sys.argv):
    if arg == "--":
        unparseable_args = sys.argv[i:]
        break

    parseable_args.append(arg)


parser = build_parser()
args, filtered_args = parser.parse_known_args(args=parseable_args)

sys.argv = filtered_args + unparseable_args


def in_git():
    try:
        subprocess.check_output(["git", "rev-parse", "--is-inside-work-tree"])
        return True
    except:
        return False


def has_ninja():
    try:
        subprocess.check_output(["ninja", "--version"])
        return True
    except:
        return False


def is_pip():
    # This will end with python if driven with python setup.py ...
    return osp.basename(os.environ.get("_", "/pip/no")).startswith("pip")


class CMakeExtension(Extension):
    def __init__(self, name, sourcedir=""):
        Extension.__init__(self, name, sources=[])
        self.sourcedir = os.path.abspath(sourcedir)


class CMakeBuild(build_ext):
    def run(self):
        try:
            subprocess.check_output(["cmake", "--version"])
        except OSError:
            raise RuntimeError(
                "CMake must be installed to build the following extensions: "
                + ", ".join(e.name for e in self.extensions)
            )

        for ext in self.extensions:
            self.build_extension(ext)

    def build_extension(self, ext):
        args_cache_file = osp.join(self.build_temp, "setuppy_args.json")

        if not is_pip() and not args.no_cached_args and osp.exists(args_cache_file):
            with open(args_cache_file, "r") as f:
                cached_args = json.load(f)

            for k, v in cached_args.items():
                if k not in ARG_CACHE_BLACKLIST:
                    setattr(args, k, v)

        extdir = os.path.abspath(os.path.dirname(self.get_ext_fullpath(ext.name)))

        if in_git() and not args.no_update_submodules:
            subprocess.check_call(
                ["git", "submodule", "update", "--init", "--recursive"]
            )

        cmake_args = [
            "-DCMAKE_LIBRARY_OUTPUT_DIRECTORY=" + extdir,
            "-DPYTHON_EXECUTABLE=" + sys.executable,
            "-DCMAKE_EXPORT_COMPILE_COMMANDS={}".format("OFF" if is_pip() else "ON"),
        ]
        cmake_args += shlex.split(args.cmake_args)

        cfg = "Debug" if self.debug else "RelWithDebInfo"
        build_args = ["--config", cfg]

        cmake_args += ["-DCMAKE_BUILD_TYPE=" + cfg]
        build_args += ["--"]

        if has_ninja():
            cmake_args += ["-GNinja"]
        else:
            build_args += ["-j"]

        cmake_args += [
            "-DBUILD_GUI_VIEWERS={}".format("ON" if not args.headless else "OFF")
        ]
        cmake_args += ["-DBUILD_TESTS={}".format("ON" if args.build_tests else "OFF")]

        env = os.environ.copy()
        env["CXXFLAGS"] = '{} -DVERSION_INFO=\\"{}\\"'.format(
            env.get("CXXFLAGS", ""), self.distribution.get_version()
        )

        if self.run_cmake(cmake_args):
            subprocess.check_call(
                shlex.split("cmake -H{} -B{}".format(ext.sourcedir, self.build_temp))
                + cmake_args,
                env=env,
            )

        subprocess.check_call(shlex.split("cmake --build {}".format(self.build_temp)))
        print()  # Add an empty line for cleaner output

        # The things following this don't work with pip
        if is_pip():
            return

        if not args.headless:
            link_dst = osp.join(osp.dirname(self.build_temp), "viewer")
            if not osp.islink(link_dst):
                os.symlink(
                    osp.abspath(osp.join(self.build_temp, "utils/viewer/viewer")),
                    link_dst,
                )

        if not osp.islink(osp.join(osp.dirname(self.build_temp), "utils")):
            os.symlink(
                osp.abspath(osp.join(self.build_temp, "utils")),
                osp.join(osp.dirname(self.build_temp), "utils"),
            )

        self.create_compile_commands()
        with open(args_cache_file, "w") as f:
            json.dump(vars(args), f, indent=4, sort_keys=True)

    def run_cmake(self, cmake_args):
        if args.force_cmake:
            return True

        cache_parser = re.compile(r"(?P<K>\w+?)(:\w+?|)=(?P<V>.*?)$")

        cmake_cache = osp.join(self.build_temp, "CMakeCache.txt")
        if osp.exists(cmake_cache):
            with open(cmake_cache, "r") as f:
                cache_contents = f.readlines()

            for arg in cmake_args:
                if arg[0:2] == "-G":
                    continue

                k, v = arg.split("=", 1)
                # Strip +D
                k = k[2:]
                for l in cache_contents:

                    match = cache_parser.match(l)
                    if match is None:
                        continue

                    if match.group("K") == k and match.group("V") != v:
                        return True

            return False

        return True

    def create_compile_commands(self):
        def load(filename):
            with open(filename) as f:
                return json.load(f)

        commands = glob.glob("build/*/compile_commands.json")
        all_commands = [entry for f in commands for entry in load(f)]

        # cquery does not like c++ compiles that start with gcc.
        # It forgets to include the c++ header directories.
        # We can work around this by replacing the gcc calls that python
        # setup.py generates with g++ calls instead
        for command in all_commands:
            if command["command"].startswith("gcc "):
                command["command"] = "g++ " + command["command"][4:]

        new_contents = json.dumps(all_commands, indent=2)
        contents = ""
        if os.path.exists("compile_commands.json"):
            with open("compile_commands.json", "r") as f:
                contents = f.read()
        if contents != new_contents:
            with open("compile_commands.json", "w") as f:
                f.write(new_contents)


if __name__ == "__main__":
    assert StrictVersion(
        "{}.{}".format(sys.version_info[0], sys.version_info[1])
    ) >= StrictVersion("3.6"), "Must use python3.6 or newer"

    if os.environ.get("HEADLESS", "").lower() == "true":
        args.headless = True

    if os.environ.get("CMAKE_ARGS", None) is not None:
        args.cmake_args = os.environ["CMAKE_ARGS"]

    requirements = ["attrs", "numba", "numpy", "numpy-quaternion", "pillow"]

    builtins.__HSIM_SETUP__ = True
    import habitat_sim

    setup(
        name="habitat_sim",
        version=habitat_sim.__version__,
        author="FAIR A-STAR",
        description="A high performance simulator for training embodied agents",
        long_description="",
        packages=find_packages(),
        install_requires=requirements,
        # add extension module
        ext_modules=[CMakeExtension("habitat_sim._ext.habitat_sim_bindings", "src")],
        # add custom build_ext command
        cmdclass=dict(build_ext=CMakeBuild),
        zip_safe=False,
    )
