# SPDX-FileCopyrightText: 2018 Michael Schroeder
#
# SPDX-License-Identifier: MIT

"""Adabot utility for Arduino Libraries."""
import time
import os
import argparse
import logging
import sys
import traceback
import semver
import requests

from adabot import github_requests as gh_reqs
from adabot import gh_forking_prs


logger = logging.getLogger(__name__)
ch = logging.StreamHandler(stream=sys.stdout)
logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[ch])

# Setup ArgumentParser
cmd_line_parser = argparse.ArgumentParser(
    description="Adabot utility for Arduino Libraries.",
    prog="Adabot Arduino Libraries Utility",
)
cmd_line_parser.add_argument(
    "-o",
    "--output_file",
    help="Output log to the filename provided.",
    metavar="<OUTPUT FILENAME>",
    dest="output_file",
)
cmd_line_parser.add_argument(
    "-v",
    "--verbose",
    help="Set the level of verbosity printed to the command prompt."
    " Zero is off; One is on (default).",
    type=int,
    default=1,
    dest="verbose",
    choices=[0, 1],
)
#todo: refactor this and remove CI arg to funcs. add to sysv
cmd_line_parser.add_argument(
    "-ci",
    "--ci",
    help="Set the option to check changes for CI-only and flag if so."
    " Zero is off; One is on (default).",
    type=int,
    default=1,
    dest="ci",
    choices=[0, 1],
)
#todo: add -bc aka --bump-ci-repos with default dry-run plus --idiot flag to update library.properties if release tag is higher and ci only changes.
cmd_line_parser.add_argument(
    "-bc",
    "--bump-ci-repos",
    help="Bump the version number in library.properties for CI-only changes."
    " Zero is off; One is on (default).",
    type=int,
    default=0,
    dest="bump_ci_repos",
    choices=[0, 1],
)
cmd_line_parser.add_argument(
    "-idiot",
    "--idiot",
    help="Opposite of Dry-Run. Set the option to create Pull Requests to update library.properties for CI-only changes if release tag is higher. Zero is off (default); One is on. 'Only an idiot would blindly trust automated scripts'",
    type=int,
    default=0,
    dest="idiot",
    choices=[0, 1],
)

all_libraries = []
adafruit_library_index = []


def check_changes_for_ci_only(repo,ref1,ref2):
    """
    Checks if all changes between two tags/refs only contain changes in the `.github` folder, and therefore are CI only changes.

    Args:
        repo (dict): A dictionary representing the repository.
        ref1 (str): The first tag/ref to compare.
        ref2 (str): The second tag/ref to compare.

    Returns:
        bool: True if all changes between the two tags/refs only contain changes in the `.github` folder, False otherwise.
    """
    # Check changes in json["files"] affects only /.github/*
    compare_tags = gh_reqs.get(
        "/repos/"
        + repo["full_name"]
        + "/compare/"
        + ref1
        + "..."
        + ref2
    )
    if not compare_tags.ok:
        logger.error(
            "Error: failed to compare %s '%s' to '%s' (CI/md/img-only changes check), maybe the first tag doesn't exist?",
            repo["name"],
            ref1,
            ref2,
        )
        return False
    comparison = compare_tags.json()
    if "files" not in comparison:
        return False
    for file in comparison["files"]:
        if (not file["filename"].startswith(".github/")
        and not file["filename"].lower().endswith(".md")
        and file["filename"].lower() not in ["assets/board.jpeg", "assets/board.png", "assets/board.jpg"]):
            return False
    return True


def create_version_update_pr(repo, lib_version, release_version):
    """
    Creates a pull request to update the version number in the library.properties file of a given repository.

    Args:
        repo (dict): A dictionary containing information about the repository, including its name and default branch.
        lib_version (str): The new version number to be used in the library.properties file.
        release_version (str): The release version associated with the new library version.

    Returns:
        bool: False if the bump flag is not set, otherwise None.
    """
    global cmd_line_parser
    try:
        args = cmd_line_parser.parse_args()
        if not args.bump_ci_repos:
            logging.info("Skip PR bumping library.properties [CI/md/img-only check] " + repo['name'] + " (-bc=0)")
            return False
        owner = os.environ.get("ADABOT_GITHUB_USER")
        
        if not args.idiot:
            logging.info("Not creating Pull Request to bump library.properties for CI/md/img-only changes - " + repo['name'] + " (--idiot=0)")
            return
        
        # Check if the user has already forked the repository
        fork = gh_forking_prs.get_user_fork(owner, repo)
        if not fork:
            reponame = "adafruit-" + repo["name"]
            # If the user hasn't forked the repository, create a new fork
            fork = gh_forking_prs.create_fork(owner, repo, reponame)
        else:
            reponame = fork["name"]
            # Ensure the fork's default branch matches the upstream repository's default branch
            gh_forking_prs.sync_fork(repo, fork)

        # Wait for the fork to match the upstream repository, check SHA for 3mins
        main_ref = gh_forking_prs.get_latest_ref(repo, repo['default_branch'])
        WAIT_TIME = 300
        for _ in range(WAIT_TIME):
            try:
                if gh_forking_prs.get_latest_ref(fork, repo['default_branch']) == main_ref:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            logging.error("Fork still not in sync after " + str(WAIT_TIME) + " seconds, skipping " + repo["name"])
            return

        # Create a new branch for the changes
        branch_name = "bump-version-" + time.strftime("%Y-%m-%d-%H-%M-%S")
        gh_forking_prs.create_branch(owner, repo, fork, branch_name)

        # Update the version number in library.properties
        file_path = "library.properties"
        file_contents = gh_forking_prs.get_file_contents(owner, fork, file_path)
        new_contents = gh_forking_prs.update_version_number(file_contents, lib_version, release_version)
        gh_forking_prs.update_file_contents(owner, reponame, fork, file_path, new_contents, branch_name)

        # Create a new pull request
        head = f"{fork['owner']['login']}:{branch_name}"
        base = repo["default_branch"]
        gh_forking_prs.create_draft_pull_request(owner, repo, fork,branch_name)
        logging.info("Pull request created: %s/%s#%s", owner, fork['name'], branch_name)
    except Exception as e:
        logging.error(e)
        logging.error(f"Failed to add PR to bump library.properties for CI-only changes for {repo['name']}")



def list_repos():
    """Return a list of all Adafruit repositories with 'Arduino' in either the
    name, description, or readme. Each list item is a dictionary of GitHub API
    repository state.
    """
    repos = []
    result = gh_reqs.get(
        "/search/repositories",
        params={
            "q": (
                "Arduino in:name in:description in:readme fork:true user:adafruit archived:false"
                " OR Library in:name in:description in:readme fork:true user:adafruit"
                " archived:false OR Adafruit_ in:name fork:true user:adafruit archived:false AND"
                " NOT PCB in:name AND NOT Python in:name"
            ),
            "per_page": 100,
            "sort": "updated",
            "order": "asc",
        },
    )
    while result.ok:
        repos.extend(
            result.json()["items"]
        )  # uncomment and comment below, to include all forks

        if result.links.get("next"):
            result = gh_reqs.get(result.links["next"]["url"])
        else:
            break

    return repos


def is_arduino_library(repo):
    """Returns if the repo is an Arduino library, as determined by the existence of
    the 'library.properties' file.
    """
    lib_prop_file = requests.get(
        "https://raw.githubusercontent.com/adafruit/"
        + repo["name"]
        + "/"
        + repo["default_branch"]
        + "/library.properties"
    )
    return lib_prop_file.ok


def print_list_output(title, coll):
    """Helper function to format output."""
    logger.info("")
    logger.info(title.format(len(coll) - 2))
    long_col = [
        (max([len(str(row[i])) for row in coll]) + 3) for i in range(len(coll[0]))
    ]
    row_format = "".join(["{:<" + str(this_col) + "}" for this_col in long_col])
    for lib in coll:
        logger.info("%s", row_format.format(*lib))

def validate_library_properties(repo):
    """Checks if the latest GitHub Release Tag and version in the library_properties
    file match. Will also check if the library_properties is there, but no release
    has been made.

    Args:
        repo (dict): A dictionary of GitHub API repository state.

    Returns:
        list: A list of two elements, where the first element is the latest release tag
        and the second element is the version in the library_properties file. If the
        library_properties file is not present or the latest release tag cannot be
        obtained, the method returns None.
    """
    lib_prop_file = None
    lib_version = None
    release_tag = None
    lib_prop_file = requests.get(
        "https://raw.githubusercontent.com/adafruit/"
        + repo["name"]
        + "/"
        + repo["default_branch"]
        + "/library.properties"
    )
    if not lib_prop_file.ok:
        # print("{} skipped".format(repo["name"]))
        return None  # no library properties file!

    lines = lib_prop_file.text.split("\n")
    for line in lines:
        if "version" in line:
            lib_version = str(line[len("version=") :]).strip()
            break

    get_latest_release = gh_reqs.get(
        "/repos/adafruit/" + repo["name"] + "/releases/latest"
    )
    release_tag = "None"
    if get_latest_release.ok:
        response = get_latest_release.json()
        if "tag_name" in response:
            release_tag = response["tag_name"]
        if "message" in response:
            if response["message"] != "Not Found":
                release_tag = "Unknown"

    if lib_version:
        return [release_tag, lib_version]

    return None


def validate_release_state(repo):
    """Validate if a repo 1) has a release, and 2) if there have been commits
    since the last release. Returns a list of string error messages for the
    repository.
    """
    if not is_arduino_library(repo):
        return None

    compare_tags = gh_reqs.get(
        "/repos/"
        + repo["full_name"]
        + "/compare/"
        + repo["default_branch"]
        + "..."
        + repo["tag_name"]
    )
    if not compare_tags.ok:
        logger.error(
            "Error: failed to compare %s '%s' to tag '%s'",
            repo["name"],
            repo["default_branch"],
            repo["tag_name"],
        )
        return None
    compare_tags_json = compare_tags.json()
    if "status" in compare_tags_json:
        if compare_tags_json["status"] != "identical":
            return [repo["tag_name"], compare_tags_json["behind_by"]]
    elif "errors" in compare_tags_json:
        logger.error(
            "Error: comparing latest release to '%s' failed on '%s'. Error Message: %s",
            repo["default_branch"],
            repo["name"],
            compare_tags_json["message"],
        )

    return None


def validate_actions(repo):
    """Validate if a repo has workflows/githubci.yml"""
    repo_has_actions = requests.get(
        "https://raw.githubusercontent.com/adafruit/"
        + repo["name"]
        + "/"
        + repo["default_branch"]
        + "/.github/workflows/githubci.yml"
    )
    return repo_has_actions.ok


def validate_example(repo):
    """Validate if a repo has any files in examples directory"""
    repo_has_ino = gh_reqs.get("/repos/adafruit/" + repo["name"] + "/contents/examples")
    return repo_has_ino.ok and len(repo_has_ino.json())


# pylint: disable=too-many-branches
# pylint: disable=too-many-statements
def run_arduino_lib_checks(ci=0):
    """Run necessary functions and outout the results."""
    logger.info("Running Arduino Library Checks")
    logger.info("Getting list of libraries to check...")

    repo_list = list_repos()
    logger.info("Found %s Arduino libraries to check\n", len(repo_list))
    failed_lib_prop = [
        ["  Repo", "Release Tag", "library.properties Version"],
        ["  ----", "-----------", "--------------------------"],
    ]
    needs_release_list = [
        ["  Repo", "Latest Release", "Commits Behind", "Comparison"],
        ["  ----", "--------------", "--------------", "----------"],
    ]
    needs_registration_list = [
        ["  Repo", "Latest Changes"],
        ["  ----", "--------------"],
    ]
    missing_actions_list = [["  Repo"], ["  ----"]]
    missing_library_properties_list = [
        ["  Repo", "Latest Changes"],
        ["  ----", "--------------"],
    ]

    no_examples = [
        ["  Repo", "Latest Changes"],
        ["  ----", "--------------"],
    ]

    for repo in repo_list:
        have_examples = validate_example(repo)
        if not have_examples:
            # not a library, probably worth rechecking that it's got no library.properties file
            if is_arduino_library(repo):
                no_examples.append(
                    ["  " + str(repo["name"] or repo["clone_url"]) + " *LibraryNoExamples*", repo["pushed_at"]]
                )
            else:
                no_examples.append(
                    ["  " + str(repo["name"] or repo["clone_url"]), repo["pushed_at"]]
                )
                continue

        entry = {"name": repo["name"]}

        lib_check = validate_library_properties(repo)
        if not lib_check:
            missing_library_properties_list.append(
                ["  " + str(repo["name"]), repo["pushed_at"]]
            )
            continue

        if lib_check[0] in ("None", "Unknown"):
            compare_url = (
                str(repo["html_url"]) + "/compare/" + repo["default_branch"] + "...HEAD"
            )
            needs_release_list.append(
                ["  " + str(repo["name"]), "*None*", repo["pushed_at"], compare_url]
            )
            continue

        if lib_check[0] != lib_check[1]:
            # version mismatch between release and library.properties
            release_tag = lib_check[0]
            libprops_tag = lib_check[1]
            if ci and check_changes_for_ci_only(repo, libprops_tag, release_tag):
                lib_check[0] = str(lib_check[0]) + " *CI/Md/IMG-only*"
            failed_lib_prop.append(
                [
                    "  " + str((repo["name"] or repo["clone_url"])),
                    lib_check[0],
                    lib_check[1],
                ]
            )
            create_version_update_pr(repo, libprops_tag, release_tag)
            # don't loop with continue
            # we later check changes not between tags, i.e. main vs tag

        for lib in adafruit_library_index:
            if (repo["clone_url"] == lib["repository"]) or (
                repo["html_url"] == lib["website"]
            ):
                if (  # pylint: disable=too-many-boolean-expressions
                    "arduino_version" not in entry
                    or not entry["arduino_version"]
                    or (
                        entry["arduino_version"]
                        and semver.parse(entry["arduino_version"])
                        and semver.parse(lib["version"])
                        and semver.compare(entry["arduino_version"], lib["version"]) < 0
                    )
                ):
                    entry["arduino_version"] = lib["version"]

        if "arduino_version" not in entry or not entry["arduino_version"]:
            needs_registration_list.append(
                ["  " + str(repo["name"]), repo["pushed_at"]]
            )

        entry["release"] = lib_check[0].split(' ')[0]
        entry["version"] = lib_check[1]
        repo["tag_name"] = lib_check[0].split(' ')[0]

        needs_release = validate_release_state(repo)
        entry["needs_release"] = needs_release
        if needs_release:
            compare_url = (
                str(repo["html_url"]) + "/compare/" + needs_release[0].split(' ')[0] + "...HEAD"
            )
            if ci and check_changes_for_ci_only(repo, entry["release"], repo["default_branch"]):
                needs_release[1] = str(needs_release[1]) + " *CI/Md/IMG*"
            needs_release_list.append(
                [
                    "  " + str(repo["name"]),
                    needs_release[0],
                    needs_release[1],
                    compare_url,
                ]
            )

        missing_actions = not validate_actions(repo)
        entry["needs_actions"] = missing_actions
        if missing_actions:
            missing_actions_list.append(["  " + str(repo["name"])])

        all_libraries.append(entry)

    for entry in all_libraries:
        logging.info(entry)

    if len(no_examples) > 2:
        print_list_output(
            "Repos with no examples (considered non-libraries): ({})",
            no_examples,
        )

    if len(failed_lib_prop) > 2:
        print_list_output(
            "Libraries Have Mismatched Release Tag and library.properties Version: ({})",
            failed_lib_prop,
        )

    if len(needs_registration_list) > 2:
        print_list_output(
            "Libraries that are not registered with Arduino: ({})",
            needs_registration_list,
        )

    if len(needs_release_list) > 2:
        print_list_output(
            "Libraries have commits since last release: ({})", needs_release_list
        )

    if len(missing_actions_list) > 2:
        print_list_output(
            "Libraries that is not configured with Actions: ({})", missing_actions_list
        )

    if len(missing_library_properties_list) > 2:
        print_list_output(
            "Libraries that is missing library.properties file: ({})",
            missing_library_properties_list,
        )


def main(verbosity=1, output_file=None, ci=1):  # pylint: disable=missing-function-docstring
    if output_file:
        file_handler = logging.FileHandler(output_file)
        logger.addHandler(file_handler)

    if verbosity == 0:
        logger.setLevel("CRITICAL")

    try:
        reply = requests.get("http://downloads.arduino.cc/libraries/library_index.json")
        if not reply.ok:
            logging.error(
                "Could not fetch http://downloads.arduino.cc/libraries/library_index.json"
            )
            sys.exit()
        arduino_library_index = reply.json()
        for lib in arduino_library_index["libraries"]:
            if "adafruit" in lib["url"]:
                adafruit_library_index.append(lib)
        run_arduino_lib_checks(ci)
    except:
        _, exc_val, exc_tb = sys.exc_info()
        logger.error("Exception Occurred!", quiet=True)
        logger.error(("-" * 60), quiet=True)
        logger.error("Traceback (most recent call last):")
        trace = traceback.format_tb(exc_tb)
        for line in trace:
            logger.error(line)
        logger.error(exc_val)

        raise


if __name__ == "__main__":
    cmd_line_args = cmd_line_parser.parse_args()
    main(verbosity=cmd_line_args.verbose, output_file=cmd_line_args.output_file, ci=cmd_line_args.ci)
