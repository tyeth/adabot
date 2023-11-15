import base64
import logging
import semver
from adabot import github_requests as ghr
import requests_cache
import json
import sys
import os
import traceback
try:
    import webbrowser
except ImportError:
    webbrowser = None

def create_fork(owner,repo, name):
    """
    Creates a fork of the repository.

    Args:
        owner (str): The owner of the repository.
        repo (str): The name of the repository.
        name (str): The name of the new fork.

    Returns:
        dict: A dictionary representing the forked repository.
    """
    fork_url = repo['forks_url'] # f"https://api.github.com/repos/{repo['owner']['login']}/{repo['name']}/forks"
    json = {
        "owner": owner,
        "repo": repo["name"],
        "name": name,
        "default_branch_only": True,
    }
    response = ghr.post(fork_url, json=json)
    if response.status_code != 202:
        raise Exception(f"Failed to create fork: {response.status_code} {response.text}")
    return response.json()


def get_user_fork(owner, repo):
    """
    Gets the authenticated user's fork of the repository.

    Args:
        owner (str): The owner of the repository.
        repo (str): The name of the repository.

    Returns:
        dict: A dictionary representing the authenticated user's fork of the repository.
    """
    forks_url = repo['forks_url']#f"https://api.github.com/repos/{owner}/{repo}/forks"
    with requests_cache.disabled():
        response = ghr.get(forks_url)
    if response.status_code != 200:
        raise Exception(f"Failed to get forks: {response.status_code} {response.text}")
    forks = response.json()
    for fork in forks:
        if fork["owner"]["login"] == owner:
            return fork
    return None

def load_existing_prs():
    if os.path.exists('recent_pulls.json'):
        with open('recent_pulls.json', 'r') as f:
            return json.load(f)
    else:
        return []

def load_verify_and_merge_prs(extra_prs, idiot_mode):
    """
    Loads the pull requests that have been created and merges them if they are ready.

    Args:
        extra_prs (list): A list of pull requests to merge.
        idiot_mode (bool): Whether or not idiot mode is enabled.
    """
    extra_prs = extra_prs or []
    prs = load_existing_prs()
    prs.extend(extra_prs)

    new_list=[]
    for pr in prs:
        try:
            # update PR object with latest info
            pr_url = pr['url']
            response = ghr.get(pr_url)
            if response.status_code != 200:
                raise Exception(f"Failed to get pull request: {response.status_code} {response.text}")
            pr = response.json()
            new_list.append(pr)
            already_merged = False
            if pr['state'] == 'closed':
                if not pr['merged_at']:
                    logging.info("*#* PR %s was closed without merge -- REMOVING FROM LIST", pr['html_url'])
                    new_list.remove(pr)
                    continue
                logging.info("PR %s is already merged", pr['html_url'])
                already_merged = True
            else:
                # Check PR status
                status = get_pull_request_actions_status(pr)
                if status == 'success':
                    # Merge PR
                    if idiot_mode:
                        ret = merge_pull_request(pr)
                        if ret and ret.get('merged', False) == True:
                            logging.info("PR %s now merged", pr['html_url'])
                        else:
                            logging.info("*#* PR %s merge FAILED (%s)", pr['html_url'], ret)
                            webbrowser.open(url=pr['html_url'], new=2, autoraise=False)
                            continue
                        response = ghr.get(pr_url)
                        if response.status_code != 200:
                            raise Exception(f"Failed to get pull request: {response.status_code} {response.text}")
                        new_list.remove(pr)
                        pr = response.json()
                        new_list.append(pr)
                    else:
                        logging.info("*** PR %s is ready to merge", pr['html_url'])
                elif status == 'pending':
                    logging.info("PR %s is still pending", pr['html_url'])
                    continue
                elif status == 'failure':
                    logging.info("*#* PR %s action FAILED", pr['html_url'])
                    webbrowser.open(url=pr['html_url'], new=2, autoraise=False)
                    continue
                elif status == 'error':
                    logging.info("*#* PR %s action ERRORED", pr['html_url'])
                    webbrowser.open(url=pr['html_url'], new=2, autoraise=False)
                    continue
                else:
                    logging.info("*#*#* PR %s UNKNOWN action status: %s", pr['html_url'], status)
                    continue
                
            
            if idiot_mode:
                # get version from pr title (last word is semver)
                new_version = pr['title'].split()[-1]

                if already_merged:
                    # check release tag of version number exists
                    # if not then create new release tag below
                    try:
                        if get_latest_ref(pr['base']['repo'],new_version, "tags") == pr['merge_commit_sha']:
                            logging.info("#*# PR %s is already merged and release tag exists - skipping - manually verify!", pr['html_url'])
                            continue
                        pass
                    except:
                        pass # no tag found, so create one below as part of release

                logging.info("New version release page opening for %s", new_version)
                # Open GitHub UI for release creation
                open_new_release_webpage(owner=pr['base']['repo']['owner']['login'], repo=pr['base']['repo'], new_version=new_version)
            else:
                logging.info("*** PR %s is ready to release", pr['html_url'])
        except Exception as e:
            logging.error("Exception Occurred tracking PR %s", pr['html_url'])
            _, exc_val, exc_tb = sys.exc_info()
            logging.error("Exception Occurred!")
            logging.error(("-" * 60))
            logging.error("Traceback (most recent call last):")
            trace = traceback.format_tb(exc_tb)
            for line in trace:
                logging.error(line)
            logging.error(exc_val)
    return new_list

def open_new_release_webpage(owner, repo, new_version):
    """
    Opens the new release page for the repository with the version number pre-populated.

    Args:
        owner (str): The owner of the repository.
        repo (str): The name of the repository.
        new_version (str): The new version number.
    """
    release_url = f"https://github.com/{owner}/{repo['name']}/releases/new?tag={new_version}"
    if webbrowser is None:
        logging.info("** Open the following URL to create a new release: %s", release_url)
    else:
        # webbrowser.open_new_tab(release_url)
        webbrowser.open(url=release_url, new=2, autoraise=False)

def merge_pull_request(pr):
    """
    Merges a pull request.

    Args:
        pr (dict): The pull request to merge.

    Returns:
        dict: A dictionary representing the merged pull request.
    """
    merge_url = pr['url'] + "/merge"
    response = ghr.put(merge_url)
    if response.status_code != 200:
        raise Exception(f"Failed to merge pull request: {response.status_code} {response.text}")
    return response.json()

def get_pull_request_checks_status(pr):
    """
    Gets the status of the pull request's checks.

    Args:
        pr (dict): The pull request to check the status of.

    Returns:
        str: The status of the pull request's checks.
    """
    url = f"https://api.github.com/repos/{pr['base']['repo']['owner']['login']}/{pr['base']['repo']['name']}/commits/{pr['head']['sha']}/check-runs"
    response = ghr.get(url)

    if response.status_code != 200:
        raise Exception(f"Failed to fetch check runs: {response.status_code} {response.text}")

    check_runs = response.json().get('check_runs', [])
    if not check_runs:
        return 'none'

    # Consolidate the status of all check runs
    statuses = set(check_run['conclusion'] for check_run in check_runs if check_run['status'] == 'completed')
    if 'failure' in statuses:
        return 'failure'
    elif 'success' in statuses and len(statuses) == 1:
        return 'success'
    else:
        return 'pending'

def get_pull_request_actions_status(pr):
    """
    Gets the status of a pull request.

    Args:
        pr (dict): The pull request to check the status of.

    Returns:
        str: The status of the pull request.
    """
    status_url = pr['statuses_url']
    response = ghr.get(status_url)
    if response.status_code != 200:
        raise Exception(f"Failed to get pull request status: {response.status_code} {response.text}")
    data = response.json() # change to use pr['url'] and state field, also reuse for statuses_url if 'open'
    return data[0]['state'] if not data == [] else get_pull_request_checks_status(pr)


def print_load_verify_and_merge_prs(new_PRs=[]):
    print("")
    print("###########################################")
    print("New/existing PRs to monitor:")
    for pr in new_PRs:
        print(pr["html_url"])
    print("Starting load/verify/merge PRs:")
    new_PRs = load_verify_and_merge_prs(new_PRs, idiot_mode=1)
    save_prs(new_PRs)

def save_prs(prs):
    with open('recent_pulls.json', 'w') as f:
        json.dump(prs, f)

def sync_fork(repo, fork):
    """
    Ensures that the fork's default branch matches the upstream repository's default branch.

    Args:
        repo (dict): A dictionary representing the original repository.
        fork (dict): A dictionary representing the forked repository.
    """
    sync_url = f"https://api.github.com/repos/{fork['owner']['login']}/{fork['name']}/merge-upstream"
    json = {
        "branch": fork["default_branch"]
    }
    response = ghr.post(sync_url, json=json)
    if response.status_code != 200:
        raise Exception(f"Failed to sync fork({fork['owner']['login']}/{fork['name']}): {response.status_code} {response.text}")

def create_branch(owner, repo, fork, branch_name):
    """
    Creates a new branch for the changes.

    Args:
        owner (str): The owner of the repository.
        repo (dict): A dictionary representing the original repository.
        fork (dict): A dictionary representing the forked repository.
        branch_name (str): The name of the new branch.
    """
    head_sha = get_latest_ref(repo, repo['default_branch']) # Shouldn't error as we already have correct upstream SHA
    ref_url = f"https://api.github.com/repos/{fork['owner']['login']}/{fork['name']}/git/refs"
    ref_data = {"ref": f"refs/heads/{branch_name}", "sha": head_sha}
    response = ghr.post(ref_url, json=ref_data)
    if response.status_code != 201:
        raise Exception(f"Failed to create branch: {response.status_code} {response.text}")


def get_latest_ref(repo, branchname, ref_type="heads"):
    """
    Returns the SHA of the latest commit on the default branch of the given repository.

    Args:
        repo (dict): A dictionary containing information about the repository, including the owner's login,
                     the repository name
        branchname (str): The name of the branch to get the latest commit from.
        ref_type (str): The type of ref to get the latest commit from. Defaults to "heads", also "tags" supported.

    Returns:
        str: The SHA of the latest commit on the default branch.
    """
    head_url = f"https://api.github.com/repos/{repo['owner']['login']}/{repo['name']}/git/refs/{ref_type}/{branchname}"
    with requests_cache.disabled():
        response = ghr.get(head_url)
    if response.status_code != 200:
        raise Exception(f"Failed to get HEAD: {response.status_code} {response.text}")
    return response.json()["object"]["sha"]


def update_file_contents(owner, repo, fork, file_path, new_contents, branch_name, message="Update version number"):
    """
    Updates the contents of the file in the fork with the new contents.

    Args:
        owner (str): The owner of the repository.
        repo (dict): A dictionary representing the original repository.
        fork (dict): A dictionary representing the forked repository.
        file_path (str): The path of the file to update.
        new_contents (str): The new contents of the file.
        branch_name (str): The name of the branch to commit the changes to.
        message (str): The commit message. Defaults to "Update version number".
    """
    file_url = f"https://api.github.com/repos/{fork['owner']['login']}/{fork['name']}/contents/{file_path}"
    with requests_cache.disabled():
        file_contents = ghr.get(file_url).json()
    file_data = {
        "message": message,
        "content": base64.b64encode(new_contents.encode("utf-8")).decode("utf-8"),
        "sha": file_contents["sha"],
        "branch": branch_name,
    }
    response = ghr.put(file_url, json=file_data)
    if response.status_code != 200:
        raise Exception(f"Failed to update file: {response.status_code} {response.text}")

def create_draft_pull_request(owner, repo, fork, branch_name, draft=True, title="Update version number", body="This pull request updates the version number in library.properties."):
    """
    Creates a new pull request for the changes in the fork.

    Args:
        owner (str): The owner of the repository - unused - reads repo[owner][login] instead.
        repo (str): The name of the repository.
        fork (dict): A dictionary representing the forked repository.
        branch_name (str): The name of the branch to create the pull request from.
        draft (bool): Whether or not the pull request should be a draft. Defaults to True.
        title (str): The title of the pull request. Defaults to "Update version number"
        body (str): The body of the pull request. Defaults to "This pull request updates the version number in library.properties."
    """
    pr_url = f"https://api.github.com/repos/{repo['owner']['login']}/{repo['name']}/pulls"
    pr_data = {
        "title": title,
        "body": body,
        "head": f"{fork['owner']['login']}:{branch_name}",
        "base": repo["default_branch"],
        "draft": draft,
    }
    response = ghr.post(pr_url, json=pr_data)
    if response.status_code != 201:
        raise Exception(f"Failed to create pull request: {response.status_code} {response.text}")
    return response.json()


def get_file_contents(owner, repo, file_path):
    """
    Gets the contents of a file in the repository.

    Args:
        owner (str): The owner of the repository.
        repo (str): The name of the repository.
        file_path (str): The path to the file in the repository.

    Returns:
        str: The contents of the file.
    """
    file_url = f"https://api.github.com/repos/{owner}/{repo['name']}/contents/{file_path}"
    response = ghr.get(file_url)
    if response.status_code != 200:
        raise Exception(f"Failed to get file contents: {response.status_code} {response.text}")
    file_contents = response.json()
    if "content" not in file_contents:
        raise Exception(f"File contents not found: {file_path}")
    return base64.b64decode(file_contents["content"]).decode("utf-8")

def get_pull_requests(owner, repo):
    """
    Gets a list of pull requests for the repository.

    Args:
        owner (str): The owner of the repository.
        repo (str): The name of the repository.

    Returns:
        list: A list of pull requests.
    """
    pr_url = f"https://api.github.com/repos/{repo['owner']['login']}/{repo['name']}/pulls"
    with requests_cache.disabled():
        response = ghr.get(pr_url)
    if response.status_code != 200:
        raise Exception(f"Failed to get pull requests: {response.status_code} {response.text}")
    return response.json()


def pr_exists_with_same_title(owner, repo, fork, title):
    """
    Checks if a pull request already exists with the same title and fork.

    Args:
        owner (str): The owner of the repository.
        repo (str): The name of the repository.
        fork (dict): A dictionary representing the forked repository.
        title (str): The title of the pull request.

    Returns:
        bool: True if a pull request already exists with the same title and fork, False otherwise.
    """
    pr_list = get_pull_requests(owner, repo)
    for pr in pr_list:
        if pr['head']['repo'] and pr['head']['repo']['owner']['login'] == fork['owner']['login'] and pr['head']['repo']['name'] == fork['name'] and pr['title'] == title:
            logging.info("PR already exists with same title + fork: %s", pr['html_url'])
            return pr
    return None

def update_version_number(file_contents, new_version):
    """
    Updates the version number in the library.properties file.

    Args:
        file_contents (str): The contents of the library.properties file.
        new_version (str): The new release version of the library.

    Returns:
        str: The updated contents of the library.properties file.
    """
    logging.info("Updating version number to %s", new_version)
    lines = file_contents.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("version="):
            lines[i] = f"version={new_version}"
            break
    else:
        raise Exception("version not found in library.properties")
    return "\n".join(lines)