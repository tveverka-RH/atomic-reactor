"""
Copyright (c) 2017-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import logging
import os
import re
import sys
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from textwrap import dedent

import koji
import pytest
import responses
import yaml
from flexmock import flexmock

from atomic_reactor.constants import (
    PLUGIN_KOJI_PARENT_KEY,
    BASE_IMAGE_KOJI_BUILD, DOCKERFILE_FILENAME
)
from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.pre_resolve_composes import (ResolveComposesPlugin,
                                                         ODCS_DATETIME_FORMAT, UNPUBLISHED_REPOS)
from atomic_reactor.source import SourceConfig
from atomic_reactor.utils.odcs import ODCSClient, construct_compose_url, WaitComposeToFinishTimeout
from tests.mock_env import MockEnv
from tests.util import add_koji_map_in_workflow

KOJI_HUB = 'http://koji.com/hub'

KOJI_BUILD_ID = 123456789

KOJI_TAG_NAME = 'test-tag'
KOJI_TARGET_NAME = 'test-target'
KOJI_TARGET = {
    'build_tag_name': KOJI_TAG_NAME,
    'name': KOJI_TARGET_NAME
}

ODCS_URL = 'https://odcs.fedoraproject.org/odcs/1'

ODCS_COMPOSE_ID = 84
ODCS_COMPOSE_REPO = 'https://odcs.fedoraproject.org/composes/latest-odcs-1-1/compose/Temporary'
ODCS_COMPOSE_REPOFILE = ODCS_COMPOSE_REPO + '/odcs-1.repo'
ODCS_COMPOSE_SECONDS_TO_LIVE = timedelta(hours=24)
ODCS_COMPOSE_TIME_TO_EXPIRE = datetime.utcnow() + ODCS_COMPOSE_SECONDS_TO_LIVE
ODCS_COMPOSE_DEFAULT_ARCH = 'x86_64'
ODCS_COMPOSE_DEFAULT_ARCH_LIST = [ODCS_COMPOSE_DEFAULT_ARCH]
ODCS_COMPOSE = {
    'id': ODCS_COMPOSE_ID,
    'result_repo': ODCS_COMPOSE_REPO,
    'result_repofile': ODCS_COMPOSE_REPOFILE,
    'source': KOJI_TAG_NAME,
    'source_type': 'tag',
    'sigkeys': '',
    'state_name': 'done',
    'arches': ODCS_COMPOSE_DEFAULT_ARCH,
    'time_to_expire': ODCS_COMPOSE_TIME_TO_EXPIRE.strftime(ODCS_DATETIME_FORMAT),
}

SIGNING_INTENTS = {
    'release': ['R123'],
    'beta': ['R123', 'B456', 'B457'],
    'unsigned': [],
}

DEFAULT_SIGNING_INTENT = 'release'


@pytest.fixture
def mocked_env(workflow, source_dir):
    env = (
        MockEnv(workflow)
        .for_plugin("prebuild", ResolveComposesPlugin.key)
        .set_orchestrator_platforms(ODCS_COMPOSE_DEFAULT_ARCH_LIST)
        .set_dockerfile_images(["Fedora:22"])
        .set_check_platforms_result(set(ODCS_COMPOSE_DEFAULT_ARCH_LIST))
        .set_reactor_config(make_reactor_config(source_dir))
    )

    env.workflow.source = MockSource(source_dir)
    mock_repo_config(source_dir)

    # These are used for further mocking and are not normally part of MockEnv
    env._tmpdir = source_dir
    env._koji_session = mock_koji_session()
    return env


class MockSource(object):
    def __init__(self, source_dir: Path):
        self.dockerfile_path = str(source_dir / DOCKERFILE_FILENAME)
        self.path = str(source_dir)
        self._config = None

    def get_build_file_path(self):
        return self.dockerfile_path, self.path

    @property
    def config(self):  # lazy load after container.yaml has been created
        self._config = self._config or SourceConfig(self.path)
        return self._config


def make_reactor_config(source_dir: Path, data=None, default_si=DEFAULT_SIGNING_INTENT):
    if data is None:
        data = dedent("""\
            version: 1
            odcs:
               signing_intents:
               - name: release
                 keys: ['R123']
               - name: beta
                 keys: ['R123', 'B456', 'B457']
               - name: unsigned
                 keys: []
               default_signing_intent: {}
               api_url: {}
               auth:
                   ssl_certs_dir: {}
            koji:
                hub_url: /
                root_url: ''
                auth: {{}}
            """.format(default_si, ODCS_URL, source_dir))

    source_dir.joinpath('cert').write_text("", "utf-8")
    config = yaml.safe_load(data)
    return config


def mock_repo_config(source_dir: Path, data=None, signing_intent=None):
    if data is None:
        data = dedent("""\
            compose:
                packages:
                - spam
                - bacon
                - eggs
            """)
        if signing_intent:
            data += "    signing_intent: {}".format(signing_intent)

    source_dir.joinpath('container.yaml').write_text(data, "utf-8")


def mock_content_sets_config(source_dir: Path, data=None):
    if data is None:
        data = dedent("""\
            x86_64:
            - pulp-spam-rpms
            - pulp-bacon-rpms
            - pulp-eggs-rpms
        """)

    source_dir.joinpath('content_sets.yml').write_text(data, "utf-8")


def mock_odcs_client_start_compose():
    """
    Common mock for tests requiring basic compose operation. Typically, this
    should be used with mock_odcs_client_wait_for_compose. However, if the
    fake data set in this mock cannot fulfill the requirement of a test, please
    write a custom one specifically.
    """
    (flexmock(ODCSClient)
        .should_receive('start_compose')
        .with_args(
            source_type='tag',
            source=KOJI_TAG_NAME,
            arches=ODCS_COMPOSE_DEFAULT_ARCH_LIST,
            packages=['spam', 'bacon', 'eggs'],
            sigkeys=['R123'])
        .and_return(ODCS_COMPOSE))


def mock_odcs_client_wait_for_compose():
    """Refer to the doc of mock_odcs_client_start_compose"""
    (flexmock(ODCSClient)
        .should_receive('wait_for_compose')
        .with_args(ODCS_COMPOSE_ID)
        .and_return(ODCS_COMPOSE))


def mock_koji_session():
    koji_session = flexmock()
    flexmock(koji).should_receive('ClientSession').and_return(koji_session)

    def mock_get_build_target(target_name, strict):
        assert strict is True

        if target_name == KOJI_TARGET_NAME:
            return KOJI_TARGET

        raise koji.GenericError('No matching build target found: {}'.format(target_name))

    (flexmock(koji_session)
        .should_receive('getBuildTarget')
        .replace_with(mock_get_build_target))
    (flexmock(koji_session)
        .should_receive('krb_login')
        .and_return(True))

    return koji_session


def mock_koji_parent(mocked_env,
                     scratch=False, isolated=False, parent_repo=None, parent_compose_ids=None):
    mocked_env.set_scratch(scratch).set_isolated(isolated)

    parent_build_info = {
        'id': 1234,
        'nvr': 'fedora-27-1',
        'extra': {'image': {'odcs': {'compose_ids': parent_compose_ids,
                                     'signing_intent': 'unsigned'},
                            'yum_repourls': [parent_repo]}},
    }
    if not parent_repo:
        parent_build_info['extra']['image'].pop('yum_repourls')
    if not parent_compose_ids:
        parent_build_info['extra']['image'].pop('odcs')

    mocked_env.set_plugin_result(
        "prebuild", PLUGIN_KOJI_PARENT_KEY, {BASE_IMAGE_KOJI_BUILD: parent_build_info}
    )


class TestResolveComposes(object):

    def teardown_method(self, method):
        sys.modules.pop('pre_resolve_composes', None)

    def test_request_compose(self, mocked_env):
        mock_odcs_client_start_compose()
        mock_odcs_client_wait_for_compose()
        self.run_plugin_with_args(mocked_env)

    @pytest.mark.parametrize('arches', (
        ['ppc64le', 'x86_64'],
        ['x86_64'],
    ))
    def test_request_compose_for_multiarch_tag(self, mocked_env, arches):
        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(
                source_type='tag',
                source='test-tag',
                packages=['spam', 'bacon', 'eggs'],
                sigkeys=['R123'],
                arches=arches)
            .once()
            .and_return(ODCS_COMPOSE))
        mock_odcs_client_wait_for_compose()
        mocked_env.set_check_platforms_result(arches)
        self.run_plugin_with_args(mocked_env)

    @pytest.mark.parametrize(('parent_compose', 'parent_repourls', 'repo_provided'), [
        (True, True, False),
        (True, False, False),
        (False, True, False),
        (False, False, False),
        (True, True, True),
        (True, False, True),
        (False, True, True),
        (False, False, True),
    ])
    @pytest.mark.parametrize(('inherit_parent', 'scratch', 'isolated', 'allow_inherit', 'ids',
                              'compose_defined'), [
        (True, True, False, False, True, True),
        (True, False, True, False, True, True),
        (True, False, False, True, True, True),
        (False, True, False, False, True, True),
        (False, False, True, False, True, True),
        (False, False, False, False, True, True),
        (True, True, False, False, False, True),
        (True, False, True, False, False, True),
        (True, False, False, True, False, True),
        (False, True, False, False, False, True),
        (False, False, True, False, False, True),
        (False, False, False, False, False, True),
        (True, True, False, False, True, False),
        (True, False, True, False, True, False),
        (True, False, False, True, True, False),
        (False, True, False, False, True, False),
        (False, False, True, False, True, False),
        (False, False, False, False, True, False),
        (True, True, False, False, False, False),
        (True, False, True, False, False, False),
        (True, False, False, True, False, False),
        (False, True, False, False, False, False),
        (False, False, True, False, False, False),
        (False, False, False, False, False, False),
    ])
    def test_inherit_parents(self, mocked_env, parent_compose, parent_repourls,
                             repo_provided, inherit_parent, scratch, isolated, allow_inherit,
                             compose_defined, ids, caplog):
        arches = ['ppc64le', 'x86_64']
        odcs_with_arches = ODCS_COMPOSE.copy()
        odcs_with_arches['arches'] = ' '.join(arches)
        workflow = mocked_env.workflow
        if inherit_parent and compose_defined:
            repo_config = dedent("""\
                compose:
                    packages:
                    - spam
                    - bacon
                    - eggs
                    inherit: true
                """)
            mock_repo_config(mocked_env._tmpdir, repo_config)
        elif inherit_parent:
            repo_config = dedent("""\
                compose:
                    inherit: true
                """)
            mock_repo_config(mocked_env._tmpdir, repo_config)
        elif not compose_defined:
            mocked_env._tmpdir.joinpath('container.yaml').write_text("", "utf-8")

        parent_compose_ids = [10, 11]
        parent_repo = "http://example.com/parent.repo"
        mock_koji_parent(mocked_env,
                         parent_compose_ids=parent_compose_ids if parent_compose else None,
                         parent_repo=parent_repo if parent_repourls else None,
                         scratch=scratch, isolated=isolated)

        if ids:
            (flexmock(ODCSClient)
             .should_receive('start_compose')
             .never())
        elif compose_defined:
            sigkeys = []
            if not parent_compose:
                sigkeys = ['R123']
            (flexmock(ODCSClient)
                .should_receive('start_compose')
                .with_args(
                    source_type='tag',
                    source='test-tag',
                    packages=['spam', 'bacon', 'eggs'],
                    sigkeys=sigkeys,
                    arches=arches)
                .once()
                .and_return(odcs_with_arches))

            (flexmock(ODCSClient)
                .should_receive('wait_for_compose')
                .with_args(ODCS_COMPOSE_ID)
                .and_return(odcs_with_arches))

        compose_ids = []
        current_repourls = ["http://example.com/current.repo"]
        expected_yum_repourls = defaultdict(list)
        if not ids and compose_defined:
            for arch in arches:
                expected_yum_repourls[arch].append(odcs_with_arches['result_repofile'])

        if ids:
            for compose_id in range(3, 6):
                compose = odcs_with_arches.copy()
                compose['id'] = compose_id
                compose['result_repofile'] = ODCS_COMPOSE_REPO + '/odcs-{}.repo'.format(compose_id)

                (flexmock(ODCSClient)
                    .should_receive('wait_for_compose')
                    .once()
                    .with_args(compose_id)
                    .and_return(compose))

                compose_ids.append(compose_id)
                for arch in arches:
                    expected_yum_repourls[arch].append(compose['result_repofile'])

        if allow_inherit and parent_compose:
            for compose_id in parent_compose_ids:
                compose = odcs_with_arches.copy()
                compose['id'] = compose_id
                compose['result_repofile'] = ODCS_COMPOSE_REPO + '/odcs-{}.repo'.format(compose_id)

                (flexmock(ODCSClient)
                 .should_receive('wait_for_compose')
                 .once()
                 .with_args(compose_id)
                 .and_return(compose))
                for arch in arches:
                    expected_yum_repourls[arch].append(compose['result_repofile'])

        if repo_provided:
            for arch in expected_yum_repourls or arches:
                expected_yum_repourls[arch].extend(current_repourls)

        if allow_inherit and parent_repourls:
            for arch in expected_yum_repourls or arches:
                expected_yum_repourls[arch].append(parent_repo)

        mocked_env.set_check_platforms_result(arches)

        plugin_args = {}
        if repo_provided:
            plugin_args['repourls'] = current_repourls
        if ids:
            plugin_args['compose_ids'] = compose_ids

        results = self.run_plugin_with_args(mocked_env, plugin_args)

        yum_repurls = results.get('yum_repourls') or {}

        for k, v in expected_yum_repourls.items():
            assert k in yum_repurls
            assert sorted(yum_repurls[k]) == sorted(v)

        if allow_inherit and parent_compose:
            for parent_id in parent_compose_ids:
                assert 'Inheriting compose id {}'.format(parent_id) in caplog.text

        all_yum_repourls = []
        if repo_provided:
            all_yum_repourls = list(current_repourls)
        if allow_inherit and parent_repourls:
            all_yum_repourls.append(parent_repo)
            assert 'Inheriting yum repo http://example.com/parent.repo' in caplog.text

        assert set(workflow.all_yum_repourls) == set(all_yum_repourls)

    @pytest.mark.parametrize('arches', (
        ['ppc64le', 'x86_64'],
        ['x86_64'],
    ))
    def test_request_compose_for_modules(self, mocked_env, arches):
        repo_config = dedent("""\
            compose:
                modules:
                - spam:stable
                - bacon:stable
                - eggs:stable/profile
            """)
        mock_repo_config(mocked_env._tmpdir, repo_config)

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(
                source_type='module',
                source='spam:stable bacon:stable eggs:stable',
                sigkeys=['R123'],
                arches=arches)
            .once()
            .and_return(ODCS_COMPOSE))

        mock_odcs_client_wait_for_compose()

        mocked_env.set_check_platforms_result(arches)
        self.run_plugin_with_args(mocked_env)

    @pytest.mark.parametrize('multilib', (True, False))
    @pytest.mark.parametrize('is_true', (True, False))
    @pytest.mark.parametrize('arches', (
        ['ppc64le', 'x86_64'],
        ['x86_64'],
    ))
    def test_request_compose_for_modular_tags(self, mocked_env, multilib, is_true, arches):
        repo_config = {'compose': {'modular_koji_tags': ['earliest', 'latest']}}
        if is_true:
            repo_config['compose']['modular_koji_tags'] = True
        if multilib:
            repo_config['compose']['multilib_arches'] = arches
            repo_config['compose']['multilib_method'] = ["all"]

        mock_repo_config(mocked_env._tmpdir, yaml.safe_dump(repo_config))

        use_kwargs = {'source_type': 'tag',
                      'source': 'test-tag',
                      'sigkeys': ['R123'],
                      'arches': arches}

        if is_true:
            use_kwargs['modular_koji_tags'] = ['test-tag']
        else:
            use_kwargs['modular_koji_tags'] = ['earliest', 'latest']

        if multilib:
            use_kwargs['multilib_arches'] = arches
            use_kwargs['multilib_method'] = ["all"]

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(**use_kwargs)
            .once()
            .and_return(ODCS_COMPOSE))

        mock_odcs_client_wait_for_compose()

        mocked_env.set_check_platforms_result(arches)
        self.run_plugin_with_args(mocked_env)

    def test_request_compose_for_modular_tags_auto_without_tag(self, mocked_env):
        repo_config = dedent("""\
            compose:
                modular_koji_tags: true
            """)
        mock_repo_config(mocked_env._tmpdir, repo_config)

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .never())
        mocked_env.set_check_platforms_result('x86_64')

        with pytest.raises(PluginFailedException) as exc:
            self.run_plugin_with_args(mocked_env, with_target=False)
        assert "koji_tag is required when modular_koji_tags is True" in str(exc.value)

    def test_request_compose_packages_modules_modular_tags(self, mocked_env):
        repo_config = dedent("""\
            compose:
                packages:
                - pkg_spam
                - pkg_bacon
                modules:
                - spam:stable
                - bacon:stable
                - eggs:stable
                modular_koji_tags:
                - earliest
                - latest
                module_resolve_tags:
                - special
            """)
        mock_repo_config(mocked_env._tmpdir, repo_config)

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(source_type='tag',
                       source='test-tag',
                       sigkeys=['R123'],
                       packages=['pkg_spam', 'pkg_bacon'],
                       arches=['x86_64'])
            .and_return(ODCS_COMPOSE))
        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(source_type='tag',
                       source='test-tag',
                       sigkeys=['R123'],
                       modular_koji_tags=['earliest', 'latest'],
                       arches=['x86_64'])
            .and_return(ODCS_COMPOSE))
        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(source_type='module',
                       source='spam:stable bacon:stable eggs:stable',
                       sigkeys=['R123'],
                       modular_koji_tags=['special'],
                       arches=['x86_64'])
            .and_return(ODCS_COMPOSE))

        mock_odcs_client_wait_for_compose()

        self.run_plugin_with_args(mocked_env)

    @pytest.mark.parametrize('is_true', (True, False))
    def test_request_compose_packages_for_module_resolve_tags(self, mocked_env, is_true):
        repo_config = yaml.safe_load(dedent("""\
            compose:
                modules:
                - spam:stable
                - bacon:stable
                - eggs:stable
            """))

        if is_true:
            repo_config['compose']['module_resolve_tags'] = True
            expected_modular_koji_tags = ['test-tag']
        else:
            repo_config['compose']['module_resolve_tags'] = ['special']
            expected_modular_koji_tags = ['special']

        mock_repo_config(mocked_env._tmpdir, yaml.safe_dump(repo_config))

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(source_type='module',
                       source='spam:stable bacon:stable eggs:stable',
                       sigkeys=['R123'],
                       modular_koji_tags=expected_modular_koji_tags,
                       arches=['x86_64'])
            .and_return(ODCS_COMPOSE))

        mock_odcs_client_wait_for_compose()

        self.run_plugin_with_args(mocked_env)

    def test_request_compose_for_module_resolve_tags_auto_without_tag(self, mocked_env):
        repo_config = dedent("""\
            compose:
                modules:
                - spam:stable
                - bacon:stable
                - eggs:stable
            compose:
                module_resolve_tags: true
            """)
        mock_repo_config(mocked_env._tmpdir, repo_config)

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .never())
        mocked_env.set_check_platforms_result('x86_64')

        with pytest.raises(PluginFailedException) as exc:
            self.run_plugin_with_args(mocked_env, with_target=False)
        assert "koji_tag is required when module_resolve_tags is True" in str(exc.value)

    @pytest.mark.parametrize(('with_modules'), (True, False))
    def test_request_compose_empty_packages(self, mocked_env, with_modules):
        repo_config = dedent("""\
            compose:
                packages:
            """)
        if with_modules:
            repo_config = dedent("""\
                compose:
                    packages:
                    modules:
                    - spam:stable
                    - bacon:stable
                    - eggs:stable
                """)
        mock_repo_config(mocked_env._tmpdir, repo_config)

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(source_type='tag',
                       source='test-tag',
                       sigkeys=['R123'],
                       packages=None,
                       arches=['x86_64'])
            .and_return(ODCS_COMPOSE))
        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(source_type='module',
                       source='spam:stable bacon:stable eggs:stable',
                       sigkeys=['R123'],
                       arches=['x86_64'])
            .and_return(ODCS_COMPOSE))

        mock_odcs_client_wait_for_compose()

        self.run_plugin_with_args(mocked_env)

    @pytest.mark.parametrize(('compose_arches', 'pulp_arches', 'multilib_arches',
                              'request_multilib'), [
        (['i686'], None, None, None),
        (['i686'], None, ['i686'], ['i686']),
        (['i686'], None, ['ppc64le'], None),
        (['i686'], None, ['s390x', 'i686', 'ppc64le'], ['i686']),
        (['i686', 'ppc64le'], None, ['s390x', 'i686', 'ppc64le'], ['i686', 'ppc64le']),
        (['i686'], ['ppc64le'], None, None),
        (['i686'], ['ppc64le'], ['i686'], ['i686']),
        # pcc64le is in the pulp list but not the compose list, so it's not built at all
        (['i686'], ['ppc64le'], ['ppc64le'], None),
        (['i686'], ['ppc64le'], ['s390x', 'i686', 'ppc64le'], ['i686']),
        (['i686', 'ppc64le'], ['ppc64le'], ['s390x', 'i686', 'ppc64le'],
         ['i686', 'ppc64le']),
    ])
    @pytest.mark.parametrize(('multilib_method', 'method_results'), [
        (["none"], ['none']),
        (["devel"], ['devel']),
        (["runtime"], ['runtime']),
        (["all"], ['all']),
        (["runtime", "devel"], ['devel', 'runtime']),
        (None, []),
    ])
    def test_multilib(self, mocked_env, compose_arches, pulp_arches, multilib_arches,
                      request_multilib, multilib_method, method_results):
        base_repos = ['spam', 'bacon', 'eggs']

        content_dict = {}
        for arch in pulp_arches or []:
            pulp_repos = []
            for repo in base_repos:
                pulp_repos.append('{repo}-{arch}-rpms'.format(repo=repo, arch=arch))
            content_dict[arch] = pulp_repos

        mock_content_sets_config(mocked_env._tmpdir, yaml.safe_dump(content_dict))

        repo_config = {
            'compose': {
                'packages': base_repos
            }
        }
        if multilib_arches:
            repo_config['compose']['multilib_arches'] = multilib_arches
        if multilib_method:
            repo_config['compose']['multilib_method'] = multilib_method
        if pulp_arches:
            repo_config['compose']['pulp_repos'] = True

        mock_repo_config(mocked_env._tmpdir, yaml.safe_dump(repo_config))
        mocked_env.set_check_platforms_result(set(compose_arches))

        mocked_env.reactor_config.conf['koji'] = {'hub_url': KOJI_HUB, 'root_url': '', 'auth': {}}

        # just confirm that render_requests is returning valid data, without the overhead of
        # mocking the compose results
        plugin = ResolveComposesPlugin(mocked_env.workflow, koji_target=KOJI_TARGET_NAME)
        plugin.read_configs()
        plugin.adjust_compose_config()
        composed_arches = set()
        composes = plugin.compose_config.render_requests()
        for compose_config in composes:
            composed_arches.update(compose_config['arches'])
            if request_multilib:
                if compose_config['source_type'] == 'tag':
                    assert sorted(compose_config['multilib_arches']) == sorted(request_multilib)
                    compose_methods = compose_config['multilib_method'] or []
                    assert sorted(compose_methods) == sorted(method_results)
                    continue
                else:
                    if compose_config['arches'][0] in request_multilib:
                        assert compose_config['multilib_arches'] == compose_config['arches']
                        compose_methods = compose_config['multilib_method'] or []
                        assert sorted(compose_methods) == sorted(method_results)
                        continue
            # fall through if multilib wasn't requested or if the pulp arch wasn't in
            # the multilib request
            assert 'multilib_arches' not in compose_config
            assert 'multilib_method' not in compose_config
        assert composed_arches == set(compose_arches)

    @pytest.mark.parametrize(('pulp_arches', 'arches', 'signing_intent', 'expected_intent'), (
        (None, None, 'unsigned', 'unsigned'),
        # For the next test, since arches is none, no compose is performed even though pulp_arches
        # has a value. Expected intent doesn't change when nothing is composed.
        (['x86_64'], None, 'release', 'release'),
        # pulp composes have the beta signing intent and downgrade the release intent to beta.
        (['x86_64'], ['x86_64'], 'release', 'beta'),
        (['x86_64', 'ppce64le'], ['x86_64', 'ppce64le'], 'release', 'beta'),
        (['x86_64', 'ppce64le'], ['x86_64'], 'release', 'beta'),
        (['x86_64', 'ppce64le', 'arm64'], ['x86_64', 'ppce64le', 'arm64'], 'beta', 'beta'),
        # pulp composes have the beta signing intent but the unsigned intent overrides that
        (['x86_64', 'ppce64le', 'arm64'], ['x86_64', 'ppce64le', 'arm64'], 'unsigned', 'unsigned'),
        # For the next test, since arches is none, no compose is performed even though pulp_arches
        # has a value. Expected intent doesn't change when nothing is composed.
        (['x86_64', 'ppce64le', 'arm64'], None, 'beta', 'beta'),
    ))
    @pytest.mark.parametrize(('flags', 'expected_flags'), [
        ({}, []),
        ({UNPUBLISHED_REPOS: False}, []),
        ({UNPUBLISHED_REPOS: True}, [UNPUBLISHED_REPOS])
    ])
    def test_request_pulp_and_multiarch(self, mocked_env, pulp_arches, arches, signing_intent,
                                        expected_intent, flags, expected_flags):
        content_set = ''
        pulp_composes = {}
        base_repos = ['spam', 'bacon', 'eggs']
        pulp_id = ODCS_COMPOSE_ID
        arches = arches or []

        for arch in pulp_arches or []:
            pulp_id += 1
            pulp_repos = []
            content_set += """\n    {0}:""".format(arch)
            for repo in base_repos:
                pulp_repo = '{repo}-{arch}-rpms'.format(repo=repo, arch=arch)
                pulp_repos.append(pulp_repo)
                content_set += """\n    - {0}""".format(pulp_repo)
            source = ' '.join(pulp_repos)

            if arch not in arches:
                continue

            pulp_compose = {
                'id': pulp_id,
                'result_repo': ODCS_COMPOSE_REPO,
                'result_repofile': ODCS_COMPOSE_REPO + '/pulp_compose-' + arch,
                'source': source,
                'source_type': 'pulp',
                'sigkeys': "B457",
                'state_name': 'done',
                'arches': arch,
                'time_to_expire': ODCS_COMPOSE_TIME_TO_EXPIRE.strftime(ODCS_DATETIME_FORMAT),
            }
            pulp_composes[arch] = pulp_compose
            if expected_flags:
                pulp_composes['flags'] = expected_flags

            (flexmock(ODCSClient)
                .should_receive('start_compose')
                .with_args(source_type='pulp', source=source, arches=[arch], sigkeys=[],
                           flags=expected_flags)
                .and_return(pulp_composes[arch]).once())
            (flexmock(ODCSClient)
                .should_receive('wait_for_compose')
                .with_args(pulp_id)
                .and_return(pulp_composes[arch]).once())

        mock_content_sets_config(mocked_env._tmpdir, content_set)

        repo_config = dedent("""\
            compose:
                pulp_repos: true
                packages:
                - spam
                - bacon
                - eggs
                signing_intent: {0}
            """.format(signing_intent))
        for flag in flags:
            repo_config += ("    {0}: {1}\n".format(flag, flags[flag]))
        mock_repo_config(mocked_env._tmpdir, repo_config)
        mocked_env.set_check_platforms_result(arches)
        tag_compose = deepcopy(ODCS_COMPOSE)

        sig_keys = SIGNING_INTENTS[signing_intent]
        tag_compose['sigkeys'] = ' '.join(sig_keys)
        if arches:
            tag_compose['arches'] = ' '.join(arches)
            (flexmock(ODCSClient)
                .should_receive('start_compose')
                .with_args(source_type='tag', source=KOJI_TAG_NAME, arches=sorted(arches),
                           packages=['spam', 'bacon', 'eggs'], sigkeys=sig_keys)
                .and_return(tag_compose).once())
        else:
            tag_compose.pop('arches')
            (flexmock(ODCSClient)
                .should_receive('start_compose')
                .with_args(source_type='tag', source=KOJI_TAG_NAME,
                           packages=['spam', 'bacon', 'eggs'], sigkeys=sig_keys)
                .and_return(tag_compose).once())

        (flexmock(ODCSClient)
            .should_receive('wait_for_compose')
            .with_args(ODCS_COMPOSE_ID)
            .and_return(tag_compose).once())

        plugin_result = self.run_plugin_with_args(mocked_env, platforms=arches, is_pulp=pulp_arches)

        assert plugin_result['signing_intent'] == expected_intent

    def test_invalid_flag(self, mocked_env):
        expect_error = "at top level: validating 'anyOf' has failed"
        arches = ['x86_64']
        repo_config = dedent("""\
            compose:
                pulp_repos: true
                packages:
                - spam
                - bacon
                - eggs
                signing_intent: unsigned
                some_invalid_flag: true
            """)
        mock_repo_config(mocked_env._tmpdir, repo_config)
        mocked_env.set_check_platforms_result(set(arches))
        with pytest.raises(PluginFailedException) as exc:
            self.run_plugin_with_args(mocked_env, platforms=arches, is_pulp=False)
        assert expect_error in str(exc.value)

    def test_request_compose_for_pulp_no_content_sets(self, mocked_env):
        mock_content_sets_config(mocked_env._tmpdir, '')

        repo_config = dedent("""\
            compose:
                pulp_repos: true
                packages:
                - spam
                - bacon
                - eggs
            """)
        mock_repo_config(mocked_env._tmpdir, repo_config)

        mock_odcs_client_start_compose()
        mock_odcs_client_wait_for_compose()

        self.run_plugin_with_args(mocked_env)

    def test_signing_intent_and_compose_ids_mutex(self, mocked_env):
        plugin_args = {'compose_ids': [1, 2], 'signing_intent': 'unsigned'}
        self.run_plugin_with_args(mocked_env, plugin_args,
                                  expect_error='cannot be used at the same time')

    @pytest.mark.parametrize(('plugin_args', 'expected_kwargs'), (
        (
            {'odcs_insecure': True},
            {'insecure': True, 'timeout': None}
        ),
        (
            {'odcs_insecure': False},
            {'insecure': False, 'timeout': None}
        ),
        (
            {'odcs_openidc_secret_path': True},
            {'token': 'the-token', 'insecure': False, 'timeout': None}
        ),
        (
            {'odcs_ssl_secret_path': True},
            {'cert': '<tbd-cert-path>', 'insecure': False, 'timeout': None}
        ),
        (
            {'odcs_ssl_secret_path': 'non-existent-path'},
            {'insecure': False, 'timeout': None}
        ),
    ))
    def test_odcs_session_creation(self, mocked_env, plugin_args, expected_kwargs):
        plug_args = deepcopy(plugin_args)
        exp_kwargs = deepcopy(expected_kwargs)
        mocked_env.set_reactor_config(make_reactor_config(mocked_env._tmpdir))

        if plug_args.get('odcs_openidc_secret_path') is True:
            mocked_env._tmpdir.joinpath('token').write_text('the-token', 'utf-8')
            plug_args['odcs_openidc_secret_path'] = str(mocked_env._tmpdir)

        if plug_args.get('odcs_ssl_secret_path') is True:
            mocked_env._tmpdir.joinpath('cert').write_text('the-cert', 'utf-8')
            plug_args['odcs_ssl_secret_path'] = str(mocked_env._tmpdir)
            exp_kwargs['cert'] = str(mocked_env._tmpdir.joinpath('cert'))

        exp_kwargs['insecure'] = False
        if 'token' in exp_kwargs:
            mocked_env.reactor_config.conf['odcs']['auth'].pop('ssl_certs_dir')
            mocked_env.reactor_config.conf['odcs']['auth']['openidc_dir'] = str(mocked_env._tmpdir)
        else:
            exp_kwargs['cert'] = os.path.join(
                mocked_env.reactor_config.conf['odcs']['auth']['ssl_certs_dir'], 'cert'
            )

        mock_odcs_client_start_compose()
        mock_odcs_client_wait_for_compose()

        (flexmock(ODCSClient)
            .should_receive('__init__')
            .with_args(ODCS_URL, **exp_kwargs))

        self.run_plugin_with_args(mocked_env, plug_args)

    @pytest.mark.parametrize(('plugin_args', 'ssl_login'), (
        ({
            'koji_target': KOJI_TARGET_NAME,
            'koji_hub': KOJI_BUILD_ID,
            'koji_ssl_certs_dir': '/path/to/certs',
        }, True),
        ({
            'koji_target': KOJI_TARGET_NAME,
            'koji_hub': KOJI_BUILD_ID,
        }, False),
    ))
    def test_koji_session_creation(self, mocked_env, plugin_args, ssl_login):
        koji_session = mocked_env._koji_session

        (flexmock(koji_session)
            .should_receive('ssl_login')
            .times(int(ssl_login))
            .and_return(True))

        (flexmock(koji_session)
            .should_receive('getBuildTarget')
            .once()
            .with_args(plugin_args['koji_target'], strict=True)
            .and_return(KOJI_TARGET))

        mock_odcs_client_start_compose()
        mock_odcs_client_wait_for_compose()

        self.run_plugin_with_args(mocked_env, plugin_args)

    @pytest.mark.parametrize(('default_si', 'config_si', 'arg_si', 'parent_si', 'expected_si',
                              'overridden'), (
        # Downgraded by parent's signing intent
        ('release', None, None, 'beta', 'beta', True),
        ('beta', None, None, 'unsigned', 'unsigned', True),
        ('release', 'release', None, 'beta', 'beta', True),
        ('release', 'beta', None, 'unsigned', 'unsigned', True),

        # Not upgraded by parent's signing intent
        ('release', 'beta', None, 'release', 'beta', False),
        ('release', 'beta', 'beta', 'release', 'beta', False),

        # Downgraded by signing_intent plugin argument
        ('release', 'release', 'beta', 'release', 'beta', True),
        ('release', 'release', 'beta', None, 'beta', True),

        # Upgraded by signing_intent plugin argument
        ('release', 'beta', 'release', 'release', 'release', True),
        ('release', 'beta', 'release', None, 'release', True),

        # Upgraded by signing_intent plugin argument but capped by parent's signing intent
        ('beta', 'beta', 'release', 'unsigned', 'unsigned', True),
        ('beta', 'beta', 'release', 'beta', 'beta', False),
        ('release', 'beta', 'beta', 'unsigned', 'unsigned', True),

        # Modified by repo config
        ('release', 'unsigned', None, None, 'unsigned', False),
        ('unsigned', 'release', None, None, 'release', False),

        # Environment default signing intent used as is
        ('release', None, None, None, 'release', False),
        ('beta', None, None, None, 'beta', False),
        ('unsigned', None, None, None, 'unsigned', False),

    ))
    @pytest.mark.parametrize('use_compose_id', (False, True))
    def test_adjust_signing_intent(self, mocked_env, default_si, config_si, arg_si,
                                   parent_si, expected_si, overridden, use_compose_id):

        mocked_env.set_reactor_config(
            make_reactor_config(mocked_env._tmpdir, default_si=default_si)
        )
        mock_repo_config(mocked_env._tmpdir, signing_intent=config_si)

        sigkeys = SIGNING_INTENTS[expected_si]
        odcs_compose = ODCS_COMPOSE.copy()
        odcs_compose['sigkeys'] = ' '.join(sigkeys)

        arg_compose_ids = []
        if use_compose_id and arg_si:
            # Swap out signing_intent plugin argument with compose_ids.
            # Set mocks to return pre-existing compose instead.
            arg_compose_ids = [ODCS_COMPOSE_ID]
            sigkeys = SIGNING_INTENTS[arg_si]
            odcs_compose['sigkeys'] = sigkeys
            arg_si = None

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .times(0 if arg_compose_ids else 1)
            .with_args(
                source_type='tag',
                source=KOJI_TAG_NAME,
                packages=['spam', 'bacon', 'eggs'],
                arches=ODCS_COMPOSE_DEFAULT_ARCH_LIST,
                sigkeys=sigkeys)
            .and_return(odcs_compose))

        (flexmock(ODCSClient)
            .should_receive('wait_for_compose')
            .once()
            .with_args(odcs_compose['id'])
            .and_return(odcs_compose))

        parent_build_info = {
            'id': 1234,
            'nvr': 'fedora-27-1',
            'extra': {'image': {}},
        }
        if parent_si:
            parent_build_info['extra']['image'] = {'odcs': {'signing_intent': parent_si}}

        mocked_env.set_plugin_result(
            "prebuild", PLUGIN_KOJI_PARENT_KEY, {BASE_IMAGE_KOJI_BUILD: parent_build_info}
        )

        plugin_args = {}
        if arg_si:
            plugin_args['signing_intent'] = arg_si
        if arg_compose_ids:
            plugin_args['compose_ids'] = arg_compose_ids

        plugin_result = self.run_plugin_with_args(mocked_env, plugin_args)
        yum_repourls = defaultdict(list)
        yum_repourls[ODCS_COMPOSE_DEFAULT_ARCH].append(ODCS_COMPOSE['result_repofile'])
        expected_result = {
            'include_koji_repo': False,
            'yum_repourls': yum_repourls,
            'signing_intent': expected_si,
            'signing_intent_overridden': overridden,
            'composes': [odcs_compose],
        }
        assert plugin_result == expected_result

    @pytest.mark.parametrize(('composes_intent', 'expected_intent'), (
        (('release', 'beta'), 'beta'),
        (('beta', 'release'), 'beta'),
        (('release', 'release'), 'release'),
        (('unsigned', 'release'), 'unsigned'),
    ))
    def test_signing_intent_multiple_composes(self, mocked_env, composes_intent, expected_intent):
        composes = []

        for compose_id, signing_intent in enumerate(composes_intent):
            compose = ODCS_COMPOSE.copy()
            compose['id'] = compose_id
            compose['sigkeys'] = ' '.join(SIGNING_INTENTS[signing_intent])

            (flexmock(ODCSClient)
                .should_receive('wait_for_compose')
                .once()
                .with_args(compose_id)
                .and_return(compose))

            composes.append(compose)

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .never())

        plugin_args = {'compose_ids': [item['id'] for item in composes]}
        plugin_result = self.run_plugin_with_args(mocked_env, plugin_args)

        assert plugin_result['signing_intent'] == expected_intent
        assert plugin_result['composes'] == composes

    @pytest.mark.parametrize(('config', 'error_message'), (
        (dedent("""\
            compose:
                modules: []
            """), 'Nothing to compose'),

        (dedent("""\
            compose:
                pulp_repos: true
            """), 'Nothing to compose'),
    ))
    def test_invalid_compose_request(self, mocked_env, config, error_message):
        mock_repo_config(mocked_env._tmpdir, config)
        self.run_plugin_with_args(mocked_env, expect_error=error_message)

    def test_empty_compose_request(self, caplog, mocked_env):
        config = dedent("""\
            compose:
            """)
        mock_repo_config(mocked_env._tmpdir, config)
        self.run_plugin_with_args(mocked_env)
        msg = 'Aborting plugin execution: "compose" config not set and compose_ids not given'
        assert msg in (x.message for x in caplog.records)

    def test_only_pulp_repos(self, mocked_env):
        mock_repo_config(mocked_env._tmpdir,
                         dedent("""\
                             compose:
                                 pulp_repos: true
                             """))
        mock_content_sets_config(mocked_env._tmpdir)

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .with_args(
                source_type='pulp',
                source='pulp-spam-rpms pulp-bacon-rpms pulp-eggs-rpms',
                sigkeys=[],
                flags=[],
                arches=['x86_64'])
            .and_return(ODCS_COMPOSE))

        mock_odcs_client_wait_for_compose()

        self.run_plugin_with_args(mocked_env)

    @pytest.mark.parametrize(('content_sets', 'build_only_content_sets'), (
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    ))
    def test_only_content_sets(self, mocked_env, content_sets, build_only_content_sets):
        main_cs_list = ['pulp-spam-rpms', 'pulp-bacon-rpms', 'pulp-eggs-rpms',
                        'pulp-bar-rpms__Server__x86_64']
        build_only_cs_list = ['dev-spam-rpms', 'dev-bacon-rpms', 'dev-eggs-rpms', 'pulp-spam-rpms']

        if content_sets:
            cs_json = {'x86_64': main_cs_list}
            mocked_env._tmpdir.joinpath('content_sets.yml').write_text(
                yaml.safe_dump(cs_json), "utf-8"
            )

        container_json = {'compose': {'pulp_repos': True}}
        if build_only_content_sets:
            container_json['compose']['build_only_content_sets'] = {'x86_64': build_only_cs_list}
        else:
            container_json['compose']['build_only_content_sets'] = None

        mocked_env._tmpdir.joinpath('container.yaml').write_text(
            yaml.safe_dump(container_json), "utf-8"
        )

        all_cs = []
        if content_sets:
            all_cs = main_cs_list
        if build_only_content_sets:
            all_cs = set(build_only_cs_list).union(all_cs)
        all_sources = ' '.join(all_cs)

        if content_sets or build_only_content_sets:
            (flexmock(ODCSClient)
                .should_receive('start_compose')
                .with_args(
                    source_type='pulp',
                    source=all_sources,
                    sigkeys=[],
                    flags=[],
                    arches=['x86_64'])
                .once()
                .and_return(ODCS_COMPOSE))
            mock_odcs_client_wait_for_compose()
            self.run_plugin_with_args(mocked_env)
        else:
            (flexmock(ODCSClient)
                .should_receive('start_compose')
                .never())

            self.run_plugin_with_args(mocked_env, expect_error='Nothing to compose')

    @pytest.mark.parametrize(('state_name', 'time_to_expire_delta', 'expect_renew'), (
        ('removed', timedelta(), True),
        ('removed', timedelta(hours=-2), True),
        ('done', timedelta(), True),
        # Grace period to avoid timing issues during test runs
        ('done', timedelta(minutes=118), True),
        ('done', timedelta(hours=3), False),
    ))
    @pytest.mark.parametrize('sigkeys, depkeys', (
        ('', ''),
        ('KEY1', ''),
        ('KEY1 KEY2', ''),
        ('KEY1 KEY2', 'KEY3'),
        ('', 'KEY3'),
    ))
    def test_renew_compose(self, mocked_env, state_name, time_to_expire_delta, expect_renew,
                           sigkeys, depkeys, source_dir, caplog):
        old_odcs_compose = ODCS_COMPOSE.copy()
        time_to_expire = (ODCS_COMPOSE_TIME_TO_EXPIRE -
                          ODCS_COMPOSE_SECONDS_TO_LIVE +
                          time_to_expire_delta)
        old_odcs_compose.update({
            'state_name': state_name,
            'time_to_expire': time_to_expire.strftime("%Y-%m-%dT%H:%M:%SZ"),
            'sigkeys': ' '.join([sigkeys, depkeys]),
        })

        new_odcs_compose = ODCS_COMPOSE.copy()
        new_odcs_compose.update({
            'id': old_odcs_compose['id'] + 1,
            'sigkeys': sigkeys,
        })

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .never())

        (flexmock(ODCSClient)
            .should_receive('wait_for_compose')
            .once()
            .with_args(old_odcs_compose['id'])
            .and_return(old_odcs_compose))

        (flexmock(ODCSClient)
            .should_receive('renew_compose')
            .times(1 if expect_renew else 0)
            .with_args(old_odcs_compose['id'], sigkeys.split())
            .and_return(new_odcs_compose))

        (flexmock(ODCSClient)
            .should_receive('wait_for_compose')
            .times(1 if expect_renew else 0)
            .with_args(new_odcs_compose['id'])
            .and_return(new_odcs_compose))

        plugin_args = {
            'compose_ids': [old_odcs_compose['id']],
            'minimum_time_to_expire': timedelta(hours=2).total_seconds(),
        }

        data = dedent("""\
            version: 1
            odcs:
               signing_intents:
               - name: release
                 keys: [{}]
                 deprecated_keys: [{}]
               - name: unsigned
                 keys: []
               default_signing_intent: release
               api_url: {}
               auth:
                   ssl_certs_dir: {}
            koji:
                hub_url: /
                root_url: ''
                auth: {{}}
            """.format(sigkeys.replace(' ', ','), depkeys.replace(' ', ','), ODCS_URL, source_dir))
        mocked_env.set_reactor_config(make_reactor_config(source_dir, data=data))

        plugin_result = self.run_plugin_with_args(mocked_env, plugin_args)

        if expect_renew:
            assert plugin_result['composes'] == [new_odcs_compose]
            if depkeys:
                assert 'Updating signing keys' in caplog.text
            else:
                assert 'Updating signing keys' not in caplog.text
        else:
            assert plugin_result['composes'] == [old_odcs_compose]
            assert 'Updating signing keys' not in caplog.text

    def test_inject_yum_repos_from_new_compose(self, mocked_env):
        mock_odcs_client_start_compose()
        mock_odcs_client_wait_for_compose()
        results = self.run_plugin_with_args(mocked_env)
        yum_repourls = results.get('yum_repourls') or {}
        expected_yum_repourls = defaultdict(list)
        expected_yum_repourls[ODCS_COMPOSE_DEFAULT_ARCH].append(ODCS_COMPOSE_REPOFILE)
        assert yum_repourls == expected_yum_repourls

    def test_inject_yum_repos_from_existing_composes(self, mocked_env):
        compose_ids = []
        expected_yum_repourls = defaultdict(list)

        for compose_id in range(3):
            compose = ODCS_COMPOSE.copy()
            compose['id'] = compose_id
            compose['result_repofile'] = ODCS_COMPOSE_REPO + '/odcs-{}.repo'.format(compose_id)

            (flexmock(ODCSClient)
                .should_receive('wait_for_compose')
                .once()
                .with_args(compose_id)
                .and_return(compose))

            compose_ids.append(compose_id)
            expected_yum_repourls[ODCS_COMPOSE_DEFAULT_ARCH].append(compose['result_repofile'])

        (flexmock(ODCSClient)
            .should_receive('start_compose')
            .never())

        plugin_args = {'compose_ids': compose_ids}
        results = self.run_plugin_with_args(mocked_env, plugin_args)
        yum_repourls = results.get('yum_repourls') or {}

        assert yum_repourls == expected_yum_repourls

    def test_abort_when_odcs_config_missing(self, caplog, mocked_env):
        # Clear out default reactor config
        mocked_env.set_reactor_config(make_reactor_config(mocked_env._tmpdir, data='version: 1'))
        with caplog.at_level(logging.INFO):
            self.run_plugin_with_args(mocked_env)

        msg = 'Aborting plugin execution: ODCS config not found'
        assert msg in (x.message for x in caplog.records)

    def test_abort_when_compose_config_missing(self, caplog, mocked_env):
        # Clear out default git repo config
        mock_repo_config(mocked_env._tmpdir, '')
        # Ensure no compose_ids are passed to plugin
        plugin_args = {'compose_ids': tuple()}
        with caplog.at_level(logging.INFO):
            self.run_plugin_with_args(mocked_env, plugin_args)

        msg = 'Aborting plugin execution: "compose" config not set and compose_ids not given'
        assert msg in (x.message for x in caplog.records)

    def test_invalid_koji_build_target(self, mocked_env):
        plugin_args = {
            'koji_target': 'spam',
        }
        expect_error = 'No matching build target found'
        self.run_plugin_with_args(mocked_env, plugin_args, expect_error=expect_error)

    def run_plugin_with_args(self, mocked_env, plugin_args=None,
                             expect_error=None, platforms=None,
                             is_pulp=None, with_target=True):
        if platforms is None:
            platforms = ODCS_COMPOSE_DEFAULT_ARCH_LIST
        plugin_args = plugin_args or {}
        plugin_args.setdefault('odcs_url', ODCS_URL)
        if with_target:
            plugin_args.setdefault('koji_target', KOJI_TARGET_NAME)
        plugin_args.setdefault('koji_hub', KOJI_HUB)

        workflow = mocked_env.workflow
        add_koji_map_in_workflow(workflow, root_url='',
                                 hub_url=plugin_args.get('koji_hub'),
                                 ssl_certs_dir=plugin_args.get('koji_ssl_certs_dir'))

        del(plugin_args['koji_hub'])
        if 'koji_ssl_certs_dir' in plugin_args:
            del(plugin_args['koji_ssl_certs_dir'])

        runner = mocked_env.set_plugin_args(plugin_args).create_runner()

        if expect_error:
            with pytest.raises(PluginFailedException) as exc_info:
                runner.run()
            if hasattr(expect_error, 'search'):  # py2/3 compat way of detecting compiled regexp
                assert expect_error.search(str(exc_info.value))
            else:
                assert expect_error in str(exc_info.value)
            return

        results = runner.run()[ResolveComposesPlugin.key]
        yum_repourls = results['yum_repourls']
        for platform in platforms:
            if is_pulp:
                pulp_repo = ODCS_COMPOSE_REPO + '/pulp_compose-' + platform
                assert pulp_repo in yum_repourls[platform]
        assert set(results.keys()) == {
            'signing_intent', 'signing_intent_overridden', 'composes',
            'yum_repourls', 'include_koji_repo'
        }

        return results

    @pytest.mark.parametrize('content_sets_content, expect_error', [
        ('', None),
        ('null', None),
        ('{}', None),
        ('x86_64: ["spam-rpms"]', None),

        ('"string"', 'is not of type {}'.format(', '.join([repr('object'), repr('null')]))),
        ('x86_64: "not an array"', 'is not of type {!r}'.format('array')),

        ('x86_64: []', '[] is too short'),
        ('x86_64: [1]', '1 is not of type {!r}'.format('string')),
        ('x86_64: ["spam"]', 'does not match'),
        ('x86_64: ["spam-rpms-spam"]', 'does not match'),

        # Does not start with lowercase letter
        ('"86_64": []', re.compile(
            # newer versions of jsonchema reports this differently
            r"((Additional properties are not allowed)|"
            r"(validating 'additionalProperties' has failed))")
         ),
    ])
    def test_content_sets_validation(self, mocked_env,
                                     content_sets_content, expect_error):
        mock_odcs_client_start_compose()
        mock_odcs_client_wait_for_compose()
        mock_content_sets_config(mocked_env._tmpdir, content_sets_content)
        self.run_plugin_with_args(mocked_env, expect_error=expect_error)

    @pytest.mark.parametrize('parent_repourls,modules,packages,content_sets,expect_include_repo', [
        (True, True, False, None, False),
        (False, True, False, None, True),
        (False, True, True, None, False),
        (True, True, True, None, False),
        (False, False, True, None, False),
        (False, True, False, '{}', True),
        (True, True, True, '{}', False),
        (False, False, False, 'x86_64: ["spam-rpms"]', False),
        (True, True, True, 'x86_64: ["spam-rpms"]', False),
    ])
    def test_include_koji_repo(self, mocked_env, parent_repourls, modules,
                               packages, content_sets, expect_include_repo):

        mock_koji_parent(mocked_env, parent_repo="http://example.com/parent.repo")

        repo_config = {
            'compose': {
            }
        }

        if parent_repourls:
            repo_config['compose']['inherit'] = True
        if modules:
            repo_config['compose']['modules'] = ['mymodule:stable']
        if packages:
            repo_config['compose']['packages'] = ['bash']
        if content_sets is not None:
            repo_config['compose']['pulp_repos'] = True

        mock_repo_config(mocked_env._tmpdir, yaml.safe_dump(repo_config))
        if content_sets:
            mock_content_sets_config(mocked_env._tmpdir, content_sets)

        compose_module_id = 80
        compose_package_id = 90
        compose_pulp_id = 100
        custom_module_compose = deepcopy(ODCS_COMPOSE)
        custom_module_compose['source_type'] = 2  # PungiSourceType.MODULE
        custom_module_compose['id'] = compose_module_id
        custom_package_compose = deepcopy(ODCS_COMPOSE)
        custom_package_compose['source_type'] = 1
        custom_package_compose['id'] = compose_package_id
        custom_pulp_compose = deepcopy(ODCS_COMPOSE)
        custom_pulp_compose['source_type'] = 4
        custom_pulp_compose['id'] = compose_pulp_id

        start_chain = flexmock(ODCSClient).should_receive('start_compose')
        if packages:
            start_chain.and_return(custom_package_compose)
        if modules:
            start_chain.and_return(custom_module_compose)
        if content_sets:
            start_chain.and_return(custom_pulp_compose)

        if modules:
            (flexmock(ODCSClient)
                .should_receive('wait_for_compose')
                .with_args(compose_module_id)
                .and_return(custom_module_compose))
        if packages:
            (flexmock(ODCSClient)
                .should_receive('wait_for_compose')
                .with_args(compose_package_id)
                .and_return(custom_package_compose))
        if content_sets:
            (flexmock(ODCSClient)
                .should_receive('wait_for_compose')
                .with_args(compose_pulp_id)
                .and_return(custom_pulp_compose))

        results = self.run_plugin_with_args(mocked_env)

        yum_repourls = results.get('yum_repourls') or {}
        assert yum_repourls
        include_koji_repo = results.get('include_koji_repo')
        assert include_koji_repo == expect_include_repo

    def test_skip_adjust_composes_for_inheritance_if_image_is_based_on_scratch(
            self, mocked_env, caplog):
        plugin = ResolveComposesPlugin(mocked_env.workflow)
        mocked_env.set_dockerfile_images(["scratch"])
        plugin.adjust_for_inherit()
        assert ('This is a base image based on scratch. '
                'Skipping adjusting composes for inheritance.' in caplog.text)

    def test_skip_adjust_signing_intent_from_parent_if_image_is_based_on_scratch(
            self, mocked_env, caplog):
        plugin = ResolveComposesPlugin(mocked_env.workflow)
        mocked_env.set_dockerfile_images(["scratch"])
        plugin.adjust_signing_intent_from_parent()
        assert ('This is a base image based on scratch. '
                'Signing intent will not be adjusted for it.' in caplog.text)

    @pytest.mark.parametrize('cancel_compose', (True, False))
    @responses.activate
    def test_canceling_compose_when_timeout_of_waiting_for_the_compose(
        self, mocked_env, tmpdir, cancel_compose, caplog
    ):
        repo_config = dedent("""\
                        compose:
                            inherit: true
                        """)
        mock_repo_config(mocked_env._tmpdir, repo_config)
        parent_compose_ids = [10, 11]
        mock_koji_parent(mocked_env,
                         parent_compose_ids=parent_compose_ids,
                         parent_repo=None,
                         scratch=False, isolated=False)
        for parent_compose_id in parent_compose_ids:
            compose = ODCS_COMPOSE.copy()
            compose['id'] = parent_compose_id
            compose['result_repofile'] = ODCS_COMPOSE_REPO + '/odcs-{}.repo'.format(
                parent_compose_id)

            (flexmock(ODCSClient)
             .should_receive('wait_for_compose')
             .once()
             .with_args(parent_compose_id)
             .and_return(compose))
            # Ensure ODCS responses the compose is still waiting for process before
            # checking the timeout.
            parent_url = construct_compose_url(ODCS_URL, parent_compose_id)
            if cancel_compose:
                renew_compose = compose.copy()
                compose['state_name'] = 'removed'
                renew_compose['id'] += 5
                renew_parent_url = construct_compose_url(ODCS_URL, renew_compose['id'])
                (flexmock(ODCSClient)
                 .should_receive('renew_compose')
                 .once()
                 .with_args(compose['id'], [])
                 .and_return(renew_compose))
                (flexmock(ODCSClient)
                 .should_receive('wait_for_compose')
                 .once()
                 .with_args(renew_compose['id'])
                 .and_return(renew_compose))
                if renew_compose['id'] == 15:
                    responses.add(responses.GET, url=renew_parent_url, json={
                        'id': renew_compose['id'],
                        'state_name': 'wait'
                    })
                else:
                    responses.add(responses.GET, url=renew_parent_url, json={
                        'id': renew_compose['id'],
                        'state_name': 'done'
                    })
                responses.add(responses.DELETE, url=renew_parent_url)
            else:
                responses.add(responses.GET, url=parent_url, json={
                    'id': parent_compose_id,
                    'state_name': 'done'
                })
            # Ensure to cancel the compose
            responses.add(responses.DELETE, url=parent_url)
        # Fake data for an existing compose requested from ODCS.
        # No need to start a new one.
        plugin_args = {'compose_ids': [ODCS_COMPOSE_ID]}

        # Ensure ODCSClient.wait_for_compose raises timeout error
        (flexmock(ODCSClient)
         .should_receive('wait_for_compose')
         .with_args(ODCS_COMPOSE_ID)
         .once()
         .and_raise(WaitComposeToFinishTimeout(ODCS_COMPOSE_ID, ODCSClient.DEFAULT_WAIT_TIMEOUT)))

        # Ensure ODCS responses the compose is still waiting for process before
        # checking the timeout.
        compose_url = construct_compose_url(ODCS_URL, ODCS_COMPOSE_ID)
        responses.add(responses.GET, url=compose_url, json={
            'id': ODCS_COMPOSE_ID,
            'state_name': 'wait'
        })
        # Ensure to cancel the compose
        responses.add(responses.DELETE, url=compose_url)

        with pytest.raises(PluginFailedException) as exc:
            self.run_plugin_with_args(mocked_env, plugin_args=plugin_args)

        msg = 'Timeout of waiting for compose {}'.format(ODCS_COMPOSE_ID)
        assert msg in str(exc.value)
        if cancel_compose:
            msg = 'Canceling the compose 15'
            assert msg in caplog.text
            msg = 'The compose 16 is not in progress, skip canceling'
            assert msg in caplog.text
