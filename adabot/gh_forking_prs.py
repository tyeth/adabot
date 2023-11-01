import base64
import logging
import semver
from adabot import github_requests as ghr

def create_fork(owner,repo, name):
    """
    Creates a fork of the repository.

    Args:
        owner (str): The owner of the repository.
        repo (str): The name of the repository.
        access_token (str): The access token for the GitHub API.

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
        access_token (str): The access token for the GitHub API.

    Returns:
        dict: A dictionary representing the authenticated user's fork of the repository.
    """
    forks_url = repo['forks_url']#f"https://api.github.com/repos/{owner}/{repo}/forks"
    response = ghr.get(forks_url)
    if response.status_code != 200:
        raise Exception(f"Failed to get forks: {response.status_code} {response.text}")
    forks = response.json()
    for fork in forks:
        if fork["owner"]["login"] == owner:
            return fork
    return None


def sync_fork(repo, fork):
    """
    Ensures that the fork's default branch matches the upstream repository's default branch.

    Args:
        repo (str): The name of the repository.
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
        repo (str): The name of the repository.
        fork (dict): A dictionary representing the forked repository.
        branch_name (str): The name of the new branch.
        access_token (str): The access token for the GitHub API.
    """
    head_sha = get_latest_ref(repo, repo['default_branch']) # Shouldn't error as we already have correct upstream SHA
    ref_url = f"https://api.github.com/repos/{fork['owner']['login']}/{fork['name']}/git/refs"
    ref_data = {"ref": f"refs/heads/{branch_name}", "sha": head_sha}
    response = ghr.post(ref_url, json=ref_data)
    if response.status_code != 201:
        raise Exception(f"Failed to create branch: {response.status_code} {response.text}")


def get_latest_ref(repo, branchname):
    """
    Returns the SHA of the latest commit on the default branch of the given repository.

    Args:
        repo (dict): A dictionary containing information about the repository, including the owner's login,
                     the repository name, and the default branch.

    Returns:
        str: The SHA of the latest commit on the default branch.
    """
    head_url = f"https://api.github.com/repos/{repo['owner']['login']}/{repo['name']}/git/refs/heads/{branchname}"
    response = ghr.get(head_url)
    if response.status_code != 200:
        raise Exception(f"Failed to get HEAD: {response.status_code} {response.text}")
    return response.json()["object"]["sha"]


def update_file_contents(owner, repo, fork, file_path, new_contents, branch_name):
    """
    Updates the contents of the file in the fork with the new contents.

    Args:
        owner (str): The owner of the repository.
        repo (dict): A dictionary representing the original repository.
        fork (dict): A dictionary representing the forked repository.
        file_path (str): The path of the file to update.
        new_contents (str): The new contents of the file.
        branch_name (str): The name of the branch to commit the changes to.
        access_token (str): The access token for the GitHub API.
    """
    file_url = f"https://api.github.com/repos/{fork['owner']['login']}/{fork['name']}/contents/{file_path}"
    file_contents = ghr.get(file_url).json()
    file_data = {
        "message": "Update version number",
        "content": base64.b64encode(new_contents.encode("utf-8")).decode("utf-8"),
        "sha": file_contents["sha"],
        "branch": branch_name,
    }
    response = ghr.put(file_url, json=file_data)
    if response.status_code != 200:
        raise Exception(f"Failed to update file: {response.status_code} {response.text}")

def create_draft_pull_request(owner, repo, fork, branch_name):
    """
    Creates a new pull request for the changes in the fork.

    Args:
        owner (str): The owner of the repository.
        repo (str): The name of the repository.
        fork (dict): A dictionary representing the forked repository.
        branch_name (str): The name of the branch to create the pull request from.
        access_token (str): The access token for the GitHub API.
    """
    pr_url = f"https://api.github.com/repos/{repo['owner']['login']}/{repo['name']}/pulls"
    pr_data = {
        "title": "Update version number",
        "body": "This pull request updates the version number in library.properties.",
        "head": f"{fork['owner']['login']}:{branch_name}",
        "base": repo["default_branch"],
        "draft": True,
    }
    response = ghr.post(pr_url, json=pr_data)
    if response.status_code != 201:
        raise Exception(f"Failed to create pull request: {response.status_code} {response.text}")


def get_file_contents(owner, repo, file_path):
    """
    Gets the contents of a file in the repository.

    Args:
        owner (str): The owner of the repository.
        repo (str): The name of the repository.
        file_path (str): The path to the file in the repository.
        access_token (str): The access token for the GitHub API.

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



def update_version_number(file_contents, lib_version, release_version, increment=True):
    """
    Updates the version number in the library.properties file.

    Args:
        file_contents (str): The contents of the library.properties file.
        lib_version (str): The current version of the library.
        release_version (str): The new release version of the library.
        increment (bool): Whether to increment the revision number.

    Returns:
        str: The updated contents of the library.properties file.
    """
    new_version = release_version
    if increment:
        lib_semver = semver.VersionInfo.parse(lib_version)
        release_semver = semver.VersionInfo.parse(release_version)
        if release_semver > lib_semver:
            new_version = str(release_semver.bump_patch())
        else:
            new_version = str(lib_semver.bump_patch())
    else:
        logging.warning("** Not incrementing version number as part of release **")
        logging.info("Updating version number from %s to %s", lib_version, new_version)
    lines = file_contents.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("version="):
            lines[i] = f"version={new_version}"
            break
    else:
        raise Exception("version not found in library.properties")
    return "\n".join(lines)