"""maintains all functionality related running virtual machines, starting and tracking tests."""

import datetime
import hashlib
import json
import os
import shutil
import sys
from multiprocessing import Process
from typing import Any

import requests
from flask import (Blueprint, abort, flash, g, jsonify, redirect, request,
                   url_for)
from git import GitCommandError, InvalidGitRepositoryError, Repo
from github import ApiError, GitHub
from lxml import etree
from lxml.etree import Element
from markdown2 import markdown
from pymysql.err import IntegrityError
from sqlalchemy import and_, func, or_
from sqlalchemy.sql import label
from sqlalchemy.sql.functions import count
from werkzeug.utils import secure_filename

from decorators import get_menu_entries, template_renderer
from mailer import Mailer
from mod_auth.controllers import check_access_rights, login_required
from mod_auth.models import Role
from mod_ci.forms import AddUsersToBlacklist, RemoveUsersFromBlacklist
from mod_ci.models import BlockedUsers, Kvm, MaintenanceMode
from mod_customized.models import CustomizedTest
from mod_deploy.controllers import is_valid_signature, request_from_github
from mod_home.models import CCExtractorVersion, GeneralData
from mod_regression.models import (Category, RegressionTest,
                                   RegressionTestOutput,
                                   regressionTestLinkTable)
from mod_sample.models import Issue
from mod_test.models import (Fork, Test, TestPlatform, TestProgress,
                             TestResult, TestResultFile, TestStatus, TestType)

if sys.platform.startswith("linux"):
    import libvirt

mod_ci = Blueprint('ci', __name__)


class Status:
    """Define different states for the tests."""

    PENDING = "pending"
    SUCCESS = "success"
    ERROR = "error"
    FAILURE = "failure"


@mod_ci.before_app_request
def before_app_request() -> None:
    """Organize menu content such as Platform management before request."""
    config_entries = get_menu_entries(
        g.user, 'Platform mgmt', 'cog', [], '', [
            {'title': 'Maintenance', 'icon': 'wrench',
             'route': 'ci.show_maintenance', 'access': [Role.admin]},  # type: ignore
            {'title': 'Blocked Users', 'icon': 'ban',
             'route': 'ci.blocked_users', 'access': [Role.admin]}  # type: ignore
        ]
    )
    if 'config' in g.menu_entries and 'entries' in config_entries:
        g.menu_entries['config']['entries'] = config_entries['entries'] + g.menu_entries['config']['entries']
    else:
        g.menu_entries['config'] = config_entries


def start_platforms(db, repository, delay=None, platform=None) -> None:
    """
    Start new test on both platforms in parallel.

    We use multiprocessing module which bypasses Python GIL to make use of multiple cores of the processor.
    """
    from run import config, log, app

    with app.app_context():
        from flask import current_app
        if platform is None or platform == TestPlatform.linux:
            linux_kvm_name = config.get('KVM_LINUX_NAME', '')
            log.info('Define process to run Linux VM')
            linux_process = Process(target=kvm_processor, args=(current_app._get_current_object(), db, linux_kvm_name,
                                                                TestPlatform.linux, repository, delay,))
            linux_process.start()
            log.info('Linux VM process kicked off')

        if platform is None or platform == TestPlatform.windows:
            win_kvm_name = config.get('KVM_WINDOWS_NAME', '')
            log.info('Define process to run Windows VM')
            windows_process = Process(target=kvm_processor, args=(current_app._get_current_object(), db, win_kvm_name,
                                                                  TestPlatform.windows, repository, delay,))
            windows_process.start()
            log.info('Windows VM process kicked off')


def kvm_processor(app, db, kvm_name, platform, repository, delay) -> None:
    """
    Check whether there is no already running same kvm.

    Checks whether machine is in maintenance mode or not
    Launch kvm if not used by any other test
    Creates testing xml files to test the change in main repo.
    Creates clone with separate branch and merge pr into it.

    :param app: The Flask app
    :type app: Flask
    :param db: database connection
    :type db: sqlalchemy.orm.scoped_session
    :param kvm_name: name for the kvm
    :type kvm_name: str
    :param platform: operating system
    :type platform: str
    :param repository: repository to run tests on
    :type repository: str
    :param delay: time delay after which to start kvm processor
    :type delay: int
    """
    from run import config, log, get_github_config

    github_config = get_github_config(config)

    log.info(f"[{platform}] Running kvm_processor")
    if kvm_name == "":
        log.critical(f'[{platform}] KVM name is empty!')
        return

    if delay is not None:
        import time
        log.debug(f'[{platform}] Sleeping for {delay} seconds')
        time.sleep(delay)

    maintenance_mode = MaintenanceMode.query.filter(MaintenanceMode.platform == platform).first()
    if maintenance_mode is not None and maintenance_mode.disabled:
        log.debug(f'[{platform}] In maintenance mode! Waiting...')
        return

    conn = libvirt.open("qemu:///system")
    if conn is None:
        log.critical(f"[{platform}] Connection to libvirt failed!")
        return

    try:
        vm = conn.lookupByName(kvm_name)
    except libvirt.libvirtError:
        log.critical(f"[{platform}] No VM named {kvm_name} found!")
        return

    vm_info = vm.info()
    if vm_info[0] != libvirt.VIR_DOMAIN_SHUTOFF:
        # Running, check expiry and compare to runtime
        status = Kvm.query.filter(Kvm.name == kvm_name).first()
        max_runtime = config.get("KVM_MAX_RUNTIME", 120)
        if status is not None:
            if datetime.datetime.now() - status.timestamp >= datetime.timedelta(minutes=max_runtime):
                test_progress = TestProgress(status.test.id, TestStatus.canceled, 'Runtime exceeded')
                db.add(test_progress)
                db.delete(status)
                db.commit()

                if vm.destroy() == -1:
                    log.critical(f"[{platform}] Failed to shut down {kvm_name}")
                    return
            else:
                log.info(f"[{platform}] Current job not expired yet.")
                return
        else:
            log.warn(f"[{platform}] No task, but VM is running! Hard reset necessary")
            if vm.destroy() == -1:
                log.critical(f"[{platform}] Failed to shut down {kvm_name}")
                return

    # Check if there's no KVM status left
    status = Kvm.query.filter(Kvm.name == kvm_name).first()
    if status is not None:
        log.warn(f"[{platform}] KVM is powered off, but test {status.test.id} still present, deleting entry")
        db.delete(status)
        db.commit()

    # Get oldest test for this platform
    finished_tests = db.query(TestProgress.test_id).filter(
        TestProgress.status.in_([TestStatus.canceled, TestStatus.completed])
    ).subquery()
    fork_location = f"%/{github_config['repository_owner']}/{github_config['repository']}.git"
    fork = Fork.query.filter(Fork.github.like(fork_location)).first()
    test = Test.query.filter(
        Test.id.notin_(finished_tests), Test.platform == platform, Test.fork_id == fork.id
    ).order_by(Test.id.asc()).first()

    if test is None:
        test = Test.query.filter(Test.id.notin_(finished_tests), Test.platform == platform).order_by(
            Test.id.asc()).first()

    if test is None:
        log.info(f'[{platform}] No more tests to run, returning')
        return

    if test.test_type == TestType.pull_request and test.pr_nr == 0:
        log.warn(f'[{platform}] Test {test.id} is invalid, deleting')
        db.delete(test)
        db.commit()
        return

    # Reset to snapshot
    if vm.hasCurrentSnapshot() != 1:
        log.critical(f"[{platform}] VM {kvm_name} has no current snapshot set!")
        return

    snapshot = vm.snapshotCurrent()
    if vm.revertToSnapshot(snapshot) == -1:
        log.critical(f"[{platform}] Failed to revert to {snapshot.getName()} for {kvm_name}")
        return

    log.info(f"[{platform}] Reverted to {snapshot.getName()} for {kvm_name}")
    log.debug(f'[{platform}] Starting test {test.id}')
    status = Kvm(kvm_name, test.id)
    # Prepare data
    # 0) Write url to file
    with app.app_context():
        full_url = url_for('ci.progress_reporter', test_id=test.id, token=test.token, _external=True, _scheme="https")

    file_path = os.path.join(config.get('SAMPLE_REPOSITORY', ''), 'vm_data', kvm_name, 'reportURL')

    with open(file_path, 'w') as f:
        f.write(full_url)

    # 1) Generate test files
    base_folder = os.path.join(config.get('SAMPLE_REPOSITORY', ''), 'vm_data', kvm_name, 'ci-tests')
    categories = Category.query.order_by(Category.id.desc()).all()
    commit_name = 'fetch_commit_' + platform.value
    commit_hash = GeneralData.query.filter(GeneralData.key == commit_name).first().value
    last_commit = Test.query.filter(and_(Test.commit == commit_hash, Test.platform == platform)).first()

    log.debug(f"[{platform}] We will compare against the results of test {last_commit.id}")

    regression_ids = test.get_customized_regressiontests()

    # Init collection file
    multi_test = etree.Element('multitest')
    for category in categories:
        # Skip categories without tests
        if len(category.regression_tests) == 0:
            continue
        # Create XML file for test
        file_name = '{name}.xml'.format(name=category.name)
        single_test = etree.Element('tests')
        should_write_xml = False
        for regression_test in category.regression_tests:
            if regression_test.id not in regression_ids:
                log.debug(f'Skipping RT #{regression_test.id} ({category.name}) as not in scope')
                continue
            should_write_xml = True
            entry = etree.SubElement(single_test, 'entry', id=str(regression_test.id))
            command = etree.SubElement(entry, 'command')
            command.text = regression_test.command
            input_node = etree.SubElement(entry, 'input', type=regression_test.input_type.value)
            # Need a path that is relative to the folder we provide inside the CI environment.
            input_node.text = regression_test.sample.filename
            output_node = etree.SubElement(entry, 'output')
            output_node.text = regression_test.output_type.value
            compare = etree.SubElement(entry, 'compare')
            last_files = TestResultFile.query.filter(and_(
                TestResultFile.test_id == last_commit.id,
                TestResultFile.regression_test_id == regression_test.id
            )).subquery()

            for output_file in regression_test.output_files:
                ignore_file = str(output_file.ignore).lower()
                file_node = etree.SubElement(compare, 'file', ignore=ignore_file, id=str(output_file.id))
                last_commit_files = db.query(last_files.c.got).filter(and_(
                    last_files.c.regression_test_output_id == output_file.id,
                    last_files.c.got.isnot(None)
                )).first()
                correct = etree.SubElement(file_node, 'correct')
                # Need a path that is relative to the folder we provide inside the CI environment.
                if last_commit_files is None:
                    log.debug(f"Selecting original file for RT #{regression_test.id} ({category.name})")
                    correct.text = output_file.filename_correct
                else:
                    correct.text = output_file.create_correct_filename(last_commit_files[0])

                expected = etree.SubElement(file_node, 'expected')
                expected.text = output_file.filename_expected(regression_test.sample.sha)
        if not should_write_xml:
            continue
        save_xml_to_file(single_test, base_folder, file_name)
        # Append to collection file
        test_file = etree.SubElement(multi_test, 'testfile')
        location = etree.SubElement(test_file, 'location')
        location.text = file_name

    save_xml_to_file(multi_test, base_folder, 'TestAll.xml')

    # 2) Create git repo clone and merge PR into it (if necessary)
    try:
        repo = Repo(os.path.join(config.get('SAMPLE_REPOSITORY', ''), 'vm_data', kvm_name, 'unsafe-ccextractor'))
    except InvalidGitRepositoryError:
        log.critical(f"[{platform}] Could not open CCExtractor's repository copy!")
        return

    # Return to master
    repo.heads.master.checkout(True)
    # Update repository from upstream
    try:
        github_url = test.fork.github
        if is_main_repo(github_url):
            origin = repo.remote('origin')
        else:
            fork_id = test.fork.id
            remote = f'fork_{fork_id}'
            if remote in [remote.name for remote in repo.remotes]:
                origin = repo.remote(remote)
            else:
                origin = repo.create_remote(remote, url=github_url)
    except ValueError:
        log.critical(f"[{platform}] Origin remote doesn't exist!")
        return

    fetch_info = origin.fetch()
    if len(fetch_info) == 0:
        log.info(f'[{platform}] Fetch from remote returned no new data...')
    # Checkout to Remote Master
    repo.git.checkout(origin.refs.master)
    # Pull code (finally)
    pull_info = origin.pull('master')
    if len(pull_info) == 0:
        log.info(f"[{platform}] Pull from remote returned no new data...")
    elif pull_info[0].flags > 128:
        log.critical(f"[{platform}] Did not pull any information from remote: {pull_info[0].flags}!")
        return

    ci_branch = 'CI_Branch'
    # Delete the test branch if it exists, and recreate
    try:
        repo.delete_head(ci_branch, force=True)
    except GitCommandError:
        log.info(f"[{platform}] Could not delete CI_Branch head")

    # Remove possible left rebase-apply directory
    try:
        shutil.rmtree(os.path.join(config.get('SAMPLE_REPOSITORY', ''), 'unsafe-ccextractor', '.git', 'rebase-apply'))
    except OSError:
        log.info(f"[{platform}] Could not delete rebase-apply")
    # If PR, merge, otherwise reset to commit
    if test.test_type == TestType.pull_request:
        # Fetch PR (stored under origin/pull/<id>/head)
        pull_info = origin.fetch(f'pull/{test.pr_nr}/head:{ci_branch}')
        if len(pull_info) == 0:
            log.warn(f"[{platform}] Did not pull any information from remote PR!")
        elif pull_info[0].flags > 128:
            log.critical(f"[{platform}] Did not pull any information from remote PR: {pull_info[0].flags}!")
            return

        try:
            test_branch = repo.heads[ci_branch]
        except IndexError:
            log.critical(f'{ci_branch} does not exist')
            return

        test_branch.checkout(True)

        try:
            pull = repository.pulls(f'{test.pr_nr}').get()
        except ApiError as a:
            log.error(f'Got an exception while fetching the PR payload! Message: {a.message}')
            return
        if pull['mergeable'] is False:
            progress = TestProgress(test.id, TestStatus.canceled, "Commit could not be merged", datetime.datetime.now())
            db.add(progress)
            db.commit()
            try:
                with app.app_context():
                    repository.statuses(test.commit).post(
                        state=Status.FAILURE,
                        description="Tests canceled due to merge conflict",
                        context=f"CI - {test.platform.value}",
                        target_url=url_for('test.by_id', test_id=test.id, _external=True)
                    )
            except ApiError as a:
                log.error(f'Got an exception while posting to GitHub! Message: {a.message}')
            return

        # Merge on master if no conflict
        repo.git.merge('master')

    else:
        test_branch = repo.create_head(ci_branch, origin.refs.master)
        test_branch.checkout(True)
        try:
            repo.head.reset(test.commit, working_tree=True)
        except GitCommandError:
            log.warn(f"[{platform}] Commit {test.commit} for test {test.id} does not exist!")
            return

    # Power on machine
    try:
        vm.create()
        db.add(status)
        db.commit()
    except libvirt.libvirtError as e:
        log.critical(f"[{platform}] Failed to launch VM {kvm_name}")
        log.critical(f"Information about failure: code: {e.get_error_code()}, domain: {e.get_error_domain()}, "
                     f"level: {e.get_error_level()}, message: {e.get_error_message()}")
    except IntegrityError:
        log.warn(f"[{platform}] Duplicate entry for {test.id}")

    # Close connection to libvirt
    conn.close()


def save_xml_to_file(xml_node, folder_name, file_name) -> None:
    """
    Save the given XML node to a file in a certain folder.

    :param xml_node: The XML content element to write to the file.
    :type xml_node: Element
    :param folder_name: The folder name.
    :type folder_name: str
    :param file_name: The name of the file
    :type file_name: str
    :return: Nothing
    :rtype: None
    """
    xml_node.getroottree().write(
        os.path.join(folder_name, file_name), encoding='utf-8', xml_declaration=True, pretty_print=True
    )


def queue_test(db, gh_commit, commit, test_type, branch="master", pr_nr=0) -> None:
    """
    Store test details into Test model for each platform, and post the status to GitHub.

    :param db: Database connection.
    :type db: sqlalchemy.orm.scoped_session
    :param gh_commit: The GitHub API call for the commit. Can be None
    :type gh_commit: Any
    :param commit: The commit hash.
    :type commit: str
    :param test_type: The type of test
    :type test_type: TestType
    :param branch: Branch name
    :type branch: str
    :param pr_nr: Pull Request number, if applicable.
    :type pr_nr: int
    :return: Nothing
    :rtype: None
    """
    from run import log

    fork_url = f"%/{g.github['repository_owner']}/{g.github['repository']}.git"
    fork = Fork.query.filter(Fork.github.like(fork_url)).first()

    if test_type == TestType.pull_request:
        log.debug('pull request test type detected')
        branch = "pull_request"

    linux_test = Test(TestPlatform.linux, test_type, fork.id, branch, commit, pr_nr)
    db.add(linux_test)
    windows_test = Test(TestPlatform.windows, test_type, fork.id, branch, commit, pr_nr)
    db.add(windows_test)
    db.commit()
    add_customized_regression_tests(linux_test.id)
    add_customized_regression_tests(windows_test.id)

    if gh_commit is not None:
        status_entries = {
            linux_test.platform.value: linux_test.id,
            windows_test.platform.value: windows_test.id
        }
        for platform_name, test_id in status_entries.items():
            try:
                gh_commit.post(
                    state=Status.PENDING,
                    description="Tests queued",
                    context=f"CI - {platform_name}",
                    target_url=url_for('test.by_id', test_id=test_id, _external=True)
                )
            except ApiError as a:
                log.critical(f'Could not post to GitHub! Response: {a.response}')

    log.debug("Created tests, waiting for cron...")


def inform_mailing_list(mailer, id, title, author, body) -> None:
    """
    Send mail to subscribed users when a issue is opened via the Webhook.

    :param mailer: The mailer instance
    :type mailer: Mailer
    :param id: ID of the Issue Opened
    :type id: int
    :param title: Title of the Created Issue
    :type title: str
    :param author: The Authors Username of the Issue
    :type author: str
    :param body: The Content of the Issue
    :type body: str
    """
    from run import get_github_issue_link

    subject = f"GitHub Issue #{id}"
    url = get_github_issue_link(id)
    if not mailer.send_simple_message({
        "to": "ccextractor-dev@googlegroups.com",
        "subject": subject,
        "html": get_html_issue_body(title=title, author=author, body=body, issue_number=id, url=url)
    }):
        g.log.error('failed to send issue to mailing list')


def get_html_issue_body(title, author, body, issue_number, url) -> Any:
    """
    Curate a HTML formatted body for the issue mail.

    :param title: title of the issue
    :type title: str
    :param author: author of the issue
    :type author: str
    :param body: content of the issue
    :type body: str
    :param issue_number: issue number
    :type issue_number: int
    :param url: link to the issue
    :type url: str
    :return: email body in html format
    :rtype: str
    """
    from run import app

    html_issue_body = markdown(body, extras=["target-blank-links", "task_list", "code-friendly"])
    template = app.jinja_env.get_or_select_template("email/new_issue.txt")
    html_email_body = template.render(title=title, author=author, body=html_issue_body, url=url)
    return html_email_body


@mod_ci.route('/start-ci', methods=['GET', 'POST'])
@request_from_github()
def start_ci():
    """
    Perform various actions when the GitHub webhook is triggered.

    Reaction to the next events need to be processed

    (after verification):
        - Ping (for fun)
        - Push
        - Pull Request
        - Issues
    """
    if request.method != 'POST':
        return 'OK'
    else:
        abort_code = 418

        event = request.headers.get('X-GitHub-Event')
        if event == "ping":
            g.log.debug('server ping successful')
            return json.dumps({'msg': 'Hi!'})

        x_hub_signature = request.headers.get('X-Hub-Signature')

        if not is_valid_signature(x_hub_signature, request.data, g.github['ci_key']):
            g.log.warning(f'CI signature failed: {x_hub_signature}')
            abort(abort_code)

        payload = request.get_json()

        if payload is None:
            g.log.warning(f'CI payload is empty')
            abort(abort_code)

        gh = GitHub(access_token=g.github['bot_token'])
        repository = gh.repos(g.github['repository_owner'])(g.github['repository'])

        if event == "push":
            g.log.debug('push event detected')
            if 'after' in payload:
                commit_hash = payload['after']
                github_status = repository.statuses(commit_hash)
                # Update the db to the new last commit
                ref = repository.git().refs('heads/master').get()
                last_commit = GeneralData.query.filter(GeneralData.key == 'last_commit').first()
                for platform in TestPlatform.values():
                    commit_name = 'fetch_commit_' + platform
                    fetch_commit = GeneralData.query.filter(GeneralData.key == commit_name).first()

                    if fetch_commit is None:
                        prev_commit = GeneralData(commit_name, last_commit.value)
                        g.db.add(prev_commit)

                last_commit.value = ref['object']['sha']
                g.db.commit()
                queue_test(g.db, github_status, commit_hash, TestType.commit)
            else:
                g.log.warning('Unknown push type! Dumping payload for analysis')
                g.log.warning(payload)

        elif event == "pull_request":
            g.log.debug('Pull Request event detected')
            # If it's a valid PR, run the tests
            pr_nr = payload['pull_request']['number']
            if payload['action'] in ['opened', 'synchronize', 'reopened']:
                try:
                    commit_hash = payload['pull_request']['head']['sha']
                    github_status = repository.statuses(commit_hash)
                except KeyError:
                    g.log.error("Didn't find a SHA value for a newly opened PR!")
                    g.log.error(payload)
                    return 'ERROR'

                # Check if user blacklisted
                user_id = payload['pull_request']['user']['id']
                if BlockedUsers.query.filter(BlockedUsers.user_id == user_id).first() is not None:
                    g.log.warning("User Blacklisted")
                    github_status.post(
                        state=Status.ERROR,
                        description="CI start aborted. You may be blocked from accessing this functionality",
                        target_url=url_for('home.index', _external=True)
                    )
                    return 'ERROR'

                queue_test(g.db, github_status, commit_hash, TestType.pull_request, pr_nr=pr_nr)

            elif payload['action'] == 'closed':
                g.log.debug('PR was closed, no after hash available')
                # Cancel running queue
                tests = Test.query.filter(Test.pr_nr == pr_nr).all()
                for test in tests:
                    # Add cancelled status only if the test hasn't started yet
                    if len(test.progress) > 0:
                        continue
                    progress = TestProgress(test.id, TestStatus.canceled, "PR closed", datetime.datetime.now())
                    g.db.add(progress)
                    repository.statuses(test.commit).post(
                        state=Status.FAILURE,
                        description="Tests canceled",
                        context=f"CI - {test.platform.value}",
                        target_url=url_for('test.by_id', test_id=test.id, _external=True)
                    )

        elif event == "issues":
            g.log.debug('issues event detected')

            issue_data = payload['issue']
            issue_action = payload['action']
            issue = Issue.query.filter(Issue.issue_id == issue_data['number']).first()
            issue_title = issue_data['title']
            issue_id = issue_data['number']
            issue_author = issue_data['user']['login']
            issue_body = issue_data['body']

            if issue_action == "opened":
                inform_mailing_list(g.mailer, issue_id, issue_title, issue_author, issue_body)

            if issue is not None:
                issue.title = issue_title
                issue.status = issue_data['state']
                g.db.commit()

        elif event == "release":
            g.log.debug("Release webhook triggered")

            release_data = payload['release']
            action = payload['action']
            release_version = release_data['tag_name']
            if release_version[0] == 'v':
                release_version = release_version[1:]
            if action == "prereleased":
                g.log.debug("Ignoring event meant for pre-release")
            elif action in ["deleted", "unpublished"]:
                g.log.debug("Received delete/unpublished action")
                CCExtractorVersion.query.filter_by(version=release_version).delete()
                g.db.commit()
                g.log.info(f"Successfully deleted release {release_version} on {action} action")
            elif action in ["edited", "published"]:
                g.log.debug(f"Latest release version is {release_version}")
                release_commit = GeneralData.query.filter(GeneralData.key == 'last_commit').first().value
                release_date = release_data['published_at']
                if action == "edited":
                    release = CCExtractorVersion.query.filter(CCExtractorVersion.version == release_version).one()
                    release.released = datetime.datetime.strptime(release_date, '%Y-%m-%dT%H:%M:%SZ').date()
                    release.commit = release_commit
                else:
                    release = CCExtractorVersion(release_version, release_date, release_commit)
                    g.db.add(release)
                g.db.commit()
                g.log.info(f"Successfully updated release version with webhook action '{action}'")
                # adding test corresponding to last commit to the baseline regression results
                # this is not altered when a release is deleted or unpublished since it's based on commit
                test = Test.query.filter(and_(Test.commit == release_commit,
                                         Test.platform == TestPlatform.linux)).first()
                test_result_file = g.db.query(TestResultFile).filter(TestResultFile.test_id == test.id).subquery()
                test_result = g.db.query(TestResult).filter(TestResult.test_id == test.id).subquery()
                g.db.query(RegressionTestOutput.correct).filter(
                    and_(RegressionTestOutput.regression_id == test_result_file.c.regression_test_id,
                         test_result_file.c.got is not None)).values(test_result_file.c.got)
                g.db.query(RegressionTest.expected_rc).filter(
                    RegressionTest.id == test_result.c.regression_test_id
                ).values(test_result.c.expected_rc)
                g.db.commit()
                g.log.info("Successfully added tests for latest release!")
            else:
                g.log.warning(f"Unsupported release action: {action}")

        else:
            g.log.warning(f'CI unrecognized event: {event}')

        return json.dumps({'msg': 'EOL'})


def update_build_badge(status, test) -> None:
    """
    Build status badge for current test to be displayed on sample-platform.

    :param status: current testing status
    :type status: str
    :param test: current commit that is tested
    :type test: Test
    :return: null
    :rtype: null
    """
    if test.test_type == TestType.commit and is_main_repo(test.fork.github):
        parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        original_location = os.path.join(parent_dir, 'static', 'svg', f'{status.upper()}-{test.platform.values}.svg')
        build_status_location = os.path.join(parent_dir, 'static', 'img', 'status', f'build-{test.platform.values}.svg')
        shutil.copyfile(original_location, build_status_location)
        g.log.info('Build badge updated successfully!')


@mod_ci.route('/progress-reporter/<test_id>/<token>', methods=['POST'])
def progress_reporter(test_id, token):
    """
    Handle the progress of a certain test after validating the token. If necessary, update the status on GitHub.

    :param test_id: The id of the test to update.
    :type test_id: int
    :param token: The token to check the validity of the request.
    :type token: str
    :return: Nothing.
    :rtype: None
    """
    from run import config, log

    test = Test.query.filter(Test.id == test_id).first()
    if test is not None and test.token == token:
        repo_folder = config.get('SAMPLE_REPOSITORY', '')

        if 'type' in request.form:
            if request.form['type'] == 'progress':
                log.info('[PROGRESS_REPORTER] Progress reported')
                if not progress_type_request(log, test, test_id, request):
                    return "FAIL"

            elif request.form['type'] == 'equality':
                log.info('[PROGRESS_REPORTER] Equality reported')
                equality_type_request(log, test_id, test, request)

            elif request.form['type'] == 'logupload':
                log.info('[PROGRESS_REPORTER] Log upload')
                if not upload_log_type_request(log, test_id, repo_folder, test, request):
                    return "EMPTY"

            elif request.form['type'] == 'upload':
                log.info('[PROGRESS_REPORTER] File upload')
                if not upload_type_request(log, test_id, repo_folder, test, request):
                    return "EMPTY"

            elif request.form['type'] == 'finish':
                log.info('[PROGRESS_REPORTER] Test finished')
                finish_type_request(log, test_id, test, request)
            else:
                return "FAIL"

            return "OK"

    return "FAIL"


def progress_type_request(log, test, test_id, request) -> bool:
    """
    Handle progress updates for progress reporter.

    :param log: logger
    :type log: Logger
    :param test: concerned test
    :type test: Test
    :param test_id: The id of the test to update.
    :type test_id: int
    :param request: Request parameters
    :type request: Request
    """
    status = TestStatus.from_string(request.form['status'])
    current_status = TestStatus.progress_step(status)
    message = request.form['message']

    if len(test.progress) != 0:
        last_status = TestStatus.progress_step(test.progress[-1].status)

        if last_status in [TestStatus.completed, TestStatus.canceled]:
            return False

        if last_status > current_status:
            status = TestStatus.canceled  # type: ignore
            message = "Duplicate Entries"

        if last_status < current_status:
            # get KVM start time for finding KVM preparation time
            kvm_entry = Kvm.query.filter(Kvm.test_id == test_id).first()

            if status == TestStatus.building:
                log.info('test preparation finished')
                prep_finish_time = datetime.datetime.now()
                # save preparation finish time
                kvm_entry.timestamp_prep_finished = prep_finish_time
                g.db.commit()
                # set time taken in seconds to do preparation
                time_diff = (prep_finish_time - kvm_entry.timestamp).total_seconds()
                set_avg_time(test.platform, "prep", time_diff)

            elif status == TestStatus.testing:
                log.info('test build procedure finished')
                build_finish_time = datetime.datetime.now()
                # save build finish time
                kvm_entry.timestamp_build_finished = build_finish_time
                g.db.commit()
                # set time taken in seconds to do preparation
                time_diff = (build_finish_time - kvm_entry.timestamp_prep_finished).total_seconds()
                set_avg_time(test.platform, "build", time_diff)

    progress = TestProgress(test.id, status, message)
    g.db.add(progress)
    g.db.commit()

    gh = GitHub(access_token=g.github['bot_token'])
    repository = gh.repos(g.github['repository_owner'])(g.github['repository'])
    # Store the test commit for testing in case of commit
    if status == TestStatus.completed and is_main_repo(test.fork.github):
        commit_name = 'fetch_commit_' + test.platform.value
        commit = GeneralData.query.filter(GeneralData.key == commit_name).first()
        fetch_commit = Test.query.filter(
            and_(Test.commit == commit.value, Test.platform == test.platform)
        ).first()

        if test.test_type == TestType.commit and test.id > fetch_commit.id:
            commit.value = test.commit
            g.db.commit()

    # If status is complete, remove the Kvm entry
    if status in [TestStatus.completed, TestStatus.canceled]:
        log.debug("Test {id} has been {status}".format(id=test_id, status=status))
        var_average = 'average_time_' + test.platform.value
        current_average = GeneralData.query.filter(GeneralData.key == var_average).first()
        average_time = 0
        total_time = 0

        if current_average is None:
            platform_tests = g.db.query(Test.id).filter(Test.platform == test.platform).subquery()
            finished_tests = g.db.query(TestProgress.test_id).filter(
                and_(
                    TestProgress.status.in_([TestStatus.canceled, TestStatus.completed]),
                    TestProgress.test_id.in_(platform_tests)
                )
            ).subquery()
            in_progress_statuses = [TestStatus.preparation, TestStatus.completed, TestStatus.canceled]
            finished_tests_progress = g.db.query(TestProgress).filter(
                and_(
                    TestProgress.test_id.in_(finished_tests),
                    TestProgress.status.in_(in_progress_statuses)
                )
            ).subquery()
            times = g.db.query(
                finished_tests_progress.c.test_id,
                label('time', func.group_concat(finished_tests_progress.c.timestamp))
            ).group_by(finished_tests_progress.c.test_id).all()

            for p in times:
                parts = p.time.split(',')
                start = datetime.datetime.strptime(parts[0], '%Y-%m-%d %H:%M:%S')
                end = datetime.datetime.strptime(parts[-1], '%Y-%m-%d %H:%M:%S')
                total_time += int((end - start).total_seconds())

            if len(times) != 0:
                average_time = total_time // len(times)

            new_avg = GeneralData(var_average, average_time)
            log.info(f'new average time {str(average_time)} set successfully')
            g.db.add(new_avg)
            g.db.commit()

        else:
            all_results = TestResult.query.count()
            regression_test_count = RegressionTest.query.count()
            number_test = all_results / regression_test_count
            updated_average = float(current_average.value) * (number_test - 1)
            pr = test.progress_data()
            end_time = pr['end']
            start_time = pr['start']

            if end_time.tzinfo is not None:
                end_time = end_time.replace(tzinfo=None)

            if start_time.tzinfo is not None:
                start_time = start_time.replace(tzinfo=None)

            last_running_test = end_time - start_time
            updated_average = updated_average + last_running_test.total_seconds()
            current_average.value = updated_average // number_test
            g.db.commit()
            log.info(f'average time updated to {str(current_average.value)}')

        kvm = Kvm.query.filter(Kvm.test_id == test_id).first()

        if kvm is not None:
            log.debug("Removing KVM entry")
            g.db.delete(kvm)
            g.db.commit()

    # Post status update
    state = Status.PENDING
    target_url = url_for('test.by_id', test_id=test.id, _external=True)
    context = "CI - {name}".format(name=test.platform.value)

    if status == TestStatus.canceled:
        state = Status.ERROR
        message = 'Tests aborted due to an error; please check'

    elif status == TestStatus.completed:
        # Determine if success or failure
        # It fails if any of these happen:
        # - A crash (unexpected exit code)
        # - A not None value on the "got" of a TestResultFile (
        #       meaning the hashes do not match)
        crashes = g.db.query(count(TestResult.exit_code)).filter(
            and_(
                TestResult.test_id == test.id,
                TestResult.exit_code != TestResult.expected_rc
            )).scalar()
        results_zero_rc = g.db.query(RegressionTest.id).filter(
            RegressionTest.expected_rc == 0
        ).subquery()
        results = g.db.query(count(TestResultFile.got)).filter(
            and_(
                TestResultFile.test_id == test.id,
                TestResultFile.regression_test_id.in_(results_zero_rc),
                TestResultFile.got.isnot(None)
            )
        ).scalar()
        log.debug('Test {id} completed: {crashes} crashes, {results} results'.format(
            id=test.id, crashes=crashes, results=results
        ))
        if crashes > 0 or results > 0:
            state = Status.FAILURE
            message = 'Not all tests completed successfully, please check'

        else:
            state = Status.SUCCESS
            message = 'Tests completed'
        if test.test_type == TestType.pull_request:
            comment_pr(test.id, state, test.pr_nr, test.platform.name)
        update_build_badge(state, test)

    else:
        message = progress.message

    gh_commit = repository.statuses(test.commit)
    try:
        gh_commit.post(state=state, description=message, context=context, target_url=target_url)
    except ApiError as a:
        log.error('Got an exception while posting to GitHub! Message: {message}'.format(message=a.message))

    if status in [TestStatus.completed, TestStatus.canceled]:
        # Start next test if necessary, on the same platform
        start_platforms(g.db, repository, 60, test.platform)

    return True


def equality_type_request(log, test_id, test, request):
    """
    Handle equality request type for progress reporter.

    :param log: logger
    :type log: Logger
    :param test_id: The id of the test to update.
    :type test_id: int
    :param test: concerned test
    :type test: Test
    :param request: Request parameters
    :type request: Request
    """
    log.debug('Equality for {t}/{rt}/{rto}'.format(
        t=test_id, rt=request.form['test_id'], rto=request.form['test_file_id'])
    )
    rto = RegressionTestOutput.query.filter(RegressionTestOutput.id == request.form['test_file_id']).first()

    if rto is None:
        # Equality posted on a file that's ignored presumably
        log.info('No rto for {test_id}: {test}'.format(test_id=test_id, test=request.form['test_id']))
    else:
        result_file = TestResultFile(test.id, request.form['test_id'], rto.id, rto.correct)
        g.db.add(result_file)
        g.db.commit()


def upload_log_type_request(log, test_id, repo_folder, test, request) -> bool:
    """
    Handle logupload request type for progress reporter.

    :param log: logger
    :type log: Logger
    :param test_id: The id of the test to update.
    :type test_id: int
    :param repo_folder: repository folder
    :type repo_folder: str
    :param test: concerned test
    :type test: Test
    :param request: Request parameters
    :type request: Request
    """
    log.debug("Received log file for test {id}".format(id=test_id))
    # File upload, process
    if 'file' in request.files:
        uploaded_file = request.files['file']
        filename = secure_filename(uploaded_file.filename)
        if filename is '':
            return False

        temp_path = os.path.join(repo_folder, 'TempFiles', filename)
        # Save to temporary location
        uploaded_file.save(temp_path)
        final_path = os.path.join(repo_folder, 'LogFiles', '{id}{ext}'.format(id=test.id, ext='.txt'))

        os.rename(temp_path, final_path)
        log.debug("Stored log file")
        return True

    return False


def upload_type_request(log, test_id, repo_folder, test, request) -> bool:
    """
    Handle upload request type for progress reporter.

    :param log: logger
    :type log: Logger
    :param test_id: The id of the test to update.
    :type test_id: int
    :param repo_folder: repository folder
    :type repo_folder: str
    :param test: concerned test
    :type test: Test
    :param request: Request parameters
    :type request: Request
    """
    log.debug('Upload for {t}/{rt}/{rto}'.format(
        t=test_id, rt=request.form['test_id'], rto=request.form['test_file_id'])
    )
    # File upload, process
    if 'file' in request.files:
        uploaded_file = request.files['file']
        filename = secure_filename(uploaded_file.filename)
        if filename is '':
            log.warning('empty filename provided for uploading')
            return False
        temp_path = os.path.join(repo_folder, 'TempFiles', filename)
        # Save to temporary location
        uploaded_file.save(temp_path)
        # Get hash and check if it's already been submitted
        hash_sha256 = hashlib.sha256()
        with open(temp_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha256.update(chunk)
        file_hash = hash_sha256.hexdigest()
        filename, file_extension = os.path.splitext(filename)
        final_path = os.path.join(
            repo_folder, 'TestResults', '{hash}{ext}'.format(hash=file_hash, ext=file_extension)
        )
        os.rename(temp_path, final_path)
        rto = RegressionTestOutput.query.filter(
            RegressionTestOutput.id == request.form['test_file_id']).first()
        result_file = TestResultFile(test.id, request.form['test_id'], rto.id, rto.correct, file_hash)
        g.db.add(result_file)
        g.db.commit()
        return True

    return False


def finish_type_request(log, test_id, test, request):
    """
    Handle finish request type for progress reporter.

    :param log: logger
    :type log: Logger
    :param test_id: The id of the test to update.
    :type test_id: int
    :param test: concerned test
    :type test: Test
    :param request: Request parameters
    :type request: Request
    """
    log.debug('Finish for {t}/{rt}'.format(t=test_id, rt=request.form['test_id']))
    regression_test = RegressionTest.query.filter(RegressionTest.id == request.form['test_id']).first()
    result = TestResult(
        test.id, regression_test.id, request.form['runTime'],
        request.form['exitCode'], regression_test.expected_rc
    )
    g.db.add(result)
    try:
        g.db.commit()
    except IntegrityError as e:
        log.error('Could not save the results: {msg}'.format(msg=e))


def set_avg_time(platform: Test.platform, process_type: str, time_taken: int) -> None:
    """
    Set average platform preparation time.

    :param platform: platform to which the average time belongs
    :type platform: TestPlatform
    :param process_type: process to save the average time for
    :type process_type: str
    :param time_taken: time taken to complete the process
    :type time_taken: int
    """
    val_key = "avg_" + str(process_type) + "_time_" + platform.value
    count_key = "avg_" + str(process_type) + "_count_" + platform.value

    current_avg_count = GeneralData.query.filter(GeneralData.key == count_key).first()

    # adding average data the first time
    if current_avg_count is None:
        avg_count_GD = GeneralData(count_key, str(1))
        avg_time_GD = GeneralData(val_key, str(time_taken))
        g.db.add(avg_count_GD)
        g.db.add(avg_time_GD)

    else:
        current_average = GeneralData.query.filter(GeneralData.key == val_key).first()
        avg_count = int(current_avg_count.value)
        avg_value = int(float(current_average.value))
        new_average = ((avg_value * avg_count) + time_taken) / (avg_count + 1)
        current_avg_count.value = str(avg_count + 1)
        current_average.value = str(new_average)

    g.db.commit()


def comment_pr(test_id, state, pr_nr, platform) -> None:
    """
    Upload the test report to the github PR as comment.

    :param test_id: The identity of Test whose report will be uploaded
    :type test_id: str
    :param state: The state of the PR.
    :type state: Status
    :param pr_nr: PR number to which test commit is related and comment will be uploaded
    :type: str
    :param platform
    :type: str
    """
    from run import app, log
    regression_testid_passed = g.db.query(TestResult.regression_test_id).outerjoin(
        TestResultFile, TestResult.test_id == TestResultFile.test_id).filter(
        TestResult.test_id == test_id,
        TestResult.expected_rc == TestResult.exit_code,
        or_(
            TestResult.exit_code != 0,
            and_(TestResult.exit_code == 0,
                 TestResult.regression_test_id == TestResultFile.regression_test_id,
                 TestResultFile.got.is_(None)
                 ),
            and_(
                RegressionTestOutput.regression_id == TestResult.regression_test_id,
                RegressionTestOutput.ignore.is_(True),
            ))).subquery()
    passed = g.db.query(label('category_id', Category.id), label(
        'success', count(regressionTestLinkTable.c.regression_id))).filter(
            regressionTestLinkTable.c.regression_id.in_(regression_testid_passed),
            Category.id == regressionTestLinkTable.c.category_id).group_by(
            regressionTestLinkTable.c.category_id).subquery()
    tot = g.db.query(label('category', Category.name), label('total', count(regressionTestLinkTable.c.regression_id)),
                     label('success', passed.c.success)).outerjoin(
        passed, passed.c.category_id == Category.id).filter(
        Category.id == regressionTestLinkTable.c.category_id).group_by(
        regressionTestLinkTable.c.category_id).all()
    regression_testid_failed = RegressionTest.query.filter(RegressionTest.id.notin_(regression_testid_passed)).all()
    template = app.jinja_env.get_or_select_template('ci/pr_comment.txt')
    message = template.render(tests=tot, failed_tests=regression_testid_failed, test_id=test_id,
                              state=state, platform=platform)
    log.debug('GitHub PR Comment Message Created for Test_id: {test_id}'.format(test_id=test_id))
    try:
        gh = GitHub(access_token=g.github['bot_token'])
        repository = gh.repos(g.github['repository_owner'])(g.github['repository'])
        # Pull requests are just issues with code, so github consider pr comments in issues
        pull_request = repository.issues(pr_nr)
        comments = pull_request.comments().get()
        bot_name = g.github['bot_name']
        comment_id = None
        for comment in comments:
            if comment['user']['login'] == bot_name and platform in comment['body']:
                comment_id = comment['id']
                break
        log.debug('GitHub PR Comment ID Fetched for Test_id: {test_id}'.format(test_id=test_id))
        if comment_id is None:
            comment = pull_request.comments().post(body=message)
            comment_id = comment['id']
        else:
            repository.issues().comments(comment_id).post(body=message)
        log.debug('GitHub PR Comment ID {comment} Uploaded for Test_id: {test_id}'.format(
            comment=comment_id, test_id=test_id))
    except Exception as e:
        log.error('GitHub PR Comment Failed for Test_id: {test_id} with Exception {e}'.format(test_id=test_id, e=e))


@mod_ci.route('/show_maintenance')
@login_required
@check_access_rights([Role.admin])
@template_renderer('ci/maintenance.html')
def show_maintenance():
    """
    Get list of Virtual Machines under maintenance.

    :return: platforms in maintenance
    :rtype: dict
    """
    return {
        'platforms': MaintenanceMode.query.all()
    }


@mod_ci.route('/blocked_users', methods=['GET', 'POST'])
@login_required
@check_access_rights([Role.admin])
@template_renderer()
def blocked_users():
    """
    Render the blocked_users template.

    This returns a list of all currently blacklisted users.
    Also defines processing of forms to add/remove users from blacklist.
    When a user is added to blacklist, removes queued tests on any PR by the user.
    """
    blocked_users = BlockedUsers.query.order_by(BlockedUsers.user_id)

    # Initialize usernames dictionary
    usernames = {u.user_id: 'Error, cannot get username' for u in blocked_users}
    for key in usernames.keys():
        # Fetch usernames from GitHub API
        try:
            api_url = requests.get('https://api.github.com/user/{}'.format(key), timeout=10)
            userdata = api_url.json()
            # Set values to the actual usernames if no errors
            usernames[key] = userdata['login']
        except requests.exceptions.RequestException:
            break

    # Define addUserForm processing
    add_user_form = AddUsersToBlacklist()
    if add_user_form.add.data and add_user_form.validate_on_submit():
        if BlockedUsers.query.filter_by(user_id=add_user_form.user_id.data).first() is not None:
            flash('User already blocked.')
            return redirect(url_for('.blocked_users'))

        blocked_user = BlockedUsers(add_user_form.user_id.data, add_user_form.comment.data)
        g.db.add(blocked_user)
        g.db.commit()
        flash('User blocked successfully.')

        try:
            # Remove any queued pull request from blocked user
            gh = GitHub(access_token=g.github['bot_token'])
            repository = gh.repos(g.github['repository_owner'])(g.github['repository'])
            # Getting all pull requests by blocked user on the repo
            pulls = repository.pulls.get()
            for pull in pulls:
                if pull['user']['id'] != add_user_form.user_id.data:
                    continue
                tests = Test.query.filter(Test.pr_nr == pull['number']).all()
                for test in tests:
                    # Add canceled status only if the test hasn't started yet
                    if len(test.progress) > 0:
                        continue
                    progress = TestProgress(test.id, TestStatus.canceled, "PR closed", datetime.datetime.now())
                    g.db.add(progress)
                    g.db.commit()
                    try:
                        repository.statuses(test.commit).post(
                            state=Status.FAILURE,
                            description="Tests canceled since user blacklisted",
                            context="CI - {name}".format(name=test.platform.value),
                            target_url=url_for('test.by_id', test_id=test.id, _external=True)
                        )
                    except ApiError as a:
                        g.log.error('Got an exception while posting to GitHub! Message: {message}'.format(
                            message=a.message))
        except ApiError as a:
            g.log.error('Pull Requests of Blocked User could not be fetched: {res}'.format(res=a.response))

        return redirect(url_for('.blocked_users'))

    # Define removeUserForm processing
    remove_user_form = RemoveUsersFromBlacklist()
    if remove_user_form.remove.data and remove_user_form.validate_on_submit():
        blocked_user = BlockedUsers.query.filter_by(user_id=remove_user_form.user_id.data).first()
        if blocked_user is None:
            flash('No such user in Blacklist')
            return redirect(url_for('.blocked_users'))

        g.db.delete(blocked_user)
        g.db.commit()
        flash('User removed successfully.')
        return redirect(url_for('.blocked_users'))

    return{
        'addUserForm': add_user_form,
        'removeUserForm': remove_user_form,
        'blocked_users': blocked_users,
        'usernames': usernames
    }


@mod_ci.route('/toggle_maintenance/<platform>/<status>')
@login_required
@check_access_rights([Role.admin])
def toggle_maintenance(platform, status):
    """
    Toggle maintenance mode for a platform.

    :param platform: name of the platform
    :type platform: str
    :param status: current maintenance status
    :type status: str
    :return: success response if successful, failure response otherwise
    :rtype: JSON
    """
    result = 'failed'
    message = 'Platform Not found'
    disabled = status == 'True'
    try:
        platform = TestPlatform.from_string(platform)
        db_mode = MaintenanceMode.query.filter(MaintenanceMode.platform == platform).first()
        if db_mode is not None:
            db_mode.disabled = disabled
            g.db.commit()
            result = 'success'
            message = f'{platform.description} in maintenance? {"Yes" if disabled else "No"}'
    except ValueError:
        pass

    return jsonify({
        'status': result,
        'message': message
    })


@mod_ci.route('/maintenance-mode/<platform>')
def in_maintenance_mode(platform):
    """
    Check if platform in maintenance mode.

    :param platform: name of the platform
    :type platform: str
    :return: status of the platform
    :rtype: str
    """
    try:
        platform = TestPlatform.from_string(platform)
    except ValueError:
        return 'ERROR'

    status = MaintenanceMode.query.filter(MaintenanceMode.platform == platform).first()

    if status is None:
        status = MaintenanceMode(platform, False)
        g.db.add(status)
        g.db.commit()

    return str(status.disabled)


def is_main_repo(repo_url) -> bool:
    """
    Check whether a repo_url links to the main repository or not.

    :param repo_url: url of fork/main repository of the user
    :type repo_url: str
    :return: checks whether url of main repo is same or not
    :rtype: bool
    """
    from run import config, get_github_config

    gh_config = get_github_config(config)
    return f'{gh_config["repository_owner"]}/{gh_config["repository"]}' in repo_url


def add_customized_regression_tests(test_id) -> None:
    """
    Run custom regression tests.

    :param test_id: id of the test
    :type test_id: int
    """
    active_regression_tests = RegressionTest.query.filter(RegressionTest.active == 1).all()
    for regression_test in active_regression_tests:
        g.log.debug(f'Adding RT #{regression_test.id} to test {test_id}')
        customized_test = CustomizedTest(test_id, regression_test.id)
        g.db.add(customized_test)
    g.db.commit()
