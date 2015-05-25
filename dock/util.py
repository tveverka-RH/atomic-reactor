"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import json
import os
from pipes import quote
import re
import shlex
import shutil
import subprocess
import tempfile
import logging
import uuid
from dock.constants import DOCKERFILE_FILENAME


logger = logging.getLogger(__name__)


class ImageName(object):
    def __init__(self, registry=None, namespace=None, repo=None, tag=None):
        self.registry = registry
        self.namespace = namespace
        self.repo = repo
        self.tag = tag

    @classmethod
    def parse(cls, image_name):
        result = cls()

        # registry.org/namespace/repo:tag
        s = image_name.split('/', 2)

        if len(s) == 2:
            if '.' in s[0] or ':' in s[0]:
                result.registry = s[0]
            else:
                result.namespace = s[0]
        elif len(s) == 3:
            result.registry = s[0]
            result.namespace = s[1]
        if result.namespace == 'library':
            # https://github.com/DBuildService/dock/issues/45
            logger.debug("namespace 'library' -> ''")
            result.namespace = None
        result.repo = s[-1]

        try:
            result.repo, result.tag = result.repo.rsplit(':', 1)
        except ValueError:
            pass

        return result

    def to_str(self, registry=True, tag=True, explicit_tag=False,
               explicit_namespace=False):
        if self.repo is None:
            raise RuntimeError('No image repository specified')

        result = self.repo

        if tag and self.tag:
            result = '{0}:{1}'.format(result, self.tag)
        elif tag and explicit_tag:
            result = '{0}:{1}'.format(result, 'latest')

        if self.namespace:
            result = '{0}/{1}'.format(self.namespace, result)
        elif explicit_namespace:
            result = '{0}/{1}'.format('library', result)

        if registry and self.registry:
            result = '{0}/{1}'.format(self.registry, result)

        return result

    def __str__(self):
        return self.to_str(registry=True, tag=True)

    def __eq__(self, other):
        return type(self) == type(other) and self.__dict__ == other.__dict__

    def copy(self):
        return ImageName(
            registry=self.registry,
            namespace=self.namespace,
            repo=self.repo,
            tag=self.tag)


def get_baseimage_from_dockerfile_path(path):
    with open(path, 'r') as dockerfile:
        for line in dockerfile:
            if line.startswith("FROM"):
                return line.split()[1]


def get_baseimage_from_dockerfile(git_path, path=''):
    """ return name of base image from provided gitrepo """
    if git_path.endswith(DOCKERFILE_FILENAME):
        dockerfile_path = git_path
    else:
        if path.endswith(DOCKERFILE_FILENAME):
            dockerfile_path = os.path.join(git_path, path)
        else:
            dockerfile_path = os.path.join(git_path, path, DOCKERFILE_FILENAME)
    return get_baseimage_from_dockerfile_path(dockerfile_path)


def get_labels_from_dockerfile(path):
    """ opposite of AddLabelsPlugin, i.e. return dict of labels from dockerfile
    :param path: dockerfile path
    :return: dictionary of label:value or label:'' if there's no value
    """
    labels = {}
    multiline = False
    processed_instr = ""
    with open(path, 'r') as dockerfile:
        for line in dockerfile:
            line = line.rstrip()  # docker does this
            logger.debug("processing line %s", repr(line))
            if multiline:
                processed_instr += line
                if line.endswith("\\"):  # does multiline continue?
                    # docker strips single \
                    processed_instr = processed_instr[:-1]
                    continue
                else:
                    multiline = False
            else:
                processed_instr = line
            if processed_instr.startswith("LABEL"):
                if processed_instr.endswith("\\"):
                    logger.debug("multiline LABEL")
                    # docker strips single \
                    processed_instr = processed_instr[:-1]
                    multiline = True
                    continue
                for token in shlex.split(processed_instr[5:]):
                    key_val = token.split("=", 1)
                    if len(key_val) == 2:
                        labels[key_val[0]] = key_val[1]
                    else:
                        labels[key_val[0]] = ''
                    logger.debug("new label %s=%s", repr(key_val[0]), repr(labels[key_val[0]]))
    return labels


class CommandResult(object):
    def __init__(self, logs, error=None, error_detail=None):
        self._logs = logs
        self._error = error
        self._error_detail = error_detail

    @property
    def logs(self):
        return self._logs

    @property
    def error(self):
        return self._error

    @property
    def error_detail(self):
        return self._error_detail

    def is_failed(self):
        return bool(self.error) or bool(self.error_detail)


def wait_for_command(logs_generator):
    """
    using given generator, wait for it to raise StopIteration, which
    indicates that docker has finished with processing

    :return: list of str, logs
    """
    # FIXME: this function is getting pretty big, let's break it down a bit
    #        and merge it into CommandResult
    logger.info("wait_for_command")
    logs = []
    error = None
    error_message = None
    while True:
        try:
            parsed_item = None
            item = next(logs_generator)  # py2 & 3 compat
            item = item.decode("utf-8")
            try:
                parsed_item = json.loads(item)
            except ValueError:
                pass

            # make sure the json is an object
            if isinstance(parsed_item, dict):
                line = parsed_item.get("stream", "")
            else:
                parsed_item = None
                line = item

            for l in re.split(r"\r?\n", line, re.MULTILINE):
                # line = line.replace("\r\n", " ").replace("\n", " ").strip()
                l = l.strip()
                if l:
                    logger.debug(l)
            logs.append(item)
            if parsed_item is not None:
                error = parsed_item.get("error", None)
                error_message = parsed_item.get("errorDetail", None)
                if error:
                    logger.error(item.strip())
        except StopIteration:
            logger.info("no more logs")
            break
    cr = CommandResult(logs=logs, error=error, error_detail=error_message)
    return cr


def clone_git_repo(git_url, target_dir, commit=None):
    """
    clone provided git repo to target_dir, optionally checkout provided commit

    :param git_url: str, git repo to clone
    :param target_dir: str, filesystem path where the repo should be cloned
    :param commit: str, commit to checkout
    :return: str, commit ID of HEAD
    """
    commit = commit or "master"
    logger.info("clone git repo")
    logger.debug("url = '%s', dir = '%s', commit = '%s'",
                 git_url, target_dir, commit)

    # http://stackoverflow.com/questions/1911109/clone-a-specific-git-branch/4568323#4568323
    cmd = ["git", "clone", "-b", commit, "--single-branch", git_url, quote(target_dir)]
    subprocess.check_call(cmd)
    cmd = ["git", "-C", quote(target_dir), "rev-parse", "HEAD"]
    commit_id = subprocess.check_output(cmd)
    commit_id = commit_id.strip()
    logger.info("commit ID = %s", commit_id)
    return commit_id


def figure_out_dockerfile(absolute_path, local_path=None):
    """
    try to figure out dockerfile from provided path and optionally from relative local path
    this is meant to be used with git repo: absolute_path is path to git repo,
    local_path is path to dockerfile within git repo

    :param absolute_path:
    :param local_path:
    :return: tuple, (dockerfile_path, dir_with_dockerfile_path)
    """
    logger.info("find dockerfile")
    logger.debug("abs path = '%s', local path = '%s'", absolute_path, local_path)
    if local_path:
        if local_path.endswith(DOCKERFILE_FILENAME):
            git_df_dir = os.path.dirname(local_path)
            df_dir = os.path.abspath(os.path.join(absolute_path, git_df_dir))
        else:
            df_dir = os.path.abspath(os.path.join(absolute_path, local_path))
    else:
        df_dir = os.path.abspath(absolute_path)
    if not os.path.isdir(df_dir):
        raise IOError("Directory '%s' doesn't exist." % df_dir)
    df_path = os.path.join(df_dir, DOCKERFILE_FILENAME)
    if not os.path.isfile(df_path):
        raise IOError("Dockerfile '%s' doesn't exist." % df_path)
    logger.debug("dockerfile found: '%s'", df_path)
    return df_path, df_dir


class LazyGit(object):
    """
    usage:

        lazy_git = LazyGit(git_url="...")
        with lazy_git:
            laze_git.git_path

    or

        lazy_git = LazyGit(git_url="...", tmpdir=tmp_dir)
        lazy_git.git_path
    """
    def __init__(self, git_url, commit=None, tmpdir=None):
        self.git_url = git_url
        # provided commit ID/reference to check out
        self.commit = commit
        # commit ID of HEAD; we'll figure this out ourselves
        self._commit_id = None
        self.provided_tmpdir = tmpdir
        self._git_path = None

    @property
    def _tmpdir(self):
        return self.provided_tmpdir or self.our_tmpdir

    @property
    def commit_id(self):
        return self._commit_id

    @property
    def git_path(self):
        if self._git_path is None:
            self._commit_id = clone_git_repo(self.git_url, self._tmpdir, self.commit)
            self._git_path = self._tmpdir
        return self._git_path

    def __enter__(self):
        if not self.provided_tmpdir:
            self.our_tmpdir = tempfile.mkdtemp()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.provided_tmpdir:
            if self.our_tmpdir:
                shutil.rmtree(self.our_tmpdir)


def escape_dollar(v):
    try:
        str_type = unicode
    except NameError:
        str_type = str
    if isinstance(v, str_type):
        return v.replace('$', r'\$')
    else:
        return v


def render_yum_repo(repo):
    repo.setdefault("name", str(uuid.uuid4().hex[:6]))
    repo_name = repo["name"]
    logger.info("rendering repo '%s'", repo_name)
    rendered_repo = '[%s]\n' % repo_name
    for key, value in repo.items():
        rendered_repo += "%s=%s\n" % (key, escape_dollar(value))
    return rendered_repo
