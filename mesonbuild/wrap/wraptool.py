# SPDX-License-Identifier: Apache-2.0
# Copyright 2015-2016 The Meson development team

from __future__ import annotations

from .. import mlog
from http.client import HTTPResponse
import sys, os
import configparser
import shutil
import typing as T
import json

from glob import glob
from .wrap import (
    DEFAULT_WRAPDB_HOST,
    create_wrapdb_host_lookup_state,
    get_wrapdb_host_for_name,
    open_wrapdburl,
    read_and_decompress,
    WrapException,
    get_releases,
    get_releases_data,
    parse_patch_url,
    get_hosts,
    update_hosts,
)
from .. import mesonlib, msubprojects

if T.TYPE_CHECKING:
    import argparse

    from .wrap import WrapDBV2, WrapHostLookupState


# Note: when adding arguments, please also add them to the completion
# scripts in $MESONSRC/data/shell-completions/
def add_arguments(parser: "argparse.ArgumentParser") -> None:
    subparsers = parser.add_subparsers(title="Commands", dest="command")
    subparsers.required = True

    p = subparsers.add_parser("list", help="show all available projects")
    p.add_argument(
        "--allow-insecure",
        default=False,
        action="store_true",
        help="Allow insecure server connections.",
    )
    p.add_argument(
        "--separate-by-host",
        default=False,
        action="store_true",
        help="Separates the package names list by wrapdb host.",
    )
    p.set_defaults(wrap_func=list_projects)

    p = subparsers.add_parser("search", help="search the db by name")
    p.add_argument(
        "--allow-insecure",
        default=False,
        action="store_true",
        help="Allow insecure server connections.",
    )
    p.add_argument(
        "--print-host",
        default=False,
        action="store_true",
        help="Print the wrapdb host for the found packages.",
    )
    p.add_argument("name")
    p.set_defaults(wrap_func=search)

    p = subparsers.add_parser("install", help="install the specified project")
    p.add_argument(
        "--allow-insecure",
        default=False,
        action="store_true",
        help="Allow insecure server connections.",
    )
    p.add_argument(
        "--additional-wrapdb-host",
        default=[],
        action="append",
        help="Add additional wrapdb hosts, from where packages can be installed.",
        nargs="*",
    )
    p.add_argument(
        "--no-wrapdb-hosts-file",
        default=False,
        action="store_true",
        help="Don't use the wrapdb_hosts.json file for installs, this still uses manual supplied hosts and also doesn't add the host to the wrapdb_hosts.json file.",
    )
    p.add_argument("name", nargs="+")
    p.set_defaults(wrap_func=install)

    p = msubprojects.add_wrap_update_parser(subparsers)
    p.set_defaults(wrap_func=msubprojects.run)

    p = subparsers.add_parser("info", help="show available versions of a project")
    p.add_argument(
        "--allow-insecure",
        default=False,
        action="store_true",
        help="Allow insecure server connections.",
    )
    p.add_argument("name")
    p.set_defaults(wrap_func=info)

    p = subparsers.add_parser(
        "status", help="show installed and available versions of your projects"
    )
    p.add_argument(
        "--allow-insecure",
        default=False,
        action="store_true",
        help="Allow insecure server connections.",
    )
    p.set_defaults(wrap_func=status)

    p = subparsers.add_parser(
        "promote", help="bring a subsubproject up to the master project"
    )
    p.add_argument("project_path")
    p.set_defaults(wrap_func=promote)

    p = subparsers.add_parser(
        "update-db", help="Update list of projects available in WrapDB (Since 0.61.0)"
    )
    p.add_argument(
        "--allow-insecure",
        default=False,
        action="store_true",
        help="Allow insecure server connections.",
    )
    p.add_argument(
        "--additional-wrapdb-host",
        default=[],
        action="append",
        help="Add additional wrapdb hosts, they are added to the wrapdb.json.",
        nargs="*",
    )
    p.set_defaults(wrap_func=update_db)

    p = subparsers.add_parser(
        "add-host",
        help="Add a wrapdb host to the wrapdb_hosts.json which is used by all wrap related commands",
    )
    p.add_argument(
        "--allow-insecure",
        default=False,
        action="store_true",
        help="Allow insecure server connections.",
    )
    p.add_argument("host", nargs="+")
    p.set_defaults(wrap_func=add_host)


def list_projects(options: "argparse.Namespace") -> None:

    subproject_dir_name = mesonlib.get_subproject_dir()
    if subproject_dir_name is None:
        raise SystemExit(
            "Subproject dir not found. Run this script in your source root directory."
        )

    wrapdb_hosts = get_hosts(subproject_dir_name)
    for wrapdb_host in wrapdb_hosts:
        if options.separate_by_host:
            print(f"Host {wrapdb_host}:")
        releases = get_releases(options.allow_insecure, wrapdb_host)
        for p in releases.keys():
            print(f"\t{p}" if options.separate_by_host else p)


def search(options: "argparse.Namespace") -> None:
    name = options.name

    subproject_dir_name = mesonlib.get_subproject_dir()
    if subproject_dir_name is None:
        raise SystemExit(
            "Subproject dir not found. Run this script in your source root directory."
        )

    wrapdb_hosts = get_hosts(subproject_dir_name)

    for wrapdb_host in wrapdb_hosts:
        releases = get_releases(options.allow_insecure, wrapdb_host)
        for p, info in releases.items():
            if p.find(name) != -1:
                print(f"Host {wrapdb_host}: {p}" if options.print_host else p)
            else:
                for dep in info.get("dependency_names", []):
                    if dep.find(name) != -1:
                        if options.print_host:
                            print(
                                f"Dependency {dep} found at {wrapdb_host} in wrap {p}"
                            )
                        else:
                            print(f"Dependency {dep} found in wrap {p}")


def get_latest_version(
    name: str, allow_insecure: bool, wrapdb_host: str
) -> T.Tuple[str, str]:
    releases = get_releases(allow_insecure, wrapdb_host)
    info = releases.get(name)
    if not info:
        raise WrapException(f"Wrap {name} not found in wrapdb")
    latest_version = info["versions"][0]
    version, revision = latest_version.rsplit("-", 1)
    return version, revision


def install_one(
    name: str, allow_insecure: bool, wrapdb_state: WrapHostLookupState, print_host: bool
) -> None:
    subproject_dir_name = mesonlib.get_subproject_dir()
    if os.path.isdir(os.path.join(subproject_dir_name, name)):
        raise SystemExit("Subproject directory for this project already exists.")
    wrapfile = os.path.join(subproject_dir_name, name + ".wrap")
    if os.path.exists(wrapfile):
        raise SystemExit(f"Wrap file for {name} already exists.")

    wrapdb_host = get_wrapdb_host_for_name(wrapdb_state, name)
    if wrapdb_host is None:
        raise WrapException("Couldn't determine host for subproject")

    version, revision = get_latest_version(name, allow_insecure, wrapdb_host)

    url: HTTPResponse = open_wrapdburl(
        f"https://{wrapdb_host}/v2/{name}_{version}-{revision}/{name}.wrap",
        {wrapdb_host},
        allow_insecure,
        True,
        True,
    )
    with open(wrapfile, "wb") as f:
        f.write(read_and_decompress(url))

    if print_host:
        print(
            f"Installed {name} version {version} revision {revision} from {wrapdb_host}"
        )
    else:
        print(f"Installed {name} version {version} revision {revision}")


def install(options: "argparse.Namespace") -> None:
    # TODO: do we really need backwards compatibility for the outputted strings? if we do, we need another option that enables this opt-in
    print_host = True

    subproject_dir_name = mesonlib.get_subproject_dir()
    if subproject_dir_name is None or not os.path.isdir(subproject_dir_name):
        raise SystemExit(
            "Subprojects dir not found. Run this script in your source root directory."
        )

    hosts: set[str] = {DEFAULT_WRAPDB_HOST}

    hosts.update(options.additional_wrapdb_host)

    if not options.no_wrapdb_hosts_file:
        wrapdb_hosts = get_hosts(subproject_dir_name)
        hosts.update(wrapdb_hosts)

        # if we allow the host file, we also update it
        update_hosts(
            options.allow_insecure, subproject_dir_name, options.additional_wrapdb_host
        )

    failed = 0

    wrapdb_state = create_wrapdb_host_lookup_state(options.allow_insecure)

    for name in dict.fromkeys(options.name):
        try:
            install_one(name, options.allow_insecure, wrapdb_state, print_host)
        except WrapException as e:
            print(str(e), file=sys.stderr)
            failed += 1

    if failed > 0:
        raise SystemExit(1)


def get_current_version(
    wrapfile: str, allowed_wrapdb_hosts: set[str]
) -> T.Tuple[str, str, str, str, T.Optional[str]]:
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(wrapfile)
    try:
        wrap_data = cp["wrap-file"]
    except KeyError:
        raise WrapException("Not a wrap-file, cannot have come from the wrapdb")
    try:
        patch_url = wrap_data["patch_url"]
    except KeyError:
        # We assume a wrap without a patch_url is probably just an pointer to upstream's
        # build files. The version should be in the tarball filename, even if it isn't
        # purely guaranteed. The wrapdb revision should be 1 because it just needs uploading once.
        branch = mesonlib.search_version(wrap_data["source_filename"])
        revision, patch_filename = "1", None
    else:
        branch, revision = parse_patch_url(patch_url, allowed_wrapdb_hosts)
        patch_filename = wrap_data["patch_filename"]
    return (
        branch,
        revision,
        wrap_data["directory"],
        wrap_data["source_filename"],
        patch_filename,
    )


def info(options: "argparse.Namespace") -> None:
    # TODO: we currently only read hosts from the "wrapdb_hosts.json" file, but we should accept options for more hosts similar to install (also add them to the file, toggleable by options)

    # TODO: do we really need backwards compatibility for the outputted strings? if we do, we need another option that enables this opt-in
    print_host = True

    name = options.name

    subproject_dir_name = mesonlib.get_subproject_dir()
    if subproject_dir_name is None:
        raise SystemExit(
            "Subproject dir not found. Run this script in your source root directory."
        )

    wrapdb_hosts = get_hosts(subproject_dir_name)

    found: bool = False

    for wrapdb_host in wrapdb_hosts:
        releases = get_releases(options.allow_insecure, wrapdb_host)
        info = releases.get(name)
        if not info:
            continue

        if print_host:
            print(f"Available versions of {name} at {wrapdb_host}:")
        else:
            print(f"Available versions of {name}:")

        for v in info["versions"]:
            print(" ", v)

        found = True

    if not found:
        if print_host:
            raise WrapException(f"Wrap {name} not found in any wrapdb")
        else:
            raise WrapException(f"Wrap {name} not found in wrapdb")


def do_promotion(from_path: str, spdir_name: str) -> None:
    if os.path.isfile(from_path):
        assert from_path.endswith(".wrap")
        shutil.copy(from_path, spdir_name)
    elif os.path.isdir(from_path):
        sproj_name = os.path.basename(from_path)
        outputdir = os.path.join(spdir_name, sproj_name)
        if os.path.exists(outputdir):
            raise SystemExit(
                f"Output dir {outputdir} already exists. Will not overwrite."
            )

        subpdir = mesonlib.get_subproject_dir()
        if subpdir is not None:
            ignore = shutil.ignore_patterns(subpdir)
        else:
            ignore = None

        shutil.copytree(from_path, outputdir, ignore=ignore)


def promote(options: "argparse.Namespace") -> None:
    argument = options.project_path
    spdir_name = mesonlib.get_subproject_dir()
    if spdir_name is None:
        raise SystemExit(
            "Subproject dir not found. Run this script in your source root directory."
        )
    sprojs = mesonlib.detect_subprojects(spdir_name)

    # check if the argument is a full path to a subproject directory or wrap file
    system_native_path_argument = argument.replace("/", os.sep)
    for matches in sprojs.values():
        if system_native_path_argument in matches:
            do_promotion(system_native_path_argument, spdir_name)
            return

    # otherwise the argument is just a subproject basename which must be unambiguous
    if argument not in sprojs:
        raise SystemExit(f"Subproject {argument} not found in directory tree.")
    matches = sprojs[argument]
    if len(matches) > 1:
        print(
            f"There is more than one version of {argument} in tree. Please specify which one to promote:\n",
            file=sys.stderr,
        )
        for s in matches:
            print(s, file=sys.stderr)
        raise SystemExit(1)
    do_promotion(matches[0], spdir_name)


def status(options: "argparse.Namespace") -> None:
    print("Subproject status")
    subdir = mesonlib.unwrap(
        mesonlib.get_subproject_dir(),
        "This should only happen in a non-native subproject",
    )

    wrapdb_state = create_wrapdb_host_lookup_state(options.allow_insecure)

    subproject_dir_name = mesonlib.get_subproject_dir()
    if subproject_dir_name is None:
        raise SystemExit(
            "Subproject dir not found. Run this script in your source root directory."
        )

    allowed_wrapdb_hosts = get_hosts(subproject_dir_name)

    for w in glob(f"{subdir}/*.wrap"):
        name = os.path.basename(w)[:-5]
        try:
            wrapdb_host = get_wrapdb_host_for_name(wrapdb_state, name)
            if wrapdb_host is None:
                raise WrapException("Couldn't determine host for subproject")
            latest_branch, latest_revision = get_latest_version(
                name, options.allow_insecure, wrapdb_host
            )
        except Exception:
            print(
                "",
                mlog.cyan(name),
                mlog.red("not available in any wrapdb."),
                file=sys.stderr,
            )
            continue
        try:
            current_branch, current_revision, _, _, _ = get_current_version(
                w, allowed_wrapdb_hosts
            )
        except Exception:
            print("", name, "Wrap file not from wrapdb.", file=sys.stderr)
            continue
        if current_branch == latest_branch and current_revision == latest_revision:
            print(
                "",
                mlog.cyan(name),
                f"{mlog.green('up to date')}. Branch {mlog.blue(current_branch)}, revision {mlog.green(current_revision)}.",
            )
        else:
            print(
                "",
                name,
                f"{mlog.cyan('not up to date')}. Have {mlog.blue(current_branch)} {mlog.green(current_revision)}, but {mlog.blue(latest_branch)} {mlog.green(latest_revision)} is available.",
            )


def update_db(options: "argparse.Namespace") -> None:

    # TODO: this settings doesn't make sense, as we add the host to the wrapdb.json, but not to the "wrapdb_hosts.json" file? so just remove this and hardcode every path to be False
    no_wrapdb_hosts_file = False

    subproject_dir_name = mesonlib.get_subproject_dir()
    if subproject_dir_name is None or not os.path.isdir(subproject_dir_name):
        raise SystemExit(
            "Subprojects dir not found. Run this script in your source root directory."
        )

    hosts: set[str] = {DEFAULT_WRAPDB_HOST}

    hosts.update(options.additional_wrapdb_host)

    if not no_wrapdb_hosts_file:
        wrapdb_hosts = get_hosts(subproject_dir_name)
        hosts.update(wrapdb_hosts)

        # if we allow the host file, we also update it
        update_hosts(
            options.allow_insecure, subproject_dir_name, options.additional_wrapdb_host
        )

    wrapdb_data: WrapDBV2 = {"version": "2", "entries": {}}
    for wrapdb_host in hosts:
        data = get_releases_data(options.allow_insecure, wrapdb_host)
        wrapdb_data["entries"][wrapdb_host] = json.loads(data)

    os.makedirs(subproject_dir_name, exist_ok=True)
    with open(
        os.path.join(subproject_dir_name, "wrapdb.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(wrapdb_data, f)


def add_host(options: "argparse.Namespace") -> None:

    subproject_dir_name = mesonlib.get_subproject_dir()
    if subproject_dir_name is None or not os.path.isdir(subproject_dir_name):
        raise SystemExit(
            "Subprojects dir not found. Run this script in your source root directory."
        )

    new_hosts = list(dict.fromkeys(options.host))

    # if we allow the host file, we also update it
    failed = update_hosts(options.allow_insecure, subproject_dir_name, new_hosts)

    if failed > 0:
        raise SystemExit(1)


def run(options: "argparse.Namespace") -> int:
    options.wrap_func(options)
    return 0
