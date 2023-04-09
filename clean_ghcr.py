# Standard lib
from typing import Iterable, Any
from urllib.parse import urljoin
from functools import cache
import argparse
import json
import sys
import os

# Third party
import requests
from dxf import DXF


def str2bool(value: str) -> bool:
    """Utility to convert a boolean string representation to a boolean object."""
    if str(value).lower() in ("yes", "true", "y", "1", "on"):
        return True
    elif str(value).lower() in ("no", "false", "n", "0", "off"):
        return False
    else:
        raise argparse.ArgumentTypeError("Boolean value expected.")


def get_args():
    """Get all arguments passed into this script."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--token", type=str, required=True,
        help="Github Personal access token with delete:packages permissions",
    )
    parser.add_argument(
        "--owner", type=str.lower, required=True,
        help="The repository owner name",
    )
    parser.add_argument(
        "--repo-name", type=str.lower, required=False, default="",
        help="Delete containers only from this repository",
    )
    parser.add_argument(
        "--package-name", type=str.lower, required=False, default="",
        help="Delete only package name",
    )
    parser.add_argument(
        "--owner-type", choices=["org", "user"], default="org",
        help="Owner type (org or user)",
    )
    parser.add_argument(
        "--except-untagged-multiplatform", type=str2bool,
        help="Exempt untagged multiplatform packages from deletion, needs docker installed",
    )
    parser.add_argument(
        "--dry-run", type=str2bool, default=False,
        help="Run the script without making any changes.",
    )

    args = parser.parse_args()

    # GitHub offers the repository as an owner/repo variable
    # So we need to handle that case
    if "/" in args.repo_name:
        owner, repo_name = args.repo_name.lower().split("/")
        if owner != args.owner:
            msg = f"Mismatch in repository: {args.repo_name} and owner:{args.repository_owner}"
            raise ValueError(msg)
        args.repo_name = repo_name

    # Strip any leading or trailing '/'
    args.package_name = args.package_name.strip("/")

    return args


def request_github_api(url: str, method="GET", **options) -> requests.Response:
    """Make web request to GitHub API, returning response."""
    return requests.request(
        method, url,
        headers={
            "X-GitHub-Api-Version": "2022-11-28",
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GITHUB_TOKEN}",
        },
        **options
    )


def get_paged_resp(url: str, params: dict[str, Any] = None) -> Iterable[dict]:
    """Return an iterator of paged results, looping until all resources are collected."""
    params = params or {}
    params.update(page="1")
    params.setdefault("per_page", PER_PAGE)
    url = urljoin(API_ENDPOINT, url)

    while True:
        if not (resp := request_github_api(url, params=params)):
            print(url)
            raise Exception(resp.text)

        for objs in resp.json():
            yield objs

        # Continue with next page if one is found
        if "next" in resp.links:
            url = resp.links["next"]["url"]
            params.pop("page", None)
        else:
            break


DOCKER_ENDPOINT = "ghcr.io"
API_ENDPOINT = os.environ.get("GITHUB_API_URL", "https://api.github.com")
PER_PAGE = 100  # max 100 defaults 30
_args = get_args()

REPO_OWNER = _args.owner
REPO_NAME = _args.repo_name
PACKAGE_NAME = _args.package_name
OWNER_TYPE = _args.owner_type
GITHUB_TOKEN = _args.token
DRY_RUN = _args.dry_run


class Version:
    def __init__(self, pkg: "Package", version):
        self.version = version
        self.pkg = pkg

    @property
    def id(self):
        return self.version["id"]

    @property
    def name(self) -> str:
        return self.version["name"]

    @property
    def tags(self) -> list[str]:
        """Return list of tags for this version."""
        return self.version["metadata"]["container"]["tags"]

    @cache
    def get_deps(self) -> list[str]:
        """Return list of untagged images that this version depends on."""
        if self.tags:
            manifest = self.pkg.registry.get_manifest(self.name)
            manifest = json.loads(manifest)
            return [arch["digest"] for arch in manifest.get("manifests", [])]
        else:
            return []

    def delete(self):
        """Delete this image version from the registry."""
        print(f"Deleting {self.name}:", end=" ")
        if DRY_RUN:
            print("Dry Run")
            return True

        try:
            resp = request_github_api(self.version["url"], method="DELETE")
        except requests.RequestException as e:
            print(e.response.reason if e.response else "Fatal error")
            return False
        else:
            print("OK" if resp.status_code == 204 else resp.reason)
            return resp.ok

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return self.id == other.id
        else:
            return False


class Package:
    def __init__(self, owner: str, pkg_data):
        self.pkg = pkg_data
        self.owner = owner

        self.registry = DXF(
            DOCKER_ENDPOINT,
            repo=f"{self.owner}/{self.name}",
            auth=lambda dxf, resp: dxf.authenticate(owner, GITHUB_TOKEN, response=resp)
        )

    @property
    def name(self) -> str:
        return self.pkg["name"]

    @property
    def version_url(self) -> str:
        """Rest url to package versions."""
        url = self.pkg["url"]
        return f"{url}/versions"

    def get_versions(self) -> Iterable["Version"]:
        """Iterable of package versions."""
        for version in get_paged_resp(self.version_url):
            yield Version(self, version)

    @classmethod
    def get_all_packages(cls, owner_type: str, owner: str, repo_name: str, package_name: str) -> Iterable["Package"]:
        """Return an iterator of registry packages."""
        path = f"/{owner_type}s/{owner}/packages?package_type=container"
        for pkg in get_paged_resp(path):
            if repo_name and pkg.get("repository", {}).get("name", "").lower() != repo_name.lower():
                continue

            elif package_name and pkg["name"] != package_name:
                continue

            yield cls(owner, pkg)


def run():
    # Get list of all packages
    all_packages = Package.get_all_packages(
        owner=REPO_OWNER,
        repo_name=REPO_NAME,
        package_name=PACKAGE_NAME,
        owner_type=OWNER_TYPE,
    )

    all_deps, all_untagged = set(), set()
    for pkg in all_packages:
        for version in pkg.get_versions():
            deps = version.get_deps()
            all_deps.update(deps)

            if not version.tags:
                all_untagged.add(version)

    all_deps = frozenset(all_deps)
    status_counts = [0, 0]  # [Fail, OK]
    for untagged_version in all_untagged:
        if untagged_version.name not in all_deps:
            status = untagged_version.delete()
            status_counts[status] += 1

    print(status_counts[1], "Deletions")
    print(status_counts[0], "Errors")
    if status_counts[0]:
        sys.exit(1)


if __name__ == "__main__":
    run()
