#!/usr/bin/env python3
"""Updates HelmReleases with an annotation consumeable by rennovate.

This script adds annotations fo HelmRelease files so that rennovate can manage
chart upgrades. This script accepts a --cluster-path argument which should
point at a fluxv2 repository that contains Kustomization yaml files, referring
to HelmReleases.

The script takes a few steps:
  - Find all HelmRepository entries in the cluster, and its associated chart url
  - Find all HelmReleases that reference a HelmRepository
  - Update all files that contain HelmReleases, with an annotation to reference
    the chart url for the repository. This is done as a second pass to handle
    and kustomize overlays.
"""

import logging
import os
import subprocess
from pathlib import Path

import click
import yaml

DEFAULT_NAMESPACE = "default"
INCLUDE_FILES = [".yaml", ".yml"]
HELM_REPOSITORY_APIVERSIONS = ["source.toolkit.fluxcd.io/v1beta1"]
HELM_RELEASE_APIVERSIONS = ["helm.toolkit.fluxcd.io/v2beta1"]
RENOVATE_STRING = "# renovate: registryUrl="


class ClusterPath(click.ParamType):
    name = "cluster-path"
    def convert(self, value, param, ctx):
        clusterPath = Path(value)
        if not isinstance(value, tuple):
            if not clusterPath.exists:
                self.fail(f"invalid --cluster-path '{value}'")
        return clusterPath


def yaml_load_files(files, tolerate_yaml_errors):
    """A generator that loads the contents of all files in yaml."""
    for file in files:
        try:
            for doc in yaml.safe_load_all(file.read_bytes()):
                if doc:
                    yield (file, doc)
        except yaml.YAMLError as e:
            if tolerate_yaml_errors:
                logger().warning(f"Skipping '{file}': {e}")
                pass
            else:
                logger().error(f"File '{file}' has syntax errors.")
                raise e

def kind_filter(kind, api_versions):
    """Return a yaml doc filter for specified resource type and version."""
    def func(pair):
        (file, doc) = pair
        if isinstance(doc, list):
            return False
        if doc.get("kind") != kind:
            return False
        return doc.get("apiVersion") in api_versions
    return func


def namespaced_name(doc):
    """Return a named yaml resource, falling back to a default namespace."""
    name = doc["name"]
    namespace = doc.get("namespace", DEFAULT_NAMESPACE)
    return f"{namespace}/{name}"


@click.command()
@click.option(
    "--cluster-path", envvar="CLUSTER_PATH",
    type=ClusterPath(),
    required=True,
    help="Path to cluster root, e.g. './cluster'"
)
@click.option(
    "--excluded-folders", envvar="EXCLUDED_FOLDERS",
    type=click.Path(),
    multiple=True,
    required=False,
    help="Path to excluded folders, e.g. './cluster/ansible'"
)
@click.option(
    "--debug", envvar="DEBUG",
    is_flag=True,
    default=False,
    required=False,
    help="Turn on debug logging"
)
@click.option(
    "--dry-run", envvar="DRY_RUN",
    is_flag=True,
    default=False,
    required=False,
    help="Do not alter Helm Release files"
)
@click.option(
    "--tolerate-yaml-errors", envvar="TOLERATE_YAML_ERRORS",
    is_flag=True,
    default=False,
    required=False,
    help="Warn on yaml errors"
)
@click.pass_context
def cli(ctx, cluster_path, excluded_folders, debug, dry_run, tolerate_yaml_errors):
    ctx.obj = {
        "cluster_path": cluster_path,
        "excluded_folders": excluded_folders,
        "debug": debug,
        "dry_run": dry_run,
        "tolerate_yaml_errors": tolerate_yaml_errors
    }

    # pylint: disable=no-value-for-parameter
    log = logger()

    excluded_files = [p for folder in excluded_folders for p in Path(folder).rglob("*")]

    files = [p for p in cluster_path.rglob("*") if p.suffix in INCLUDE_FILES and p not in excluded_files]

    yaml_docs = list(yaml_load_files(files, tolerate_yaml_errors))

    # Build a map of HelmRepository name to chart url
    helm_repo_charts = {}
    is_helm_repo = kind_filter("HelmRepository", HELM_REPOSITORY_APIVERSIONS)
    for (file, doc) in filter(is_helm_repo, yaml_docs):
        helm_repo_name = namespaced_name(doc["metadata"])
        helm_repo_url = doc["spec"]["url"]
        log.info(f"Found HelmRepository '{helm_repo_name}' url '{helm_repo_url}'")
        helm_repo_charts[helm_repo_name] = helm_repo_url

    # Walk all HelmReleases and create a map of release names to repos.
    is_helm_release = kind_filter("HelmRelease", HELM_RELEASE_APIVERSIONS)
    helm_release_docs = list(filter(is_helm_release, yaml_docs))
    helm_releases = {}
    for (file, doc) in helm_release_docs:
        helm_release_name = namespaced_name(doc["metadata"])
        chart_spec = doc.get("spec",{}).get("chart",{}).get("spec", {})
        if not chart_spec:
            log.debug(f"Skipping '{helm_release_name}': No 'spec.chart.spec'")
            continue
        source_ref = chart_spec.get("sourceRef")
        if not source_ref:
            # This release may be an overlay, so the repo name could be inferred from the
            # release name of the base HelmRelease in a second pass below.
            log.debug(f"Skipping '{helm_release_name}': No 'sourceRef' in spec.chart.spec")
            continue
        helm_repo_name = namespaced_name(source_ref)
        if helm_repo_name not in helm_repo_charts:
            log.warning(f"Skipping '{helm_release_name}': No HelmRepository for '{helm_repo_name}'")
            continue
        if helm_release_name in helm_releases:
            if helm_releases[helm_release_name] != helm_repo_name:
                log.warning(f"HelmRelease '{helm_release_name}' mismatched repo '{helm_repo_name}'")
                continue
        log.info(f"Found HelmRelease '{helm_release_name}' with repo '{helm_repo_name}'")
        helm_releases[helm_release_name] = helm_repo_name

    # Walk all HelmReleases and find the referenced HelmRepository by the
    # chart sourceRef and update the renovate annotation.
    for (file, doc) in helm_release_docs:
        helm_release_name = namespaced_name(doc["metadata"])
        if helm_release_name not in helm_releases:
            log.debug(f"Skipping '{helm_release_name}': Could not determine repo")
            continue
        # Renovate can only update chart specs that contain a name and version,
        chart_spec = doc.get("spec",{}).get("chart",{}).get("spec", {})
        if not chart_spec:
            log.debug(f"Skipping '{helm_release_name}': No 'spec.chart.spec'")
            continue
        if "chart" not in chart_spec or "version" not in chart_spec:
            log.debug(f"Skipping '{helm_release_name}': No 'chart' or 'version' in spec.chart.spec")
            continue
        helm_repo_name = helm_releases[helm_release_name]
        if helm_repo_name not in helm_repo_charts:
            log.debug(f"Skipping '{file}': Not HelmRepostory '{helm_repo_name}' found")
            continue

        chart_url = helm_repo_charts[helm_repo_name]

        if dry_run:
            log.warning(
                f"Skipping '{helm_repo_name}' annotations in '{file}' with '{chart_url}' as this is a dry run"
            )
            continue

        log.info(
            f"Updating '{helm_repo_name}' renovate annotations in '{file}' with '{chart_url}'"
        )
        with open(file, mode="r") as fid:
            lines = fid.read().splitlines()

        with open(file, mode="w") as fid:
            for line in lines:
                if RENOVATE_STRING in line:
                    continue

                if " chart: " in line:
                    indent_spaces = len(line) - len(line.lstrip())
                    fid.write(f"{' ' * indent_spaces}{RENOVATE_STRING}{chart_url}\n")
                    pass

                fid.write(f"{line}\n")


@click.pass_context
def logger(ctx):
    """Set up logging
    """
    logging.basicConfig(
        level=(logging.DEBUG if ctx.obj["debug"] else logging.INFO),
        format="%(asctime)s %(name)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    return logging.getLogger("Renovate Helm Releases")


if __name__ == "__main__":
    # pylint: disable=no-value-for-parameter
    cli()
